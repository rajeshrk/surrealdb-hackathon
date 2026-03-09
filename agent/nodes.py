"""
LangGraph node implementations — all 6 nodes for the Customer Journey Agent.

Nodes are created via make_nodes() factory which injects db + llm via closure.

Performance-optimized: only 2 LLM calls per agent run.
  - LLM Call 1 (eligibility_reasoner): intent + life event + candidate ranking
  - LLM Call 2 (action_selector): action selection + response generation
  - compliance_gate between them is deterministic (no LLM)
  - 2 is the theoretical minimum: compliance gate requires candidates BEFORE,
    and response generation needs filtered candidates AFTER.

LangGraph memory management:
  - messages: Annotated[list, operator.add] — accumulates across turns
  - Checkpointer (MemorySaver/SurrealDB) — persists state between sessions
  - Chat history passed into LLM prompts for multi-turn continuity
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
    intent: str = Field(
        "general_question",
        description="Customer intent: greeting, product_inquiry, follow_up, "
                    "life_event, general_question, objection, comparison",
    )
    detected_life_event: Optional[LifeEventDetection] = None
    candidates: list[ProductCandidate]
    reasoning: str


class ActionResponseOutput(BaseModel):
    """Combined action selection + response generation (single LLM call)."""
    action: str = Field(description="recommend | escalate | retain | inform")
    product_id: Optional[str] = None
    product_name: Optional[str] = None
    rationale: str
    confidence: float = Field(ge=0.0, le=1.0)
    channel: str
    response_message: str = Field(
        description="Warm, conversational response to send to the customer"
    )


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

        # No LLM call here — intent classification is folded into eligibility_reasoner
        # to eliminate a redundant LLM round-trip (~2s saved)

        return {
            "customer_profile": profile,
            "owned_products": ctx.get("owned_products") or [],
            "life_events": ctx.get("life_events") or [],
            "interaction_history": ctx.get("interactions") or [],
            "eligible_products": eligible,
            "relevant_documents": docs,
            "journey_phase": current_phase,
            "conversation_intent": "",  # Set by eligibility_reasoner
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

    # ── Node 2: Eligibility Reasoner (LLM) ───────────────────────────────
    # Intent classification is folded into this node's output to save one LLM call.

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

        # Build chat history for LLM context
        history_text = _format_chat_history(
            state.get("messages", []), exclude_current=user_message
        )

        # ── Multi-hop graph context for richer reasoning ──────────
        interaction_trail = state.get("interaction_product_trail", [])
        trail_summary = ""
        if interaction_trail:
            trail_lines = []
            for t in interaction_trail[:5]:
                itype = t.get('interaction_type', '?')
                # product_name may be a list from graph traversal ->about_product->Product.name
                pname = t.get('product_name', '?')
                if isinstance(pname, list):
                    pname = pname[0] if pname else '?'
                sentiment = t.get('sentiment', '?')
                trail_lines.append(f"- {itype}: {pname} ({sentiment} sentiment)")
            trail_summary = "\nPRODUCT INTERACTION HISTORY (graph: Customer→Interaction→Product):\n" + "\n".join(trail_lines)

        past_decisions = state.get("journey_decision_trail", [])
        decision_summary = ""
        if past_decisions:
            dec_lines = []
            for d in past_decisions[:3]:
                action = d.get('action_taken', '?')
                # gates_passed may be nested lists from graph traversal
                gates = d.get('gates_passed') or []
                if gates and isinstance(gates[0], list):
                    gates = [g for sublist in gates for g in sublist]
                gates_str = ', '.join(str(g) for g in gates) if gates else 'none'
                conf = d.get('confidence', 0)
                if isinstance(conf, list):
                    conf = conf[0] if conf else 0
                dec_lines.append(f"- {action}: {gates_str} (confidence: {conf:.0%})")
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

{"CONVERSATION HISTORY:" if history_text else ""}
{history_text}

CUSTOMER MESSAGE: "{user_message}"

TASK:
1. Classify the customer's intent as ONE of: greeting, product_inquiry, follow_up, life_event, general_question, objection, comparison
2. Detect if the message reveals a life event (child_born, home_purchase, marriage, retirement_planning, job_change). Set confidence 0–1.
3. Rank the top 3 product candidates from ELIGIBLE PRODUCTS most relevant to what the customer is asking about. Score 0–1.
4. If fraud signals are present, note this in your reasoning.
5. Provide brief reasoning.

Return ONLY valid JSON:
{{
  "intent": "product_inquiry",
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

        # Extract intent from LLM output
        intent = output.intent
        valid_intents = {
            "greeting", "product_inquiry", "follow_up", "life_event",
            "general_question", "objection", "comparison",
        }
        if intent not in valid_intents:
            intent = "general_question"
        print(f"[Agent DEBUG] Conversation intent (from eligibility LLM): {intent}")

        # Fallback: if LLM returned no candidates but we have eligible products,
        # use the DB-scored eligible products directly
        if not output.candidates and eligible_summary:
            print(f"[Agent INFO] LLM returned no candidates; using {len(eligible_summary)} DB-scored products as fallback")
            fallback_products = [e for e in eligible_summary if e.get("id") and e.get("name")]
            output = EligibilityOutput(
                intent=intent,
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
                if pid and pid not in existing_ids:
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
            "conversation_intent": intent,
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

    # ── Node 4: Action Selector + Response (MERGED — single LLM call) ────
    #
    # WHY 2 LLM CALLS IS THE MINIMUM:
    # The compliance gate (deterministic) sits between eligibility reasoning
    # and action selection. We need LLM output BEFORE the gate (to rank
    # candidates), and LLM output AFTER the gate (to select from filtered
    # candidates and generate a response). Merging these two into one would
    # skip compliance checks entirely — unacceptable for financial services.
    #
    # Previously 3 LLM calls: intent(2.1s) + eligibility(5.3s) + action(3.9s) = 11.3s
    # Now 2 LLM calls: eligibility+intent(~5s) + action+response(~4s) = ~9s

    async def action_selector_node(state: JourneyAgentState) -> dict:
        """Select action AND generate response in a single LLM call."""
        passed = state["compliance_result"].get("passed", [])
        blocked = state["compliance_result"].get("blocked", [])
        needs_approval_list = state["compliance_result"].get("needs_approval", [])
        customer = state["customer_profile"]
        user_message = state.get("user_message", "")
        journey_phase = state.get("journey_phase", "active")
        channel = customer.get("channel_preference", "app")
        intent = state.get("conversation_intent", "general_question")

        name = customer.get("name", "Valued Customer")
        first_name = name.split()[0]

        if not passed:
            # No candidates — still need a response for blocked/no_candidates path
            # This is handled by channel_router_node (pass-through for no_candidates)
            return {
                "selected_action": None,
                "response_message": "",
                "channel": channel,
            }

        # Build chat history context
        history_text = _format_chat_history(
            state.get("messages", []), exclude_current=user_message
        )

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
        context_parts = []
        for b in blocked:
            for r in b.get("block_reasons", []):
                context_parts.append(f"BLOCKED: {b.get('product_name', '')} — {r.get('reason', '')}")
        for n in needs_approval_list:
            context_parts.append(f"NEEDS APPROVAL: {n.get('product_name', '')}")
        detected_events = state.get("detected_life_events", [])
        for ev in detected_events:
            context_parts.append(f"LIFE EVENT DETECTED: {ev.get('event_type', '')} (confidence: {ev.get('confidence', 0):.0%})")
        context_note = "\n".join(context_parts) if context_parts else ""

        # MERGED PROMPT: action selection + conversational response in ONE call
        prompt = f"""You are a friendly, professional banking assistant for {first_name}.
You must select the best action AND write a warm, conversational response.

CUSTOMER: {first_name}, {customer.get('age')}y, {customer.get('segment', '')} segment, risk={customer.get('risk_profile', '')}
OWNED PRODUCTS: {[p.get('name', '') for p in state.get('owned_products', [])]}
JOURNEY PHASE: {journey_phase}
CONVERSATION INTENT: {intent}

{"CONVERSATION HISTORY:" if history_text else ""}
{history_text}

CUSTOMER MESSAGE: "{user_message}"

COMPLIANT CANDIDATES:
{json.dumps(candidate_summary, indent=2)}

{f"COMPLIANCE NOTES:" + chr(10) + context_note if context_note else ""}

AGENT REASONING: {state['agent_reasoning']}

ACTION RULES:
- greeting → action "inform", no product
- product_inquiry / life_event / comparison → action "recommend" with best-fit product
- follow_up → action "inform" with the product they're asking about
- objection → action "retain", address concern empathetically
- If confidence < {config.CONFIDENCE_THRESHOLD} → action "escalate"

RESPONSE RULES:
- Respond naturally and conversationally to what the customer said.
- Use markdown **bold** for product names.
- Be specific — reference their age, products, situation.
- End with a relevant question or call to action.

Return ONLY valid JSON:
{{
  "action": "recommend",
  "product_id": "life_insurance",
  "product_name": "Term Life Insurance",
  "rationale": "Why this action was chosen",
  "confidence": 0.87,
  "channel": "{channel}",
  "response_message": "Warm, conversational response to the customer..."
}}

action must be one of: recommend, escalate, retain, inform"""

        try:
            response = await llm.ainvoke([HumanMessage(content=prompt)])
            raw = response.content.strip()
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                output = ActionResponseOutput(**json.loads(match.group()))
            else:
                print(f"[Agent WARN] Action+Response LLM unparseable: {raw[:200]}")
                output = ActionResponseOutput(
                    action="recommend",
                    product_id=passed[0]["product_id"],
                    product_name=passed[0]["product_name"],
                    rationale=passed[0].get("rationale", "Best match from eligible products"),
                    confidence=float(passed[0].get("score", 0.6)),
                    channel=channel,
                    response_message=f"Based on your profile, I recommend our **{passed[0]['product_name']}**. Would you like to learn more?",
                )
        except Exception as e:
            print(f"[Agent ERROR] Action+Response LLM failed: {e}")
            output = ActionResponseOutput(
                action="recommend",
                product_id=passed[0]["product_id"],
                product_name=passed[0]["product_name"],
                rationale=passed[0].get("rationale", "Top eligible product"),
                confidence=float(passed[0].get("score", 0.5)),
                channel=channel,
                response_message=f"I'd like to suggest our **{passed[0]['product_name']}** for you. Can I share more details?",
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
            "selected_action": output.model_dump(),
            "agent_reasoning": (
                state["agent_reasoning"]
                + f"\nSelected: {output.action} — {output.rationale}"
            ),
            "journey_phase": new_phase,
            "channel": output.channel,
            "response_message": output.response_message,
        }

    # ── Node 5: Channel Router (lightweight pass-through) ─────────────────
    # Response is already generated by action_selector. This node only
    # handles the no_candidates path (blocked/needs_approval without passed).

    async def channel_router_node(state: JourneyAgentState) -> dict:
        # If action_selector already set the response, pass through
        if state.get("response_message"):
            return {
                "channel": state.get("channel", "app"),
            }

        # Only called on no_candidates path — generate a fallback response
        customer = state["customer_profile"]
        channel = customer.get("channel_preference", "app")
        name = customer.get("name", "Valued Customer")
        first_name = name.split()[0]
        compliance_result = state.get("compliance_result", {})
        needs_approval_list = compliance_result.get("needs_approval", [])

        if state.get("requires_human_approval") and needs_approval_list:
            product_name = needs_approval_list[0].get("product_name", "a product")
            msg = (
                f"I'd love to recommend our **{product_name}** for you, "
                "but this requires a quick review by your advisor first. "
                "I've flagged this for them and you'll hear back very soon."
            )
        else:
            msg = (
                f"Thanks for reaching out, {first_name}! Based on your profile, "
                "I'd like to connect you with one of our advisors who can provide "
                "personalised guidance. Shall I arrange a callback?"
            )

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

        # 7. Append assistant message to chat history (LangGraph memory via operator.add reducer)
        new_messages = [{"role": "assistant", "content": state.get("response_message", "")}]
        return {"messages": new_messages}

    return {
        "context_loader": context_loader_node,
        "eligibility_reasoner": eligibility_reasoner_node,
        "compliance_gate": compliance_gate_node,
        "action_selector": action_selector_node,
        "channel_router": channel_router_node,
        "graph_updater": graph_updater_node,
    }