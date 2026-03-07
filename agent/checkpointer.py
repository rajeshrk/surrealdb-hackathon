"""
SurrealDB-backed LangGraph checkpoint saver.

Stores checkpoint state in JourneyState.checkpoint_data so the knowledge graph
and agent state live in the same database — a key demo differentiator.
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator, Iterator, Optional, Sequence

from langchain_core.runnables import RunnableConfig

try:
    from langgraph.checkpoint.base import (
        BaseCheckpointSaver,
        Checkpoint,
        CheckpointMetadata,
        CheckpointTuple,
        create_empty_checkpoint,
    )
    LANGGRAPH_AVAILABLE = True
except ImportError:
    LANGGRAPH_AVAILABLE = False
    BaseCheckpointSaver = object  # type: ignore

from db.client import SurrealClient


class SurrealDBCheckpointer(BaseCheckpointSaver):  # type: ignore[misc]
    """
    Stores LangGraph checkpoints in SurrealDB JourneyState nodes.

    Config key: configurable.thread_id  →  maps to customer short-id (e.g. "sarah")
    """

    def __init__(self, client: SurrealClient) -> None:
        super().__init__()
        self.client = client

    # ── Helpers ───────────────────────────────────────────────────────────

    def _thread_id(self, config: RunnableConfig) -> str:
        return config.get("configurable", {}).get("thread_id", "default")

    def _serialize(self, checkpoint: Any, metadata: Any) -> str:
        return json.dumps({"checkpoint": checkpoint, "metadata": metadata}, default=str)

    def _deserialize(self, raw: str) -> tuple[Any, Any]:
        data = json.loads(raw)
        return data.get("checkpoint", {}), data.get("metadata", {})

    # ── Async methods ─────────────────────────────────────────────────────

    async def aget_tuple(self, config: RunnableConfig) -> Optional[Any]:
        thread_id = self._thread_id(config)
        rows = await self.client.query(
            """
            SELECT checkpoint_data, updated_at
            FROM JourneyState
            WHERE customer_id = $cid
            LIMIT 1
            """,
            {"cid": f"Customer:{thread_id}"},
        )
        if not rows or not rows[0].get("checkpoint_data"):
            return None
        try:
            checkpoint, metadata = self._deserialize(rows[0]["checkpoint_data"])
        except (json.JSONDecodeError, KeyError):
            return None

        saved_config: RunnableConfig = {
            **config,
            "configurable": {**config.get("configurable", {}), "thread_id": thread_id},
        }
        try:
            return CheckpointTuple(
                config=saved_config,
                checkpoint=checkpoint,
                metadata=metadata,
                parent_config=None,
                pending_writes=[],
            )
        except Exception:
            # Older LangGraph versions may not have pending_writes
            return CheckpointTuple(  # type: ignore[call-arg]
                config=saved_config,
                checkpoint=checkpoint,
                metadata=metadata,
            )

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Any,
        metadata: Any,
        new_versions: Any = None,
    ) -> RunnableConfig:
        thread_id = self._thread_id(config)
        serialized = self._serialize(checkpoint, metadata)
        await self.client.query(
            """
            UPDATE JourneyState SET
                checkpoint_data = $ckpt,
                updated_at      = time::now()
            WHERE customer_id = $cid;
            """,
            {"cid": f"Customer:{thread_id}", "ckpt": serialized},
        )
        return {
            **config,
            "configurable": {**config.get("configurable", {}), "thread_id": thread_id},
        }

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
    ) -> None:
        # Intermediate writes — final state saved in aput
        pass

    async def alist(
        self,
        config: Optional[RunnableConfig],
        *,
        filter: Optional[dict] = None,
        before: Optional[RunnableConfig] = None,
        limit: Optional[int] = None,
    ) -> AsyncIterator[Any]:
        if config:
            result = await self.aget_tuple(config)
            if result:
                yield result

    # ── Sync stubs (required by ABC) ──────────────────────────────────────

    def get_tuple(self, config: RunnableConfig) -> Optional[Any]:
        from db.client import run_sync
        return run_sync(self.aget_tuple(config))

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Any,
        metadata: Any,
        new_versions: Any = None,
    ) -> RunnableConfig:
        from db.client import run_sync
        return run_sync(self.aput(config, checkpoint, metadata, new_versions))

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
    ) -> None:
        pass

    def list(
        self,
        config: Optional[RunnableConfig],
        *,
        filter: Optional[dict] = None,
        before: Optional[RunnableConfig] = None,
        limit: Optional[int] = None,
    ) -> Iterator[Any]:
        from db.client import run_sync

        async def _collect() -> list:
            results: list = []
            async for item in self.alist(config, filter=filter, before=before, limit=limit):
                results.append(item)
            return results

        return iter(run_sync(_collect()))
