-- DesiFaces Payment Gateway Schema (Strict / Token-Only)
--
-- Purpose:
--   Payment-gateway integration support tables for wallet top-ups,
--   subscriptions, webhook processing, gateway customer mapping,
--   and token-only saved payment methods.
--
-- Security boundary:
--   This schema intentionally DOES NOT store raw card details.
--   Never store PAN/card number, CVV/CVC, track data, PIN, or raw
--   gateway payloads containing sensitive authentication data.
--
-- Notes:
--   1) This file is idempotent where practical.
--   2) Foreign keys to users / ledger tables are intentionally omitted
--      because DesiFaces service/database ownership may vary by env.
--   3) Requires PostgreSQL with pgcrypto available for gen_random_uuid().

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- updated_at trigger helper
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------------
-- 1) Gateway customer mapping
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payment_gateway_customers (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL UNIQUE,
  gateway_provider TEXT NOT NULL DEFAULT 'stripe',
  gateway_customer_id TEXT NOT NULL UNIQUE,
  email TEXT,
  is_default BOOLEAN NOT NULL DEFAULT TRUE,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT payment_gateway_customers_provider_ck
    CHECK (gateway_provider IN ('stripe', 'apple_iap', 'google_play', 'other'))
);

CREATE INDEX IF NOT EXISTS ix_payment_gateway_customers_provider
  ON payment_gateway_customers (gateway_provider);

DROP TRIGGER IF EXISTS trg_payment_gateway_customers_updated_at ON payment_gateway_customers;
CREATE TRIGGER trg_payment_gateway_customers_updated_at
BEFORE UPDATE ON payment_gateway_customers
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- 2) Token-only payment methods
--
-- Stores only gateway token/reference IDs and non-sensitive display metadata.
-- NEVER store full card number, CVV, track, PIN, or raw secret payloads here.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payment_gateway_payment_methods (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL,
  gateway_provider TEXT NOT NULL DEFAULT 'stripe',
  gateway_customer_id TEXT NOT NULL,
  gateway_payment_method_id TEXT NOT NULL UNIQUE,
  method_type TEXT NOT NULL DEFAULT 'card',
  status TEXT NOT NULL DEFAULT 'active',
  brand TEXT,
  last4 TEXT,
  exp_month INTEGER,
  exp_year INTEGER,
  funding_type TEXT,
  country_code TEXT,
  wallet_type TEXT,
  network TEXT,
  billing_name TEXT,
  billing_email TEXT,
  billing_country TEXT,
  billing_postal_code TEXT,
  is_default BOOLEAN NOT NULL DEFAULT FALSE,
  fingerprint_hash TEXT,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ,
  CONSTRAINT payment_gateway_payment_methods_provider_ck
    CHECK (gateway_provider IN ('stripe', 'apple_iap', 'google_play', 'other')),
  CONSTRAINT payment_gateway_payment_methods_method_type_ck
    CHECK (method_type IN ('card', 'bank_account', 'wallet', 'other')),
  CONSTRAINT payment_gateway_payment_methods_status_ck
    CHECK (status IN ('active', 'inactive', 'detached', 'expired')),
  CONSTRAINT payment_gateway_payment_methods_last4_ck
    CHECK (last4 IS NULL OR last4 ~ '^[0-9]{4}$'),
  CONSTRAINT payment_gateway_payment_methods_exp_month_ck
    CHECK (exp_month IS NULL OR exp_month BETWEEN 1 AND 12),
  CONSTRAINT payment_gateway_payment_methods_exp_year_ck
    CHECK (exp_year IS NULL OR exp_year BETWEEN 2000 AND 9999)
);

CREATE INDEX IF NOT EXISTS ix_payment_gateway_payment_methods_user_id
  ON payment_gateway_payment_methods (user_id);

CREATE INDEX IF NOT EXISTS ix_payment_gateway_payment_methods_customer_id
  ON payment_gateway_payment_methods (gateway_customer_id);

CREATE INDEX IF NOT EXISTS ix_payment_gateway_payment_methods_default
  ON payment_gateway_payment_methods (user_id, is_default)
  WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS ix_payment_gateway_payment_methods_active
  ON payment_gateway_payment_methods (user_id, status)
  WHERE deleted_at IS NULL;

DROP TRIGGER IF EXISTS trg_payment_gateway_payment_methods_updated_at ON payment_gateway_payment_methods;
CREATE TRIGGER trg_payment_gateway_payment_methods_updated_at
BEFORE UPDATE ON payment_gateway_payment_methods
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- 3) Checkout sessions
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payment_gateway_checkout_sessions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL,
  gateway_provider TEXT NOT NULL DEFAULT 'stripe',
  gateway_checkout_session_id TEXT NOT NULL UNIQUE,
  gateway_customer_id TEXT,
  mode TEXT NOT NULL,
  purpose TEXT NOT NULL,
  local_order_id UUID,
  local_subscription_id UUID,
  currency TEXT NOT NULL,
  amount_minor BIGINT,
  status TEXT NOT NULL DEFAULT 'created',
  success_url TEXT,
  cancel_url TEXT,
  expires_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  idempotency_key TEXT,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT payment_gateway_checkout_sessions_provider_ck
    CHECK (gateway_provider IN ('stripe', 'apple_iap', 'google_play', 'other')),
  CONSTRAINT payment_gateway_checkout_sessions_mode_ck
    CHECK (mode IN ('payment', 'subscription', 'setup')),
  CONSTRAINT payment_gateway_checkout_sessions_purpose_ck
    CHECK (
      purpose IN (
        'wallet_topup',
        'plan_subscription',
        'plan_upgrade',
        'plan_downgrade',
        'invoice_pay',
        'saved_payment_method'
      )
    ),
  CONSTRAINT payment_gateway_checkout_sessions_status_ck
    CHECK (status IN ('created', 'open', 'complete', 'expired', 'failed', 'canceled'))
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_payment_gateway_checkout_sessions_idempotency_key
  ON payment_gateway_checkout_sessions (idempotency_key)
  WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_payment_gateway_checkout_sessions_user_id
  ON payment_gateway_checkout_sessions (user_id);

CREATE INDEX IF NOT EXISTS ix_payment_gateway_checkout_sessions_status
  ON payment_gateway_checkout_sessions (status);

CREATE INDEX IF NOT EXISTS ix_payment_gateway_checkout_sessions_customer_id
  ON payment_gateway_checkout_sessions (gateway_customer_id);

DROP TRIGGER IF EXISTS trg_payment_gateway_checkout_sessions_updated_at ON payment_gateway_checkout_sessions;
CREATE TRIGGER trg_payment_gateway_checkout_sessions_updated_at
BEFORE UPDATE ON payment_gateway_checkout_sessions
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- 4) Payment intents / custom payment attempts
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payment_gateway_payment_intents (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL,
  gateway_provider TEXT NOT NULL DEFAULT 'stripe',
  gateway_payment_intent_id TEXT NOT NULL UNIQUE,
  gateway_customer_id TEXT,
  gateway_payment_method_id TEXT,
  purpose TEXT NOT NULL,
  local_order_id UUID,
  currency TEXT NOT NULL,
  amount_minor BIGINT NOT NULL,
  status TEXT NOT NULL DEFAULT 'requires_payment_method',
  confirmed_at TIMESTAMPTZ,
  failed_at TIMESTAMPTZ,
  canceled_at TIMESTAMPTZ,
  idempotency_key TEXT,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT payment_gateway_payment_intents_provider_ck
    CHECK (gateway_provider IN ('stripe', 'apple_iap', 'google_play', 'other')),
  CONSTRAINT payment_gateway_payment_intents_purpose_ck
    CHECK (purpose IN ('wallet_topup', 'invoice_pay', 'manual_charge')),
  CONSTRAINT payment_gateway_payment_intents_status_ck
    CHECK (
      status IN (
        'requires_payment_method',
        'requires_confirmation',
        'requires_action',
        'processing',
        'succeeded',
        'canceled',
        'failed'
      )
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_payment_gateway_payment_intents_idempotency_key
  ON payment_gateway_payment_intents (idempotency_key)
  WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_payment_gateway_payment_intents_user_id
  ON payment_gateway_payment_intents (user_id);

CREATE INDEX IF NOT EXISTS ix_payment_gateway_payment_intents_status
  ON payment_gateway_payment_intents (status);

DROP TRIGGER IF EXISTS trg_payment_gateway_payment_intents_updated_at ON payment_gateway_payment_intents;
CREATE TRIGGER trg_payment_gateway_payment_intents_updated_at
BEFORE UPDATE ON payment_gateway_payment_intents
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- 5) Wallet top-up / credit-purchase orders
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payment_wallet_orders (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL,
  order_type TEXT NOT NULL DEFAULT 'topup',
  currency TEXT NOT NULL,
  amount_minor BIGINT NOT NULL,
  credits_to_grant NUMERIC(18,4) NOT NULL,
  gateway_provider TEXT,
  gateway_checkout_session_id TEXT,
  gateway_payment_intent_id TEXT,
  gateway_charge_id TEXT,
  gateway_customer_id TEXT,
  gateway_payment_method_id TEXT,
  payment_state TEXT NOT NULL DEFAULT 'pending',
  fulfillment_state TEXT NOT NULL DEFAULT 'pending',
  ledger_entry_id UUID,
  fulfilled_at TIMESTAMPTZ,
  reversed_at TIMESTAMPTZ,
  idempotency_key TEXT NOT NULL UNIQUE,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT payment_wallet_orders_order_type_ck
    CHECK (order_type IN ('topup', 'admin_adjustment', 'refund_reversal')),
  CONSTRAINT payment_wallet_orders_provider_ck
    CHECK (gateway_provider IS NULL OR gateway_provider IN ('stripe', 'apple_iap', 'google_play', 'other')),
  CONSTRAINT payment_wallet_orders_payment_state_ck
    CHECK (
      payment_state IN (
        'pending',
        'requires_action',
        'succeeded',
        'failed',
        'refunded',
        'canceled'
      )
    ),
  CONSTRAINT payment_wallet_orders_fulfillment_state_ck
    CHECK (
      fulfillment_state IN (
        'pending',
        'granted',
        'grant_failed',
        'reversed'
      )
    )
);

CREATE INDEX IF NOT EXISTS ix_payment_wallet_orders_user_id
  ON payment_wallet_orders (user_id);

CREATE INDEX IF NOT EXISTS ix_payment_wallet_orders_payment_state
  ON payment_wallet_orders (payment_state);

CREATE INDEX IF NOT EXISTS ix_payment_wallet_orders_fulfillment_state
  ON payment_wallet_orders (fulfillment_state);

CREATE INDEX IF NOT EXISTS ix_payment_wallet_orders_checkout_session_id
  ON payment_wallet_orders (gateway_checkout_session_id);

CREATE INDEX IF NOT EXISTS ix_payment_wallet_orders_payment_intent_id
  ON payment_wallet_orders (gateway_payment_intent_id);

DROP TRIGGER IF EXISTS trg_payment_wallet_orders_updated_at ON payment_wallet_orders;
CREATE TRIGGER trg_payment_wallet_orders_updated_at
BEFORE UPDATE ON payment_wallet_orders
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- 6) Subscription mapping
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payment_plan_subscriptions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL,
  gateway_provider TEXT NOT NULL DEFAULT 'stripe',
  gateway_customer_id TEXT,
  gateway_subscription_id TEXT NOT NULL UNIQUE,
  gateway_price_id TEXT,
  plan_code TEXT NOT NULL,
  subscription_state TEXT NOT NULL,
  current_period_start TIMESTAMPTZ,
  current_period_end TIMESTAMPTZ,
  cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE,
  canceled_at TIMESTAMPTZ,
  latest_invoice_id TEXT,
  latest_invoice_status TEXT,
  entitlement_state TEXT NOT NULL DEFAULT 'inactive',
  trial_start TIMESTAMPTZ,
  trial_end TIMESTAMPTZ,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT payment_plan_subscriptions_provider_ck
    CHECK (gateway_provider IN ('stripe', 'apple_iap', 'google_play', 'other')),
  CONSTRAINT payment_plan_subscriptions_state_ck
    CHECK (
      subscription_state IN (
        'trialing',
        'active',
        'past_due',
        'unpaid',
        'paused',
        'canceled',
        'incomplete',
        'incomplete_expired'
      )
    ),
  CONSTRAINT payment_plan_subscriptions_entitlement_state_ck
    CHECK (entitlement_state IN ('inactive', 'active', 'grace', 'suspended'))
);

CREATE INDEX IF NOT EXISTS ix_payment_plan_subscriptions_user_id
  ON payment_plan_subscriptions (user_id);

CREATE INDEX IF NOT EXISTS ix_payment_plan_subscriptions_state
  ON payment_plan_subscriptions (subscription_state);

CREATE INDEX IF NOT EXISTS ix_payment_plan_subscriptions_entitlement_state
  ON payment_plan_subscriptions (entitlement_state);

DROP TRIGGER IF EXISTS trg_payment_plan_subscriptions_updated_at ON payment_plan_subscriptions;
CREATE TRIGGER trg_payment_plan_subscriptions_updated_at
BEFORE UPDATE ON payment_plan_subscriptions
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- 7) Webhook event journal / replay shield
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payment_gateway_webhook_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  gateway_provider TEXT NOT NULL DEFAULT 'stripe',
  gateway_event_id TEXT NOT NULL UNIQUE,
  event_type TEXT NOT NULL,
  api_version TEXT,
  livemode BOOLEAN,
  object_id TEXT,
  object_type TEXT,
  received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  processed_at TIMESTAMPTZ,
  process_status TEXT NOT NULL DEFAULT 'received',
  failure_reason TEXT,
  payload_json JSONB NOT NULL,
  headers_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  CONSTRAINT payment_gateway_webhook_events_provider_ck
    CHECK (gateway_provider IN ('stripe', 'apple_iap', 'google_play', 'other')),
  CONSTRAINT payment_gateway_webhook_events_process_status_ck
    CHECK (process_status IN ('received', 'processed', 'ignored', 'failed'))
);

CREATE INDEX IF NOT EXISTS ix_payment_gateway_webhook_events_received_at
  ON payment_gateway_webhook_events (received_at DESC);

CREATE INDEX IF NOT EXISTS ix_payment_gateway_webhook_events_process_status
  ON payment_gateway_webhook_events (process_status);

CREATE INDEX IF NOT EXISTS ix_payment_gateway_webhook_events_object_id
  ON payment_gateway_webhook_events (object_id);

-- ---------------------------------------------------------------------------
-- 8) Billing entitlements snapshot / policy output
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS billing_entitlements (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL UNIQUE,
  tier_code TEXT NOT NULL,
  plan_code TEXT,
  billing_mode TEXT NOT NULL,
  settlement_mode TEXT NOT NULL,
  included_credits_total NUMERIC(18,4) NOT NULL DEFAULT 0,
  included_credits_remaining NUMERIC(18,4) NOT NULL DEFAULT 0,
  overage_allowed BOOLEAN NOT NULL DEFAULT FALSE,
  wallet_topup_allowed BOOLEAN NOT NULL DEFAULT TRUE,
  hard_stop_on_insufficient_balance BOOLEAN NOT NULL DEFAULT TRUE,
  effective_from TIMESTAMPTZ NOT NULL DEFAULT now(),
  effective_to TIMESTAMPTZ,
  source TEXT NOT NULL,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT billing_entitlements_billing_mode_ck
    CHECK (billing_mode IN ('free', 'prepaid', 'subscription', 'postpaid', 'hybrid')),
  CONSTRAINT billing_entitlements_settlement_mode_ck
    CHECK (settlement_mode IN ('credits', 'money', 'postpaid'))
);

CREATE INDEX IF NOT EXISTS ix_billing_entitlements_tier_code
  ON billing_entitlements (tier_code);

CREATE INDEX IF NOT EXISTS ix_billing_entitlements_plan_code
  ON billing_entitlements (plan_code);

DROP TRIGGER IF EXISTS trg_billing_entitlements_updated_at ON billing_entitlements;
CREATE TRIGGER trg_billing_entitlements_updated_at
BEFORE UPDATE ON billing_entitlements
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- Optional view: latest active token-only payment methods per user
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_active_payment_gateway_payment_methods AS
SELECT
  pm.id,
  pm.user_id,
  pm.gateway_provider,
  pm.gateway_customer_id,
  pm.gateway_payment_method_id,
  pm.method_type,
  pm.status,
  pm.brand,
  pm.last4,
  pm.exp_month,
  pm.exp_year,
  pm.funding_type,
  pm.country_code,
  pm.wallet_type,
  pm.network,
  pm.billing_name,
  pm.billing_email,
  pm.billing_country,
  pm.billing_postal_code,
  pm.is_default,
  pm.metadata_json,
  pm.created_at,
  pm.updated_at
FROM payment_gateway_payment_methods pm
WHERE pm.deleted_at IS NULL
  AND pm.status = 'active';

COMMIT;
