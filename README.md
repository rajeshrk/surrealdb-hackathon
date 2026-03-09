# Adaptive Customer Journey Orchestration Agent

**SurrealDB × LangGraph Hackathon Project**

An agentic system that dynamically re-orchestrates a customer's financial services journey in real-time — across onboarding, cross-sell, retention, and compliance — by reasoning over an evolving knowledge graph.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  Layer 1: SurrealDB Cloud v2 (graph + relational + vector)           │
│                                                                      │
│  Node Tables:                                                        │
│    Customer, Product, LifeEvent, ComplianceRule, Interaction,        │
│    JourneyState, DecisionLog, ApprovalRequest,                       │
│    Device, IPAddress, FraudAlert, document                           │
│                                                                      │
│  Graph Edges (TYPE RELATION):                                        │
│    owns, eligible_for, blocked_by, requires_approval, waived_by,     │
│    triggered, unlocks, had_interaction, about_product,               │
│    has_journey, has_decision,                                        │
│    used_device, from_ip, device_seen_ip, linked_identity             │
│                                                                      │
│  Vector: MTREE index (1536-dim cosine) on document.embedding         │
│  Graph Traversal: ->edge->Node.field syntax for multi-hop queries    │
└───────────────────────┬──────────────────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────────────────┐
│  Layer 2: LangGraph StateGraph (6 nodes)                             │
│                                                                      │
│  context_loader ──→ eligibility_reasoner ──→ compliance_gate         │
│  (parallel I/O)     (multi-hop context)      (fraud + rules)         │
│       │                                           │                  │
│       │ (passed)                    (blocked/needs_approval)         │
│       ▼                                           ▼                  │
│  action_selector ──→ channel_router ──→ graph_updater                │
│  (merged LLM call)   (pass-through)    (parallel writes)             │
└───────────────────────┬──────────────────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────────────────┐
│  Layer 3: Streamlit Multi-Page App                                   │
│  • Customer Portal — chat interface + agent invocation               │
│  • Advisor Dashboard — knowledge graph viz + journey timeline        │
│  • Compliance Ops — rule toggles + audit trail + edge management     │
└───────────────────────┬──────────────────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────────────────┐
│  Layer 4: Fraud Detection — Graph-Native Identity Linkage            │
│  • Customer->used_device->Device->device_seen_ip->IPAddress chains   │
│  • Customer->linked_identity->Customer identity rings                │
│  • VPN/Tor detection, shared device clusters, geo anomalies          │
│  • Multi-hop traversals impossible in RDBMS                          │
└───────────────────────┬──────────────────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────────────────┐
│  Layer 5: LangSmith Observability                                    │
│  • Full trace per agent run, stored trace_id in DecisionLog          │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Quick Start

### 1. Start SurrealDB

```bash
docker run --rm -p 8000:8000 surrealdb/surrealdb:latest start \
  --log debug --user root --pass root memory
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env — set OPENAI_API_KEY at minimum
```

### 4. Run the app

```bash
streamlit run app.py
```

The app bootstraps the SurrealDB schema and seeds demo personas on first run.

---

## Demo Personas

| Customer | Age | Segment | KYC | Scenario |
|----------|-----|---------|-----|----------|
| **Sarah Chen** | 29 | Retail | Verified (2mo) | Chat "I just had a baby" → life event detection |
| **James Morrison** | 42 | Wealth | **Expired** (14mo) | Investment blocked by KYC rule |
| **Maria Santos** | 55 | Retail | Verified (3mo) | Retention risk — negative sentiment pattern |

---

## 5-Minute Demo Script

1. **[Customer Portal]** Select Sarah. Chat: *"I just had a baby."*
   - Agent detects `child_born` life event → writes LifeEvent node to SurrealDB
   - Recommends Term Life Insurance + Education Savings Plan
   - Graph evolution counter shows interaction count increase

2. **[Advisor Dashboard]** View Sarah's knowledge graph
   - New LifeEvent node visible with `triggered` edge
   - Journey timeline shows agent reasoning + compliance gates passed
   - LangSmith trace link in each decision entry

3. **[Compliance Ops]** Set KYC Freshness `max_age_months` from 12 → 1
   - SurrealDB `blocked_by` edges created for KYC-required products

4. **[Customer Portal]** Chat as Sarah: *"Tell me about life insurance."*
   - Compliance gate now blocks (KYC 2mo old > 1mo threshold)
   - Agent reroutes to Education Savings (no KYC required)
   - Blocked decision logged with full provenance

5. **[Advisor Dashboard]** See the rerouted journey
   - `blocked_by` edge visible in knowledge graph (red)
   - Compliance gate failure logged in timeline

---

## Key Technical Features

### SurrealDB Cloud v2 — Single Multi-Model Data Layer
- **Graph traversal**: Clean `->edge->Node.field` syntax for multi-hop queries
  - `Customer->had_interaction->Interaction->about_product->Product` (4-node chain)
  - `Customer->has_journey->JourneyState->has_decision->DecisionLog` (audit trail)
- **SCHEMAFULL mode**: All tables use `DEFINE TABLE OVERWRITE ... SCHEMAFULL` with `TYPE RELATION` edges
- **Vector RAG**: `MTREE` index (1536-dim cosine) on document embeddings for policy/product search
- **Idempotent seeding**: `UPSERT` for all node records, `RELATE` with edge existence checks
- **Real-time evolution**: Every agent run writes new Interaction, LifeEvent, DecisionLog nodes

### Graph-Native Fraud Detection
- **Identity rings**: `Customer->linked_identity->Customer` recursive traversal (up to N hops)
- **Shared device clusters**: `Customer->used_device->Device<-used_device<-Customer`
- **Device-IP chains**: `Customer->used_device->Device->device_seen_ip->IPAddress`
- **VPN/Tor detection**: IP risk scoring feeds into compliance gate decisions
- **Multi-hop patterns impossible in RDBMS** — graph structure makes fraud rings visible

### LangGraph Orchestration (Optimized Pipeline)
- **Parallelized I/O**: `asyncio.gather` for concurrent DB queries in context_loader and graph_updater
- **Merged LLM calls**: action_selector generates both action + response in one call
- **Compliance gate is pure Python** — no LLM, reads live rules + fraud signals from SurrealDB
- **Conditional edges** — routes based on compliance outcome (passed/blocked/needs_approval)

### Compliance Tiers
| Tier | Behaviour |
|------|-----------|
| `hard` | Product blocked, logged, alternative suggested |
| `human_approval` | ApprovalRequest created, advisor notified |
| `audit_only` | Allowed through, flagged for regulatory review |

### Fraud Severity Integration
| Fraud Severity | Compliance Action |
|----------------|-------------------|
| `critical` / `high` | Hard block via compliance gate |
| `medium` | Escalated to human approval |
| `low` | Audit-only, logged for review |

---

## Environment Variables

```
SURREALDB_URL=ws://localhost:8000/rpc
SURREALDB_NS=hackathon
SURREALDB_DB=journey_agent
SURREALDB_USER=root
SURREALDB_PASS=root
OPENAI_API_KEY=sk-...
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=ls-...
LANGCHAIN_PROJECT=journey-orchestration-agent
LLM_MODEL=gpt-4o-mini
```

---

## File Structure

```
├── app.py                         # Streamlit entry point + DB bootstrap
├── pages/
│   ├── 1_Customer_Portal.py       # Customer chat + agent invocation
│   ├── 2_Advisor_Dashboard.py     # Graph viz + timeline + approval queue
│   └── 3_Compliance_Ops.py        # Rule management + audit trail
├── agent/
│   ├── state.py                   # JourneyAgentState TypedDict
│   ├── nodes.py                   # 6 node implementations (factory pattern)
│   ├── graph.py                   # StateGraph definition + compile
│   ├── tools.py                   # LangChain tools (graph query, write, vector)
│   └── checkpointer.py            # SurrealDB-backed BaseCheckpointSaver
├── db/
│   ├── schema.surql               # Full SurrealDB schema
│   ├── seed.surql                 # Three demo personas + compliance rules + docs
│   ├── client.py                  # Async SurrealDB client wrapper
│   └── queries.py                 # Named query functions
├── components/
│   ├── graph_viz.py               # streamlit-agraph knowledge graph component
│   ├── timeline.py                # Journey timeline component
│   └── alerts.py                  # Approval queue component
├── config.py                      # Environment variable configuration
├── requirements.txt
└── .env.example
```
