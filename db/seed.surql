-- ============================================================
-- Seed Data — Three Demo Personas + Full Graph Relationships
-- Run AFTER schema.surql
--
-- Graph paths seeded:
--   Customer->owns->Product->blocked_by->ComplianceRule       (ownership + compliance chain)
--   Customer->triggered->LifeEvent->unlocks->Product          (life event triggers product fit)
--   Customer->had_interaction->Interaction->about_product->Product (interaction history per product)
--   Customer->has_journey->JourneyState->has_decision->DecisionLog (journey audit trail)
--   Product->requires_approval->ComplianceRule                (product compliance requirements)
--   ComplianceRule->waived_by->Product                        (compliance waivers)
-- ============================================================

-- ── Products ─────────────────────────────────────────────────

CREATE Product:checking SET name = 'Basic Checking', category = 'savings', risk_level = 'low', requires_kyc = false, cooling_off_days = 0;
CREATE Product:savings_plus SET name = 'High-Yield Savings', category = 'savings', risk_level = 'low', requires_kyc = false, cooling_off_days = 7;
CREATE Product:mortgage SET name = 'Home Mortgage', category = 'mortgage', risk_level = 'medium', requires_kyc = true, min_age = 21, cooling_off_days = 14;
CREATE Product:life_insurance SET name = 'Term Life Insurance', category = 'insurance', risk_level = 'low', requires_kyc = true, cooling_off_days = 30;
CREATE Product:edu_savings SET name = 'Education Savings Plan', category = 'savings', risk_level = 'low', requires_kyc = false, cooling_off_days = 0;
CREATE Product:premium_mortgage SET name = 'Premium Fixed Mortgage', category = 'mortgage', risk_level = 'medium', requires_kyc = true, min_age = 21, cooling_off_days = 14;
CREATE Product:home_insurance SET name = 'Home Insurance Plus', category = 'insurance', risk_level = 'low', requires_kyc = false, cooling_off_days = 0;
CREATE Product:investment_portfolio SET name = 'Managed Investment Portfolio', category = 'investment', risk_level = 'high', requires_kyc = true, annual_fee = 750.0, cooling_off_days = 30;
CREATE Product:basic_savings SET name = 'Basic Savings', category = 'savings', risk_level = 'low', requires_kyc = false, cooling_off_days = 0;
CREATE Product:cd_account SET name = '12-Month CD', category = 'savings', risk_level = 'low', requires_kyc = false, cooling_off_days = 7;
CREATE Product:retirement_plan SET name = 'Retirement Planning Service', category = 'investment', risk_level = 'medium', requires_kyc = true, annual_fee = 300.0, cooling_off_days = 14;
CREATE Product:wealth_management SET name = 'Wealth Management Suite', category = 'investment', risk_level = 'high', requires_kyc = true, annual_fee = 1200.0, cooling_off_days = 30;

-- ── Compliance Rules ──────────────────────────────────────────

CREATE ComplianceRule:kyc_fresh SET
    name = 'KYC Freshness Check',
    description = 'Customer KYC must be verified within the last 12 months for any product requiring KYC',
    enforcement = 'hard',
    active = true,
    conditions = { max_age_months: 12, applies_to: 'kyc_required_products' };

CREATE ComplianceRule:risk_mismatch SET
    name = 'Risk Profile Mismatch',
    description = 'Products with risk level exceeding customer risk profile require advisor approval',
    enforcement = 'human_approval',
    active = true,
    conditions = { check: 'product.risk_level > customer.risk_profile' };

CREATE ComplianceRule:high_value_crosssell SET
    name = 'High-Value Cross-Sell Review',
    description = 'Cross-sell recommendations for products with annual fees over $500 are logged for audit',
    enforcement = 'audit_only',
    active = true,
    conditions = { annual_fee_threshold: 500 };

CREATE ComplianceRule:cooling_off SET
    name = 'Product Cooling-Off Period',
    description = 'Cannot re-offer a product within its cooling-off period after customer rejection',
    enforcement = 'hard',
    active = true,
    conditions = { check: 'days_since_rejection < product.cooling_off_days' };

CREATE ComplianceRule:vulnerable_customer SET
    name = 'Vulnerable Customer Protection',
    description = 'Customers flagged as vulnerable require human approval for all product recommendations',
    enforcement = 'human_approval',
    active = true,
    conditions = { check: 'customer.vulnerable_flag == true' };

-- ── Product -> ComplianceRule edges (blocked_by, requires_approval, waived_by) ──

-- High-risk products are blocked by KYC rule
RELATE Product:investment_portfolio->blocked_by->ComplianceRule:kyc_fresh SET block_reason = 'Investment products require current KYC verification';
RELATE Product:wealth_management->blocked_by->ComplianceRule:kyc_fresh SET block_reason = 'Wealth management requires current KYC verification';

-- High-risk products require risk mismatch approval
RELATE Product:investment_portfolio->requires_approval->ComplianceRule:risk_mismatch SET approval_reason = 'High-risk investment requires risk profile review';
RELATE Product:wealth_management->requires_approval->ComplianceRule:risk_mismatch SET approval_reason = 'High-risk wealth products require risk profile review';
RELATE Product:mortgage->requires_approval->ComplianceRule:risk_mismatch SET approval_reason = 'Medium-risk mortgage requires risk profile review for conservative customers';

-- Waiver: basic savings/checking are waived from KYC
RELATE ComplianceRule:kyc_fresh->waived_by->Product:checking SET reason = 'Basic checking exempt from KYC requirements';
RELATE ComplianceRule:kyc_fresh->waived_by->Product:basic_savings SET reason = 'Basic savings exempt from KYC requirements';
RELATE ComplianceRule:kyc_fresh->waived_by->Product:edu_savings SET reason = 'Education savings basic tier exempt from KYC';

-- ── Life Events ─────────────────────────────────────────────────

CREATE LifeEvent:child_born_template SET
    event_type = 'child_born',
    detected_at = time::now() - 1y,
    confidence = 1.0,
    source = 'template';

CREATE LifeEvent:home_purchase_template SET
    event_type = 'home_purchase',
    detected_at = time::now() - 1y,
    confidence = 1.0,
    source = 'template';

CREATE LifeEvent:marriage_template SET
    event_type = 'marriage',
    detected_at = time::now() - 1y,
    confidence = 1.0,
    source = 'template';

CREATE LifeEvent:retirement_planning_template SET
    event_type = 'retirement_planning',
    detected_at = time::now() - 1y,
    confidence = 1.0,
    source = 'template';

CREATE LifeEvent:job_change_template SET
    event_type = 'job_change',
    detected_at = time::now() - 1y,
    confidence = 1.0,
    source = 'template';

-- ── LifeEvent -> unlocks -> Product (what each life event makes relevant) ──

-- child_born -> unlocks education savings, life insurance
RELATE LifeEvent:child_born_template->unlocks->Product:edu_savings SET relevance = 0.95;
RELATE LifeEvent:child_born_template->unlocks->Product:life_insurance SET relevance = 0.90;
RELATE LifeEvent:child_born_template->unlocks->Product:savings_plus SET relevance = 0.60;

-- home_purchase -> unlocks mortgage, home insurance
RELATE LifeEvent:home_purchase_template->unlocks->Product:mortgage SET relevance = 0.95;
RELATE LifeEvent:home_purchase_template->unlocks->Product:home_insurance SET relevance = 0.90;
RELATE LifeEvent:home_purchase_template->unlocks->Product:premium_mortgage SET relevance = 0.85;

-- marriage -> unlocks life insurance, savings, mortgage
RELATE LifeEvent:marriage_template->unlocks->Product:life_insurance SET relevance = 0.85;
RELATE LifeEvent:marriage_template->unlocks->Product:savings_plus SET relevance = 0.70;
RELATE LifeEvent:marriage_template->unlocks->Product:mortgage SET relevance = 0.75;

-- retirement_planning -> unlocks retirement plan, CD, wealth management
RELATE LifeEvent:retirement_planning_template->unlocks->Product:retirement_plan SET relevance = 0.95;
RELATE LifeEvent:retirement_planning_template->unlocks->Product:cd_account SET relevance = 0.80;
RELATE LifeEvent:retirement_planning_template->unlocks->Product:wealth_management SET relevance = 0.70;

-- job_change -> unlocks savings, checking
RELATE LifeEvent:job_change_template->unlocks->Product:savings_plus SET relevance = 0.75;
RELATE LifeEvent:job_change_template->unlocks->Product:checking SET relevance = 0.60;

-- ══════════════════════════════════════════════════════════════
-- Persona 1: Sarah Chen — New-to-Bank Millennial
-- ══════════════════════════════════════════════════════════════

CREATE Customer:sarah SET
    name = 'Sarah Chen',
    age = 29,
    risk_profile = 'moderate',
    kyc_status = 'verified',
    kyc_verified_at = time::now() - 2mo,
    segment = 'retail',
    channel_preference = 'app',
    vulnerable_flag = false;

-- Customer -> owns -> Product
RELATE Customer:sarah->owns->Product:checking SET since = time::now() - 6mo, status = 'active';

-- Customer -> eligible_for -> Product
RELATE Customer:sarah->eligible_for->Product:savings_plus SET score = 0.85, reason = 'Good savings pattern';
RELATE Customer:sarah->eligible_for->Product:mortgage SET score = 0.70, reason = 'Stable income, no existing mortgage';
RELATE Customer:sarah->eligible_for->Product:life_insurance SET score = 0.65, reason = 'Young professional, moderate risk profile';
RELATE Customer:sarah->eligible_for->Product:edu_savings SET score = 0.50, reason = 'Pre-family planning eligibility';

-- Customer -> has_journey -> JourneyState
CREATE JourneyState:sarah_journey SET
    customer_id = 'Customer:sarah',
    phase = 'active',
    current_step = 'idle',
    pending_approval = false,
    last_agent_run = time::now() - 7d,
    updated_at = time::now() - 7d;

RELATE Customer:sarah->has_journey->JourneyState:sarah_journey;

-- Customer -> had_interaction -> Interaction -> about_product -> Product
CREATE Interaction:sarah_i1 SET
    interaction_type = 'inquiry',
    channel = 'app',
    content = 'Asked about savings account options',
    sentiment = 'positive',
    created_at = time::now() - 14d;

CREATE Interaction:sarah_i2 SET
    interaction_type = 'engagement',
    channel = 'app',
    content = 'Explored mortgage calculator on website',
    sentiment = 'positive',
    created_at = time::now() - 7d;

RELATE Customer:sarah->had_interaction->Interaction:sarah_i1;
RELATE Customer:sarah->had_interaction->Interaction:sarah_i2;
RELATE Interaction:sarah_i1->about_product->Product:savings_plus;
RELATE Interaction:sarah_i2->about_product->Product:mortgage;

-- JourneyState -> has_decision -> DecisionLog (past agent decisions)
CREATE DecisionLog:sarah_d1 SET
    customer_id = 'Customer:sarah',
    action_taken = 'inform',
    agent_reasoning = 'Customer browsing savings options. Provided information about High-Yield Savings.',
    confidence_score = 0.78,
    compliance_gates_passed = ['High-Yield Savings'],
    compliance_gates_failed = [],
    graph_nodes_consulted = ['Customer', 'Product', 'ComplianceRule'],
    requires_human_review = false,
    reviewed = true,
    created_at = time::now() - 14d;

RELATE JourneyState:sarah_journey->has_decision->DecisionLog:sarah_d1;

-- ══════════════════════════════════════════════════════════════
-- Persona 2: James Morrison — Existing Mortgage / Wealth
-- ══════════════════════════════════════════════════════════════

CREATE Customer:james SET
    name = 'James Morrison',
    age = 42,
    risk_profile = 'moderate',
    kyc_status = 'expired',
    kyc_verified_at = time::now() - 14mo,
    segment = 'wealth',
    channel_preference = 'advisor',
    vulnerable_flag = false;

-- Customer -> owns -> Product
RELATE Customer:james->owns->Product:premium_mortgage SET since = time::now() - 3y, status = 'active';

-- Customer -> eligible_for -> Product
RELATE Customer:james->eligible_for->Product:home_insurance SET score = 0.90, reason = 'Mortgage holder without home insurance';
RELATE Customer:james->eligible_for->Product:investment_portfolio SET score = 0.60, reason = 'Wealth segment, but KYC expired';
RELATE Customer:james->eligible_for->Product:wealth_management SET score = 0.55, reason = 'Wealth segment candidate, requires KYC renewal';
RELATE Customer:james->eligible_for->Product:life_insurance SET score = 0.70, reason = 'Mortgage holder should have life coverage';

-- Customer -> has_journey -> JourneyState
CREATE JourneyState:james_journey SET
    customer_id = 'Customer:james',
    phase = 'cross_sell',
    current_step = 'idle',
    pending_approval = false,
    last_agent_run = time::now() - 3d,
    updated_at = time::now() - 3d;

RELATE Customer:james->has_journey->JourneyState:james_journey;

-- Customer -> had_interaction -> Interaction -> about_product -> Product
CREATE Interaction:james_i1 SET
    interaction_type = 'inquiry',
    channel = 'advisor',
    content = 'Discussed investment options with advisor',
    sentiment = 'positive',
    created_at = time::now() - 30d;

CREATE Interaction:james_i2 SET
    interaction_type = 'engagement',
    channel = 'app',
    content = 'Reviewed home insurance quotes online',
    sentiment = 'positive',
    created_at = time::now() - 10d;

RELATE Customer:james->had_interaction->Interaction:james_i1;
RELATE Customer:james->had_interaction->Interaction:james_i2;
RELATE Interaction:james_i1->about_product->Product:investment_portfolio;
RELATE Interaction:james_i2->about_product->Product:home_insurance;

-- JourneyState -> has_decision -> DecisionLog
CREATE DecisionLog:james_d1 SET
    customer_id = 'Customer:james',
    action_taken = 'escalate',
    agent_reasoning = 'Customer interested in investments but KYC expired. Escalated to advisor for KYC renewal.',
    confidence_score = 0.65,
    compliance_gates_passed = ['Home Insurance Plus'],
    compliance_gates_failed = ['KYC Freshness Check'],
    graph_nodes_consulted = ['Customer', 'Product', 'ComplianceRule', 'JourneyState'],
    requires_human_review = true,
    reviewed = false,
    created_at = time::now() - 30d;

RELATE JourneyState:james_journey->has_decision->DecisionLog:james_d1;

-- ══════════════════════════════════════════════════════════════
-- Persona 3: Maria Santos — At-Risk Retention
-- ══════════════════════════════════════════════════════════════

CREATE Customer:maria SET
    name = 'Maria Santos',
    age = 55,
    risk_profile = 'conservative',
    kyc_status = 'verified',
    kyc_verified_at = time::now() - 3mo,
    segment = 'retail',
    channel_preference = 'email',
    vulnerable_flag = false;

-- Customer -> owns -> Product
RELATE Customer:maria->owns->Product:basic_savings SET since = time::now() - 8y, status = 'active';

-- Customer -> eligible_for -> Product
RELATE Customer:maria->eligible_for->Product:cd_account SET score = 0.75, reason = 'Long-standing savings customer, CD would improve yield';
RELATE Customer:maria->eligible_for->Product:retirement_plan SET score = 0.80, reason = 'Age and conservative profile match retirement planning';
RELATE Customer:maria->eligible_for->Product:savings_plus SET score = 0.60, reason = 'Could upgrade from basic savings for better yield';

-- Customer -> triggered -> LifeEvent (Maria is actively thinking about retirement)
CREATE LifeEvent:maria_retirement SET
    event_type = 'retirement_planning',
    detected_at = time::now() - 60d,
    confidence = 0.70,
    source = 'transaction_pattern';

RELATE Customer:maria->triggered->LifeEvent:maria_retirement SET detected_via = 'transaction_pattern';

-- Customer -> has_journey -> JourneyState
CREATE JourneyState:maria_journey SET
    customer_id = 'Customer:maria',
    phase = 'retention',
    current_step = 'idle',
    pending_approval = false,
    last_agent_run = time::now() - 14d,
    updated_at = time::now() - 14d;

RELATE Customer:maria->has_journey->JourneyState:maria_journey;

-- Customer -> had_interaction -> Interaction -> about_product -> Product
CREATE Interaction:maria_i1 SET
    interaction_type = 'inquiry',
    channel = 'email',
    content = 'Asked about closing account',
    sentiment = 'negative',
    created_at = time::now() - 30d;

CREATE Interaction:maria_i2 SET
    interaction_type = 'rejection',
    channel = 'email',
    content = 'Declined CD offer',
    sentiment = 'negative',
    created_at = time::now() - 14d;

CREATE Interaction:maria_i3 SET
    interaction_type = 'inquiry',
    channel = 'email',
    content = 'Asked about retirement savings options',
    sentiment = 'neutral',
    created_at = time::now() - 5d;

RELATE Customer:maria->had_interaction->Interaction:maria_i1;
RELATE Customer:maria->had_interaction->Interaction:maria_i2;
RELATE Customer:maria->had_interaction->Interaction:maria_i3;
RELATE Interaction:maria_i1->about_product->Product:basic_savings;
RELATE Interaction:maria_i2->about_product->Product:cd_account;
RELATE Interaction:maria_i3->about_product->Product:retirement_plan;

-- JourneyState -> has_decision -> DecisionLog
CREATE DecisionLog:maria_d1 SET
    customer_id = 'Customer:maria',
    action_taken = 'recommend',
    agent_reasoning = 'Long-tenure customer showing churn signals. Recommended CD account for better yield.',
    confidence_score = 0.72,
    compliance_gates_passed = ['12-Month CD'],
    compliance_gates_failed = [],
    graph_nodes_consulted = ['Customer', 'Product', 'ComplianceRule', 'JourneyState', 'LifeEvent'],
    requires_human_review = false,
    reviewed = true,
    created_at = time::now() - 14d;

CREATE DecisionLog:maria_d2 SET
    customer_id = 'Customer:maria',
    action_taken = 'retain',
    agent_reasoning = 'Customer declined CD. Detected retirement interest from transaction patterns. Pivoting to retention with retirement planning.',
    confidence_score = 0.68,
    compliance_gates_passed = ['Retirement Planning Service'],
    compliance_gates_failed = [],
    graph_nodes_consulted = ['Customer', 'Product', 'ComplianceRule', 'JourneyState', 'LifeEvent'],
    requires_human_review = false,
    reviewed = false,
    created_at = time::now() - 5d;

RELATE JourneyState:maria_journey->has_decision->DecisionLog:maria_d1;
RELATE JourneyState:maria_journey->has_decision->DecisionLog:maria_d2;

-- ══════════════════════════════════════════════════════════════
-- Fraud Detection — Graph-Native Identity Linkage Seed Data
-- ══════════════════════════════════════════════════════════════

-- ── Devices ────────────────────────────────────────────────────
CREATE Device:dev_sarah_iphone SET
    device_fingerprint = 'fp_a1b2c3d4e5_iphone14',
    device_type = 'mobile', os = 'iOS 17', browser = 'Safari',
    first_seen = time::now() - 6mo, last_seen = time::now() - 1d, risk_score = 0.0;

CREATE Device:dev_sarah_macbook SET
    device_fingerprint = 'fp_f6g7h8i9j0_macbook',
    device_type = 'desktop', os = 'macOS 14', browser = 'Chrome',
    first_seen = time::now() - 5mo, last_seen = time::now() - 3d, risk_score = 0.0;

CREATE Device:dev_james_desktop SET
    device_fingerprint = 'fp_k1l2m3n4o5_win11',
    device_type = 'desktop', os = 'Windows 11', browser = 'Edge',
    first_seen = time::now() - 2y, last_seen = time::now() - 1d, risk_score = 0.1;

CREATE Device:dev_james_tablet SET
    device_fingerprint = 'fp_p6q7r8s9t0_ipad',
    device_type = 'tablet', os = 'iPadOS 17', browser = 'Safari',
    first_seen = time::now() - 1y, last_seen = time::now() - 7d, risk_score = 0.0;

CREATE Device:dev_maria_android SET
    device_fingerprint = 'fp_u1v2w3x4y5_pixel',
    device_type = 'mobile', os = 'Android 14', browser = 'Chrome',
    first_seen = time::now() - 3y, last_seen = time::now() - 2d, risk_score = 0.0;

-- Suspicious shared device (James's desktop also used by unknown pattern)
CREATE Device:dev_suspicious_shared SET
    device_fingerprint = 'fp_z9y8x7w6v5_shared',
    device_type = 'desktop', os = 'Windows 10', browser = 'Firefox',
    first_seen = time::now() - 30d, last_seen = time::now() - 1d, risk_score = 0.75;

-- ── IP Addresses ───────────────────────────────────────────────
CREATE IPAddress:ip_sarah_home SET
    ip = '203.0.113.10', geo_country = 'US', geo_city = 'San Francisco',
    is_vpn = false, is_tor = false, risk_score = 0.0, first_seen = time::now() - 6mo;

CREATE IPAddress:ip_james_home SET
    ip = '198.51.100.22', geo_country = 'US', geo_city = 'New York',
    is_vpn = false, is_tor = false, risk_score = 0.0, first_seen = time::now() - 2y;

CREATE IPAddress:ip_james_office SET
    ip = '198.51.100.50', geo_country = 'US', geo_city = 'New York',
    is_vpn = false, is_tor = false, risk_score = 0.0, first_seen = time::now() - 1y;

CREATE IPAddress:ip_maria_home SET
    ip = '192.0.2.88', geo_country = 'US', geo_city = 'Miami',
    is_vpn = false, is_tor = false, risk_score = 0.0, first_seen = time::now() - 3y;

-- Suspicious: VPN IP used with James's account
CREATE IPAddress:ip_suspicious_vpn SET
    ip = '10.255.0.99', geo_country = 'RO', geo_city = 'Bucharest',
    is_vpn = true, is_tor = false, risk_score = 0.80, first_seen = time::now() - 5d;

-- Suspicious: Tor exit node
CREATE IPAddress:ip_tor_exit SET
    ip = '10.255.0.200', geo_country = 'DE', geo_city = 'Frankfurt',
    is_vpn = false, is_tor = true, risk_score = 0.95, first_seen = time::now() - 2d;

-- ── Customer -> Device edges ───────────────────────────────────
RELATE Customer:sarah->used_device->Device:dev_sarah_iphone SET session_count = 45, last_used = time::now() - 1d;
RELATE Customer:sarah->used_device->Device:dev_sarah_macbook SET session_count = 12, last_used = time::now() - 3d;
RELATE Customer:james->used_device->Device:dev_james_desktop SET session_count = 120, last_used = time::now() - 1d;
RELATE Customer:james->used_device->Device:dev_james_tablet SET session_count = 30, last_used = time::now() - 7d;
RELATE Customer:james->used_device->Device:dev_suspicious_shared SET session_count = 3, last_used = time::now() - 2d;
RELATE Customer:maria->used_device->Device:dev_maria_android SET session_count = 200, last_used = time::now() - 2d;

-- ── Customer -> IPAddress edges ────────────────────────────────
RELATE Customer:sarah->from_ip->IPAddress:ip_sarah_home SET session_count = 50, last_used = time::now() - 1d;
RELATE Customer:james->from_ip->IPAddress:ip_james_home SET session_count = 80, last_used = time::now() - 1d;
RELATE Customer:james->from_ip->IPAddress:ip_james_office SET session_count = 40, last_used = time::now() - 3d;
RELATE Customer:james->from_ip->IPAddress:ip_suspicious_vpn SET session_count = 2, last_used = time::now() - 5d;
RELATE Customer:maria->from_ip->IPAddress:ip_maria_home SET session_count = 190, last_used = time::now() - 2d;

-- ── Device -> IPAddress edges ──────────────────────────────────
RELATE Device:dev_sarah_iphone->device_seen_ip->IPAddress:ip_sarah_home SET times_seen = 45;
RELATE Device:dev_sarah_macbook->device_seen_ip->IPAddress:ip_sarah_home SET times_seen = 12;
RELATE Device:dev_james_desktop->device_seen_ip->IPAddress:ip_james_home SET times_seen = 80;
RELATE Device:dev_james_desktop->device_seen_ip->IPAddress:ip_james_office SET times_seen = 40;
RELATE Device:dev_james_tablet->device_seen_ip->IPAddress:ip_james_home SET times_seen = 30;
RELATE Device:dev_suspicious_shared->device_seen_ip->IPAddress:ip_suspicious_vpn SET times_seen = 3;
RELATE Device:dev_suspicious_shared->device_seen_ip->IPAddress:ip_tor_exit SET times_seen = 1;
RELATE Device:dev_maria_android->device_seen_ip->IPAddress:ip_maria_home SET times_seen = 190;

-- ── Identity Linkage (suspicious: James linked via shared device) ──
-- This creates a graph ring: James -> shared_device -> suspicious_shared -> ip -> VPN
-- A pattern that's nearly impossible to detect in a relational database
RELATE Customer:james->linked_identity->Customer:sarah SET
    link_type = 'shared_ip_range',
    confidence = 0.15,
    link_evidence = 'Same ISP range (198.51.100.x / 203.0.113.x) — likely benign',
    detected_at = time::now() - 60d;

-- ── Fraud Alerts (pre-seeded for demo) ─────────────────────────
CREATE FraudAlert:alert_james_vpn SET
    customer_id = 'Customer:james',
    alert_type = 'vpn_tor',
    severity = 'medium',
    description = 'Customer accessed account from VPN IP (Bucharest, Romania) — unusual for NYC-based customer',
    evidence = { ip: '10.255.0.99', geo: 'Bucharest, RO', is_vpn: true, normal_geo: 'New York, US' },
    status = 'open',
    created_at = time::now() - 5d;

CREATE FraudAlert:alert_james_device SET
    customer_id = 'Customer:james',
    alert_type = 'shared_device',
    severity = 'high',
    description = 'Shared device (dev_suspicious_shared) also seen with Tor exit node — potential account compromise',
    evidence = { device: 'Device:dev_suspicious_shared', tor_ip: '10.255.0.200', sessions: 3 },
    status = 'investigating',
    created_at = time::now() - 2d;

-- ── Seed Documents (embeddings populated at startup) ─────────

CREATE document:policy_kyc SET
    title = 'KYC Policy 2024',
    doc_type = 'policy',
    content = 'All customers must complete Know Your Customer verification before being offered investment, insurance, or mortgage products. KYC verification expires after 12 months and must be renewed. Exceptions may apply for existing product holders upgrading within the same product category.',
    embedding = [];

CREATE document:product_mortgage SET
    title = 'Home Mortgage Product Brief',
    doc_type = 'product_brief',
    content = 'The Home Mortgage product requires verified KYC, minimum age 21, stable income verification, and a credit score assessment. Pre-qualification is available for existing savings customers with 6+ months of deposit history. Fixed and variable rate options available with terms from 15 to 30 years.',
    embedding = [];

CREATE document:regulation_consumer SET
    title = 'Consumer Protection Regulation Summary',
    doc_type = 'regulation',
    content = 'Financial institutions must ensure product recommendations are suitable for the customer risk profile. Conservative-profile customers should not be offered high-risk investment products without explicit advisor consultation. All cross-sell activities must be logged with reasoning for regulatory audit purposes.',
    embedding = [];

CREATE document:product_insurance SET
    title = 'Life Insurance Product Brief',
    doc_type = 'product_brief',
    content = 'Term Life Insurance is suitable for customers with dependents or significant financial obligations. Triggered by life events such as childbirth, marriage, or home purchase. Requires verified KYC. Coverage amounts from $100k to $2M. Premiums are age-dependent and locked at policy inception.',
    embedding = [];

CREATE document:product_edu_savings SET
    title = 'Education Savings Plan Brief',
    doc_type = 'product_brief',
    content = 'The Education Savings Plan helps parents save for their children\'s higher education expenses. Tax-advantaged contributions up to $2,500 per year. Best suited for customers who have recently had children or have children under 10 years old. No KYC requirement for basic tier.',
    embedding = [];

CREATE document:advisor_retention SET
    title = 'Retention Strategy Advisor Note',
    doc_type = 'advisor_note',
    content = 'Customers showing negative sentiment in two or more consecutive interactions within 60 days should be flagged for retention intervention. Effective retention offers include fee waivers, rate upgrades on existing products, or complementary service access. High-tenure customers (5+ years) should receive priority advisor contact.',
    embedding = [];
