"""
Named SurrealDB query functions — all graph traversal + write-back logic lives here.
Application code imports these functions; raw SurrealQL stays out of business logic.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from db.client import SurrealClient


def _sanitize_id(raw: str) -> str:
    """Strip table prefix if present and validate the ID is alphanumeric/underscore only."""
    # Remove table prefix like "Customer:" if present
    if ":" in raw:
        raw = raw.split(":", 1)[1]
    if not re.match(r'^[a-zA-Z0-9_]+$', raw):
        raise ValueError(f"Invalid record ID: {raw}")
    return raw


# ── Read queries ─────────────────────────────────────────────

async def get_all_customers(db: SurrealClient) -> list[dict]:
    return await db.query("SELECT id, name, age, segment, kyc_status, channel_preference, risk_profile FROM Customer")


async def get_customer_context(db: SurrealClient, customer_id: str) -> dict:
    """
    Single round-trip graph traversal: customer + owned products +
    life events + recent interactions + journey state.
    """
    cid = _sanitize_id(customer_id)
    rows = await db.query(
        f"""
        SELECT *,
            ->owns->(Product AS product) AS owned_products,
            ->triggered->(LifeEvent AS event) AS life_events,
            ->had_interaction->(Interaction AS interaction) AS interactions,
            ->has_journey->(JourneyState AS journey) AS journey_states,
            ->eligible_for AS eligible_edges
        FROM Customer:{cid}
        FETCH owned_products, life_events, interactions, journey_states
        """
    )
    return rows[0] if rows else {}


async def get_eligible_products(db: SurrealClient, customer_id: str) -> list[dict]:
    cid = _sanitize_id(customer_id)
    # Use SELECT * and FETCH out to get the full product record resolved
    # under the 'out' key alongside score/reason/evaluated_at
    results = await db.query(
        f"""
        SELECT *, out.name AS product_name, out.category AS product_category,
               out.risk_level AS product_risk_level, out.requires_kyc AS product_requires_kyc,
               out.annual_fee AS product_annual_fee, out.cooling_off_days AS product_cooling_off_days
        FROM eligible_for
        WHERE in = Customer:{cid}
        FETCH out
        """
    )
    # Normalize: ensure each result has an 'out' dict with product fields
    normalized = []
    for r in results:
        out = r.get("out")
        if isinstance(out, str):
            # out wasn't fetched — build product dict from projected fields
            out = {
                "id": r.get("out", ""),
                "name": r.get("product_name", ""),
                "category": r.get("product_category", ""),
                "risk_level": r.get("product_risk_level", "low"),
                "requires_kyc": r.get("product_requires_kyc", True),
                "annual_fee": r.get("product_annual_fee"),
                "cooling_off_days": r.get("product_cooling_off_days", 0),
            }
        elif isinstance(out, dict):
            # out was fetched correctly — already a full product record
            pass
        else:
            # Fallback: try building from projected fields
            out = {
                "id": r.get("product_name", ""),
                "name": r.get("product_name", ""),
                "category": r.get("product_category", ""),
                "risk_level": r.get("product_risk_level", "low"),
                "requires_kyc": r.get("product_requires_kyc", True),
                "annual_fee": r.get("product_annual_fee"),
                "cooling_off_days": r.get("product_cooling_off_days", 0),
            }
        normalized.append({
            "out": out,
            "score": r.get("score", 0.5),
            "reason": r.get("reason", ""),
            "evaluated_at": r.get("evaluated_at"),
        })
    return normalized


async def get_compliance_blocks(db: SurrealClient, product_id: str) -> list[dict]:
    """Return all hard-block rules associated with a product."""
    pid = _sanitize_id(product_id)
    return await db.query(
        f"""
        SELECT out.*, block_reason
        FROM blocked_by
        WHERE in = Product:{pid}
        FETCH out
        """
    )


async def get_compliance_approval_rules(db: SurrealClient, product_id: str) -> list[dict]:
    pid = _sanitize_id(product_id)
    return await db.query(
        f"""
        SELECT out.*, approval_reason
        FROM requires_approval
        WHERE in = Product:{pid}
        FETCH out
        """
    )


async def get_active_compliance_rules(db: SurrealClient) -> list[dict]:
    return await db.query("SELECT * FROM ComplianceRule WHERE active = true")


async def get_all_compliance_rules(db: SurrealClient) -> list[dict]:
    return await db.query("SELECT * FROM ComplianceRule ORDER BY enforcement ASC")


async def get_journey_state(db: SurrealClient, customer_id: str) -> dict | None:
    cid = _sanitize_id(customer_id)
    rows = await db.query(
        f"SELECT * FROM JourneyState WHERE customer_id = 'Customer:{cid}' LIMIT 1"
    )
    return rows[0] if rows else None


async def get_decision_logs(
    db: SurrealClient,
    customer_id: str | None = None,
    limit: int = 50,
) -> list[dict]:
    if customer_id:
        cid = _sanitize_id(customer_id)
        return await db.query(
            f"SELECT * FROM DecisionLog WHERE customer_id = 'Customer:{cid}' ORDER BY created_at DESC LIMIT $lim",
            {"lim": limit},
        )
    return await db.query(
        "SELECT * FROM DecisionLog ORDER BY created_at DESC LIMIT $lim",
        {"lim": limit},
    )


async def get_pending_approvals(db: SurrealClient, customer_id: str | None = None) -> list[dict]:
    if customer_id:
        cid = _sanitize_id(customer_id)
        return await db.query(
            f"SELECT * FROM ApprovalRequest WHERE status = 'pending' AND customer_id = 'Customer:{cid}' ORDER BY created_at DESC"
        )
    return await db.query(
        "SELECT * FROM ApprovalRequest WHERE status = 'pending' ORDER BY created_at DESC"
    )


async def get_recent_interactions(db: SurrealClient, customer_id: str, limit: int = 20) -> list[dict]:
    cid = _sanitize_id(customer_id)
    return await db.query(
        f"""
        SELECT out.*
        FROM had_interaction
        WHERE in = Customer:{cid}
        ORDER BY out.created_at DESC
        LIMIT $lim
        """,
        {"lim": limit},
    )


async def get_graph_for_viz(db: SurrealClient, customer_id: str) -> dict:
    """Return nodes + edges suitable for the graph visualizer."""
    cid = _sanitize_id(customer_id)
    nodes_q = await db.query(
        f"SELECT 'customer' AS node_type, id, name, segment, kyc_status, risk_profile FROM Customer:{cid}"
    )
    products_owned = await db.query(
        f"SELECT out.id, out.name, out.category, 'owned' AS edge_type FROM owns WHERE in = Customer:{cid} FETCH out"
    )
    products_eligible = await db.query(
        f"SELECT out.id, out.name, out.category, score, 'eligible' AS edge_type FROM eligible_for WHERE in = Customer:{cid} FETCH out"
    )
    life_events = await db.query(
        f"SELECT out.id, out.event_type, out.confidence, out.detected_at FROM triggered WHERE in = Customer:{cid} FETCH out"
    )
    interactions = await db.query(
        f"SELECT out.id, out.interaction_type, out.sentiment, out.created_at FROM had_interaction WHERE in = Customer:{cid} FETCH out"
    )
    blocked = await db.query(
        f"""
        SELECT p.name AS product_name, p.id AS product_id, cr.name AS rule_name, cr.id AS rule_id, bb.block_reason
        FROM blocked_by AS bb
        INNER JOIN Product AS p ON bb.in = p.id
        INNER JOIN ComplianceRule AS cr ON bb.out = cr.id
        WHERE bb.in IN (SELECT out FROM owns WHERE in = Customer:{cid})
           OR bb.in IN (SELECT out FROM eligible_for WHERE in = Customer:{cid})
        """
    )
    return {
        "customer": nodes_q[0] if nodes_q else {},
        "owned_products": products_owned,
        "eligible_products": products_eligible,
        "life_events": life_events,
        "interactions": interactions,
        "blocked_edges": blocked,
    }


# ── Write queries ─────────────────────────────────────────────

async def write_interaction(
    db: SurrealClient,
    customer_id: str,
    interaction_type: str,
    channel: str,
    content: str,
    sentiment: str | None = None,
) -> str:
    """Create an Interaction node and link it to the customer. Returns the new record ID."""
    cid = _sanitize_id(customer_id)
    # Build SET clause — omit sentiment if None to avoid NULL vs NONE issues
    sentiment_clause = "sentiment = $sentiment," if sentiment else ""
    params: dict = {
        "itype": interaction_type,
        "chan": channel,
        "content": content,
    }
    if sentiment:
        params["sentiment"] = sentiment
    rows = await db.query(
        f"""
        LET $inode = (CREATE Interaction SET
            interaction_type = $itype,
            channel          = $chan,
            content          = $content,
            {sentiment_clause}
            created_at       = time::now()
        );
        LET $iid = $inode[0].id;
        RELATE Customer:{cid}->had_interaction->$iid;
        RETURN $iid;
        """,
        params,
    )
    if rows:
        return str(rows[-1]) if not isinstance(rows[-1], dict) else str(rows[-1].get("id", ""))
    return ""


async def write_life_event(
    db: SurrealClient,
    customer_id: str,
    event_type: str,
    confidence: float,
    source: str,
) -> str:
    cid = _sanitize_id(customer_id)
    rows = await db.query(
        f"""
        LET $ev = (CREATE LifeEvent SET
            event_type  = $etype,
            confidence  = $conf,
            source      = $src,
            detected_at = time::now()
        );
        LET $evid = $ev[0].id;
        RELATE Customer:{cid}->triggered->$evid SET detected_via = $src;
        RETURN $evid;
        """,
        {"etype": event_type, "conf": confidence, "src": source},
    )
    return str(rows[-1]) if rows else ""


async def write_decision_log(
    db: SurrealClient,
    customer_id: str,
    action_taken: str,
    agent_reasoning: str,
    confidence_score: float,
    compliance_gates_passed: list,
    compliance_gates_failed: list,
    graph_nodes_consulted: list,
    langsmith_trace_id: str | None = None,
    requires_human_review: bool = False,
) -> str:
    cid = _sanitize_id(customer_id)
    # Step 1: Create the DecisionLog node
    # Omit optional fields when None to avoid NULL vs NONE coercion errors
    trace_clause = "langsmith_trace_id = $trace_id," if langsmith_trace_id else ""
    params: dict = {
        "action": action_taken,
        "reasoning": agent_reasoning,
        "score": confidence_score,
        "passed": compliance_gates_passed,
        "failed": compliance_gates_failed,
        "nodes": graph_nodes_consulted,
        "needs_review": requires_human_review,
    }
    if langsmith_trace_id:
        params["trace_id"] = langsmith_trace_id
    rows = await db.query(
        f"""
        CREATE DecisionLog SET
            customer_id             = 'Customer:{cid}',
            action_taken            = $action,
            agent_reasoning         = $reasoning,
            confidence_score        = $score,
            compliance_gates_passed = $passed,
            compliance_gates_failed = $failed,
            graph_nodes_consulted   = $nodes,
            {trace_clause}
            requires_human_review   = $needs_review,
            reviewed                = false,
            created_at              = time::now();
        """,
        params,
    )
    dl_id = ""
    if rows and isinstance(rows[0], dict):
        dl_id = str(rows[0].get("id", ""))

    # Step 2: Link to JourneyState if one exists (separate query to avoid IF...THEN RELATE parse issues)
    if dl_id:
        js_rows = await db.query(
            f"SELECT id FROM JourneyState WHERE customer_id = 'Customer:{cid}' LIMIT 1"
        )
        if js_rows and isinstance(js_rows[0], dict) and js_rows[0].get("id"):
            js_id = str(js_rows[0]["id"])
            js_id_safe = _sanitize_id(js_id)
            dl_id_safe = _sanitize_id(dl_id)
            try:
                await db.query(
                    f"RELATE JourneyState:{js_id_safe}->has_decision->DecisionLog:{dl_id_safe};"
                )
            except Exception:
                pass  # Non-fatal — the log is still created

    return dl_id


async def update_eligible_for(
    db: SurrealClient,
    customer_id: str,
    product_id: str,
    score: float,
    reason: str,
) -> None:
    cid = _sanitize_id(customer_id)
    pid = _sanitize_id(product_id)
    await db.query(
        f"""
        DELETE eligible_for WHERE in = Customer:{cid} AND out = Product:{pid};
        RELATE Customer:{cid}->eligible_for->Product:{pid}
            SET score = $score, reason = $reason, evaluated_at = time::now();
        """,
        {"score": score, "reason": reason},
    )


async def update_journey_state(
    db: SurrealClient,
    customer_id: str,
    phase: str,
    current_step: str,
    checkpoint_data: str | None = None,
    pending_approval: bool = False,
) -> None:
    cid = _sanitize_id(customer_id)
    ckpt_clause = "checkpoint_data = $ckpt," if checkpoint_data else ""
    params: dict = {
        "phase": phase,
        "step": current_step,
        "pending": pending_approval,
    }
    if checkpoint_data:
        params["ckpt"] = checkpoint_data
    await db.query(
        f"""
        UPDATE JourneyState SET
            phase            = $phase,
            current_step     = $step,
            {ckpt_clause}
            pending_approval = $pending,
            last_agent_run   = time::now(),
            updated_at       = time::now()
        WHERE customer_id = 'Customer:{cid}';
        """,
        params,
    )


async def create_approval_request(
    db: SurrealClient,
    customer_id: str,
    proposed_action: str,
    agent_rationale: str,
    risk_factors: list,
) -> str:
    cid = _sanitize_id(customer_id)
    rows = await db.query(
        f"""
        CREATE ApprovalRequest SET
            customer_id     = 'Customer:{cid}',
            proposed_action = $action,
            agent_rationale = $rationale,
            risk_factors    = $risks,
            status          = 'pending',
            created_at      = time::now();
        """,
        {
            "action": proposed_action,
            "rationale": agent_rationale,
            "risks": risk_factors,
        },
    )
    return str(rows[0].get("id", "")) if rows else ""


async def resolve_approval_request(
    db: SurrealClient,
    request_id: str,
    status: str,
    advisor_response: str,
) -> None:
    rid = _sanitize_id(request_id)
    await db.query(
        f"""
        UPDATE ApprovalRequest:{rid} SET
            status           = $status,
            advisor_response = $response,
            resolved_at      = time::now();
        """,
        {"status": status, "response": advisor_response},
    )


async def toggle_compliance_rule(db: SurrealClient, rule_id: str, active: bool) -> None:
    rid = _sanitize_id(rule_id)
    await db.query(
        f"UPDATE ComplianceRule:{rid} SET active = $active;",
        {"active": active},
    )


async def update_compliance_enforcement(db: SurrealClient, rule_id: str, enforcement: str) -> None:
    rid = _sanitize_id(rule_id)
    await db.query(
        f"UPDATE ComplianceRule:{rid} SET enforcement = $enforcement;",
        {"enforcement": enforcement},
    )


async def mark_decision_reviewed(db: SurrealClient, log_id: str) -> None:
    lid = _sanitize_id(log_id)
    await db.query(f"UPDATE DecisionLog:{lid} SET reviewed = true;")


async def add_blocked_by_edge(
    db: SurrealClient, product_id: str, rule_id: str, block_reason: str
) -> None:
    pid = _sanitize_id(product_id)
    rid = _sanitize_id(rule_id)
    await db.query(
        f"""
        IF NOT (SELECT * FROM blocked_by WHERE in = Product:{pid} AND out = ComplianceRule:{rid}) THEN
            RELATE Product:{pid}->blocked_by->ComplianceRule:{rid}
                SET block_reason = $reason, blocking_since = time::now()
        END;
        """,
        {"reason": block_reason},
    )


async def remove_blocked_by_edge(db: SurrealClient, product_id: str, rule_id: str) -> None:
    pid = _sanitize_id(product_id)
    rid = _sanitize_id(rule_id)
    await db.query(
        f"DELETE blocked_by WHERE in = Product:{pid} AND out = ComplianceRule:{rid};"
    )


async def get_interaction_count(db: SurrealClient) -> int:
    rows = await db.query("SELECT count() AS cnt FROM Interaction GROUP ALL")
    return rows[0].get("cnt", 0) if rows else 0


async def update_document_embedding(db: SurrealClient, doc_id: str, embedding: list[float]) -> None:
    did = _sanitize_id(doc_id)
    await db.query(
        f"UPDATE document:{did} SET embedding = $emb;",
        {"emb": embedding},
    )


async def vector_search_documents(
    db: SurrealClient, query_embedding: list[float], limit: int = 5
) -> list[dict]:
    return await db.query(
        "SELECT id, title, content, doc_type, vector::similarity::cosine(embedding, $emb) AS score FROM document WHERE embedding != [] ORDER BY score DESC LIMIT $lim",
        {"emb": query_embedding, "lim": limit},
    )


async def update_compliance_conditions(db: SurrealClient, rule_id: str, conditions: dict) -> None:
    """Update the conditions JSON on a ComplianceRule — supports dynamic rule parameters."""
    rid = _sanitize_id(rule_id)
    await db.query(
        f"UPDATE ComplianceRule:{rid} SET conditions = $cond;",
        {"cond": conditions},
    )
