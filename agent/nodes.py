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
    response_message: str = Field("", description="Customer-facing response message")


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
        import asyncio
        customer_id = state["customer_id"]
        user_message = state.get("user_message", "")

        # ── Parallelize ALL DB queries + intent classification ────────
        # This cuts context_loader from ~2.2s to ~1.2s by running
        # 5 queries + 1 LLM call concurrently instead of sequentially.

        async def _get_ctx():
            return await Q.get_customer_context(db, customer_id)

        async def _get_eligible():
            return await Q.get_eligible_products(db, customer_id)

        async def _get_interaction_trail():
            return await Q.get_customer_interaction_product_trail(db, customer_id)

        async def _get_journey_decisions():
            return await Q.get_customer_journey_decision_trail(db, customer_id)

        async def _get_fraud_signals():
            return await Q.get_fraud_signals(db, customer_id)

        async def _get_docs():
            if user_message and embeddings:
                try:
                    emb = await embeddings.aembed_query(user_message)
                    return await Q.vector_search_documents(db, emb)
                except Exception:
                    pass
            return []

        async def _get_intent():
            return await _classify_intent(
                llm, user_message, state.get("messages", []),
                state.get("previously_recommended", []),
            )

        # Fire all concurrently
        (ctx, eligible, interaction_product_trail, journey_decisions,
         fraud_signals, docs, intent) = await asyncio.gather(
            _get_ctx(), _get_eligible(), _get_interaction_trail(),
            _get_journey_decisions(), _get_fraud_signals(), _get_docs(),
            _get_intent(),
        )

        print(f"[Agent DEBUG] Customer '{customer_id}' eligible: {len(eligible)}, "
              f"trails: {len(interaction_product_trail)}, decisions: {len(journey_decisions)}, "
              f"fraud_signals: {len(fraud_signals)}, intent: {intent}")

        # Flatten profile (strip nested list fields)
        profile = {k: v for k, v in ctx.items() if not isinstance(v, list)}
        journey_states: list = ctx.get("journey_states") or []
        current_phase = (
            journey_states[0].get("phase", "active") if journey_states else "active"
        )

        # Enrich profile with multi-hop graph context
        profile["_interaction_product_trail"] = interaction_product_trail[:10]
        profile["_journey_decisions"] = journey_decisions[:5]
        profile["_fraud_signals"] = fraud_signals

        return {
            "customer_profile": profile,
            "owned_products": ctx.get("owned_products") or [],
            "life_events": ctx.get("life_events") or [],
            "interaction_history": ctx.get("interactions") or [],
            "eligible_products": eligible,
            "relevant_documents": docs,
            "journey_phase": current_phase,
            "conversation_intent": intent,
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

        # ── Multi-hop graph context ─────────────────────────────
        # Interaction → Product trail: what products has this customer discussed?
        interaction_trail = customer.get("_interaction_product_trail", [])
        trail_summary = ""
        if interaction_trail:
            trail_lines = [
                f"- {t.get('interaction_type','?')}: {t.get('product_name','?')} ({t.get('sentiment','?')} sentiment)"
                for t in interaction_trail[:5]
            ]
            trail_summary = "PRODUCT INTERACTION HISTORY (graph: Customer→Interaction→Product):\n" + "\n".join(trail_lines)

        # Journey → Decision trail: what has the agent already tried?
        past_decisions = customer.get("_journey_decisions", [])
        decision_summary = ""
        if past_decisions:
            dec_lines = [
                f"- {d.get('action_taken','?')}: {', '.join(d.get('gates_passed', []))} (confidence: {d.get('confidence', 0):.0%})"
                for d in past_decisions[:3]
            ]
            decision_summary = "PAST AGENT DECISIONS (graph: Journey→Decision):\n" + "\n".join(dec_lines)

        # Fraud signals from graph traversal
        fraud_signals = customer.get("_fraud_signals", [])
        fraud_note = ""
        if fraud_signals:
            fraud_lines = [f"- {f.get('signal_type','?')}: {f.get('description','')}" for f in fraud_signals[:3]]
            fraud_note = "⚠ FRAUD SIGNALS DETECTED:\n" + "\n".join(fraud_lines)

        prompt = f"""You are a financial services AI agent orchestrating a customer journey.

CUSTOMER: {customer.get('name')}, {customer.get('age')}y, segment={customer.get('segment')}, risk={customer.get('risk_profile')}, KYC={customer.get('kyc_status')}
OWNED PRODUCTS: {[p.get('name', '') for p in state['owned_products']]}
LIFE EVENTS: {[e.get('event_type', '') for e in life_events]}

ELIGIBLE PRODUCTS (from knowledge graph):
{json.dumps(eligible_summary, indent=2)}
{trail_summary}
{decision_summary}
{fraud_note}

{doc_snippets}
{prev_products_note}

{"CONVERSATION:" if history_text else ""}
{history_text}

MESSAGE: "{user_message}"
INTENT: {intent}

TASK: 1) Detect life event (child_born|home_purchase|marriage|retirement_planning|job_change) with confidence 0–1.
2) Rank top 3 products from ELIGIBLE PRODUCTS. Score 0–1 based on fit. Use the multi-hop graph context above.
3) If a product was previously recommended, deprioritize it.
4) If fraud signals are present, note this in reasoning.

Return ONLY valid JSON:
{{
  "detected_life_event": {{"event_type": "child_born", "confidence": 0.95, "source": "chat"}} or null,
  "candidates": [
    {{"product_id": "life_insurance", "product_name": "Term Life Insurance", "score": 0.92, "rationale": "..."}}
  ],
  "reasoning": "Brief reasoning..."
}}

Use short product id (e.g. "life_insurance" not "Product:life_insurance").
Only include products from ELIGIBLE PRODUCTS list."""

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
            # Find products unlocked by this life event and add as candidates
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
            # Fraud signals from multi-hop graph traversal feed directly
            # into compliance decisions — blocking or flagging products.
            fraud_signals = customer.get("_fraud_signals", [])
            for signal in fraud_signals:
                severity = signal.get("severity", "low")
                sig_type = signal.get("signal_type", "")

                if severity in ("critical", "high"):
                    # High/critical fraud → hard block all high-value products
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
                    # Medium fraud → require human approval
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
        """Combined action selection + response generation in ONE LLM call.

        This merges what was previously two separate LLM calls (action_selector + channel_router)
        into a single call, saving ~2-3s of latency per turn.
        """
        passed = state["compliance_result"].get("passed", [])
        customer = state["customer_profile"]
        user_message = state.get("user_message", "")
        journey_phase = state.get("journey_phase", "active")
        channel = customer.get("channel_preference", "app")
        intent = state.get("conversation_intent", "general_question")
        previously_recommended = state.get("previously_recommended", [])
        compliance_result = state.get("compliance_result", {})
        blocked = compliance_result.get("blocked", [])
        needs_approval_list = compliance_result.get("needs_approval", [])

        name = customer.get("name", "Valued Customer")
        first_name = name.split()[0]

        if not passed and not blocked and not needs_approval_list:
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
            prev_note = f"\nALREADY RECOMMENDED: {', '.join(previously_recommended)} — choose a DIFFERENT product and do NOT repeat these."

        candidate_summary = [
            {
                "product_id": c["product_id"],
                "product_name": c["product_name"],
                "score": c["score"],
                "rationale": c["rationale"],
            }
            for c in passed[:3]
        ]

        # Build context about blocked/approval items
        compliance_notes = []
        for b in blocked:
            reasons = b.get("block_reasons", [])
            for r in reasons:
                compliance_notes.append(f"BLOCKED: {b.get('product_name', '')} — {r.get('reason', '')}")
        for n in needs_approval_list:
            compliance_notes.append(f"NEEDS APPROVAL: {n.get('product_name', '')} — flagged for advisor review")
        compliance_text = "\n".join(compliance_notes) if compliance_notes else ""

        detected_events = state.get("detected_life_events", [])
        event_notes = ""
        if detected_events:
            event_notes = "LIFE EVENTS DETECTED: " + ", ".join(
                f"{ev.get('event_type', '')} ({ev.get('confidence', 0):.0%})" for ev in detected_events
            )

        # Fraud signals
        fraud_signals = customer.get("_fraud_signals", [])
        fraud_note = ""
        if fraud_signals:
            fraud_note = "⚠ FRAUD SIGNALS: " + "; ".join(f.get("description", "") for f in fraud_signals[:3])

        prompt = f"""You are {first_name}'s banking assistant. Select the best action AND write a conversational response in ONE step.

CUSTOMER: {first_name}, {customer.get('age')}y, {customer.get('segment')} segment, risk={customer.get('risk_profile')}
OWNED: {[p.get('name', '') for p in state.get('owned_products', [])]}
INTENT: {intent} | PHASE: {journey_phase} | CHANNEL: {channel}
{prev_note}
{event_notes}
{fraud_note}

{"CONVERSATION:" if history_text else ""}
{history_text}

MESSAGE: "{user_message}"

COMPLIANT CANDIDATES: {json.dumps(candidate_summary) if candidate_summary else "None"}
{compliance_text}

REASONING SO FAR: {state.get('agent_reasoning', '')}

INSTRUCTIONS:
1. Pick ONE action: recommend (interested) | inform (follow-up/question) | retain (objection) | escalate (uncertain/complex)
2. Write a warm, specific response for {first_name} (2-4 sentences for app/sms). Reference their actual situation.
3. Use **bold** for product names. End with a question or call to action.
4. For follow-ups: go DEEPER (fees, benefits, how to apply) — don't just repeat.
5. For life events: acknowledge warmly, then suggest how products help.
6. If blocked/needs approval: explain transparently what's happening.
7. NEVER repeat the same recommendation from conversation history.

Return ONLY valid JSON:
{{
  "action": "recommend",
  "product_id": "life_insurance",
  "product_name": "Term Life Insurance",
  "rationale": "Why this action",
  "confidence": 0.87,
  "channel": "{channel}",
  "response_message": "The actual message to show the customer"
}}

action: recommend|escalate|retain|inform
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
                    action="recommend" if passed else "escalate",
                    product_id=passed[0]["product_id"] if passed else None,
                    product_name=passed[0]["product_name"] if passed else None,
                    rationale=passed[0].get("rationale", "Best match") if passed else "No candidates",
                    confidence=float(passed[0].get("score", 0.6)) if passed else 0.3,
                    channel=channel,
                )
        except Exception as e:
            print(f"[Agent ERROR] Action LLM call failed: {e}")
            action = ActionOutput(
                action="recommend" if passed else "escalate",
                product_id=passed[0]["product_id"] if passed else None,
                product_name=passed[0]["product_name"] if passed else None,
                rationale=f"Top eligible product (LLM unavailable: {e})" if passed else str(e),
                confidence=float(passed[0].get("score", 0.5)) if passed else 0.3,
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
            "response_message": action.response_message,
        }

    # ── Node 5: Channel Router (lightweight — uses response from action_selector) ─

    async def channel_router_node(state: JourneyAgentState) -> dict:
        """Lightweight channel routing — response already generated by action_selector.

        Only falls back to LLM if action_selector didn't produce a response_message
        (e.g., no_candidates path that skipped action_selector).
        This saves ~2.5s by eliminating a redundant LLM call.
        """
        action = state.get("selected_action")
        customer = state["customer_profile"]
        channel = state.get("channel") or customer.get("channel_preference", "app")
        compliance_result = state.get("compliance_result", {})
        needs_approval_list = compliance_result.get("needs_approval", [])

        name = customer.get("name", "Valued Customer")
        first_name = name.split()[0]

        # If action_selector already generated a response, use it
        existing_msg = state.get("response_message", "")
        if existing_msg:
            # High-value products → route to advisor
            if action and isinstance(action.get("product"), dict):
                annual_fee = float((action["product"]).get("annual_fee") or 0)
                if annual_fee > config.HIGH_VALUE_FEE_THRESHOLD:
                    channel = "advisor"
            return {"channel": channel, "response_message": existing_msg}

        # ── Fallback: no_candidates path — generate response via LLM ──
        user_message = state.get("user_message", "")
        intent = state.get("conversation_intent", "general_question")
        history_text = _format_chat_history(
            state.get("messages", []), exclude_current=user_message
        )

        blocked = compliance_result.get("blocked", [])
        blocked_info = ""
        if blocked:
            blocked_info = "BLOCKED PRODUCTS: " + ", ".join(
                f"{b.get('product_name','')} ({b.get('block_reasons',[{}])[0].get('reason','')})"
                for b in blocked
            )

        prompt = f"""You are {first_name}'s banking assistant. No products passed compliance, so help them with their question directly.

CUSTOMER: {first_name}, {customer.get('age')}y, {customer.get('segment')} segment
INTENT: {intent}
{blocked_info}

{"CONVERSATION:" if history_text else ""}
{history_text}

MESSAGE: "{user_message}"

Write a warm 2-3 sentence response. If products were blocked, explain what they need (e.g. KYC renewal).
If they need an advisor, offer to connect them. Use **bold** for product names. End with a question."""

        try:
            response = await llm.ainvoke([HumanMessage(content=prompt)])
            msg = response.content.strip()
        except Exception as e:
            print(f"[Agent WARN] Channel router LLM failed: {e}")
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
            else:
                msg = (
                    f"Based on your profile, {first_name}, I'd like to connect you with one of our advisors "
                    "who can provide personalised guidance. Shall I arrange a callback?"
                )

        return {"channel": channel, "response_message": msg}

    # ── Node 6: Graph Updater (Write-back to SurrealDB) ──────────────────

    async def graph_updater_node(state: JourneyAgentState) -> dict:
        """Write-back to SurrealDB — all independent writes run in parallel."""
        import asyncio
        customer_id = state["customer_id"]
        action = state.get("selected_action")
        compliance_result = state.get("compliance_result", {})

        # ── Build all write tasks for parallel execution ──────────
        write_tasks = []

        # 1. Write customer's message as Interaction node
        user_msg = state.get("user_message", "")
        if user_msg:
            write_tasks.append(Q.write_interaction(
                db, customer_id,
                interaction_type="inquiry",
                channel=state.get("channel", "app"),
                content=user_msg, sentiment=None,
            ))

        # 2. Write agent response as Interaction node
        response_msg = state.get("response_message", "")
        if response_msg:
            itype = "recommendation" if action and action.get("action") == "recommend" else "engagement"
            write_tasks.append(Q.write_interaction(
                db, customer_id,
                interaction_type=itype,
                channel=state.get("channel", "app"),
                content=response_msg,
                sentiment="positive" if action else "neutral",
            ))

        # 3. Update eligible_for edges for passed candidates
        for candidate in compliance_result.get("passed", []):
            prod_id = candidate.get("product_id", "")
            if prod_id:
                write_tasks.append(Q.update_eligible_for(
                    db, customer_id, prod_id,
                    score=float(candidate.get("score", 0.5)),
                    reason=candidate.get("rationale", "Agent recommended"),
                ))

        # 4. Build compliance provenance for DecisionLog
        passed_names = [c.get("product_name", "") for c in compliance_result.get("passed", [])]
        failed_names = [
            (b.get("block_reasons") or [{}])[0].get("name", "")
            for b in compliance_result.get("blocked", [])
        ]
        write_tasks.append(Q.write_decision_log(
            db, customer_id=customer_id,
            action_taken=action.get("action", "no_action") if action else "blocked",
            agent_reasoning=state.get("agent_reasoning", ""),
            confidence_score=float(action.get("confidence", 0.0)) if action else 0.0,
            compliance_gates_passed=passed_names,
            compliance_gates_failed=failed_names,
            graph_nodes_consulted=["Customer", "Product", "ComplianceRule", "JourneyState", "LifeEvent", "Device"],
            langsmith_trace_id=state.get("langsmith_trace_id"),
            requires_human_review=state.get("requires_human_approval", False),
        ))

        # 5. Update JourneyState phase + step
        write_tasks.append(Q.update_journey_state(
            db, customer_id=customer_id,
            phase=state.get("journey_phase", "active"),
            current_step=action.get("action", "completed") if action else "blocked",
            pending_approval=state.get("requires_human_approval", False),
        ))

        # 6. Create ApprovalRequests for items needing advisor sign-off
        if state.get("requires_human_approval"):
            for cand in compliance_result.get("needs_approval", []):
                write_tasks.append(Q.create_approval_request(
                    db, customer_id=customer_id,
                    proposed_action=f"Recommend {cand.get('product_name', 'product')}",
                    agent_rationale=cand.get("rationale", ""),
                    risk_factors=[r.get("reason", "") for r in cand.get("approval_reasons", [])],
                ))

        # ── Execute all writes in parallel ────────────────────────
        results = await asyncio.gather(*write_tasks, return_exceptions=True)
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                print(f"[Agent WARN] graph_updater write task {i} failed: {r}")

        # 7. Append assistant message to chat history + track recommended products
        new_messages = [{"role": "assistant", "content": state.get("response_message", "")}]

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
