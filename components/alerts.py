"""
Approval queue component — renders pending ApprovalRequests with action buttons.
"""
from __future__ import annotations

import streamlit as st

from db.client import SurrealClient, run_sync
import db.queries as Q


def render_approval_queue(
    db: SurrealClient,
    approvals: list[dict],
    on_resolved: callable | None = None,
) -> None:
    """
    Render the advisor approval queue.

    Args:
        db: Connected SurrealDB client.
        approvals: List of pending ApprovalRequest records.
        on_resolved: Optional callback invoked after an approval action.
    """
    if not approvals:
        st.success("✅ No pending approvals.")
        return

    st.markdown(f"### ⚠️ Pending Approvals ({len(approvals)})")

    for req in approvals:
        req_id = str(req.get("id", "")).split(":")[-1]
        customer_id = str(req.get("customer_id", "")).replace("Customer:", "")
        proposed = req.get("proposed_action", "Unknown action")
        rationale = req.get("agent_rationale", "")
        risk_factors = req.get("risk_factors", [])
        created = str(req.get("created_at", ""))[:19].replace("T", " ")

        with st.expander(f"🔔 {proposed}  —  customer: **{customer_id}**  |  `{created}`"):
            st.markdown(f"**Proposed Action:** {proposed}")
            if rationale:
                st.markdown(f"**Agent Rationale:** {rationale}")
            if risk_factors:
                st.markdown("**Risk Factors:**")
                for rf in risk_factors:
                    st.markdown(f"  - {rf}")

            col1, col2, col3 = st.columns(3)

            with col1:
                if st.button("✅ Approve", key=f"approve_{req_id}"):
                    response = st.text_input(
                        "Approval note (optional)",
                        key=f"approve_note_{req_id}",
                        value="Approved by advisor.",
                    )
                    _resolve(db, req_id, "approved", response or "Approved by advisor.")
                    if on_resolved:
                        on_resolved(req_id, "approved", customer_id)
                    st.rerun()

            with col2:
                if st.button("❌ Reject", key=f"reject_{req_id}"):
                    response = st.text_input(
                        "Rejection reason",
                        key=f"reject_note_{req_id}",
                        value="Not suitable at this time.",
                    )
                    _resolve(db, req_id, "rejected", response or "Not suitable at this time.")
                    if on_resolved:
                        on_resolved(req_id, "rejected", customer_id)
                    st.rerun()

            with col3:
                if st.button("✏️ Modify", key=f"modify_{req_id}"):
                    response = st.text_input(
                        "Modified action / note",
                        key=f"modify_note_{req_id}",
                        placeholder="Describe the modification...",
                    )
                    if response:
                        _resolve(db, req_id, "modified", response)
                        if on_resolved:
                            on_resolved(req_id, "modified", customer_id)
                        st.rerun()


def _resolve(db: SurrealClient, req_id: str, status: str, response: str) -> None:
    try:
        run_sync(Q.resolve_approval_request(db, req_id, status, response))
        st.toast(f"Request {status}.", icon="✅" if status == "approved" else "ℹ️")
    except Exception as e:
        st.error(f"Failed to update approval: {e}")
