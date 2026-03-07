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
            verify=False,
        )
        client = cls(http, url, config.SURREALDB_NS, config.SURREALDB_DB)
        # signin + use via RPC to establish session
        try:
            await client._ensure_signed_in()
        except Exception:
            pass  # Basic auth + headers already provide access
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
            if isinstance(item, dict):
                # Check for statement-level errors
                if item.get("status") == "ERR":
                    error_msg = item.get("result", "Unknown query error")
                    raise RuntimeError(f"SurrealDB query error: {error_msg}")
                if "result" in item:
                    r = item["result"]
                    if isinstance(r, list):
                        rows.extend(r)
                    elif r is not None:
                        rows.append(r)
                else:
                    rows.append(item)
            elif isinstance(item, list):
                rows.extend(item)
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
        import logging
        logger = logging.getLogger("surrealdb")
        text = pathlib.Path(path).read_text()
        # Split on ';' but preserve semicolons inside strings is tricky;
        # we rely on statements being separated by ';\n'
        for stmt in text.split(";\n"):
            stmt = stmt.strip()
            # Skip empty lines and comment-only lines
            lines = [ln.strip() for ln in stmt.splitlines() if ln.strip() and not ln.strip().startswith("--")]
            if not lines:
                continue
            try:
                await self.query(stmt + ";")
            except Exception as exc:
                # Log the error so we can debug seed/schema issues
                snippet = stmt[:120].replace("\n", " ")
                logger.warning("SurrealDB statement failed: %s — %s", snippet, exc)
                print(f"[SurrealDB WARN] Statement failed: {snippet}... — {exc}")

    async def _ensure_edges(self) -> None:
        """Ensure graph edges exist — fallback to INSERT if RELATE failed."""
        print("[SurrealDB] Checking graph edges...")

        # Define all expected edges as (table, in_id, out_id, extra_set_clause)
        # extra_set_clause uses SurrealQL syntax (not JSON) for proper type handling
        edge_defs = [
            # owns
            ("owns", "Customer:sarah", "Product:checking",
             "SET status = 'active'"),
            ("owns", "Customer:james", "Product:premium_mortgage",
             "SET status = 'active'"),
            ("owns", "Customer:maria", "Product:basic_savings",
             "SET status = 'active'"),
            # eligible_for
            ("eligible_for", "Customer:sarah", "Product:savings_plus",
             "SET score = 0.85, reason = 'Good savings pattern'"),
            ("eligible_for", "Customer:sarah", "Product:mortgage",
             "SET score = 0.70, reason = 'Stable income, no existing mortgage'"),
            ("eligible_for", "Customer:james", "Product:home_insurance",
             "SET score = 0.90, reason = 'Mortgage holder without home insurance'"),
            ("eligible_for", "Customer:james", "Product:investment_portfolio",
             "SET score = 0.60, reason = 'Wealth segment, but KYC expired'"),
            ("eligible_for", "Customer:maria", "Product:cd_account",
             "SET score = 0.75, reason = 'Long-standing savings customer, CD would improve yield'"),
            ("eligible_for", "Customer:maria", "Product:retirement_plan",
             "SET score = 0.80, reason = 'Age and conservative profile match retirement planning'"),
            # has_journey
            ("has_journey", "Customer:sarah", "JourneyState:sarah_journey", ""),
            ("has_journey", "Customer:james", "JourneyState:james_journey", ""),
            ("has_journey", "Customer:maria", "JourneyState:maria_journey", ""),
            # had_interaction
            ("had_interaction", "Customer:maria", "Interaction:maria_i1", ""),
            ("had_interaction", "Customer:maria", "Interaction:maria_i2", ""),
        ]

        created = 0
        skipped = 0
        for table, in_id, out_id, set_clause in edge_defs:
            # Check if edge already exists
            try:
                existing = await self.query(
                    f"SELECT count() FROM {table} WHERE in = {in_id} AND out = {out_id} GROUP ALL"
                )
                count = existing[0].get("count", 0) if existing else 0
                if count > 0:
                    skipped += 1
                    continue
            except Exception as exc:
                print(f"[SurrealDB WARN] Edge check failed for {table} {in_id}->{out_id}: {exc}")

            # Try RELATE with native SurrealQL syntax
            relate_stmt = f"RELATE {in_id}->{table}->{out_id} {set_clause}".strip()
            try:
                await self.query(f"{relate_stmt};")
                created += 1
                print(f"[SurrealDB OK] Created edge: {table} {in_id}->{out_id}")
                continue
            except Exception as e:
                print(f"[SurrealDB WARN] RELATE failed for {table} ({in_id}->{out_id}): {e}")

            # Fallback: use native SurrealQL INSERT with record ID syntax
            # This avoids json.dumps which would quote record IDs as strings
            insert_stmt = (
                f"INSERT INTO {table} "
                f"{{ in: {in_id}, out: {out_id}"
            )
            if set_clause:
                # Convert "SET score = 0.85, reason = 'foo'" to object fields
                fields = set_clause.removeprefix("SET ").strip()
                insert_stmt += f", {fields}"
            insert_stmt += " }"
            try:
                await self.query(f"{insert_stmt};")
                created += 1
                print(f"[SurrealDB OK] Inserted edge via INSERT: {table} {in_id}->{out_id}")
            except Exception as e2:
                print(f"[SurrealDB ERROR] All methods failed for {table} {in_id}->{out_id}: "
                      f"RELATE={e}, INSERT={e2}")

        print(f"[SurrealDB] Edge check complete: {created} created, {skipped} already existed")

    async def bootstrap(self) -> None:
        """Apply schema then seed data if Customer table is empty."""
        print("[SurrealDB] Bootstrap starting...")
        base = pathlib.Path(__file__).parent
        await self.apply_file(base / "schema.surql")
        count_result = await self.query("SELECT count() FROM Customer GROUP ALL")
        count = count_result[0].get("count", 0) if count_result else 0
        if count == 0:
            await self.apply_file(base / "seed.surql")
            # Verify seed worked
            verify = await self.query("SELECT count() FROM Customer GROUP ALL")
            verify_count = verify[0].get("count", 0) if verify else 0
            if verify_count == 0:
                print("[SurrealDB ERROR] Seed completed but Customer table is still empty!")
            else:
                print(f"[SurrealDB OK] Seeded {verify_count} customers")

        # Always ensure edges exist (fixes RELATE failures)
        await self._ensure_edges()

        # Report edge counts
        for table in ("owns", "eligible_for", "has_journey", "had_interaction"):
            try:
                ec = await self.query(f"SELECT count() FROM {table} GROUP ALL")
                n = ec[0].get("count", 0) if ec else 0
                print(f"[SurrealDB OK] {table} edges: {n}")
            except Exception:
                print(f"[SurrealDB WARN] Could not count {table} edges")


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
