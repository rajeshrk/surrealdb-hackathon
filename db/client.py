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
import os
import pathlib
from contextlib import asynccontextmanager
from typing import Any

from surrealdb import AsyncSurreal

import config


class SurrealClient:
    """Thin wrapper around the surrealdb SDK with helper methods."""

    def __init__(self, client):
        self._db = client

    # ── Factory ──────────────────────────────────────────────

    @classmethod
    async def connect(cls) -> "SurrealClient":
        db = AsyncSurreal(config.SURREALDB_URL)
        # HTTP connections don't need connect(); WS connections do
        try:
            await db.connect()
        except NotImplementedError:
            pass
        await db.signin({"username": config.SURREALDB_USER, "password": config.SURREALDB_PASS})
        await db.use(config.SURREALDB_NS, config.SURREALDB_DB)
        return cls(db)

    async def close(self):
        try:
            await self._db.close()
        except (NotImplementedError, Exception):
            pass

    # ── Core query helpers ────────────────────────────────────

    async def query(self, surql: str, params: dict | None = None) -> list[Any]:
        """Execute SurrealQL statement(s) and return all results as a flat list."""
        raw = await self._db.query_raw(surql, params or {})
        # query_raw returns {"result": [{"result": [...], "status": "OK"}, ...]}
        statements = []
        if isinstance(raw, dict) and "result" in raw:
            statements = raw["result"]
        elif isinstance(raw, list):
            statements = raw

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
        result = await self._db.create(table, data)
        if isinstance(result, list):
            return result[0] if result else None
        return result

    async def select(self, thing: str) -> list[dict] | dict | None:
        return await self._db.select(thing)

    async def update(self, thing: str, data: dict) -> dict | None:
        return await self._db.update(thing, data)

    async def merge(self, thing: str, data: dict) -> dict | None:
        return await self._db.merge(thing, data)

    async def delete(self, thing: str) -> Any:
        return await self._db.delete(thing)

    async def relate(self, record_in: str, relation: str, record_out: str, data: dict | None = None) -> Any:
        surql = f"RELATE {record_in}->{relation}->{record_out}"
        if data:
            pairs = ", ".join(f"{k} = ${k}" for k in data)
            surql += f" SET {pairs}"
        return await self.query(surql, data or {})

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
