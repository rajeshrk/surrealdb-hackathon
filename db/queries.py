"""
Named SurrealDB query functions — all graph traversal + write-back logic lives here.
Application code imports these functions; raw SurrealQL stays out of business logic.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from db.client import SurrealClient


# ── Read queries ─────────────────────────────────────────────

async def get_all_customers(db: SurrealClient) -> list[dict]:
    return await db.query("SELECT id, name, age, segment, kyc_status, channel_preference, risk_profile FROM Customer")


async def get_customer_context(db: SurrealClient, customer_id: str) -> dict:
    """
    Single round-trip graph traversal: customer + owned products +
    life events + recent interactions + journey state.
    """
    rows = await db.query(
        """
        SELECT *,
            ->owns->(Product AS product) AS owned_products,
            ->triggered->(LifeEvent AS event) AS life_events,
            ->had_interaction->(Interaction AS interaction) AS interactions,
            ->has_journey->(JourneyState AS journey) AS journey_states,
            ->eligible_for AS eligible_edges
        FROM type::thing('Customer', $cid)
        FETCH owned_products, life_events, interactions, journey_states
        """,
        {"cid": customer_id},
    )
    return rows[0] if rows else {}


async def get_eligible_products(db: SurrealClient, customer_id: str) -> list[dict]:
    return await db.query(
        """
        SELECT out.*, score, reason, evaluated_at
        FROM eligible_for
        WHERE in = type::thing('Customer', $cid)
        FETCH out
        """,
        {"cid": customer_id},
    )


async def get_compliance_blocks(db: SurrealClient, product_id: str) -> list[dict]:
    """Return all hard-block rules associated with a product."""
    return await db.query(
        """
        SELECT out.*, block_reason
        FROM blocked_by
        WHERE in = type::thing('Product', $pid)
        FETCH out
        """,
        {"pid": product_id},
    )


async def get_compliance_approval_rules(db: SurrealClient, product_id: str) -> list[dict]:
    return await db.query(
        """
        SELECT out.*, approval_reason
        FROM requires_approval
        WHERE in = type::thing('Product', $pid)
        FETCH out
        """,
        {"pid": product_id},
    )


async def get_active_compliance_rules(db: SurrealClient) -> list[dict]:
    return await db.query("SELECT * FROM ComplianceRule WHERE active = true")


async def get_all_compliance_rules(db: SurrealClient) -> list[dict]:
    return await db.query("SELECT * FROM ComplianceRule ORDER BY enforcement ASC")


async def get_journey_state(db: SurrealClient, customer_id: str) -> dict | None:
    rows = await db.query(
        "SELECT * FROM JourneyState WHERE customer_id = $cid LIMIT 1",
        {"cid": f"Customer:{customer_id}"},
    )
    return rows[0] if rows else None


async def get_decision_logs(
    db: SurrealClient,
    customer_id: str | None = None,
    limit: int = 50,
) -> list[dict]:
    if customer_id:
        return await db.query(
            "SELECT * FROM DecisionLog WHERE customer_id = $cid ORDER BY created_at DESC LIMIT $lim",
            {"cid": f"Customer:{customer_id}", "lim": limit},
        )
    return await db.query(
        "SELECT * FROM DecisionLog ORDER BY created_at DESC LIMIT $lim",
        {"lim": limit},
    )


async def get_pending_approvals(db: SurrealClient, customer_id: str | None = None) -> list[dict]:
    if customer_id:
        return await db.query(
            "SELECT * FROM ApprovalRequest WHERE status = 'pending' AND customer_id = $cid ORDER BY created_at DESC",
            {"cid": f"Customer:{customer_id}"},
        )
    return await db.query(
        "SELECT * FROM ApprovalRequest WHERE status = 'pending' ORDER BY created_at DESC"
    )


async def get_recent_interactions(db: SurrealClient, customer_id: str, limit: int = 20) -> list[dict]:
    return await db.query(
        """
        SELECT out.*
        FROM had_interaction
        WHERE in = type::thing('Customer', $cid)
        FETCH out
        ORDER BY out.created_at DESC
        LIMIT $lim
        """,
        {"cid": customer_id, "lim": limit},
    )


async def get_graph_for_viz(db: SurrealClient, customer_id: str) -> dict:
    """Return nodes + edges suitable for the graph visualizer."""
    nodes_q = await db.query(
        """
        SELECT 'customer' AS node_type, id, name, segment, kyc_status, risk_profile FROM type::thing('Customer', $cid)
        """,
        {"cid": customer_id},
    )
    products_owned = await db.query(
        "SELECT out.id, out.name, out.category, 'owned' AS edge_type FROM owns WHERE in = type::thing('Customer', $cid) FETCH out",
        {"cid": customer_id},
    )
    products_eligible = await db.query(
        "SELECT out.id, out.name, out.category, score, 'eligible' AS edge_type FROM eligible_for WHERE in = type::thing('Customer', $cid) FETCH out",
        {"cid": customer_id},
    )
    life_events = await db.query(
        "SELECT out.id, out.event_type, out.confidence, out.detected_at FROM triggered WHERE in = type::thing('Customer', $cid) FETCH out",
        {"cid": customer_id},
    )
    interactions = await db.query(
        "SELECT out.id, out.interaction_type, out.sentiment, out.created_at FROM had_interaction WHERE in = type::thing('Customer', $cid) FETCH out",
        {"cid": customer_id},
    )
    blocked = await db.query(
        """
        SELECT p.name AS product_name, p.id AS product_id, cr.name AS rule_name, cr.id AS rule_id, bb.block_reason
        FROM blocked_by AS bb
        INNER JOIN Product AS p ON bb.in = p.id
        INNER JOIN ComplianceRule AS cr ON bb.out = cr.id
        WHERE bb.in IN (SELECT out FROM owns WHERE in = type::thing('Customer', $cid))
           OR bb.in IN (SELECT out FROM eligible_for WHERE in = type::thing('Customer', $cid))
        """,
        {"cid": customer_id},
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
    rows = await db.query(
        """
        LET $inode = (CREATE Interaction SET
            interaction_type = $itype,
            channel          = $chan,
            content          = $content,
            sentiment        = $sentiment,
            created_at       = time::now()
        );
        RELATE type::thing('Customer', $cid)->had_interaction->$inode[0].id;
        RETURN $inode[0].id;
        """,
        {
            "itype": interaction_type,
            "chan": channel,
            "content": content,
            "sentiment": sentiment,
            "cid": customer_id,
        },
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
    rows = await db.query(
        """
        LET $ev = (CREATE LifeEvent SET
            event_type  = $etype,
            confidence  = $conf,
            source      = $src,
            detected_at = time::now()
        );
        RELATE type::thing('Customer', $cid)->triggered->$ev[0].id
            SET detected_via = $src;
        RETURN $ev[0].id;
        """,
        {"etype": event_type, "conf": confidence, "src": source, "cid": customer_id},
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
    rows = await db.query(
        """
        LET $dl = (CREATE DecisionLog SET
            customer_id             = $cid,
            action_taken            = $action,
            agent_reasoning         = $reasoning,
            confidence_score        = $score,
            compliance_gates_passed = $passed,
            compliance_gates_failed = $failed,
            graph_nodes_consulted   = $nodes,
            langsmith_trace_id      = $trace_id,
            requires_human_review   = $needs_review,
            reviewed                = false,
            created_at              = time::now()
        );
        LET $js = (SELECT id FROM JourneyState WHERE customer_id = $cid LIMIT 1)[0];
        IF $js.id != NONE THEN RELATE $js.id->has_decision->$dl[0].id END;
        RETURN $dl[0].id;
        """,
        {
            "cid": f"Customer:{customer_id}",
            "action": action_taken,
            "reasoning": agent_reasoning,
            "score": confidence_score,
            "passed": compliance_gates_passed,
            "failed": compliance_gates_failed,
            "nodes": graph_nodes_consulted,
            "trace_id": langsmith_trace_id,
            "needs_review": requires_human_review,
        },
    )
    return str(rows[-1]) if rows else ""


async def update_eligible_for(
    db: SurrealClient,
    customer_id: str,
    product_id: str,
    score: float,
    reason: str,
) -> None:
    await db.query(
        """
        DELETE eligible_for WHERE in = type::thing('Customer', $cid) AND out = type::thing('Product', $pid);
        RELATE type::thing('Customer', $cid)->eligible_for->type::thing('Product', $pid)
            SET score = $score, reason = $reason, evaluated_at = time::now();
        """,
        {"cid": customer_id, "pid": product_id, "score": score, "reason": reason},
    )


async def update_journey_state(
    db: SurrealClient,
    customer_id: str,
    phase: str,
    current_step: str,
    checkpoint_data: str | None = None,
    pending_approval: bool = False,
) -> None:
    await db.query(
        """
        UPDATE JourneyState SET
            phase            = $phase,
            current_step     = $step,
            checkpoint_data  = $ckpt,
            pending_approval = $pending,
            last_agent_run   = time::now(),
            updated_at       = time::now()
        WHERE customer_id = $cid;
        """,
        {
            "cid": f"Customer:{customer_id}",
            "phase": phase,
            "step": current_step,
            "ckpt": checkpoint_data,
            "pending": pending_approval,
        },
    )


async def create_approval_request(
    db: SurrealClient,
    customer_id: str,
    proposed_action: str,
    agent_rationale: str,
    risk_factors: list,
) -> str:
    rows = await db.query(
        """
        CREATE ApprovalRequest SET
            customer_id     = $cid,
            proposed_action = $action,
            agent_rationale = $rationale,
            risk_factors    = $risks,
            status          = 'pending',
            created_at      = time::now();
        """,
        {
            "cid": f"Customer:{customer_id}",
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
    await db.query(
        """
        UPDATE type::thing('ApprovalRequest', $rid) SET
            status           = $status,
            advisor_response = $response,
            resolved_at      = time::now();
        """,
        {"rid": request_id, "status": status, "response": advisor_response},
    )


async def toggle_compliance_rule(db: SurrealClient, rule_id: str, active: bool) -> None:
    await db.query(
        "UPDATE type::thing('ComplianceRule', $rid) SET active = $active;",
        {"rid": rule_id, "active": active},
    )


async def update_compliance_enforcement(db: SurrealClient, rule_id: str, enforcement: str) -> None:
    await db.query(
        "UPDATE type::thing('ComplianceRule', $rid) SET enforcement = $enforcement;",
        {"rid": rule_id, "enforcement": enforcement},
    )


async def mark_decision_reviewed(db: SurrealClient, log_id: str) -> None:
    await db.query(
        "UPDATE type::thing('DecisionLog', $lid) SET reviewed = true;",
        {"lid": log_id},
    )


async def add_blocked_by_edge(
    db: SurrealClient, product_id: str, rule_id: str, block_reason: str
) -> None:
    await db.query(
        """
        IF NOT (SELECT * FROM blocked_by WHERE in = type::thing('Product', $pid) AND out = type::thing('ComplianceRule', $rid)) THEN
            RELATE type::thing('Product', $pid)->blocked_by->type::thing('ComplianceRule', $rid)
                SET block_reason = $reason, blocking_since = time::now()
        END;
        """,
        {"pid": product_id, "rid": rule_id, "reason": block_reason},
    )


async def remove_blocked_by_edge(db: SurrealClient, product_id: str, rule_id: str) -> None:
    await db.query(
        "DELETE blocked_by WHERE in = type::thing('Product', $pid) AND out = type::thing('ComplianceRule', $rid);",
        {"pid": product_id, "rid": rule_id},
    )


async def get_interaction_count(db: SurrealClient) -> int:
    rows = await db.query("SELECT count() AS cnt FROM Interaction GROUP ALL")
    return rows[0].get("cnt", 0) if rows else 0


async def update_document_embedding(db: SurrealClient, doc_id: str, embedding: list[float]) -> None:
    await db.query(
        "UPDATE type::thing('document', $did) SET embedding = $emb;",
        {"did": doc_id, "emb": embedding},
    )


async def vector_search_documents(
    db: SurrealClient, query_embedding: list[float], limit: int = 5
) -> list[dict]:
    return await db.query(
        "SELECT id, title, content, doc_type, vector::similarity::cosine(embedding, $emb) AS score FROM document WHERE embedding != [] ORDER BY score DESC LIMIT $lim",
        {"emb": query_embedding, "lim": limit},
    )
