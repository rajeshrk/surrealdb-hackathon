-- ============================================================
-- Adaptive Customer Journey Orchestration Agent — Schema
-- SurrealDB SCHEMAFULL graph + relational + vector
-- ============================================================

-- ── Node Tables ─────────────────────────────────────────────

DEFINE TABLE OVERWRITE Customer SCHEMAFULL;
DEFINE FIELD OVERWRITE name               ON Customer TYPE string;
DEFINE FIELD OVERWRITE age                ON Customer TYPE int;
DEFINE FIELD OVERWRITE risk_profile       ON Customer TYPE string; -- conservative, moderate, aggressive
DEFINE FIELD OVERWRITE kyc_status         ON Customer TYPE string; -- pending, verified, expired
DEFINE FIELD OVERWRITE kyc_verified_at    ON Customer TYPE option<datetime>;
DEFINE FIELD OVERWRITE segment            ON Customer TYPE string; -- retail, wealth, private
DEFINE FIELD OVERWRITE channel_preference ON Customer TYPE string; -- email, sms, app, advisor
DEFINE FIELD OVERWRITE vulnerable_flag    ON Customer TYPE bool DEFAULT false;
DEFINE FIELD OVERWRITE created_at         ON Customer TYPE datetime DEFAULT time::now();

DEFINE TABLE OVERWRITE Product SCHEMAFULL;
DEFINE FIELD OVERWRITE name             ON Product TYPE string;
DEFINE FIELD OVERWRITE category         ON Product TYPE string; -- savings, mortgage, insurance, investment, credit
DEFINE FIELD OVERWRITE risk_level       ON Product TYPE string; -- low, medium, high
DEFINE FIELD OVERWRITE min_age          ON Product TYPE option<int>;
DEFINE FIELD OVERWRITE requires_kyc     ON Product TYPE bool DEFAULT true;
DEFINE FIELD OVERWRITE annual_fee       ON Product TYPE option<float>;
DEFINE FIELD OVERWRITE cooling_off_days ON Product TYPE int DEFAULT 0;

DEFINE TABLE OVERWRITE LifeEvent SCHEMAFULL;
DEFINE FIELD OVERWRITE event_type  ON LifeEvent TYPE string; -- child_born, home_purchase, marriage, retirement_planning, job_change
DEFINE FIELD OVERWRITE detected_at ON LifeEvent TYPE datetime DEFAULT time::now();
DEFINE FIELD OVERWRITE confidence  ON LifeEvent TYPE float;  -- 0.0–1.0
DEFINE FIELD OVERWRITE source      ON LifeEvent TYPE string; -- chat, transaction_pattern, explicit

DEFINE TABLE OVERWRITE ComplianceRule SCHEMAFULL;
DEFINE FIELD OVERWRITE name        ON ComplianceRule TYPE string;
DEFINE FIELD OVERWRITE description ON ComplianceRule TYPE string;
DEFINE FIELD OVERWRITE enforcement ON ComplianceRule TYPE string; -- hard, human_approval, audit_only
DEFINE FIELD OVERWRITE active      ON ComplianceRule TYPE bool DEFAULT true;
DEFINE FIELD OVERWRITE conditions  ON ComplianceRule TYPE object;
DEFINE FIELD OVERWRITE created_at  ON ComplianceRule TYPE datetime DEFAULT time::now();

DEFINE TABLE OVERWRITE Interaction SCHEMAFULL;
DEFINE FIELD OVERWRITE interaction_type ON Interaction TYPE string; -- recommendation, engagement, rejection, inquiry
DEFINE FIELD OVERWRITE channel          ON Interaction TYPE string;
DEFINE FIELD OVERWRITE content          ON Interaction TYPE string;
DEFINE FIELD OVERWRITE sentiment        ON Interaction TYPE option<string>; -- positive, neutral, negative
DEFINE FIELD OVERWRITE created_at       ON Interaction TYPE datetime DEFAULT time::now();

DEFINE TABLE OVERWRITE JourneyState SCHEMAFULL;
DEFINE FIELD OVERWRITE customer_id      ON JourneyState TYPE string;
DEFINE FIELD OVERWRITE phase            ON JourneyState TYPE string; -- onboarding, active, cross_sell, retention, churned
DEFINE FIELD OVERWRITE current_step     ON JourneyState TYPE string;
DEFINE FIELD OVERWRITE checkpoint_data  ON JourneyState TYPE option<string>; -- serialized LangGraph state
DEFINE FIELD OVERWRITE pending_approval ON JourneyState TYPE bool DEFAULT false;
DEFINE FIELD OVERWRITE last_agent_run   ON JourneyState TYPE datetime DEFAULT time::now();
DEFINE FIELD OVERWRITE updated_at       ON JourneyState TYPE datetime DEFAULT time::now();

DEFINE TABLE OVERWRITE DecisionLog SCHEMAFULL;
DEFINE FIELD OVERWRITE customer_id              ON DecisionLog TYPE string;
DEFINE FIELD OVERWRITE action_taken             ON DecisionLog TYPE string;
DEFINE FIELD OVERWRITE agent_reasoning          ON DecisionLog TYPE string;
DEFINE FIELD OVERWRITE confidence_score         ON DecisionLog TYPE float;
DEFINE FIELD OVERWRITE compliance_gates_passed  ON DecisionLog TYPE array;
DEFINE FIELD OVERWRITE compliance_gates_failed  ON DecisionLog TYPE array;
DEFINE FIELD OVERWRITE graph_nodes_consulted    ON DecisionLog TYPE array;
DEFINE FIELD OVERWRITE langsmith_trace_id       ON DecisionLog TYPE option<string>;
DEFINE FIELD OVERWRITE requires_human_review    ON DecisionLog TYPE bool DEFAULT false;
DEFINE FIELD OVERWRITE reviewed                 ON DecisionLog TYPE bool DEFAULT false;
DEFINE FIELD OVERWRITE created_at               ON DecisionLog TYPE datetime DEFAULT time::now();

DEFINE TABLE OVERWRITE ApprovalRequest SCHEMAFULL;
DEFINE FIELD OVERWRITE customer_id      ON ApprovalRequest TYPE string;
DEFINE FIELD OVERWRITE proposed_action  ON ApprovalRequest TYPE string;
DEFINE FIELD OVERWRITE agent_rationale  ON ApprovalRequest TYPE string;
DEFINE FIELD OVERWRITE risk_factors     ON ApprovalRequest TYPE array;
DEFINE FIELD OVERWRITE status           ON ApprovalRequest TYPE string DEFAULT 'pending'; -- pending, approved, rejected, modified
DEFINE FIELD OVERWRITE advisor_response ON ApprovalRequest TYPE option<string>;
DEFINE FIELD OVERWRITE created_at       ON ApprovalRequest TYPE datetime DEFAULT time::now();
DEFINE FIELD OVERWRITE resolved_at      ON ApprovalRequest TYPE option<datetime>;

-- ── Edge (Relationship) Tables ──────────────────────────────
-- TYPE RELATION is required for RELATE statements in SurrealDB v2+

DEFINE TABLE OVERWRITE owns TYPE RELATION SCHEMAFULL;           -- Customer -> Product
DEFINE FIELD OVERWRITE in     ON owns TYPE record<Customer>;
DEFINE FIELD OVERWRITE out    ON owns TYPE record<Product>;
DEFINE FIELD OVERWRITE since  ON owns TYPE datetime DEFAULT time::now();
DEFINE FIELD OVERWRITE status ON owns TYPE string DEFAULT 'active'; -- active, closed, suspended

DEFINE TABLE OVERWRITE eligible_for TYPE RELATION SCHEMAFULL;  -- Customer -> Product
DEFINE FIELD OVERWRITE in           ON eligible_for TYPE record<Customer>;
DEFINE FIELD OVERWRITE out          ON eligible_for TYPE record<Product>;
DEFINE FIELD OVERWRITE score        ON eligible_for TYPE float;
DEFINE FIELD OVERWRITE reason       ON eligible_for TYPE string;
DEFINE FIELD OVERWRITE evaluated_at ON eligible_for TYPE datetime DEFAULT time::now();

DEFINE TABLE OVERWRITE blocked_by TYPE RELATION SCHEMAFULL;    -- Product -> ComplianceRule
DEFINE FIELD OVERWRITE in              ON blocked_by TYPE record<Product>;
DEFINE FIELD OVERWRITE out             ON blocked_by TYPE record<ComplianceRule>;
DEFINE FIELD OVERWRITE block_reason    ON blocked_by TYPE string;
DEFINE FIELD OVERWRITE blocking_since  ON blocked_by TYPE datetime DEFAULT time::now();

DEFINE TABLE OVERWRITE requires_approval TYPE RELATION SCHEMAFULL;  -- Product -> ComplianceRule
DEFINE FIELD OVERWRITE in              ON requires_approval TYPE record<Product>;
DEFINE FIELD OVERWRITE out             ON requires_approval TYPE record<ComplianceRule>;
DEFINE FIELD OVERWRITE approval_reason ON requires_approval TYPE string;

DEFINE TABLE OVERWRITE triggered TYPE RELATION SCHEMAFULL;     -- Customer -> LifeEvent
DEFINE FIELD OVERWRITE in           ON triggered TYPE record<Customer>;
DEFINE FIELD OVERWRITE out          ON triggered TYPE record<LifeEvent>;
DEFINE FIELD OVERWRITE detected_via ON triggered TYPE string;

DEFINE TABLE OVERWRITE unlocks TYPE RELATION SCHEMAFULL;       -- LifeEvent -> Product
DEFINE FIELD OVERWRITE in        ON unlocks TYPE record<LifeEvent>;
DEFINE FIELD OVERWRITE out       ON unlocks TYPE record<Product>;
DEFINE FIELD OVERWRITE relevance ON unlocks TYPE float;

DEFINE TABLE OVERWRITE had_interaction TYPE RELATION SCHEMAFULL;  -- Customer -> Interaction
DEFINE FIELD OVERWRITE in  ON had_interaction TYPE record<Customer>;
DEFINE FIELD OVERWRITE out ON had_interaction TYPE record<Interaction>;

DEFINE TABLE OVERWRITE about_product TYPE RELATION SCHEMAFULL;   -- Interaction -> Product
DEFINE FIELD OVERWRITE in  ON about_product TYPE record<Interaction>;
DEFINE FIELD OVERWRITE out ON about_product TYPE record<Product>;

DEFINE TABLE OVERWRITE has_journey TYPE RELATION SCHEMAFULL;     -- Customer -> JourneyState
DEFINE FIELD OVERWRITE in  ON has_journey TYPE record<Customer>;
DEFINE FIELD OVERWRITE out ON has_journey TYPE record<JourneyState>;

DEFINE TABLE OVERWRITE has_decision TYPE RELATION SCHEMAFULL;    -- JourneyState -> DecisionLog
DEFINE FIELD OVERWRITE in  ON has_decision TYPE record<JourneyState>;
DEFINE FIELD OVERWRITE out ON has_decision TYPE record<DecisionLog>;

DEFINE TABLE OVERWRITE waived_by TYPE RELATION SCHEMAFULL;       -- ComplianceRule -> Product
DEFINE FIELD OVERWRITE in  ON waived_by TYPE record<ComplianceRule>;
DEFINE FIELD OVERWRITE out ON waived_by TYPE record<Product>;

-- ── Vector Document Table ────────────────────────────────────

DEFINE TABLE OVERWRITE document SCHEMAFULL;
DEFINE FIELD OVERWRITE title     ON document TYPE string;
DEFINE FIELD OVERWRITE content   ON document TYPE string;
DEFINE FIELD OVERWRITE doc_type  ON document TYPE string; -- policy, product_brief, advisor_note, regulation
DEFINE FIELD OVERWRITE embedding ON document TYPE array<float>;
DEFINE INDEX OVERWRITE idx_document_embedding ON document FIELDS embedding MTREE DIMENSION 1536 DIST COSINE;
