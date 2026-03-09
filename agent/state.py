"""LangGraph state definition for the Customer Journey Agent."""
from __future__ import annotations

import operator
from typing import Annotated, List, Optional, TypedDict


class ChatMessage(TypedDict):
    """A single message in the conversation."""
    role: str       # "user" or "assistant"
    content: str


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

    # ── Multi-hop graph context ────────────────────────────
    interaction_product_trail: List[dict]   # Customer→Interaction→Product chain
    journey_decision_trail: List[dict]      # Customer→Journey→DecisionLog chain
    fraud_signals: List[dict]              # Device/IP/identity-ring signals from graph traversal

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

    # ── Multi-turn conversation (LangGraph memory via reducer) ──
    messages: Annotated[list, operator.add]  # ChatMessage list, accumulated across turns via operator.add reducer
    conversation_intent: str                 # classify: greeting, product_inquiry, follow_up, life_event, general_question, objection