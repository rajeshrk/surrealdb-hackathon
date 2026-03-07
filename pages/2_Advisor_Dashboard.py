"""
Advisor Dashboard — knowledge graph visualizer, journey timeline, approval queue.
"""
import streamlit as st

from db.client import SurrealClient, run_sync
import db.queries as Q
from components.graph_viz import render_knowledge_graph
from components.timeline import render_journey_timeline
from components.alerts import render_approval_queue

st.set_page_config(page_title="Advisor Dashboard", page_icon="📊", layout="wide")
st.title("📊 Advisor Dashboard")


@st.cache_resource
def get_db() -> SurrealClient:
    db = run_sync(SurrealClient.connect())
    run_sync(db.bootstrap())
    return db


# ── Customer selector ─────────────────────────────────────────────────────

customers = run_sync(Q.get_all_customers(get_db()))
customer_options = {
    f"{c.get('name', 'Unknown')} ({str(c.get('id', '')).split(':')[-1]})": (
        str(c.get("id", "")).split(":")[-1]
    )
    for c in customers
}

col_sel, col_refresh = st.columns([4, 1])
with col_sel:
    selected_label = st.selectbox("Select Customer", list(customer_options.keys()))
    customer_id = customer_options.get(selected_label, "sarah")
with col_refresh:
    st.markdown("<br/>", unsafe_allow_html=True)
    if st.button("🔄 Refresh"):
        st.rerun()

# ── Customer summary bar ──────────────────────────────────────────────────

customer = next(
    (c for c in customers if str(c.get("id", "")).split(":")[-1] == customer_id),
    {},
)
journey = run_sync(Q.get_journey_state(get_db(), customer_id))

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Segment", customer.get("segment", "—").capitalize())
c2.metric("Risk Profile", customer.get("risk_profile", "—").capitalize())
c3.metric("KYC Status", customer.get("kyc_status", "—").capitalize())
c4.metric("Journey Phase", (journey or {}).get("phase", "—").capitalize() if journey else "—")
c5.metric(
    "Pending Approval",
    "Yes ⚠️" if (journey or {}).get("pending_approval") else "No ✅",
)

st.divider()

# ── Tabs ──────────────────────────────────────────────────────────────────

tab_graph, tab_timeline, tab_approvals, tab_nba = st.tabs([
    "🕸️ Knowledge Graph",
    "📅 Journey Timeline",
    "✅ Approval Queue",
    "💡 Next Best Action",
])

# ── Tab 1: Knowledge Graph ────────────────────────────────────────────────

with tab_graph:
    st.subheader("Customer Knowledge Graph")
    st.caption(
        "Live view of the SurrealDB graph — nodes and edges update after every agent run."
    )
    graph_data = run_sync(Q.get_graph_for_viz(get_db(), customer_id))
    render_knowledge_graph(graph_data)

    with st.expander("📋 Raw graph data"):
        import json
        st.json(
            {k: v for k, v in graph_data.items() if v},
            expanded=False,
        )

# ── Tab 2: Journey Timeline ───────────────────────────────────────────────

with tab_timeline:
    logs = run_sync(Q.get_decision_logs(get_db(), customer_id, limit=30))
    render_journey_timeline(logs)

    if logs:
        # Mark-as-reviewed for audit_only decisions
        unreviewed = [
            l for l in logs
            if l.get("requires_human_review") and not l.get("reviewed")
        ]
        if unreviewed:
            st.markdown(f"**{len(unreviewed)} unreviewed audit decision(s)**")
            for log in unreviewed[:5]:
                log_id = str(log.get("id", "")).split(":")[-1]
                action = log.get("action_taken", "")
                if st.button(f"Mark reviewed: {action} ({log_id[:8]}…)", key=f"rev_{log_id}"):
                    run_sync(Q.mark_decision_reviewed(get_db(), log_id))
                    st.rerun()

# ── Tab 3: Approval Queue ─────────────────────────────────────────────────

with tab_approvals:
    approvals = run_sync(Q.get_pending_approvals(get_db(), customer_id))

    def on_resolved(req_id: str, status: str, cid: str) -> None:
        # After approval, update journey pending flag
        run_sync(
            Q.update_journey_state(
                get_db(), cid, "active", "approval_resolved", pending_approval=False
            )
        )

    render_approval_queue(get_db(), approvals, on_resolved=on_resolved)

    # Show all pending across all customers
    all_pending = run_sync(Q.get_pending_approvals(get_db()))
    if len(all_pending) > len(approvals):
        st.caption(
            f"ℹ️ {len(all_pending) - len(approvals)} additional pending approvals "
            "from other customers."
        )

# ── Tab 4: Next Best Action ───────────────────────────────────────────────

with tab_nba:
    st.subheader("Current Agent Recommendation")

    eligible = run_sync(Q.get_eligible_products(get_db(), customer_id))
    recent_logs = run_sync(Q.get_decision_logs(get_db(), customer_id, limit=1))
    latest_log = recent_logs[0] if recent_logs else {}

    if latest_log:
        action = latest_log.get("action_taken", "—")
        reasoning = latest_log.get("agent_reasoning", "No reasoning recorded.")
        confidence = float(latest_log.get("confidence_score", 0))
        trace_id = latest_log.get("langsmith_trace_id")

        col_a, col_b = st.columns([1, 3])
        with col_a:
            st.metric("Last Action", action.upper())
            st.metric("Confidence", f"{confidence:.0%}")
        with col_b:
            st.markdown("**Agent Reasoning:**")
            st.info(reasoning[:500] if reasoning else "_No reasoning recorded._")
            if trace_id:
                st.markdown(f"🔍 LangSmith trace: `{trace_id}`")
    else:
        st.info("No agent runs recorded yet. Chat with the customer to trigger the agent.")

    st.markdown("---")
    st.markdown("**Eligible Products (from knowledge graph):**")
    if eligible:
        for e in eligible:
            prod = e.get("out") or e
            score = e.get("score", 0)
            reason = e.get("reason", "")
            pname = prod.get("name", str(prod.get("id", "")))
            cat = prod.get("category", "")
            st.markdown(
                f"- **{pname}** ({cat}) — score: `{score:.2f}` — _{reason}_"
            )
    else:
        st.info("No eligible products in graph.")
