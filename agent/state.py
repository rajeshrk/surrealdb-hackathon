"""LangGraph state definition for the Customer Journey Agent."""
from __future__ import annotations

import operator
from typing import Annotated, List, Optional, TypedDict


class JourneyAgentState(TypedDict):
    # ── Input ────────────────────────────────────────────────
    customer_id: str
    user_message: str            # The incoming chat message from the customer

    # ── Graph context loaded from SurrealDB ──────────────────
    customer_profile: dict
    owned_products: List[dict]
    life_events: List[dict]
    interaction_history: List[dict]
    eligible_products: List[dict]
    compliance_blocks: List[dict]

    # ── Vector RAG context ───────────────────────────────────
    relevant_documents: List[dict]

    # ── Agent reasoning ──────────────────────────────────────
    detected_life_events: List[dict]   # New life events detected this run
    candidates: List[dict]             # Ranked product recommendations
    compliance_result: dict            # {passed, blocked, needs_approval}
    selected_action: Optional[dict]
    agent_reasoning: str

    # ── Output ───────────────────────────────────────────────
    channel: str
    response_message: str

    # ── Control flow ─────────────────────────────────────────
    requires_human_approval: bool
    journey_phase: str
    langsmith_trace_id: Optional[str]

    # ── Chat history (accumulated across turns) ──────────────
    messages: Annotated[list, operator.add]
