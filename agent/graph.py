"""
LangGraph StateGraph definition for the Customer Journey Agent.

Graph topology:
    context_loader
        → eligibility_reasoner
        → compliance_gate
        → (has_candidates) → action_selector → channel_router → graph_updater → END
        → (no_candidates)  → channel_router → graph_updater → END
"""
from __future__ import annotations

from typing import Optional

from langchain_openai import AzureChatOpenAI, AzureOpenAIEmbeddings
from langgraph.graph import StateGraph, END

import config
from agent.checkpointer import SurrealDBCheckpointer
from agent.nodes import make_nodes
from agent.state import JourneyAgentState
from db.client import SurrealClient


def route_after_compliance(state: JourneyAgentState) -> str:
    """Conditional edge: route based on compliance gate output."""
    result = state.get("compliance_result", {})
    if result.get("passed"):
        return "has_candidates"
    return "no_candidates"


def build_journey_graph(
    db: SurrealClient,
    llm: Optional[AzureChatOpenAI] = None,
    embeddings: Optional[AzureOpenAIEmbeddings] = None,
    use_checkpointer: bool = True,
):
    """
    Build and compile the LangGraph StateGraph.

    Args:
        db: Connected SurrealDB client (injected into nodes via closure).
        llm: LangChain LLM (defaults to AzureChatOpenAI).
        embeddings: Azure OpenAI embeddings for vector RAG (optional).
        use_checkpointer: Whether to attach the SurrealDB checkpoint saver.

    Returns:
        Compiled LangGraph runnable.
    """
    if llm is None:
        llm = AzureChatOpenAI(
            azure_deployment=config.AZURE_OPENAI_DEPLOYMENT,
            azure_endpoint=config.AZURE_OPENAI_ENDPOINT,
            api_key=config.AZURE_OPENAI_API_KEY,
            api_version=config.AZURE_OPENAI_API_VERSION,
            temperature=0.2,
        )

    if embeddings is None and config.AZURE_OPENAI_API_KEY:
        try:
            embeddings = AzureOpenAIEmbeddings(
                azure_deployment=config.AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT,
                azure_endpoint=config.AZURE_OPENAI_ENDPOINT,
                api_key=config.AZURE_OPENAI_API_KEY,
                api_version=config.AZURE_OPENAI_API_VERSION,
            )
        except Exception:
            embeddings = None

    nodes = make_nodes(db, llm, embeddings)

    graph = StateGraph(JourneyAgentState)

    # ── Add nodes ────────────────────────────────────────────────────────
    graph.add_node("context_loader", nodes["context_loader"])
    graph.add_node("eligibility_reasoner", nodes["eligibility_reasoner"])
    graph.add_node("compliance_gate", nodes["compliance_gate"])
    graph.add_node("action_selector", nodes["action_selector"])
    graph.add_node("channel_router", nodes["channel_router"])
    graph.add_node("graph_updater", nodes["graph_updater"])

    # ── Define edges ─────────────────────────────────────────────────────
    graph.set_entry_point("context_loader")
    graph.add_edge("context_loader", "eligibility_reasoner")
    graph.add_edge("eligibility_reasoner", "compliance_gate")

    graph.add_conditional_edges(
        "compliance_gate",
        route_after_compliance,
        {
            "has_candidates": "action_selector",
            "no_candidates": "channel_router",
        },
    )

    graph.add_edge("action_selector", "channel_router")
    graph.add_edge("channel_router", "graph_updater")
    graph.add_edge("graph_updater", END)

    # ── Compile ──────────────────────────────────────────────────────────
    checkpointer = None
    if use_checkpointer:
        try:
            ckpt = SurrealDBCheckpointer(db)
            # Test that LangGraph accepts it before passing it in
            compiled = graph.compile(checkpointer=ckpt)
            return compiled
        except (TypeError, ValueError):
            # Fall back to InMemorySaver if custom checkpointer is rejected
            from langgraph.checkpoint.memory import MemorySaver
            checkpointer = MemorySaver()
    return graph.compile(checkpointer=checkpointer)


def make_run_config(customer_id: str) -> dict:
    """Return LangGraph run config keyed by customer_id as thread_id."""
    return {
        "configurable": {"thread_id": customer_id},
        "metadata": {
            "customer_id": customer_id,
            "project": config.LANGCHAIN_PROJECT,
        },
    }