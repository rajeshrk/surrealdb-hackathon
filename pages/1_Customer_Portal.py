"""
Customer Portal — chat interface that invokes the LangGraph agent.

Multi-turn conversation: maintains chat history and previously recommended
products in session state so the agent can provide contextual responses.
"""
import streamlit as st

import config
from db.client import SurrealClient, run_sync
import db.queries as Q

st.set_page_config(page_title="Customer Portal", page_icon="💬", layout="wide")
st.title("💬 Customer Portal")

# ── Helpers ───────────────────────────────────────────────────────────────

@st.cache_resource
def get_db() -> SurrealClient:
    db = run_sync(SurrealClient.connect())
    run_sync(db.bootstrap())
    return db


def get_customers() -> list[dict]:
    return run_sync(Q.get_all_customers(get_db()))


def run_agent(
    customer_id: str,
    message: str,
    chat_history: list[dict] | None = None,
) -> dict:
    """Run the LangGraph agent for a given customer + message."""
    from agent.graph import build_journey_graph, make_run_config

    db = get_db()
    graph = build_journey_graph(db)
    run_config = make_run_config(customer_id)

    # Pass full conversation history so the agent can generate contextual responses
    # LangGraph memory: messages accumulate via operator.add reducer across turns
    all_messages = list(chat_history or [])
    all_messages.append({"role": "user", "content": message})

    initial_state = {
        "customer_id": customer_id,
        "user_message": message,
        "messages": all_messages,
    }

    result = run_sync(graph.ainvoke(initial_state, config=run_config))
    return result


# ── Customer selector (sidebar) ───────────────────────────────────────────

with st.sidebar:
    st.header("👤 Customer")

    customers = get_customers()
    customer_options = {
        f"{c.get('name', 'Unknown')} ({str(c.get('id', '')).split(':')[-1]})": (
            str(c.get("id", "")).split(":")[-1]
        )
        for c in customers
    }

    selected_label = st.selectbox("Select Customer", list(customer_options.keys()))
    customer_id = customer_options.get(selected_label, "sarah")

    # Show customer profile
    customer = next(
        (c for c in customers if str(c.get("id", "")).split(":")[-1] == customer_id),
        {},
    )
    if customer:
        st.markdown("---")
        st.markdown("**Profile**")
        st.markdown(f"- **Segment:** {customer.get('segment', '')}")
        st.markdown(f"- **Risk:** {customer.get('risk_profile', '')}")
        st.markdown(f"- **KYC:** {customer.get('kyc_status', '')}")
        st.markdown(f"- **Channel:** {customer.get('channel_preference', '')}")

    # Show current products + journey phase
    st.markdown("---")
    journey = run_sync(Q.get_journey_state(get_db(), customer_id))
    if journey:
        phase = journey.get("phase", "unknown")
        step = journey.get("current_step", "")
        st.markdown(f"**Journey Phase:** `{phase}`")
        st.markdown(f"**Current Step:** `{step}`")
        pending = journey.get("pending_approval", False)
        if pending:
            st.warning("⚠️ Pending advisor approval")

    st.markdown("---")
    st.caption("💡 Try: *'I just had a baby'* or *'Tell me about investing'*")

# ── Reset chat when customer changes ─────────────────────────────────────

if st.session_state.get("last_customer") != customer_id:
    st.session_state["messages"] = []
    st.session_state["last_customer"] = customer_id

if "messages" not in st.session_state:
    st.session_state["messages"] = []

# ── Graph evolution counter ───────────────────────────────────────────────

interaction_count_before = run_sync(Q.get_interaction_count(get_db()))

# ── Render chat history ───────────────────────────────────────────────────

for msg in st.session_state["messages"]:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# ── Chat input ────────────────────────────────────────────────────────────

if user_input := st.chat_input("Type a message…"):
    # Show user message immediately
    st.session_state["messages"].append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    # Run agent with full conversation context
    with st.chat_message("assistant"):
        with st.spinner("Thinking…"):
            try:
                result = run_agent(
                    customer_id,
                    user_input,
                    chat_history=st.session_state["messages"][:-1],  # exclude current msg (passed separately)
                )
                response = result.get("response_message", "I'm processing your request.")
                requires_approval = result.get("requires_human_approval", False)
                detected_events = result.get("detected_life_events", [])
                compliance = result.get("compliance_result", {})

            except Exception as exc:
                response = f"⚠️ Agent error: {exc}"
                requires_approval = False
                detected_events = []
                compliance = {}

        st.markdown(response)

        # Show agent insights
        if detected_events:
            for evt in detected_events:
                st.info(
                    f"🌟 Life event detected: **{evt.get('event_type', '')}** "
                    f"(confidence: {float(evt.get('confidence', 0)):.0%})"
                )

        if requires_approval:
            st.warning("⏳ This recommendation is pending advisor approval.")

        blocked = compliance.get("blocked", [])
        if blocked:
            for b in blocked[:2]:
                reasons = b.get("block_reasons", [{}])
                reason_name = reasons[0].get("name", "compliance rule") if reasons else "compliance rule"
                st.error(f"🚫 Blocked by: **{reason_name}**")

    st.session_state["messages"].append({"role": "assistant", "content": response})

    # Graph evolution indicator
    interaction_count_after = run_sync(Q.get_interaction_count(get_db()))
    if interaction_count_after > interaction_count_before:
        st.success(
            f"📈 Graph evolved: {interaction_count_before} → {interaction_count_after} interactions"
        )
