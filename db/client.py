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
            auth=(config.SURREALDB_USER, config.SURREALDB_PASS),
            headers={
                "Accept": "application/json",
                "Surreal-NS": config.SURREALDB_NS,
                "Surreal-DB": config.SURREALDB_DB,
            },
            timeout=30.0,
        )
        return cls(http, url, config.SURREALDB_NS, config.SURREALDB_DB)

    async def close(self):
        await self._http.aclose()

    # ── Core query helpers ────────────────────────────────────

    async def query(self, surql: str, params: dict | None = None) -> list[Any]:
        """Execute SurrealQL via the /sql REST endpoint and return results as a flat list."""
        body = surql
        headers = {}
        if params:
            # SurrealDB Cloud accepts variables via JSON body on /sql
            # We bind them inline via LET statements for HTTP compatibility
            let_stmts = "".join(f"LET ${k} = {json.dumps(v)};\n" for k, v in params.items())
            body = let_stmts + surql

        resp = await self._http.post("/sql", content=body, headers={"Content-Type": "application/json"})
        resp.raise_for_status()
        raw = resp.json()

        # /sql returns a list of statement results: [{"result": [...], "status": "OK"}, ...]
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


# ── Synchronous helper for Streamlit (runs event loop once) ──

def run_sync(coro):
    """Run an async coroutine synchronously (for Streamlit callbacks)."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)
