"""
Knowledge graph visualizer using streamlit-agraph.

Renders the customer knowledge graph with color-coded edges:
  green  = owns
  cyan   = eligible_for
  orange = triggered (life events)
  purple = had_interaction
  red    = blocked_by
"""
from __future__ import annotations

import streamlit as st


def render_knowledge_graph(graph_data: dict) -> None:
    """
    Render the customer knowledge graph using streamlit-agraph.

    Args:
        graph_data: Dict returned by db.queries.get_graph_for_viz()
    """
    try:
        from streamlit_agraph import agraph, Config, Edge, Node
    except ImportError:
        st.warning("streamlit-agraph not installed. Run: pip install streamlit-agraph")
        _render_fallback(graph_data)
        return

    nodes: list[Node] = []
    edges: list[Edge] = []
    seen_node_ids: set[str] = set()

    def add_node(node_id: str, label: str, color: str, size: int = 15) -> None:
        if node_id not in seen_node_ids:
            nodes.append(Node(id=node_id, label=label, color=color, size=size))
            seen_node_ids.add(node_id)

    # ── Customer node ────────────────────────────────────────────────────
    customer = graph_data.get("customer", {})
    cid = str(customer.get("id", "customer"))
    cname = customer.get("name", "Customer")
    add_node(cid, cname, "#1565C0", size=30)

    # ── Owned products (green) ────────────────────────────────────────────
    for item in graph_data.get("owned_products", []):
        prod = item.get("out") or item
        pid = str(prod.get("id", ""))
        if not pid:
            continue
        pname = prod.get("name", pid)
        add_node(pid, pname, "#2E7D32")
        edges.append(Edge(source=cid, target=pid, label="owns", color="#43A047"))

    # ── Eligible products (cyan) ──────────────────────────────────────────
    for item in graph_data.get("eligible_products", []):
        prod = item.get("out") or item
        pid = str(prod.get("id", ""))
        if not pid:
            continue
        pname = prod.get("name", pid)
        score = item.get("score", 0)
        add_node(pid, pname, "#00838F")
        edges.append(
            Edge(
                source=cid,
                target=pid,
                label=f"eligible {score:.2f}",
                color="#00BCD4",
            )
        )

    # ── Life events (orange) ──────────────────────────────────────────────
    for item in graph_data.get("life_events", []):
        out = item.get("out") or item
        eid = str(out.get("id", ""))
        if not eid:
            continue
        etype = out.get("event_type", eid)
        conf = out.get("confidence", 0)
        add_node(eid, f"⭐ {etype}", "#E65100")
        edges.append(
            Edge(
                source=cid,
                target=eid,
                label=f"triggered ({conf:.0%})",
                color="#FF9800",
            )
        )

    # ── Interactions (purple) ─────────────────────────────────────────────
    for item in graph_data.get("interactions", []):
        out = item.get("out") or item
        iid = str(out.get("id", ""))
        if not iid:
            continue
        itype = out.get("interaction_type", "interaction")
        sentiment = out.get("sentiment") or ""
        label = f"{itype}" + (f" ({sentiment})" if sentiment else "")
        add_node(iid, label, "#6A1B9A", size=10)
        edges.append(Edge(source=cid, target=iid, label="interaction", color="#AB47BC"))

    # ── Compliance blocks (red) ───────────────────────────────────────────
    for item in graph_data.get("blocked_edges", []):
        pid = str(item.get("product_id", ""))
        rid = str(item.get("rule_id", ""))
        pname = item.get("product_name", pid)
        rname = item.get("rule_name", rid)
        if pid:
            add_node(pid, pname, "#B71C1C")
        if rid:
            add_node(rid, f"⚠ {rname}", "#D32F2F", size=12)
        if pid and rid:
            edges.append(
                Edge(source=pid, target=rid, label="blocked_by", color="#F44336")
            )

    if not nodes:
        st.info("No graph data available for this customer.")
        return

    agraph_config = Config(
        width=720,
        height=480,
        directed=True,
        physics=True,
        hierarchical=False,
        nodeHighlightBehavior=True,
        highlightColor="#F7A7A6",
        collapsible=False,
        node={"labelProperty": "label"},
        link={"labelProperty": "label", "renderLabel": True},
    )

    agraph(nodes=nodes, edges=edges, config=agraph_config)

    # Legend
    st.caption(
        "🟢 owns  🔵 eligible_for  🟠 life event  🟣 interaction  🔴 blocked_by"
    )


def _render_fallback(graph_data: dict) -> None:
    """Simple text fallback when streamlit-agraph is unavailable."""
    customer = graph_data.get("customer", {})
    st.markdown(f"**Customer:** {customer.get('name', 'Unknown')}")

    owned = graph_data.get("owned_products", [])
    if owned:
        st.markdown("**Owned Products:**")
        for p in owned:
            prod = p.get("out") or p
            st.markdown(f"  - {prod.get('name', str(prod.get('id', '')))}")

    eligible = graph_data.get("eligible_products", [])
    if eligible:
        st.markdown("**Eligible Products:**")
        for p in eligible:
            prod = p.get("out") or p
            score = p.get("score", 0)
            st.markdown(
                f"  - {prod.get('name', str(prod.get('id', '')))} (score: {score:.2f})"
            )

    events = graph_data.get("life_events", [])
    if events:
        st.markdown("**Life Events:**")
        for e in events:
            out = e.get("out") or e
            st.markdown(f"  - {out.get('event_type', str(out.get('id', '')))}")
