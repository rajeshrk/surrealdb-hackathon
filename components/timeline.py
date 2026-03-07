"""
Journey timeline component — renders DecisionLog entries as a vertical timeline.
"""
from __future__ import annotations

import streamlit as st


_ACTION_ICONS = {
    "recommend": "💡",
    "escalate": "👤",
    "retain": "🤝",
    "inform": "ℹ️",
    "blocked": "🚫",
    "no_action": "⏸️",
    "BLOCKED": "🚫",
}

_ACTION_COLOURS = {
    "recommend": "green",
    "escalate": "orange",
    "retain": "blue",
    "blocked": "red",
    "BLOCKED": "red",
    "no_action": "gray",
    "inform": "blue",
}


def render_journey_timeline(decision_logs: list[dict]) -> None:
    """
    Render a vertical timeline of DecisionLog entries.

    Args:
        decision_logs: List of DecisionLog dicts from db.queries.get_decision_logs()
    """
    if not decision_logs:
        st.info("No decisions recorded yet for this customer.")
        return

    st.markdown("### Journey Timeline")

    for log in decision_logs:
        action = str(log.get("action_taken", "unknown"))
        icon = _ACTION_ICONS.get(action, "📋")
        colour = _ACTION_COLOURS.get(action, "gray")
        created = log.get("created_at", "")
        if isinstance(created, str):
            created_display = created[:19].replace("T", " ")
        else:
            created_display = str(created)[:19]

        reasoning = log.get("agent_reasoning", "")[:300]
        confidence = float(log.get("confidence_score", 0))
        passed = log.get("compliance_gates_passed", [])
        failed = log.get("compliance_gates_failed", [])
        trace_id = log.get("langsmith_trace_id")
        needs_review = log.get("requires_human_review", False)
        reviewed = log.get("reviewed", False)
        log_id = str(log.get("id", "")).split(":")[-1]

        with st.container():
            col_icon, col_body = st.columns([1, 11])
            with col_icon:
                st.markdown(f"## {icon}")
            with col_body:
                badge = f":{colour}[**{action.upper()}**]"
                review_badge = ""
                if needs_review:
                    review_badge = " 🔍 *Needs Review*" if not reviewed else " ✅ *Reviewed*"

                st.markdown(
                    f"{badge}  `{created_display}`{review_badge}"
                )
                if reasoning:
                    st.caption(reasoning)

                meta_parts = []
                if confidence:
                    meta_parts.append(f"Confidence: {confidence:.0%}")
                if passed:
                    meta_parts.append(f"✅ {', '.join(str(p) for p in passed[:2])}")
                if failed:
                    meta_parts.append(f"❌ {', '.join(str(f) for f in failed[:2])}")
                if meta_parts:
                    st.caption(" · ".join(meta_parts))

                if trace_id:
                    st.caption(f"🔍 LangSmith trace: `{trace_id}`")

            st.divider()
