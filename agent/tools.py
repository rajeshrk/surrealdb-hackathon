"""
LangChain tools that wrap SurrealDB operations.
Each tool appears as a callable node in LangSmith traces.
"""
from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool

from db.client import SurrealClient
import db.queries as Q


# ── Tool 1: Graph traversal query ────────────────────────────

@tool
async def surreal_graph_query(customer_id: str, db: SurrealClient) -> str:
    """
    Fetch full customer context via SurrealDB graph traversal.
    Returns owned products, life events, interactions and journey state.
    """
    ctx = await Q.get_customer_context(db, customer_id)
    return json.dumps(ctx, default=str)


# ── Tool 2: Vector similarity search ────────────────────────

@tool
async def surreal_vector_search(query_embedding: list[float], db: SurrealClient, limit: int = 5) -> str:
    """
    Search policy/product documents using cosine similarity on stored embeddings.
    Returns the top-k most relevant documents.
    """
    docs = await Q.vector_search_documents(db, query_embedding, limit)
    return json.dumps(docs, default=str)


# ── Tool 3: Write-back to graph ──────────────────────────────

@tool
async def surreal_write(
    operation: str,
    payload: dict,
    db: SurrealClient,
) -> str:
    """
    Write nodes/edges to SurrealDB.  operation is one of:
      - 'interaction'   : create Interaction + had_interaction edge
      - 'life_event'    : create LifeEvent + triggered edge
      - 'decision_log'  : create DecisionLog + has_decision edge
      - 'eligible_for'  : upsert eligible_for edge
      - 'journey_state' : update JourneyState checkpoint
      - 'approval'      : create ApprovalRequest
    """
    if operation == "interaction":
        result = await Q.write_interaction(
            db,
            payload["customer_id"],
            payload["interaction_type"],
            payload["channel"],
            payload["content"],
            payload.get("sentiment"),
        )
    elif operation == "life_event":
        result = await Q.write_life_event(
            db,
            payload["customer_id"],
            payload["event_type"],
            payload["confidence"],
            payload["source"],
        )
    elif operation == "decision_log":
        result = await Q.write_decision_log(
            db,
            payload["customer_id"],
            payload["action_taken"],
            payload["agent_reasoning"],
            payload["confidence_score"],
            payload.get("compliance_gates_passed", []),
            payload.get("compliance_gates_failed", []),
            payload.get("graph_nodes_consulted", []),
            payload.get("langsmith_trace_id"),
            payload.get("requires_human_review", False),
        )
    elif operation == "eligible_for":
        await Q.update_eligible_for(
            db,
            payload["customer_id"],
            payload["product_id"],
            payload["score"],
            payload["reason"],
        )
        result = "ok"
    elif operation == "journey_state":
        await Q.update_journey_state(
            db,
            payload["customer_id"],
            payload["phase"],
            payload["current_step"],
            payload.get("checkpoint_data"),
            payload.get("pending_approval", False),
        )
        result = "ok"
    elif operation == "approval":
        result = await Q.create_approval_request(
            db,
            payload["customer_id"],
            payload["proposed_action"],
            payload["agent_rationale"],
            payload.get("risk_factors", []),
        )
    else:
        result = f"unknown operation: {operation}"

    return json.dumps({"result": result}, default=str)


# ── Tool 4: Deterministic compliance check ───────────────────

@tool
async def surreal_compliance_check(
    customer_profile: dict,
    product: dict,
    recent_interactions: list[dict],
    db: SurrealClient,
) -> str:
    """
    Evaluate a single product against all active compliance rules.
    Returns {'verdict': 'pass'|'block'|'needs_approval', 'rules': [...]}
    This tool contains no LLM calls — purely deterministic rule evaluation.
    """
    import config
    from datetime import datetime, timezone, timedelta

    product_id = str(product.get("id", "")).replace("Product:", "")
    verdicts = []

    # ── Rule: KYC Freshness ──────────────────────────────────
    if product.get("requires_kyc"):
        kyc_status = customer_profile.get("kyc_status", "pending")
        kyc_verified_at = customer_profile.get("kyc_verified_at")
        kyc_ok = False
        if kyc_status == "verified" and kyc_verified_at:
            if isinstance(kyc_verified_at, str):
                try:
                    dt = datetime.fromisoformat(kyc_verified_at.replace("Z", "+00:00"))
                    age_months = (datetime.now(timezone.utc) - dt).days / 30
                    kyc_ok = age_months <= config.KYC_FRESHNESS_MONTHS
                except ValueError:
                    kyc_ok = False
            else:
                kyc_ok = True  # datetime object already validated
        if not kyc_ok:
            verdicts.append({
                "rule": "kyc_fresh",
                "name": "KYC Freshness Check",
                "enforcement": "hard",
                "reason": f"KYC status is '{kyc_status}' or expired beyond {config.KYC_FRESHNESS_MONTHS} months",
            })

    # ── Rule: Risk Profile Mismatch ───────────────────────────
    risk_order = {"low": 0, "medium": 1, "high": 2}
    product_risk = risk_order.get(product.get("risk_level", "low"), 0)
    customer_risk = risk_order.get(customer_profile.get("risk_profile", "moderate"), 1)
    if product_risk > customer_risk:
        verdicts.append({
            "rule": "risk_mismatch",
            "name": "Risk Profile Mismatch",
            "enforcement": "human_approval",
            "reason": f"Product risk '{product.get('risk_level')}' exceeds customer profile '{customer_profile.get('risk_profile')}'",
        })

    # ── Rule: High-Value Cross-Sell Audit ─────────────────────
    annual_fee = product.get("annual_fee") or 0
    if annual_fee > config.HIGH_VALUE_FEE_THRESHOLD:
        verdicts.append({
            "rule": "high_value_crosssell",
            "name": "High-Value Cross-Sell Review",
            "enforcement": "audit_only",
            "reason": f"Annual fee ${annual_fee} exceeds audit threshold ${config.HIGH_VALUE_FEE_THRESHOLD}",
        })

    # ── Rule: Cooling-Off Period ──────────────────────────────
    cooling_days = product.get("cooling_off_days", 0)
    if cooling_days > 0:
        for interaction in recent_interactions:
            if (
                interaction.get("interaction_type") == "rejection"
                and interaction.get("sentiment") == "negative"
            ):
                created = interaction.get("created_at", "")
                if isinstance(created, str) and created:
                    try:
                        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        days_since = (datetime.now(timezone.utc) - dt).days
                        if days_since < cooling_days:
                            verdicts.append({
                                "rule": "cooling_off",
                                "name": "Product Cooling-Off Period",
                                "enforcement": "hard",
                                "reason": f"Only {days_since}d since last rejection; cooling-off is {cooling_days}d",
                            })
                    except ValueError:
                        pass

    # ── Rule: Vulnerable Customer ─────────────────────────────
    if customer_profile.get("vulnerable_flag"):
        verdicts.append({
            "rule": "vulnerable_customer",
            "name": "Vulnerable Customer Protection",
            "enforcement": "human_approval",
            "reason": "Customer is flagged as vulnerable",
        })

    # ── Also check graph-stored blocks ───────────────────────
    graph_blocks = await Q.get_compliance_blocks(db, product_id)
    for block in graph_blocks:
        verdicts.append({
            "rule": str(block.get("out", {}).get("id", "")),
            "name": block.get("out", {}).get("name", "Graph rule"),
            "enforcement": "hard",
            "reason": block.get("block_reason", "Blocked by compliance rule"),
        })

    graph_approvals = await Q.get_compliance_approval_rules(db, product_id)
    for appr in graph_approvals:
        verdicts.append({
            "rule": str(appr.get("out", {}).get("id", "")),
            "name": appr.get("out", {}).get("name", "Approval rule"),
            "enforcement": "human_approval",
            "reason": appr.get("approval_reason", "Requires advisor approval"),
        })

    # ── Derive verdict ────────────────────────────────────────
    hard_blocks = [v for v in verdicts if v["enforcement"] == "hard"]
    approvals   = [v for v in verdicts if v["enforcement"] == "human_approval"]
    audits      = [v for v in verdicts if v["enforcement"] == "audit_only"]

    if hard_blocks:
        verdict = "block"
    elif approvals:
        verdict = "needs_approval"
    else:
        verdict = "pass"

    return json.dumps({
        "verdict": verdict,
        "hard_blocks": hard_blocks,
        "needs_approval": approvals,
        "audit_flags": audits,
    })
