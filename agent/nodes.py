"""
LangGraph node implementations — all 6 nodes for the Customer Journey Agent.

Nodes are created via make_nodes() factory which injects db + llm via closure.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Optional

from langchain_core.messages import HumanMessage
from langchain_openai import AzureChatOpenAI, AzureOpenAIEmbeddings
from pydantic import BaseModel, Field

import config
import db.queries as Q
from agent.state import JourneyAgentState
from db.client import SurrealClient


# ── Structured output schemas ─────────────────────────────────────────────

class LifeEventDetection(BaseModel):
    event_type: Optional[str] = Field(
        None,
        description="Detected life event type: child_born, home_purchase, marriage, "
                    "retirement_planning, job_change — or null if none detected",
    )
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    source: str = "chat"


class ProductCandidate(BaseModel):
    product_id: str = Field(description="Short product id, e.g. 'life_insurance'")
    product_name: str
    score: float = Field(ge=0.0, le=1.0)
    rationale: str


class EligibilityOutput(BaseModel):
    detected_life_event: Optional[LifeEventDetection] = None
    candidates: list[ProductCandidate]
    reasoning: str


class ActionOutput(BaseModel):
    action: str = Field(description="recommend | escalate | retain | inform")
    product_id: Optional[str] = None
    product_name: Optional[str] = None
    rationale: str
    confidence: float = Field(ge=0.0, le=1.0)
    channel: str


# ── Node factory ──────────────────────────────────────────────────────────

def make_nodes(
    db: SurrealClient,
    llm: AzureChatOpenAI,
    embeddings: Optional[AzureOpenAIEmbeddings],
) -> dict:
    """
    Create all 6 node functions with db/llm injected via closure.
    Returns a dict mapping node_name -> coroutine/function.
    """

    # ── Node 1: Context Loader (Tool node — SurrealDB graph + vector) ────

    async def context_loader_node(state: JourneyAgentState) -> dict:
        customer_id = state["customer_id"]

        ctx = await Q.get_customer_context(db, customer_id)
        eligible = await Q.get_eligible_products(db, customer_id)
        print(f"[Agent DEBUG] Customer '{customer_id}' context keys: {list(ctx.keys())}")
        print(f"[Agent DEBUG] Eligible products loaded: {len(eligible)} items")
        if eligible:
            print(f"[Agent DEBUG] Eligible sample: {eligible[0]}")
        else:
            # Diagnostic: check if eligible_for table has ANY data
            ef_all = await db.query("SELECT count() FROM eligible_for GROUP ALL")
            ef_raw = await db.query(f"SELECT * FROM eligible_for WHERE in = Customer:{Q._sanitize_id(customer_id)}")
            print(f"[Agent DEBUG] No eligible products for Customer:{customer_id}")
            print(f"[Agent DEBUG] Total eligible_for edges in DB: {ef_all}")
            print(f"[Agent DEBUG] Raw eligible_for query result: {ef_raw}")

        # Vector RAG on the user's message
        user_message = state.get("user_message", "")
        docs: list[dict] = []
        if user_message and embeddings:
            try:
                emb = await embeddings.aembed_query(user_message)
                docs = await Q.vector_search_documents(db, emb)
            except Exception:
                docs = []

        # Flatten profile (strip nested list fields)
        profile = {k: v for k, v in ctx.items() if not isinstance(v, list)}
        journey_states: list = ctx.get("journey_states") or []
        current_phase = (
            journey_states[0].get("phase", "active") if journey_states else "active"
        )

        return {
            "customer_profile": profile,
            "owned_products": ctx.get("owned_products") or [],
            "life_events": ctx.get("life_events") or [],
            "interaction_history": ctx.get("interactions") or [],
            "eligible_products": eligible,
            "relevant_documents": docs,
            "journey_phase": current_phase,
            # Reset per-run state
            "detected_life_events": [],
            "candidates": [],
            "compliance_result": {},
            "selected_action": None,
            "agent_reasoning": "",
            "requires_human_approval": False,
            "response_message": "",
            "langsmith_trace_id": None,
        }

    # ── Node 2: Eligibility Reasoner (LLM) ───────────────────────────────

    async def eligibility_reasoner_node(state: JourneyAgentState) -> dict:
        customer = state["customer_profile"]
        eligible = state["eligible_products"]
        life_events = state["life_events"]
        interactions = state["interaction_history"]
        docs = state["relevant_documents"]
        user_message = state.get("user_message", "")

        doc_snippets = "\n".join(
            f"- [{d.get('title', '')}]: {d.get('content', '')[:200]}"
            for d in docs[:3]
        )

        eligible_summary = []
        for e in eligible[:6]:
            prod = e.get("out") or {}
            if not prod:
                prod = e
            pid = str(prod.get("id", "")).replace("Product:", "") or e.get("product_id", "")
            eligible_summary.append({
                "id": pid,
                "name": prod.get("name", ""),
                "category": prod.get("category", ""),
                "risk_level": prod.get("risk_level", ""),
                "requires_kyc": prod.get("requires_kyc", True),
                "annual_fee": prod.get("annual_fee"),
                "score": e.get("score", 0),
            })

        prompt = f"""You are a financial services AI agent orchestrating a customer journey.

CUSTOMER PROFILE:
{json.dumps({k: v for k, v in customer.items() if k not in ('id', 'created_at')}, default=str, indent=2)}

OWNED PRODUCTS: {[p.get('name', '') for p in state['owned_products']]}
EXISTING LIFE EVENTS: {[e.get('event_type', '') for e in life_events]}

RECENT INTERACTIONS (last 5):
{json.dumps(interactions[-5:], default=str)}

ELIGIBLE PRODUCTS (from knowledge graph):
{json.dumps(eligible_summary, indent=2)}

RELEVANT POLICY DOCS:
{doc_snippets}

CUSTOMER MESSAGE: "{user_message}"

TASK:
1. Detect if the customer message reveals a life event (child_born, home_purchase, marriage, retirement_planning, job_change). Set confidence 0–1.
2. Rank the top 3 product candidates from ELIGIBLE PRODUCTS. Score 0–1 based on fit.
3. Provide brief reasoning.

Return ONLY valid JSON:
{{
  "detected_life_event": {{"event_type": "child_born", "confidence": 0.95, "source": "chat"}} or null,
  "candidates": [
    {{"product_id": "life_insurance", "product_name": "Term Life Insurance", "score": 0.92, "rationale": "..."}}
  ],
  "reasoning": "Brief reasoning..."
}}

Use the short product id (e.g. "life_insurance" not "Product:life_insurance").
Only include products from the ELIGIBLE PRODUCTS list."""

        try:
            response = await llm.ainvoke([HumanMessage(content=prompt)])
            raw = response.content.strip()
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                output = EligibilityOutput(**json.loads(match.group()))
            else:
                print(f"[Agent WARN] Eligibility LLM returned unparseable output: {raw[:200]}")
                output = EligibilityOutput(candidates=[], reasoning="Could not parse LLM output")
        except Exception as e:
            print(f"[Agent ERROR] Eligibility LLM call failed: {e}")
            output = EligibilityOutput(candidates=[], reasoning=f"LLM error: {e}")

        # Fallback: if LLM returned no candidates but we have eligible products,
        # use the DB-scored eligible products directly so the pipeline can still
        # provide useful responses
        if not output.candidates and eligible_summary:
            print(f"[Agent INFO] LLM returned no candidates; using {len(eligible_summary)} DB-scored products as fallback")
            output = EligibilityOutput(
                detected_life_event=output.detected_life_event,
                candidates=[
                    ProductCandidate(
                        product_id=e["id"],
                        product_name=e["name"],
                        score=float(e.get("score", 0.5)),
                        rationale=f"Eligible from knowledge graph: {e.get('category', '')} product",
                    )
                    for e in eligible_summary
                    if e.get("id") and e.get("name")
                ],
                reasoning=output.reasoning or "Using pre-scored eligible products from knowledge graph",
            )

        # Write detected life event to SurrealDB immediately
        new_events: list[dict] = []
        if output.detected_life_event and output.detected_life_event.event_type:
            evt = output.detected_life_event
            ev_id = await Q.write_life_event(
                db,
                state["customer_id"],
                evt.event_type,
                evt.confidence,
                evt.source,
            )
            new_events.append({
                "event_type": evt.event_type,
                "confidence": evt.confidence,
                "id": ev_id,
            })

        # Enrich candidates with full product metadata
        candidates: list[dict] = []
        for c in output.candidates:
            product_data: dict = {}
            for e in eligible:
                prod = e.get("out") or {}
                pid = str(prod.get("id", "")).replace("Product:", "")
                if pid == c.product_id or str(prod.get("id", "")) == c.product_id:
                    product_data = prod
                    break
            candidates.append({
                "product_id": c.product_id,
                "product_name": c.product_name,
                "score": c.score,
                "rationale": c.rationale,
                "product": product_data,
            })

        return {
            "detected_life_events": new_events,
            "candidates": candidates,
            "agent_reasoning": output.reasoning,
        }

    # ── Node 3: Compliance Gate (DETERMINISTIC — NO LLM) ─────────────────

    async def compliance_gate_node(state: JourneyAgentState) -> dict:
        """Pure deterministic rule evaluation — reads live rules from SurrealDB."""
        customer = state["customer_profile"]
        interactions = state["interaction_history"]

        # Load active rules from SurrealDB — supports real-time rule changes via UI
        active_rules = await Q.get_active_compliance_rules(db)
        rules_map = {
            str(r.get("id", "")).split(":")[-1]: r for r in active_rules
        }

        passed: list[dict] = []
        blocked: list[dict] = []
        needs_approval: list[dict] = []

        for candidate in state["candidates"]:
            product = candidate.get("product") or {}
            hard_blocks: list[dict] = []
            approval_reqs: list[dict] = []
            audit_flags: list[dict] = []

            product_id = (
                str(product.get("id", "")).replace("Product:", "")
                or candidate.get("product_id", "")
            )

            # ── KYC Freshness ─────────────────────────────────────────────
            kyc_rule = rules_map.get("kyc_fresh")
            if kyc_rule and product.get("requires_kyc"):
                max_months = int(
                    (kyc_rule.get("conditions") or {}).get(
                        "max_age_months", config.KYC_FRESHNESS_MONTHS
                    )
                )
                kyc_status = customer.get("kyc_status", "pending")
                kyc_verified_at = customer.get("kyc_verified_at")
                kyc_ok = False
                if kyc_status == "verified" and kyc_verified_at:
                    if isinstance(kyc_verified_at, str):
                        try:
                            dt = datetime.fromisoformat(
                                kyc_verified_at.replace("Z", "+00:00")
                            )
                            age_months = (datetime.now(timezone.utc) - dt).days / 30
                            kyc_ok = age_months <= max_months
                        except ValueError:
                            pass
                    else:
                        kyc_ok = True  # datetime object, assume valid
                if not kyc_ok:
                    hard_blocks.append({
                        "rule": "kyc_fresh",
                        "name": "KYC Freshness Check",
                        "enforcement": "hard",
                        "reason": (
                            f"KYC '{kyc_status}' must be verified within {max_months} months"
                        ),
                    })

            # ── Risk Profile Mismatch ──────────────────────────────────────
            risk_rule = rules_map.get("risk_mismatch")
            if risk_rule:
                risk_order = {"low": 0, "medium": 1, "high": 2}
                prod_risk = risk_order.get(str(product.get("risk_level", "low")), 0)
                cust_risk = risk_order.get(
                    str(customer.get("risk_profile", "moderate")), 1
                )
                if prod_risk > cust_risk:
                    approval_reqs.append({
                        "rule": "risk_mismatch",
                        "name": "Risk Profile Mismatch",
                        "enforcement": "human_approval",
                        "reason": (
                            f"Product risk '{product.get('risk_level')}' exceeds "
                            f"customer profile '{customer.get('risk_profile')}'"
                        ),
                    })

            # ── High-Value Cross-Sell Audit ────────────────────────────────
            hv_rule = rules_map.get("high_value_crosssell")
            if hv_rule:
                threshold = float(
                    (hv_rule.get("conditions") or {}).get(
                        "annual_fee_threshold", config.HIGH_VALUE_FEE_THRESHOLD
                    )
                )
                annual_fee = float(product.get("annual_fee") or 0)
                if annual_fee > threshold:
                    audit_flags.append({
                        "rule": "high_value_crosssell",
                        "name": "High-Value Cross-Sell Review",
                        "enforcement": "audit_only",
                        "reason": f"Annual fee ${annual_fee:.0f} > audit threshold ${threshold:.0f}",
                    })

            # ── Cooling-Off Period ─────────────────────────────────────────
            cooling_rule = rules_map.get("cooling_off")
            if cooling_rule:
                cooling_days = int(product.get("cooling_off_days", 0))
                if cooling_days > 0:
                    for interaction in interactions:
                        if interaction.get("interaction_type") == "rejection":
                            created = interaction.get("created_at", "")
                            if isinstance(created, str) and created:
                                try:
                                    dt = datetime.fromisoformat(
                                        created.replace("Z", "+00:00")
                                    )
                                    days_since = (datetime.now(timezone.utc) - dt).days
                                    if days_since < cooling_days:
                                        hard_blocks.append({
                                            "rule": "cooling_off",
                                            "name": "Product Cooling-Off Period",
                                            "enforcement": "hard",
                                            "reason": (
                                                f"{days_since}d since rejection; "
                                                f"cooling-off is {cooling_days}d"
                                            ),
                                        })
                                except ValueError:
                                    pass

            # ── Vulnerable Customer ────────────────────────────────────────
            vuln_rule = rules_map.get("vulnerable_customer")
            if vuln_rule and customer.get("vulnerable_flag"):
                approval_reqs.append({
                    "rule": "vulnerable_customer",
                    "name": "Vulnerable Customer Protection",
                    "enforcement": "human_approval",
                    "reason": "Customer is flagged as vulnerable",
                })

            # ── Graph-stored blocked_by edges ─────────────────────────────
            graph_blocks = await Q.get_compliance_blocks(db, product_id)
            for block in graph_blocks:
                rule_info = block.get("out") or {}
                hard_blocks.append({
                    "rule": str(rule_info.get("id", "")).split(":")[-1],
                    "name": rule_info.get("name", "Graph rule"),
                    "enforcement": "hard",
                    "reason": block.get("block_reason", "Blocked by compliance rule"),
                })

            # ── Verdict ───────────────────────────────────────────────────
            if hard_blocks:
                blocked.append({
                    **candidate,
                    "block_reasons": hard_blocks,
                    "audit_flags": audit_flags,
                })
            elif approval_reqs:
                needs_approval.append({
                    **candidate,
                    "approval_reasons": approval_reqs,
                    "audit_flags": audit_flags,
                })
            else:
                passed.append({**candidate, "audit_flags": audit_flags})

        requires_approval = bool(needs_approval) and not passed

        return {
            "compliance_result": {
                "passed": passed,
                "blocked": blocked,
                "needs_approval": needs_approval,
            },
            "requires_human_approval": requires_approval,
        }

    # ── Node 4: Action Selector (LLM + structured output) ────────────────

    async def action_selector_node(state: JourneyAgentState) -> dict:
        passed = state["compliance_result"].get("passed", [])
        customer = state["customer_profile"]
        user_message = state.get("user_message", "")
        journey_phase = state.get("journey_phase", "active")
        channel = customer.get("channel_preference", "app")

        if not passed:
            return {
                "selected_action": None,
                "response_message": (
                    "I don't have any suitable product recommendations right now. "
                    "Let me connect you with your advisor for personalised guidance."
                ),
                "channel": channel,
            }

        candidate_summary = [
            {
                "product_id": c["product_id"],
                "product_name": c["product_name"],
                "score": c["score"],
                "rationale": c["rationale"],
            }
            for c in passed[:3]
        ]

        prompt = f"""You are selecting the single best next action for a financial services customer.

CUSTOMER: {customer.get('name')}, {customer.get('age')}y, segment={customer.get('segment')}, risk={customer.get('risk_profile')}
JOURNEY PHASE: {journey_phase}
CUSTOMER MESSAGE: "{user_message}"

COMPLIANT CANDIDATES:
{json.dumps(candidate_summary, indent=2)}

PREVIOUS REASONING: {state['agent_reasoning']}

Select the SINGLE best action. Return ONLY valid JSON:
{{
  "action": "recommend",
  "product_id": "life_insurance",
  "product_name": "Term Life Insurance",
  "rationale": "Detailed rationale for this recommendation",
  "confidence": 0.87,
  "channel": "{channel}"
}}

action must be one of: recommend, escalate, retain, inform
If confidence < {config.CONFIDENCE_THRESHOLD}, set action to "escalate"."""

        try:
            response = await llm.ainvoke([HumanMessage(content=prompt)])
            raw = response.content.strip()
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                action = ActionOutput(**json.loads(match.group()))
            else:
                print(f"[Agent WARN] Action LLM returned unparseable output: {raw[:200]}")
                action = ActionOutput(
                    action="recommend",
                    product_id=passed[0]["product_id"],
                    product_name=passed[0]["product_name"],
                    rationale=passed[0].get("rationale", "Best match from eligible products"),
                    confidence=float(passed[0].get("score", 0.6)),
                    channel=channel,
                )
        except Exception as e:
            print(f"[Agent ERROR] Action LLM call failed: {e}")
            # Fallback: recommend the top-scored passed candidate directly
            action = ActionOutput(
                action="recommend",
                product_id=passed[0]["product_id"],
                product_name=passed[0]["product_name"],
                rationale=passed[0].get("rationale", f"Top eligible product (LLM unavailable: {e})"),
                confidence=float(passed[0].get("score", 0.5)),
                channel=channel,
            )

        # Determine next journey phase
        phase_transitions = {
            "onboarding": "active",
            "active": "cross_sell",
            "cross_sell": "cross_sell",
            "retention": "retention",
            "churned": "retention",
        }
        new_phase = phase_transitions.get(journey_phase, journey_phase)

        return {
            "selected_action": action.model_dump(),
            "agent_reasoning": (
                state["agent_reasoning"]
                + f"\nSelected: {action.action} — {action.rationale}"
            ),
            "journey_phase": new_phase,
            "channel": action.channel,
        }

    # ── Node 5: Channel Router (Deterministic) ────────────────────────────

    def channel_router_node(state: JourneyAgentState) -> dict:
        action = state.get("selected_action")
        customer = state["customer_profile"]
        channel = state.get("channel") or customer.get("channel_preference", "app")
        compliance_result = state.get("compliance_result", {})
        blocked = compliance_result.get("blocked", [])
        needs_approval_list = compliance_result.get("needs_approval", [])

        name = customer.get("name", "Valued Customer")
        first_name = name.split()[0]

        if state.get("requires_human_approval"):
            product_name = (
                needs_approval_list[0].get("product_name", "a product")
                if needs_approval_list
                else "a product"
            )
            msg = (
                f"I'd love to recommend our **{product_name}** for you, "
                "but this requires a quick review by your advisor first. "
                "I've flagged this for them and you'll hear back very soon."
            )

        elif not action or action.get("action") == "escalate":
            # Provide more context about why we're escalating
            escalate_reason = ""
            if blocked:
                block_names = [
                    (b.get("block_reasons") or [{}])[0].get("name", "")
                    for b in blocked
                ]
                block_names = [n for n in block_names if n]
                if block_names:
                    escalate_reason = (
                        f" Some products require attention: **{', '.join(block_names)}**."
                    )
            msg = (
                f"Based on your profile, I'd like to connect you with one of our advisors "
                f"who can provide personalised guidance.{escalate_reason} "
                "Shall I arrange a callback?"
            )

        elif action.get("action") == "retain":
            msg = (
                f"As a valued customer of {customer.get('segment', 'our')} banking, "
                f"we have a special offer for you: {action.get('rationale', '')} "
                "Would you like to learn more?"
            )

        elif blocked and not compliance_result.get("passed"):
            block_reason = (
                (blocked[0].get("block_reasons") or [{}])[0].get(
                    "name", "a compliance requirement"
                )
            )
            alt_rationale = action.get("rationale", "") if action else ""
            msg = (
                f"I'm sorry, I can't offer that product right now due to "
                f"**{block_reason}**. "
                + (f"{alt_rationale} " if alt_rationale else "")
                + "Would you like more information or to explore alternatives?"
            )

        else:
            product_name = action.get("product_name", "a suitable product")
            rationale = action.get("rationale", "")
            if channel == "email":
                msg = (
                    f"Dear {name},\n\n"
                    f"Based on your profile, we recommend our **{product_name}**.\n\n"
                    f"{rationale}\n\n"
                    "Please let us know if you'd like to proceed.\n\n"
                    "Kind regards,\nYour Banking Team"
                )
            elif channel == "sms":
                msg = (
                    f"Hi {first_name}! We recommend {product_name}. "
                    f"{rationale[:100]}... Reply YES to learn more."
                )
            else:  # app / advisor
                msg = (
                    f"Great news, {first_name}! Based on your profile, "
                    f"I recommend our **{product_name}**.\n\n{rationale}\n\n"
                    "Would you like to proceed or find out more?"
                )

            # High-value products → route to advisor
            annual_fee = float(
                (action.get("product") or {}).get("annual_fee") or 0
            ) if isinstance(action.get("product"), dict) else 0
            if annual_fee > config.HIGH_VALUE_FEE_THRESHOLD:
                channel = "advisor"

        return {"channel": channel, "response_message": msg}

    # ── Node 6: Graph Updater (Write-back to SurrealDB) ──────────────────

    async def graph_updater_node(state: JourneyAgentState) -> dict:
        customer_id = state["customer_id"]
        action = state.get("selected_action")
        compliance_result = state.get("compliance_result", {})

        # 1. Write customer's message as Interaction node
        user_msg = state.get("user_message", "")
        if user_msg:
            await Q.write_interaction(
                db,
                customer_id,
                interaction_type="inquiry",
                channel=state.get("channel", "app"),
                content=user_msg,
                sentiment=None,
            )

        # 2. Write agent response as Interaction node
        response_msg = state.get("response_message", "")
        if response_msg:
            itype = (
                "recommendation"
                if action and action.get("action") == "recommend"
                else "engagement"
            )
            await Q.write_interaction(
                db,
                customer_id,
                interaction_type=itype,
                channel=state.get("channel", "app"),
                content=response_msg,
                sentiment="positive" if action else "neutral",
            )

        # 3. Update eligible_for edges for passed candidates
        for candidate in compliance_result.get("passed", []):
            prod_id = candidate.get("product_id", "")
            if prod_id:
                await Q.update_eligible_for(
                    db,
                    customer_id,
                    prod_id,
                    score=float(candidate.get("score", 0.5)),
                    reason=candidate.get("rationale", "Agent recommended"),
                )

        # 4. Build compliance provenance for DecisionLog
        passed_names = [
            c.get("product_name", "") for c in compliance_result.get("passed", [])
        ]
        failed_names = [
            (b.get("block_reasons") or [{}])[0].get("name", "")
            for b in compliance_result.get("blocked", [])
        ]
        graph_nodes_consulted = [
            "Customer", "Product", "ComplianceRule", "JourneyState", "LifeEvent",
        ]

        await Q.write_decision_log(
            db,
            customer_id=customer_id,
            action_taken=action.get("action", "no_action") if action else "blocked",
            agent_reasoning=state.get("agent_reasoning", ""),
            confidence_score=float(action.get("confidence", 0.0)) if action else 0.0,
            compliance_gates_passed=passed_names,
            compliance_gates_failed=failed_names,
            graph_nodes_consulted=graph_nodes_consulted,
            langsmith_trace_id=state.get("langsmith_trace_id"),
            requires_human_review=state.get("requires_human_approval", False),
        )

        # 5. Update JourneyState phase + step
        await Q.update_journey_state(
            db,
            customer_id=customer_id,
            phase=state.get("journey_phase", "active"),
            current_step=action.get("action", "completed") if action else "blocked",
            pending_approval=state.get("requires_human_approval", False),
        )

        # 6. Create ApprovalRequests for items needing advisor sign-off
        if state.get("requires_human_approval"):
            for cand in compliance_result.get("needs_approval", []):
                await Q.create_approval_request(
                    db,
                    customer_id=customer_id,
                    proposed_action=f"Recommend {cand.get('product_name', 'product')}",
                    agent_rationale=cand.get("rationale", ""),
                    risk_factors=[
                        r.get("reason", "")
                        for r in cand.get("approval_reasons", [])
                    ],
                )

        return {}

    return {
        "context_loader": context_loader_node,
        "eligibility_reasoner": eligibility_reasoner_node,
        "compliance_gate": compliance_gate_node,
        "action_selector": action_selector_node,
        "channel_router": channel_router_node,
        "graph_updater": graph_updater_node,
    }
