"""
SurrealDB async client wrapper.

Usage (async context):
    async with get_client() as db:
        results = await db.query("SELECT * FROM Customer")

Or for a long-lived singleton (Streamlit session):
    db = await SurrealClient.connect()
    results = await db.query(...)
    await db.close()
"""
import asyncio
import json
import pathlib
from contextlib import asynccontextmanager
from typing import Any

import httpx

import config


class SurrealClient:
    """HTTP-based SurrealDB client for cloud and local connections."""

    def __init__(self, http: httpx.AsyncClient, url: str, ns: str, db: str):
        self._http = http
        self._url = url.rstrip("/")
        self._ns = ns
        self._db = db

    # ── Factory ──────────────────────────────────────────────

    @classmethod
    async def connect(cls) -> "SurrealClient":
        url = config.SURREALDB_URL
        # Normalise WS URLs to HTTP for the REST API
        if url.startswith("ws://"):
            url = url.replace("ws://", "http://", 1)
        elif url.startswith("wss://"):
            url = url.replace("wss://", "https://", 1)
        # Strip /rpc suffix if present
        url = url.removesuffix("/rpc")

        http = httpx.AsyncClient(
            base_url=url,
            headers={"Accept": "application/json"},
            timeout=30.0,
            verify=False,
        )
        client = cls(http, url, config.SURREALDB_NS, config.SURREALDB_DB)
        await client._ensure_signed_in()
        return client

    async def close(self):
        await self._http.aclose()

    # ── RPC helpers ───────────────────────────────────────────

    _rpc_id = 0

    async def _rpc(self, method: str, params: list | None = None) -> Any:
        """Send a JSON-RPC request to the /rpc endpoint."""
        SurrealClient._rpc_id += 1
        payload = {
            "id": SurrealClient._rpc_id,
            "method": method,
            "params": params or [],
        }
        resp = await self._http.post(
            "/rpc",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data and data["error"]:
            raise RuntimeError(data["error"].get("message", str(data["error"])))
        return data.get("result")

    async def _ensure_signed_in(self) -> None:
        """Sign in and select NS/DB via RPC (called once on connect)."""
        await self._rpc("signin", [{"user": config.SURREALDB_USER, "pass": config.SURREALDB_PASS}])
        await self._rpc("use", [self._ns, self._db])

    # ── Core query helpers ────────────────────────────────────

    async def query(self, surql: str, params: dict | None = None) -> list[Any]:
        """Execute SurrealQL via JSON-RPC and return results as a flat list."""
        raw = await self._rpc("query", [surql, params or {}])

        # RPC query returns a list: [{"result": [...], "status": "OK"}, ...]
        statements = raw if isinstance(raw, list) else []

        rows: list[Any] = []
        for item in statements:
            if isinstance(item, dict) and "result" in item:
                r = item["result"]
                if isinstance(r, list):
                    rows.extend(r)
                elif r is not None:
                    rows.append(r)
            elif isinstance(item, list):
                rows.extend(item)
            elif isinstance(item, dict):
                rows.append(item)
        return rows

    async def query_one(self, surql: str, params: dict | None = None) -> dict | None:
        rows = await self.query(surql, params)
        return rows[0] if rows else None

    async def create(self, table: str, data: dict) -> dict | None:
        surql = f"CREATE {table} CONTENT {json.dumps(data)};"
        rows = await self.query(surql)
        return rows[0] if rows else None

    async def select(self, thing: str) -> list[dict] | dict | None:
        rows = await self.query(f"SELECT * FROM {thing};")
        return rows

    async def update(self, thing: str, data: dict) -> dict | None:
        surql = f"UPDATE {thing} CONTENT {json.dumps(data)};"
        rows = await self.query(surql)
        return rows[0] if rows else None

    async def merge(self, thing: str, data: dict) -> dict | None:
        surql = f"UPDATE {thing} MERGE {json.dumps(data)};"
        rows = await self.query(surql)
        return rows[0] if rows else None

    async def delete(self, thing: str) -> Any:
        return await self.query(f"DELETE {thing};")

    async def relate(self, record_in: str, relation: str, record_out: str, data: dict | None = None) -> Any:
        surql = f"RELATE {record_in}->{relation}->{record_out}"
        if data:
            surql += f" CONTENT {json.dumps(data)}"
        surql += ";"
        return await self.query(surql)

    # ── Schema / seed bootstrap ───────────────────────────────

    async def apply_file(self, path: str | pathlib.Path) -> None:
        """Execute every statement in a .surql file."""
        text = pathlib.Path(path).read_text()
        # Split on ';' but preserve semicolons inside strings is tricky;
        # we rely on statements being separated by ';\n'
        for stmt in text.split(";\n"):
            stmt = stmt.strip()
            if stmt and not stmt.startswith("--"):
                try:
                    await self.query(stmt + ";")
                except Exception:
                    pass  # Ignore "already exists" errors on re-seed

    async def bootstrap(self) -> None:
        """Apply schema then seed data if Customer table is empty."""
        base = pathlib.Path(__file__).parent
        await self.apply_file(base / "schema.surql")
        count_result = await self.query("SELECT count() FROM Customer GROUP ALL")
        count = count_result[0].get("count", 0) if count_result else 0
        if count == 0:
            await self.apply_file(base / "seed.surql")


# ── Module-level async context manager ───────────────────────

@asynccontextmanager
async def get_client():
    client = await SurrealClient.connect()
    try:
        yield client
    finally:
        await client.close()


# ── Persistent event loop in a background thread ─────────────
# This keeps httpx AsyncClient connections alive across multiple
# run_sync() calls (Streamlit re-runs the script on every interaction).

import threading

_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_lock = threading.Lock()


def _get_loop() -> asyncio.AbstractEventLoop:
    global _loop, _thread
    with _lock:
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
            _thread = threading.Thread(target=_loop.run_forever, daemon=True)
            _thread.start()
    return _loop


def run_sync(coro):
    """Run an async coroutine synchronously using a persistent background loop."""
    loop = _get_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result()
