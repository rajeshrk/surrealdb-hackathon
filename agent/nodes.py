"""
LangGraph node implementations — all 6 nodes for the Customer Journey Agent.

Nodes are created via make_nodes() factory which injects db + llm via closure.

Multi-turn conversation principles:
  1. State stores full chat history (messages) + intent + previously_recommended
  2. Chat history is passed into every LLM prompt for continuity
  3. Channel router uses LLM to generate contextual, conversational responses
  4. Previously recommended products are deprioritized / not repeated
  5. Assistant messages are appended to state after every turn
  6. Conversation intent is classified to drive different behavior
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


# ── Helpers ───────────────────────────────────────────────────────────────

def _format_chat_history(messages: list[dict], exclude_current: str = "") -> str:
    """Format chat messages into a readable conversation transcript."""
    lines = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        content = m.get("content", "")
        if content == exclude_current:
            continue
        role = m.get("role", "user").upper()
        lines.append(f"{role}: {content}")
    # Keep last 10 messages (5 exchanges) for context window efficiency
    return "\n".join(lines[-10:])


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
        print(f"[Agent DEBUG] Customer '{customer_id}' eligible products: {len(eligible)}")
        if not eligible:
            ef_all = await db.query("SELECT count() FROM eligible_for GROUP ALL")
            print(f"[Agent DEBUG] Total eligible_for edges in DB: {ef_all}")

        # ── Multi-hop graph traversals ─────────────────────────
        # These use SurrealDB v2 ->edge->Node.field syntax for deep context
        interaction_product_trail = await Q.get_customer_interaction_product_trail(db, customer_id)
        journey_decision_trail = await Q.get_customer_journey_decision_trail(db, customer_id)
        fraud_signals = await Q.get_fraud_signals(db, customer_id)
        print(f"[Agent DEBUG] Multi-hop: {len(interaction_product_trail)} interaction trails, "
              f"{len(journey_decision_trail)} journey decisions, {len(fraud_signals)} fraud signals")

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
        current_phase = "active"
        if journey_states:
            js = journey_states[0]
            if isinstance(js, dict):
                current_phase = js.get("phase", "active")

        # Classify conversation intent from the message + history
        intent = await _classify_intent(
            llm, user_message, state.get("messages", []),
            state.get("previously_recommended", []),
        )
        print(f"[Agent DEBUG] Conversation intent: {intent}")

        return {
            "customer_profile": profile,
            "owned_products": ctx.get("owned_products") or [],
            "life_events": ctx.get("life_events") or [],
            "interaction_history": ctx.get("interactions") or [],
            "eligible_products": eligible,
            "relevant_documents": docs,
            "journey_phase": current_phase,
            "conversation_intent": intent,
            # Multi-hop graph context
            "interaction_product_trail": interaction_product_trail,
            "journey_decision_trail": journey_decision_trail,
            "fraud_signals": fraud_signals,
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

    async def _classify_intent(
        llm: AzureChatOpenAI,
        user_message: str,
        messages: list[dict],
        previously_recommended: list[str],
    ) -> str:
        """Classify conversation intent to drive different agent behavior."""
        history = _format_chat_history(messages, exclude_current=user_message)
        prev_products = ", ".join(previously_recommended) if previously_recommended else "none"

        prompt = f"""Classify the customer's intent. Return ONLY one of these labels:
- greeting: Hello, hi, good morning, etc.
- product_inquiry: Asking about a specific product or product category
- follow_up: Asking more about something already discussed (e.g., "tell me more", "what are the fees?")
- life_event: Sharing a life event (baby, marriage, new home, retirement, job change)
- general_question: General financial question not about a specific product
- objection: Expressing concern, hesitation, or declining a recommendation
- comparison: Asking to compare products or alternatives

{"CONVERSATION HISTORY:" if history else ""}
{history}

PRODUCTS ALREADY RECOMMENDED: {prev_products}
CURRENT MESSAGE: "{user_message}"

Return ONLY the label, nothing else."""

        try:
            response = await llm.ainvoke([HumanMessage(content=prompt)])
            intent = response.content.strip().lower().replace('"', '').replace("'", "")
            valid_intents = {
                "greeting", "product_inquiry", "follow_up", "life_event",
                "general_question", "objection", "comparison",
            }
            if intent in valid_intents:
                return intent
        except Exception as e:
            print(f"[Agent WARN] Intent classification failed: {e}")
        return "general_question"

    # ── Node 2: Eligibility Reasoner (LLM) ───────────────────────────────

    async def eligibility_reasoner_node(state: JourneyAgentState) -> dict:
        customer = state["customer_profile"]
        eligible = state["eligible_products"]
        life_events = state["life_events"]
        interactions = state["interaction_history"]
        docs = state["relevant_documents"]
        user_message = state.get("user_message", "")
        intent = state.get("conversation_intent", "general_question")
        previously_recommended = state.get("previously_recommended", [])

        # For follow-up/greeting intents, skip heavy eligibility reasoning
        # and reuse prior context — the channel router will handle the response
        if intent in ("follow_up", "greeting"):
            return {
                "detected_life_events": [],
                "candidates": [],
                "agent_reasoning": f"Intent '{intent}' — skipping product eligibility, using conversation context.",
            }

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

        # Build chat history for LLM context
        history_text = _format_chat_history(
            state.get("messages", []), exclude_current=user_message
        )
        prev_products_note = ""
        if previously_recommended:
            prev_products_note = (
                f"\nPREVIOUSLY RECOMMENDED (do NOT re-recommend these): "
                f"{', '.join(previously_recommended)}"
            )

        # ── Multi-hop graph context for richer reasoning ──────────
        interaction_trail = state.get("interaction_product_trail", [])
        trail_summary = ""
        if interaction_trail:
            trail_lines = [
                f"- {t.get('interaction_type','?')}: {t.get('product_name','?')} ({t.get('sentiment','?')} sentiment)"
                for t in interaction_trail[:5]
            ]
            trail_summary = "\nPRODUCT INTERACTION HISTORY (graph: Customer→Interaction→Product):\n" + "\n".join(trail_lines)

        past_decisions = state.get("journey_decision_trail", [])
        decision_summary = ""
        if past_decisions:
            dec_lines = [
                f"- {d.get('action_taken','?')}: {', '.join(d.get('gates_passed') or [])} (confidence: {d.get('confidence', 0):.0%})"
                for d in past_decisions[:3]
            ]
            decision_summary = "\nPAST AGENT DECISIONS (graph: Journey→DecisionLog):\n" + "\n".join(dec_lines)

        fraud_signals = state.get("fraud_signals", [])
        fraud_note = ""
        if fraud_signals:
            fraud_lines = [f"- {f.get('signal_type','?')}: {f.get('description','')}" for f in fraud_signals[:3]]
            fraud_note = "\n⚠ FRAUD SIGNALS DETECTED (graph: Customer→Device→IP traversal):\n" + "\n".join(fraud_lines)

        prompt = f"""You are a financial services AI agent orchestrating a customer journey.

CUSTOMER PROFILE:
{json.dumps({k: v for k, v in customer.items() if k not in ('id', 'created_at')}, default=str, indent=2)}

OWNED PRODUCTS: {[p.get('name', '') for p in state['owned_products']]}
EXISTING LIFE EVENTS: {[e.get('event_type', '') for e in life_events]}

RECENT INTERACTIONS (last 5):
{json.dumps(interactions[-5:], default=str)}

ELIGIBLE PRODUCTS (from knowledge graph):
{json.dumps(eligible_summary, indent=2)}
{trail_summary}
{decision_summary}
{fraud_note}

RELEVANT POLICY DOCS:
{doc_snippets}
{prev_products_note}

{"CONVERSATION HISTORY:" if history_text else ""}
{history_text}

CUSTOMER MESSAGE: "{user_message}"
DETECTED INTENT: {intent}

TASK:
1. Detect if the customer message reveals a life event (child_born, home_purchase, marriage, retirement_planning, job_change). Set confidence 0–1.
2. Rank the top 3 product candidates from ELIGIBLE PRODUCTS. Score 0–1 based on fit to the customer's message and situation.
3. If a product was previously recommended, deprioritize it — suggest alternatives instead.
4. If fraud signals are present, note this in your reasoning — it may affect product suitability.
5. Provide brief reasoning.

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
        # use the DB-scored eligible products directly
        if not output.candidates and eligible_summary:
            print(f"[Agent INFO] LLM returned no candidates; using {len(eligible_summary)} DB-scored products as fallback")
            # Filter out previously recommended
            fallback_products = [
                e for e in eligible_summary
                if e.get("id") and e.get("name") and e["id"] not in previously_recommended
            ]
            if not fallback_products:
                fallback_products = [e for e in eligible_summary if e.get("id") and e.get("name")]
            output = EligibilityOutput(
                detected_life_event=output.detected_life_event,
                candidates=[
                    ProductCandidate(
                        product_id=e["id"],
                        product_name=e["name"],
                        score=float(e.get("score", 0.5)),
                        rationale=f"Eligible from knowledge graph: {e.get('category', '')} product",
                    )
                    for e in fallback_products
                ],
                reasoning=output.reasoning or "Using pre-scored eligible products from knowledge graph",
            )

        # Write detected life event to SurrealDB + find unlocked products
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

            # Multi-hop: LifeEvent → unlocks → Product
            # Graph traversal finds products unlocked by this life event
            unlocked = await Q.get_life_event_product_paths(db, evt.event_type)
            existing_ids = {c.product_id for c in output.candidates}
            for u in unlocked:
                pid = str(u.get("product_id", "")).replace("Product:", "")
                if pid and pid not in existing_ids and pid not in previously_recommended:
                    output.candidates.append(ProductCandidate(
                        product_id=pid,
                        product_name=u.get("product_name", ""),
                        score=float(u.get("relevance", 0.7)),
                        rationale=f"Unlocked by {evt.event_type} life event (graph: LifeEvent→unlocks→Product)",
                    ))
                    existing_ids.add(pid)
            print(f"[Agent DEBUG] Life event '{evt.event_type}' unlocked {len(unlocked)} products via graph traversal")

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

            # ── Graph-Native Fraud Detection ──────────────────────────────
            # Fraud signals from multi-hop graph traversal:
            #   Customer→used_device→Device→device_seen_ip→IPAddress
            #   Customer→linked_identity→Customer (identity rings)
            # Severity drives compliance action:
            #   critical/high → hard block on KYC/high-value products
            #   medium → require human approval
            fraud_signals = state.get("fraud_signals", [])
            for signal in fraud_signals:
                severity = signal.get("severity", "low")
                sig_type = signal.get("signal_type", "")

                if severity in ("critical", "high"):
                    annual_fee = float(product.get("annual_fee") or 0)
                    requires_kyc = product.get("requires_kyc", False)
                    if annual_fee > 0 or requires_kyc:
                        hard_blocks.append({
                            "rule": f"fraud_{sig_type}",
                            "name": f"Fraud: {sig_type.replace('_', ' ').title()}",
                            "enforcement": "hard",
                            "reason": signal.get("description", "Fraud signal detected via graph traversal"),
                        })
                elif severity == "medium":
                    approval_reqs.append({
                        "rule": f"fraud_{sig_type}",
                        "name": f"Fraud Review: {sig_type.replace('_', ' ').title()}",
                        "enforcement": "human_approval",
                        "reason": signal.get("description", "Fraud signal requires review"),
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
        intent = state.get("conversation_intent", "general_question")
        previously_recommended = state.get("previously_recommended", [])

        if not passed:
            return {
                "selected_action": None,
                "response_message": "",
                "channel": channel,
            }

        # Build chat history context
        history_text = _format_chat_history(
            state.get("messages", []), exclude_current=user_message
        )
        prev_note = ""
        if previously_recommended:
            prev_note = f"\nALREADY RECOMMENDED THIS SESSION: {', '.join(previously_recommended)} — choose a DIFFERENT product if possible."

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
CONVERSATION INTENT: {intent}
{prev_note}

{"CONVERSATION HISTORY:" if history_text else ""}
{history_text}

CUSTOMER MESSAGE: "{user_message}"

COMPLIANT CANDIDATES:
{json.dumps(candidate_summary, indent=2)}

PREVIOUS REASONING: {state['agent_reasoning']}

Select the SINGLE best action considering the conversation context.
- If the customer is asking a follow-up, set action to "inform" (provide info, don't re-recommend).
- If they seem interested, set action to "recommend".
- If they're objecting or hesitant, set action to "retain".
- If uncertain, set action to "escalate".

Return ONLY valid JSON:
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

    # ── Node 5: Channel Router (LLM-powered conversational response) ─────

    async def channel_router_node(state: JourneyAgentState) -> dict:
        action = state.get("selected_action")
        customer = state["customer_profile"]
        channel = state.get("channel") or customer.get("channel_preference", "app")
        compliance_result = state.get("compliance_result", {})
        blocked = compliance_result.get("blocked", [])
        needs_approval_list = compliance_result.get("needs_approval", [])
        user_message = state.get("user_message", "")
        intent = state.get("conversation_intent", "general_question")

        name = customer.get("name", "Valued Customer")
        first_name = name.split()[0]

        # Build conversation history
        history_text = _format_chat_history(
            state.get("messages", []), exclude_current=user_message
        )

        # Build context about what happened in the pipeline
        context_parts = []
        if action:
            context_parts.append(
                f"SELECTED ACTION: {action.get('action')} — "
                f"Product: {action.get('product_name', 'N/A')} — "
                f"Rationale: {action.get('rationale', '')}"
            )
        if blocked:
            for b in blocked:
                reasons = b.get("block_reasons", [])
                for r in reasons:
                    context_parts.append(
                        f"BLOCKED: {b.get('product_name', '')} — {r.get('name', '')}: {r.get('reason', '')}"
                    )
        if needs_approval_list:
            for n in needs_approval_list:
                context_parts.append(
                    f"NEEDS APPROVAL: {n.get('product_name', '')} — flagged for advisor review"
                )

        detected_events = state.get("detected_life_events", [])
        if detected_events:
            for ev in detected_events:
                context_parts.append(
                    f"LIFE EVENT DETECTED: {ev.get('event_type', '')} (confidence: {ev.get('confidence', 0):.0%})"
                )

        context_summary = "\n".join(context_parts) if context_parts else "No specific action taken."

        previously_recommended = state.get("previously_recommended", [])
        prev_note = ""
        if previously_recommended:
            prev_note = f"\nPRODUCTS ALREADY DISCUSSED: {', '.join(previously_recommended)} — do NOT repeat these recommendations."

        # Generate conversational response via LLM
        response_prompt = f"""You are a friendly, professional banking assistant chatting with {first_name}.
Your tone should be warm, helpful, and conversational. You are NOT a generic chatbot — you have
real knowledge about the customer and their financial situation.

CUSTOMER: {first_name}, {customer.get('segment', '')} segment, age {customer.get('age', '')}, risk profile: {customer.get('risk_profile', '')}
OWNED PRODUCTS: {[p.get('name', '') for p in state.get('owned_products', [])]}
CHANNEL: {channel}
CONVERSATION INTENT: {intent}
{prev_note}

{"CONVERSATION HISTORY:" if history_text else ""}
{history_text}

CURRENT MESSAGE: "{user_message}"

AGENT DECISION:
{context_summary}

AGENT REASONING: {state.get('agent_reasoning', '')}

INSTRUCTIONS BY INTENT:
- greeting: Warmly greet {first_name}, mention you're aware of their profile, ask how you can help.
- product_inquiry: Answer their question about the product. Include specific details like fees, benefits, eligibility.
- follow_up: Provide MORE DETAILS about the product already discussed. Don't just repeat the recommendation — go deeper (fees, benefits, how to apply, timeline).
- life_event: Acknowledge the life event warmly, then naturally suggest how your products can help.
- general_question: Answer their question helpfully using your knowledge of their profile and products.
- objection: Address their concern directly and empathetically. Offer alternatives or more information.
- comparison: Compare the relevant products objectively, highlighting pros/cons for their specific situation.

GENERAL RULES:
- Respond naturally to what the customer said — this is a conversation, not a product pitch.
- Keep responses concise and focused on the customer's needs and concerns.
- Use markdown **bold** for product names.
- NEVER repeat the same recommendation verbatim from conversation history.
- Be specific — reference the customer's actual situation, age, products, etc.
- End with a relevant question or call to action."""

        try:
            response = await llm.ainvoke([HumanMessage(content=response_prompt)])
            msg = response.content.strip()
        except Exception as e:
            print(f"[Agent WARN] Channel router LLM failed: {e}")
            # Fallback to template-based response
            if state.get("requires_human_approval"):
                product_name = (
                    needs_approval_list[0].get("product_name", "a product")
                    if needs_approval_list else "a product"
                )
                msg = (
                    f"I'd love to recommend our **{product_name}** for you, "
                    "but this requires a quick review by your advisor first. "
                    "I've flagged this for them and you'll hear back very soon."
                )
            elif not action or action.get("action") == "escalate":
                msg = (
                    f"Based on your profile, I'd like to connect you with one of our advisors "
                    "who can provide personalised guidance. Shall I arrange a callback?"
                )
            elif action:
                product_name = action.get("product_name", "a suitable product")
                rationale = action.get("rationale", "")
                msg = (
                    f"Great news, {first_name}! Based on your profile, "
                    f"I recommend our **{product_name}**.\n\n{rationale}\n\n"
                    "Would you like to proceed or find out more?"
                )
            else:
                msg = "I'm here to help! Could you tell me more about what you're looking for?"

        # High-value products → route to advisor
        if action and isinstance(action.get("product"), dict):
            annual_fee = float((action["product"]).get("annual_fee") or 0)
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
        if state.get("fraud_signals"):
            graph_nodes_consulted.extend(["Device", "IPAddress", "FraudAlert"])

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

        # 7. Append assistant message to chat history + track recommended products
        new_messages = [{"role": "assistant", "content": state.get("response_message", "")}]

        # Track which products have been recommended this session
        newly_recommended = []
        if action and action.get("action") == "recommend" and action.get("product_id"):
            pid = action["product_id"]
            if pid not in state.get("previously_recommended", []):
                newly_recommended.append(pid)

        result: dict = {"messages": new_messages}
        if newly_recommended:
            result["previously_recommended"] = (
                state.get("previously_recommended", []) + newly_recommended
            )
        return result

    return {
        "context_loader": context_loader_node,
        "eligibility_reasoner": eligibility_reasoner_node,
        "compliance_gate": compliance_gate_node,
        "action_selector": action_selector_node,
        "channel_router": channel_router_node,
        "graph_updater": graph_updater_node,
    }