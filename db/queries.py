"""
Named SurrealDB query functions — all graph traversal + write-back logic lives here.
Application code imports these functions; raw SurrealQL stays out of business logic.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from db.client import SurrealClient


def _safe_strings(items: list) -> list[str]:
    """Coerce items to plain strings, replacing colons to prevent SurrealDB
    from interpreting values like 'vpn_usage:IP' as record links (table:id)."""
    return [str(v).replace(":", " -") for v in items]


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

    Uses subselects to ensure graph traversal returns full objects, not just
    record ID strings (SurrealDB v2 graph traversal returns IDs by default).
    """
    cid = _sanitize_id(customer_id)
    rows = await db.query(
        f"""
        SELECT *,
            (SELECT id, name, category, risk_level, requires_kyc, annual_fee FROM ->owns->Product) AS owned_products,
            (SELECT id, event_type, confidence, detected_at, source FROM ->triggered->LifeEvent) AS life_events,
            (SELECT id, interaction_type, content, sentiment, channel, created_at FROM ->had_interaction->Interaction ORDER BY created_at DESC LIMIT 10) AS interactions,
            (SELECT id, phase, current_step, pending_approval FROM ->has_journey->JourneyState) AS journey_states
        FROM Customer:{cid}
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
        SELECT
            in AS product_id,
            in.name AS product_name,
            out AS rule_id,
            out.name AS rule_name,
            block_reason
        FROM blocked_by
        WHERE in IN (SELECT VALUE out FROM owns WHERE in = Customer:{cid})
           OR in IN (SELECT VALUE out FROM eligible_for WHERE in = Customer:{cid})
        FETCH in, out
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


# ── Multi-hop graph traversal queries ────────────────────────

async def get_life_event_product_paths(db: SurrealClient, event_type: str) -> list[dict]:
    """
    LifeEvent -> unlocks -> Product
    Given an event type, find all products unlocked by it with relevance scores.
    """
    return await db.query(
        """
        SELECT
            in.event_type AS event_type,
            out.id AS product_id,
            out.name AS product_name,
            out.category AS product_category,
            out.risk_level AS product_risk_level,
            out.requires_kyc AS requires_kyc,
            relevance
        FROM unlocks
        WHERE in.event_type = $etype
        ORDER BY relevance DESC
        FETCH in, out
        """,
        {"etype": event_type},
    )


async def get_customer_product_compliance_chain(db: SurrealClient, customer_id: str) -> list[dict]:
    """
    Customer -> eligible_for -> Product -> blocked_by -> ComplianceRule
    For each eligible product, find which compliance rules block it.
    Uses clean SurrealDB v2 graph traversal syntax.
    """
    cid = _sanitize_id(customer_id)
    return await db.query(
        f"""
        SELECT
            in AS product_id,
            in.name AS product_name,
            out AS rule_id,
            out.name AS rule_name,
            out.enforcement AS enforcement,
            block_reason
        FROM blocked_by
        WHERE in IN (SELECT VALUE out FROM eligible_for WHERE in = Customer:{cid})
        FETCH in, out
        """
    )


async def get_customer_interaction_product_trail(db: SurrealClient, customer_id: str) -> list[dict]:
    """
    Customer -> had_interaction -> Interaction -> about_product -> Product
    Returns the chain showing what products the customer has discussed.
    Uses clean SurrealDB v2 graph traversal: start from Interaction records.
    """
    cid = _sanitize_id(customer_id)
    return await db.query(
        f"""
        SELECT
            interaction_type,
            content AS interaction_content,
            sentiment,
            created_at AS interaction_date,
            ->about_product->Product.id AS product_id,
            ->about_product->Product.name AS product_name,
            ->about_product->Product.category AS product_category
        FROM Customer:{cid}->had_interaction->Interaction
        ORDER BY created_at DESC
        """
    )


async def get_customer_journey_decision_trail(db: SurrealClient, customer_id: str) -> list[dict]:
    """
    Customer -> has_journey -> JourneyState -> has_decision -> DecisionLog
    Returns the full audit trail of agent decisions through the journey.
    Uses clean SurrealDB v2 graph traversal syntax.
    """
    cid = _sanitize_id(customer_id)
    return await db.query(
        f"""
        SELECT
            phase AS journey_phase,
            current_step,
            ->has_decision->DecisionLog.action_taken AS action_taken,
            ->has_decision->DecisionLog.agent_reasoning AS agent_reasoning,
            ->has_decision->DecisionLog.confidence_score AS confidence,
            ->has_decision->DecisionLog.compliance_gates_passed AS gates_passed,
            ->has_decision->DecisionLog.compliance_gates_failed AS gates_failed,
            ->has_decision->DecisionLog.requires_human_review AS needs_review,
            ->has_decision->DecisionLog.created_at AS decision_date
        FROM Customer:{cid}->has_journey->JourneyState
        ORDER BY decision_date DESC
        """
    )


async def get_compliance_waiver_paths(db: SurrealClient, product_id: str) -> list[dict]:
    """
    ComplianceRule -> waived_by -> Product
    Check if any compliance rules have waivers for this product.
    """
    pid = _sanitize_id(product_id)
    return await db.query(
        f"""
        SELECT
            in.id AS rule_id,
            in.name AS rule_name,
            in.enforcement AS enforcement,
            reason
        FROM waived_by
        WHERE out = Product:{pid}
        FETCH in
        """
    )


async def get_fraud_signals(db: SurrealClient, customer_id: str) -> list[dict]:
    """
    Graph-native fraud detection via multi-hop traversal.
    Detects: identity linkage rings, shared device patterns, behavioral anomalies,
    VPN/Tor usage, and geo anomalies — patterns impossible to find in RDBMS.

    Traversal chains:
      Customer -> used_device -> Device -> device_seen_ip -> IPAddress (device-IP chain)
      Customer -> from_ip -> IPAddress (direct IP usage)
      Customer -> linked_identity -> Customer (identity rings)
      Customer -> used_device -> Device <- used_device <- Customer (shared device detection)
    """
    cid = _sanitize_id(customer_id)
    signals: list[dict] = []

    # 1. Check for VPN/Tor IP usage (Customer -> from_ip -> IPAddress)
    suspicious_ips = await db.query(
        f"""
        SELECT out.ip AS ip, out.geo_country AS country, out.geo_city AS city,
               out.is_vpn AS is_vpn, out.is_tor AS is_tor, out.risk_score AS risk_score,
               session_count, last_used
        FROM from_ip
        WHERE in = Customer:{cid} AND (out.is_vpn = true OR out.is_tor = true OR out.risk_score > 0.5)
        FETCH out
        """
    )
    for ip in suspicious_ips:
        sig_type = "tor_usage" if ip.get("is_tor") else "vpn_usage" if ip.get("is_vpn") else "suspicious_ip"
        signals.append({
            "signal_type": sig_type,
            "severity": "high" if ip.get("is_tor") else "medium",
            "description": f"{sig_type}: IP {ip.get('ip')} from {ip.get('city', '?')}, {ip.get('country', '?')} "
                          f"(risk: {ip.get('risk_score', 0):.0%}, sessions: {ip.get('session_count', 0)})",
            "evidence": {"ip": ip.get("ip"), "geo": f"{ip.get('city')}, {ip.get('country')}"},
        })

    # 2. Shared device detection (Customer -> used_device -> Device <- used_device <- OtherCustomer)
    # First get this customer's devices
    my_devices = await db.query(
        f"""
        SELECT
            out AS device_id,
            out.device_type AS device_type,
            out.risk_score AS device_risk
        FROM used_device
        WHERE in = Customer:{cid}
        FETCH out
        """
    )
    # Then find other customers who share those devices
    shared_devices = []
    for dev in my_devices:
        dev_id = dev.get("device_id", "")
        if not dev_id:
            continue
        others = await db.query(
            f"""
            SELECT
                in AS other_customer_id,
                in.name AS other_customer_name,
                session_count
            FROM used_device
            WHERE out = {dev_id} AND in != Customer:{cid}
            FETCH in
            """
        )
        for other in others:
            shared_devices.append({
                "device_id": dev_id,
                "device_type": dev.get("device_type"),
                "device_risk": dev.get("device_risk", 0),
                "other_customer_id": other.get("other_customer_id"),
                "other_customer_name": other.get("other_customer_name"),
            })
    for sd in shared_devices:
        signals.append({
            "signal_type": "shared_device",
            "severity": "high" if (sd.get("device_risk") or 0) > 0.5 else "medium",
            "description": f"Device {sd.get('device_id')} ({sd.get('device_type')}) shared with {sd.get('other_customer_name', 'unknown')} "
                          f"(device risk: {sd.get('device_risk', 0):.0%})",
            "evidence": {"device": str(sd.get("device_id")), "shared_with": str(sd.get("other_customer_id"))},
        })

    # 3. Identity linkage rings (Customer -> linked_identity -> Customer)
    identity_links = await db.query(
        f"""
        SELECT out.id AS linked_id, out.name AS linked_name,
               link_type, confidence, link_evidence
        FROM linked_identity
        WHERE in = Customer:{cid} AND confidence > 0.3
        ORDER BY confidence DESC
        """
    )
    for link in identity_links:
        signals.append({
            "signal_type": "identity_ring",
            "severity": "critical" if (link.get("confidence") or 0) > 0.8 else "medium",
            "description": f"Identity link ({link.get('link_type')}): linked to {link.get('linked_name', '?')} "
                          f"(confidence: {link.get('confidence', 0):.0%}, evidence: {link.get('link_evidence', '')})",
            "evidence": {"linked_to": str(link.get("linked_id")), "type": link.get("link_type")},
        })

    # 4. Device -> suspicious IP chain (Customer -> used_device -> Device -> device_seen_ip -> IPAddress)
    # Use graph traversal: start from customer's devices, traverse to suspicious IPs
    device_ip_chain = await db.query(
        f"""
        SELECT
            id AS device_id,
            ->device_seen_ip->IPAddress.ip AS ip,
            ->device_seen_ip->IPAddress.is_vpn AS is_vpn,
            ->device_seen_ip->IPAddress.is_tor AS is_tor,
            ->device_seen_ip->IPAddress.risk_score AS ip_risk,
            ->device_seen_ip->IPAddress.geo_country AS country
        FROM Customer:{cid}->used_device->Device
        WHERE ->device_seen_ip->IPAddress.risk_score CONTAINSANY [true]
           OR ->device_seen_ip->IPAddress.is_tor CONTAINSANY [true]
        """
    )
    # Flatten: the traversal returns arrays, so normalize
    normalized_chains: list[dict] = []
    for dev in device_ip_chain:
        ips_list = dev.get("ip") or []
        if isinstance(ips_list, list):
            for i, ip_val in enumerate(ips_list):
                risk = (dev.get("ip_risk") or [0])[i] if isinstance(dev.get("ip_risk"), list) and i < len(dev.get("ip_risk", [])) else 0
                is_tor_val = (dev.get("is_tor") or [False])[i] if isinstance(dev.get("is_tor"), list) and i < len(dev.get("is_tor", [])) else False
                is_vpn_val = (dev.get("is_vpn") or [False])[i] if isinstance(dev.get("is_vpn"), list) and i < len(dev.get("is_vpn", [])) else False
                if (risk or 0) > 0.5 or is_tor_val:
                    normalized_chains.append({
                        "device_id": dev.get("device_id"),
                        "ip": ip_val,
                        "is_vpn": is_vpn_val,
                        "is_tor": is_tor_val,
                        "ip_risk": risk,
                        "country": (dev.get("country") or [""])[i] if isinstance(dev.get("country"), list) and i < len(dev.get("country", [])) else "",
                    })
    device_ip_chain = normalized_chains
    for chain in device_ip_chain:
        signals.append({
            "signal_type": "device_ip_anomaly",
            "severity": "high",
            "description": f"Device {chain.get('device_id')} connected to suspicious IP {chain.get('ip')} "
                          f"({chain.get('country', '?')}, VPN={chain.get('is_vpn')}, Tor={chain.get('is_tor')})",
            "evidence": {"device": str(chain.get("device_id")), "ip": chain.get("ip")},
        })

    # 5. Open fraud alerts
    alerts = await db.query(
        f"SELECT * FROM FraudAlert WHERE customer_id = 'Customer:{cid}' AND status IN ['open', 'investigating'] ORDER BY created_at DESC"
    )
    for alert in alerts:
        signals.append({
            "signal_type": f"alert_{alert.get('alert_type', 'unknown')}",
            "severity": alert.get("severity", "medium"),
            "description": alert.get("description", ""),
            "evidence": alert.get("evidence", {}),
        })

    return signals


async def get_identity_ring(db: SurrealClient, customer_id: str, max_depth: int = 3) -> list[dict]:
    """
    Detect identity linkage rings via recursive graph traversal.
    Customer -> linked_identity -> Customer -> linked_identity -> ... (up to max_depth hops)

    This is the kind of query that makes graph databases shine — finding rings
    of connected identities that share devices, IPs, or behavioral patterns.
    """
    cid = _sanitize_id(customer_id)
    # SurrealDB doesn't support recursive CTEs, so we do iterative hops
    visited = set()
    visited.add(f"Customer:{cid}")
    ring: list[dict] = []
    current_layer = [f"Customer:{cid}"]

    for depth in range(max_depth):
        if not current_layer:
            break
        # Find all linked identities from current layer
        ids_str = ", ".join(current_layer)
        links = await db.query(
            f"""
            SELECT in AS from_id, out.id AS to_id, out.name AS to_name,
                   link_type, confidence, link_evidence
            FROM linked_identity
            WHERE in IN [{ids_str}]
            FETCH out
            """
        )
        next_layer = []
        for link in links:
            to_id = str(link.get("to_id", ""))
            if to_id and to_id not in visited:
                visited.add(to_id)
                next_layer.append(to_id)
                ring.append({
                    "from": str(link.get("from_id")),
                    "to": to_id,
                    "to_name": link.get("to_name"),
                    "link_type": link.get("link_type"),
                    "confidence": link.get("confidence"),
                    "depth": depth + 1,
                })
        current_layer = next_layer

    return ring


async def get_shared_device_cluster(db: SurrealClient, device_id: str) -> list[dict]:
    """
    Find all customers who have used a specific device.
    Device <- used_device <- Customer
    Used to detect account sharing or takeover patterns.
    """
    did = _sanitize_id(device_id)
    return await db.query(
        f"""
        SELECT in.id AS customer_id, in.name AS customer_name,
               session_count, last_used
        FROM used_device
        WHERE out = Device:{did}
        ORDER BY last_used DESC
        FETCH in
        """
    )


async def get_full_customer_graph(db: SurrealClient, customer_id: str) -> dict:
    """
    Full multi-hop graph exploration for a customer — pulls all relationship chains.
    Used by the advisor dashboard for complete graph visualization.

    Chains:
      Customer -> owns -> Product -> blocked_by -> ComplianceRule
      Customer -> triggered -> LifeEvent -> unlocks -> Product
      Customer -> had_interaction -> Interaction -> about_product -> Product
      Customer -> has_journey -> JourneyState -> has_decision -> DecisionLog
      Product -> requires_approval -> ComplianceRule
      ComplianceRule -> waived_by -> Product
    """
    cid = _sanitize_id(customer_id)

    # 1. Customer -> owns -> Product
    owned = await db.query(
        f"SELECT out.id, out.name, out.category, since, status FROM owns WHERE in = Customer:{cid} FETCH out"
    )

    # 2. Customer -> eligible_for -> Product
    eligible = await db.query(
        f"SELECT out.id, out.name, out.category, score, reason FROM eligible_for WHERE in = Customer:{cid} FETCH out"
    )

    # 3. Customer -> triggered -> LifeEvent -> unlocks -> Product (2-hop graph traversal)
    life_event_chains = await db.query(
        f"""
        SELECT
            event_type,
            confidence,
            detected_at,
            source,
            ->unlocks->Product.id AS unlocked_product_id,
            ->unlocks->Product.name AS unlocked_product_name,
            ->unlocks.relevance AS product_relevance
        FROM Customer:{cid}->triggered->LifeEvent
        """
    )

    # 4. Customer -> had_interaction -> Interaction -> about_product -> Product (2-hop graph traversal)
    interaction_chains = await db.query(
        f"""
        SELECT
            interaction_type AS type,
            content,
            sentiment,
            created_at AS date,
            ->about_product->Product.id AS product_id,
            ->about_product->Product.name AS product_name
        FROM Customer:{cid}->had_interaction->Interaction
        ORDER BY created_at DESC
        """
    )

    # 5. Product -> blocked_by -> ComplianceRule (for owned + eligible products)
    all_product_ids = set()
    for p in owned:
        pid = p.get("id") or (p.get("out", {}) if isinstance(p.get("out"), dict) else {}).get("id", "")
        if pid:
            all_product_ids.add(str(pid))
    for p in eligible:
        pid = p.get("id") or (p.get("out", {}) if isinstance(p.get("out"), dict) else {}).get("id", "")
        if pid:
            all_product_ids.add(str(pid))

    blocked_chains = await db.query(
        "SELECT in AS product_id, out.id AS rule_id, out.name AS rule_name, out.enforcement, block_reason FROM blocked_by FETCH out"
    )

    # 6. Product -> requires_approval -> ComplianceRule
    approval_chains = await db.query(
        "SELECT in AS product_id, out.id AS rule_id, out.name AS rule_name, out.enforcement, approval_reason FROM requires_approval FETCH out"
    )

    # 7. ComplianceRule -> waived_by -> Product
    waiver_chains = await db.query(
        "SELECT in AS rule_id, in.name AS rule_name, out AS product_id, reason FROM waived_by FETCH in"
    )

    # 8. Customer -> has_journey -> JourneyState -> has_decision -> DecisionLog (2-hop graph traversal)
    journey_chains = await db.query(
        f"""
        SELECT
            phase,
            current_step AS step,
            pending_approval AS pending,
            ->has_decision->DecisionLog.action_taken AS decision_action,
            ->has_decision->DecisionLog.agent_reasoning AS decision_reasoning,
            ->has_decision->DecisionLog.confidence_score AS decision_confidence,
            ->has_decision->DecisionLog.created_at AS decision_date
        FROM Customer:{cid}->has_journey->JourneyState
        ORDER BY decision_date DESC
        """
    )

    # 9. Customer -> used_device -> Device (fraud: device fingerprints)
    devices = await db.query(
        f"""
        SELECT out.id AS device_id, out.device_type, out.os, out.risk_score AS device_risk,
               session_count, last_used
        FROM used_device
        WHERE in = Customer:{cid}
        FETCH out
        """
    )

    # 10. Customer -> from_ip -> IPAddress (fraud: IP history)
    ips = await db.query(
        f"""
        SELECT out.ip, out.geo_country, out.geo_city, out.is_vpn, out.is_tor,
               out.risk_score AS ip_risk, session_count, last_used
        FROM from_ip
        WHERE in = Customer:{cid}
        FETCH out
        """
    )

    # 11. Identity linkage ring (fraud: shared identities)
    identity_links = await db.query(
        f"""
        SELECT out.id AS linked_id, out.name AS linked_name,
               link_type, confidence, link_evidence
        FROM linked_identity
        WHERE in = Customer:{cid}
        FETCH out
        """
    )

    # 12. Fraud alerts
    fraud_alerts = await db.query(
        f"SELECT * FROM FraudAlert WHERE customer_id = 'Customer:{cid}' ORDER BY created_at DESC"
    )

    return {
        "customer_id": f"Customer:{cid}",
        "owns": owned,
        "eligible_for": eligible,
        "life_event_chains": life_event_chains,
        "interaction_chains": interaction_chains,
        "blocked_chains": blocked_chains,
        "approval_chains": approval_chains,
        "waiver_chains": waiver_chains,
        "journey_decision_chains": journey_chains,
        # Fraud detection graph data
        "devices": devices,
        "ip_addresses": ips,
        "identity_links": identity_links,
        "fraud_alerts": fraud_alerts,
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
        "passed": _safe_strings(compliance_gates_passed),
        "failed": _safe_strings(compliance_gates_failed),
        "nodes": _safe_strings(graph_nodes_consulted),
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
            "risks": _safe_strings(risk_factors),
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
        "SELECT id, title, content, doc_type, vector::similarity::cosine(embedding, $emb) AS score FROM document WHERE embedding IS NOT NONE ORDER BY score DESC LIMIT $lim",
        {"emb": query_embedding, "lim": limit},
    )


async def update_compliance_conditions(db: SurrealClient, rule_id: str, conditions: dict) -> None:
    """Update the conditions JSON on a ComplianceRule — supports dynamic rule parameters."""
    rid = _sanitize_id(rule_id)
    await db.query(
        f"UPDATE ComplianceRule:{rid} SET conditions = $cond;",
        {"cond": conditions},
    )
