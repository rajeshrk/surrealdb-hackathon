-- ============================================================
-- Seed Data — Three Demo Personas + Compliance Rules + Docs
-- Run AFTER schema.surql
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

-- ── Persona 1: Sarah Chen — New-to-Bank Millennial ───────────

CREATE Customer:sarah SET
    name = 'Sarah Chen',
    age = 29,
    risk_profile = 'moderate',
    kyc_status = 'verified',
    kyc_verified_at = time::now() - 2mo,
    segment = 'retail',
    channel_preference = 'app',
    vulnerable_flag = false;

RELATE Customer:sarah->owns->Product:checking SET since = time::now() - 6mo, status = 'active';
RELATE Customer:sarah->eligible_for->Product:savings_plus SET score = 0.85, reason = 'Good savings pattern';
RELATE Customer:sarah->eligible_for->Product:mortgage SET score = 0.70, reason = 'Stable income, no existing mortgage';

CREATE JourneyState:sarah_journey SET
    customer_id = 'Customer:sarah',
    phase = 'active',
    current_step = 'idle',
    pending_approval = false,
    last_agent_run = time::now() - 7d,
    updated_at = time::now() - 7d;

RELATE Customer:sarah->has_journey->JourneyState:sarah_journey;

-- ── Persona 2: James Morrison — Existing Mortgage / Wealth ──

CREATE Customer:james SET
    name = 'James Morrison',
    age = 42,
    risk_profile = 'moderate',
    kyc_status = 'expired',
    kyc_verified_at = time::now() - 14mo,
    segment = 'wealth',
    channel_preference = 'advisor',
    vulnerable_flag = false;

RELATE Customer:james->owns->Product:premium_mortgage SET since = time::now() - 3y, status = 'active';
RELATE Customer:james->eligible_for->Product:home_insurance SET score = 0.90, reason = 'Mortgage holder without home insurance';
RELATE Customer:james->eligible_for->Product:investment_portfolio SET score = 0.60, reason = 'Wealth segment, but KYC expired';

CREATE JourneyState:james_journey SET
    customer_id = 'Customer:james',
    phase = 'cross_sell',
    current_step = 'idle',
    pending_approval = false,
    last_agent_run = time::now() - 3d,
    updated_at = time::now() - 3d;

RELATE Customer:james->has_journey->JourneyState:james_journey;

-- ── Persona 3: Maria Santos — At-Risk Retention ──────────────

CREATE Customer:maria SET
    name = 'Maria Santos',
    age = 55,
    risk_profile = 'conservative',
    kyc_status = 'verified',
    kyc_verified_at = time::now() - 3mo,
    segment = 'retail',
    channel_preference = 'email',
    vulnerable_flag = false;

RELATE Customer:maria->owns->Product:basic_savings SET since = time::now() - 8y, status = 'active';
RELATE Customer:maria->eligible_for->Product:cd_account SET score = 0.75, reason = 'Long-standing savings customer, CD would improve yield';
RELATE Customer:maria->eligible_for->Product:retirement_plan SET score = 0.80, reason = 'Age and conservative profile match retirement planning';

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

RELATE Customer:maria->had_interaction->Interaction:maria_i1;
RELATE Customer:maria->had_interaction->Interaction:maria_i2;

CREATE JourneyState:maria_journey SET
    customer_id = 'Customer:maria',
    phase = 'retention',
    current_step = 'idle',
    pending_approval = false,
    last_agent_run = time::now() - 14d,
    updated_at = time::now() - 14d;

RELATE Customer:maria->has_journey->JourneyState:maria_journey;

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
