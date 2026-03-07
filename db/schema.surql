-- ============================================================
-- Adaptive Customer Journey Orchestration Agent — Schema
-- SurrealDB SCHEMAFULL graph + relational + vector
-- ============================================================

-- ── Node Tables ─────────────────────────────────────────────

DEFINE TABLE Customer SCHEMAFULL;
DEFINE FIELD name               ON Customer TYPE string;
DEFINE FIELD age                ON Customer TYPE int;
DEFINE FIELD risk_profile       ON Customer TYPE string; -- conservative, moderate, aggressive
DEFINE FIELD kyc_status         ON Customer TYPE string; -- pending, verified, expired
DEFINE FIELD kyc_verified_at    ON Customer TYPE option<datetime>;
DEFINE FIELD segment            ON Customer TYPE string; -- retail, wealth, private
DEFINE FIELD channel_preference ON Customer TYPE string; -- email, sms, app, advisor
DEFINE FIELD vulnerable_flag    ON Customer TYPE bool DEFAULT false;
DEFINE FIELD created_at         ON Customer TYPE datetime DEFAULT time::now();

DEFINE TABLE Product SCHEMAFULL;
DEFINE FIELD name             ON Product TYPE string;
DEFINE FIELD category         ON Product TYPE string; -- savings, mortgage, insurance, investment, credit
DEFINE FIELD risk_level       ON Product TYPE string; -- low, medium, high
DEFINE FIELD min_age          ON Product TYPE option<int>;
DEFINE FIELD requires_kyc     ON Product TYPE bool DEFAULT true;
DEFINE FIELD annual_fee       ON Product TYPE option<float>;
DEFINE FIELD cooling_off_days ON Product TYPE int DEFAULT 0;

DEFINE TABLE LifeEvent SCHEMAFULL;
DEFINE FIELD event_type  ON LifeEvent TYPE string; -- child_born, home_purchase, marriage, retirement_planning, job_change
DEFINE FIELD detected_at ON LifeEvent TYPE datetime DEFAULT time::now();
DEFINE FIELD confidence  ON LifeEvent TYPE float;  -- 0.0–1.0
DEFINE FIELD source      ON LifeEvent TYPE string; -- chat, transaction_pattern, explicit

DEFINE TABLE ComplianceRule SCHEMAFULL;
DEFINE FIELD name        ON ComplianceRule TYPE string;
DEFINE FIELD description ON ComplianceRule TYPE string;
DEFINE FIELD enforcement ON ComplianceRule TYPE string; -- hard, human_approval, audit_only
DEFINE FIELD active      ON ComplianceRule TYPE bool DEFAULT true;
DEFINE FIELD conditions  ON ComplianceRule TYPE object;
DEFINE FIELD created_at  ON ComplianceRule TYPE datetime DEFAULT time::now();

DEFINE TABLE Interaction SCHEMAFULL;
DEFINE FIELD interaction_type ON Interaction TYPE string; -- recommendation, engagement, rejection, inquiry
DEFINE FIELD channel          ON Interaction TYPE string;
DEFINE FIELD content          ON Interaction TYPE string;
DEFINE FIELD sentiment        ON Interaction TYPE option<string>; -- positive, neutral, negative
DEFINE FIELD created_at       ON Interaction TYPE datetime DEFAULT time::now();

DEFINE TABLE JourneyState SCHEMAFULL;
DEFINE FIELD customer_id      ON JourneyState TYPE string;
DEFINE FIELD phase            ON JourneyState TYPE string; -- onboarding, active, cross_sell, retention, churned
DEFINE FIELD current_step     ON JourneyState TYPE string;
DEFINE FIELD checkpoint_data  ON JourneyState TYPE option<string>; -- serialized LangGraph state
DEFINE FIELD pending_approval ON JourneyState TYPE bool DEFAULT false;
DEFINE FIELD last_agent_run   ON JourneyState TYPE datetime DEFAULT time::now();
DEFINE FIELD updated_at       ON JourneyState TYPE datetime DEFAULT time::now();

DEFINE TABLE DecisionLog SCHEMAFULL;
DEFINE FIELD customer_id              ON DecisionLog TYPE string;
DEFINE FIELD action_taken             ON DecisionLog TYPE string;
DEFINE FIELD agent_reasoning          ON DecisionLog TYPE string;
DEFINE FIELD confidence_score         ON DecisionLog TYPE float;
DEFINE FIELD compliance_gates_passed  ON DecisionLog TYPE array;
DEFINE FIELD compliance_gates_failed  ON DecisionLog TYPE array;
DEFINE FIELD graph_nodes_consulted    ON DecisionLog TYPE array;
DEFINE FIELD langsmith_trace_id       ON DecisionLog TYPE option<string>;
DEFINE FIELD requires_human_review    ON DecisionLog TYPE bool DEFAULT false;
DEFINE FIELD reviewed                 ON DecisionLog TYPE bool DEFAULT false;
DEFINE FIELD created_at               ON DecisionLog TYPE datetime DEFAULT time::now();

DEFINE TABLE ApprovalRequest SCHEMAFULL;
DEFINE FIELD customer_id      ON ApprovalRequest TYPE string;
DEFINE FIELD proposed_action  ON ApprovalRequest TYPE string;
DEFINE FIELD agent_rationale  ON ApprovalRequest TYPE string;
DEFINE FIELD risk_factors     ON ApprovalRequest TYPE array;
DEFINE FIELD status           ON ApprovalRequest TYPE string DEFAULT 'pending'; -- pending, approved, rejected, modified
DEFINE FIELD advisor_response ON ApprovalRequest TYPE option<string>;
DEFINE FIELD created_at       ON ApprovalRequest TYPE datetime DEFAULT time::now();
DEFINE FIELD resolved_at      ON ApprovalRequest TYPE option<datetime>;

-- ── Edge (Relationship) Tables ──────────────────────────────

DEFINE TABLE owns SCHEMAFULL;           -- Customer -> Product
DEFINE FIELD in     ON owns TYPE record<Customer>;
DEFINE FIELD out    ON owns TYPE record<Product>;
DEFINE FIELD since  ON owns TYPE datetime DEFAULT time::now();
DEFINE FIELD status ON owns TYPE string DEFAULT 'active'; -- active, closed, suspended

DEFINE TABLE eligible_for SCHEMAFULL;  -- Customer -> Product
DEFINE FIELD in           ON eligible_for TYPE record<Customer>;
DEFINE FIELD out          ON eligible_for TYPE record<Product>;
DEFINE FIELD score        ON eligible_for TYPE float;
DEFINE FIELD reason       ON eligible_for TYPE string;
DEFINE FIELD evaluated_at ON eligible_for TYPE datetime DEFAULT time::now();

DEFINE TABLE blocked_by SCHEMAFULL;    -- Product -> ComplianceRule
DEFINE FIELD in              ON blocked_by TYPE record<Product>;
DEFINE FIELD out             ON blocked_by TYPE record<ComplianceRule>;
DEFINE FIELD block_reason    ON blocked_by TYPE string;
DEFINE FIELD blocking_since  ON blocked_by TYPE datetime DEFAULT time::now();

DEFINE TABLE requires_approval SCHEMAFULL;  -- Product -> ComplianceRule
DEFINE FIELD in              ON requires_approval TYPE record<Product>;
DEFINE FIELD out             ON requires_approval TYPE record<ComplianceRule>;
DEFINE FIELD approval_reason ON requires_approval TYPE string;

DEFINE TABLE triggered SCHEMAFULL;     -- Customer -> LifeEvent
DEFINE FIELD in           ON triggered TYPE record<Customer>;
DEFINE FIELD out          ON triggered TYPE record<LifeEvent>;
DEFINE FIELD detected_via ON triggered TYPE string;

DEFINE TABLE unlocks SCHEMAFULL;       -- LifeEvent -> Product
DEFINE FIELD in        ON unlocks TYPE record<LifeEvent>;
DEFINE FIELD out       ON unlocks TYPE record<Product>;
DEFINE FIELD relevance ON unlocks TYPE float;

DEFINE TABLE had_interaction SCHEMAFULL;  -- Customer -> Interaction
DEFINE FIELD in  ON had_interaction TYPE record<Customer>;
DEFINE FIELD out ON had_interaction TYPE record<Interaction>;

DEFINE TABLE about_product SCHEMAFULL;   -- Interaction -> Product
DEFINE FIELD in  ON about_product TYPE record<Interaction>;
DEFINE FIELD out ON about_product TYPE record<Product>;

DEFINE TABLE has_journey SCHEMAFULL;     -- Customer -> JourneyState
DEFINE FIELD in  ON has_journey TYPE record<Customer>;
DEFINE FIELD out ON has_journey TYPE record<JourneyState>;

DEFINE TABLE has_decision SCHEMAFULL;    -- JourneyState -> DecisionLog
DEFINE FIELD in  ON has_decision TYPE record<JourneyState>;
DEFINE FIELD out ON has_decision TYPE record<DecisionLog>;

DEFINE TABLE waived_by SCHEMAFULL;       -- ComplianceRule -> Product
DEFINE FIELD in  ON waived_by TYPE record<ComplianceRule>;
DEFINE FIELD out ON waived_by TYPE record<Product>;

-- ── Vector Document Table ────────────────────────────────────

DEFINE TABLE document SCHEMAFULL;
DEFINE FIELD title     ON document TYPE string;
DEFINE FIELD content   ON document TYPE string;
DEFINE FIELD doc_type  ON document TYPE string; -- policy, product_brief, advisor_note, regulation
DEFINE FIELD embedding ON document TYPE array<float>;
DEFINE INDEX idx_document_embedding ON document FIELDS embedding MTREE DIMENSION 1536 DIST COSINE;
