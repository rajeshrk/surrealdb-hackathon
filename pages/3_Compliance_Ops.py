"""
Compliance Operations — rule management, audit trail, real-time graph evolution.

KEY DEMO FEATURE: Toggling a rule immediately creates/removes blocked_by edges
in SurrealDB, demonstrating real-time graph evolution.
"""
import streamlit as st

from db.client import SurrealClient, run_sync
import db.queries as Q

st.set_page_config(page_title="Compliance Ops", page_icon="🛡️", layout="wide")
st.title("🛡️ Compliance Operations")


@st.cache_resource
def get_db() -> SurrealClient:
    return run_sync(SurrealClient.connect())


# ── Analytics strip ───────────────────────────────────────────────────────

all_logs = run_sync(Q.get_decision_logs(get_db(), limit=200))
total_logs = len(all_logs)
blocked_logs = sum(1 for l in all_logs if str(l.get("action_taken", "")).upper() in ("BLOCKED", "blocked"))
needs_review = sum(1 for l in all_logs if l.get("requires_human_review") and not l.get("reviewed"))
all_approvals = run_sync(Q.get_pending_approvals(get_db()))

c1, c2, c3, c4 = st.columns(4)
c1.metric("Total Decisions", total_logs)
c2.metric("Block Rate", f"{blocked_logs / max(total_logs, 1):.0%}")
c3.metric("Pending Approvals", len(all_approvals))
c4.metric("Unreviewed Audits", needs_review)

st.divider()

tab_rules, tab_audit = st.tabs(["📋 Compliance Rules", "🔍 Audit Trail"])

# ── Tab 1: Compliance Rules ───────────────────────────────────────────────

with tab_rules:
    st.subheader("Active Compliance Rules")
    st.caption(
        "Toggling a rule instantly updates `blocked_by` edges in the SurrealDB knowledge graph."
    )

    rules = run_sync(Q.get_all_compliance_rules(get_db()))
    products = run_sync(get_db().query("SELECT id, name, requires_kyc FROM Product"))

    ENFORCEMENT_OPTIONS = ["hard", "human_approval", "audit_only"]
    ENFORCEMENT_COLOURS = {
        "hard": "🔴",
        "human_approval": "🟡",
        "audit_only": "🟢",
    }

    for rule in rules:
        rule_id = str(rule.get("id", "")).split(":")[-1]
        rule_name = rule.get("name", rule_id)
        description = rule.get("description", "")
        enforcement = rule.get("enforcement", "hard")
        active = rule.get("active", True)
        conditions = rule.get("conditions") or {}

        colour = ENFORCEMENT_COLOURS.get(enforcement, "⚪")

        with st.expander(
            f"{colour} **{rule_name}**  {'✅ Active' if active else '⏸️ Inactive'}",
            expanded=False,
        ):
            st.caption(description)

            col_active, col_enforcement, col_conditions = st.columns([1, 2, 3])

            with col_active:
                new_active = st.toggle(
                    "Active",
                    value=active,
                    key=f"toggle_{rule_id}",
                )
                if new_active != active:
                    run_sync(Q.toggle_compliance_rule(get_db(), rule_id, new_active))

                    # ── CRITICAL DEMO FEATURE ─────────────────────────────
                    # When deactivating a KYC or hard rule, remove blocked_by edges.
                    # When activating, re-create them for affected products.
                    _apply_graph_evolution(get_db(), rule_id, rule, products, new_active)
                    st.success(f"Rule {'activated' if new_active else 'deactivated'}. Graph updated.")
                    st.rerun()

            with col_enforcement:
                new_enforcement = st.selectbox(
                    "Enforcement tier",
                    ENFORCEMENT_OPTIONS,
                    index=ENFORCEMENT_OPTIONS.index(enforcement)
                    if enforcement in ENFORCEMENT_OPTIONS
                    else 0,
                    key=f"enforce_{rule_id}",
                )
                if new_enforcement != enforcement:
                    run_sync(
                        Q.update_compliance_enforcement(get_db(), rule_id, new_enforcement)
                    )
                    st.success(f"Enforcement updated to **{new_enforcement}**.")
                    st.rerun()

            with col_conditions:
                st.markdown("**Conditions:**")
                if conditions:
                    for k, v in conditions.items():
                        new_val = st.text_input(
                            k,
                            value=str(v),
                            key=f"cond_{rule_id}_{k}",
                        )
                        if new_val != str(v):
                            # Parse value back to correct type
                            try:
                                parsed = int(new_val) if "." not in new_val else float(new_val)
                            except ValueError:
                                parsed = new_val
                            updated_conditions = {**conditions, k: parsed}
                            run_sync(
                                Q.update_compliance_conditions(
                                    get_db(), rule_id, updated_conditions
                                )
                            )
                            st.success(f"Condition `{k}` updated to `{parsed}`.")
                            st.rerun()
                else:
                    st.caption("No conditions stored.")


def _apply_graph_evolution(
    db: SurrealClient,
    rule_id: str,
    rule: dict,
    products: list[dict],
    new_active: bool,
) -> None:
    """
    Create or remove blocked_by edges when a compliance rule is toggled.
    This makes graph evolution visible in the Advisor Dashboard.
    """
    enforcement = rule.get("enforcement", "hard")
    conditions = rule.get("conditions") or {}

    if enforcement != "hard":
        return  # Only hard rules create blocked_by edges

    # Determine which products are affected
    affected_product_ids: list[str] = []

    if rule_id == "kyc_fresh":
        # Affects all products requiring KYC
        affected_product_ids = [
            str(p.get("id", "")).split(":")[-1]
            for p in products
            if p.get("requires_kyc")
        ]
    elif rule_id == "cooling_off":
        # Affects all products with cooling-off days
        all_products = run_sync(db.query("SELECT id, cooling_off_days FROM Product"))
        affected_product_ids = [
            str(p.get("id", "")).split(":")[-1]
            for p in all_products
            if int(p.get("cooling_off_days", 0)) > 0
        ]
    else:
        # Generic: affect all products
        affected_product_ids = [
            str(p.get("id", "")).split(":")[-1] for p in products
        ]

    block_reason = f"Compliance rule '{rule.get('name', rule_id)}' enforcement"

    for pid in affected_product_ids:
        if new_active:
            run_sync(Q.add_blocked_by_edge(db, pid, rule_id, block_reason))
        else:
            run_sync(Q.remove_blocked_by_edge(db, pid, rule_id))


# ── Tab 2: Audit Trail ────────────────────────────────────────────────────

with tab_audit:
    st.subheader("Decision Audit Trail")

    # Filters
    col_f1, col_f2, col_f3 = st.columns(3)
    with col_f1:
        filter_customer = st.selectbox(
            "Filter by customer",
            ["All"] + [
                str(c.get("id", "")).split(":")[-1]
                for c in run_sync(Q.get_all_customers(get_db()))
            ],
        )
    with col_f2:
        filter_action = st.selectbox(
            "Filter by action",
            ["All", "recommend", "escalate", "retain", "blocked", "no_action"],
        )
    with col_f3:
        filter_reviewed = st.selectbox(
            "Review status",
            ["All", "Needs review", "Reviewed"],
        )

    cid_filter = None if filter_customer == "All" else filter_customer
    audit_logs = run_sync(Q.get_decision_logs(get_db(), cid_filter, limit=100))

    # Apply filters
    if filter_action != "All":
        audit_logs = [
            l for l in audit_logs
            if str(l.get("action_taken", "")).lower() == filter_action.lower()
        ]
    if filter_reviewed == "Needs review":
        audit_logs = [l for l in audit_logs if l.get("requires_human_review") and not l.get("reviewed")]
    elif filter_reviewed == "Reviewed":
        audit_logs = [l for l in audit_logs if l.get("reviewed")]

    st.caption(f"Showing {len(audit_logs)} decision(s)")

    for log in audit_logs[:50]:
        log_id = str(log.get("id", "")).split(":")[-1]
        action = str(log.get("action_taken", "—"))
        cid_display = str(log.get("customer_id", "")).replace("Customer:", "")
        created = str(log.get("created_at", ""))[:19].replace("T", " ")
        confidence = float(log.get("confidence_score", 0))
        reasoning = (log.get("agent_reasoning") or "")[:200]
        passed = log.get("compliance_gates_passed", [])
        failed = log.get("compliance_gates_failed", [])
        needs_rev = log.get("requires_human_review", False)
        reviewed = log.get("reviewed", False)
        trace_id = log.get("langsmith_trace_id")

        icon = "🚫" if action.upper() == "BLOCKED" else ("✅" if action == "recommend" else "📋")
        rev_badge = "🔍 *Needs review*" if needs_rev and not reviewed else ("✅ *Reviewed*" if reviewed else "")

        with st.expander(
            f"{icon} **{cid_display}** · {action} · `{created}` {rev_badge}",
            expanded=False,
        ):
            cols = st.columns([2, 2, 2, 2])
            cols[0].metric("Confidence", f"{confidence:.0%}")
            cols[1].metric("Passed Rules", len(passed))
            cols[2].metric("Failed Rules", len(failed))
            cols[3].metric("Needs Review", "Yes" if needs_rev else "No")

            if reasoning:
                st.markdown(f"**Reasoning:** {reasoning}")
            if failed:
                st.error(f"Failed: {', '.join(str(f) for f in failed)}")
            if trace_id:
                st.caption(f"🔍 LangSmith trace: `{trace_id}`")

            col_btn, _ = st.columns([2, 8])
            with col_btn:
                if needs_rev and not reviewed:
                    if st.button("Mark Reviewed", key=f"rev_{log_id}"):
                        run_sync(Q.mark_decision_reviewed(get_db(), log_id))
                        st.rerun()
