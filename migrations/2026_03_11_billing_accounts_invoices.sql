BEGIN;

-- =========================================================
-- 1) Billing accounts
-- =========================================================
CREATE TABLE IF NOT EXISTS public.pricing_billing_accounts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_code text NOT NULL UNIQUE,
    account_type text NOT NULL CHECK (account_type IN ('individual', 'business', 'enterprise', 'internal')),
    display_name text NOT NULL,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive', 'suspended', 'closed')),

    -- This is the commercial settlement mode for the customer account:
    -- prepaid  = wallet / credit-ledger driven
    -- postpaid = invoice driven
    -- hybrid   = can use both depending on entitlement/rules
    billing_mode text NOT NULL CHECK (billing_mode IN ('prepaid', 'postpaid', 'hybrid')),

    default_currency text NOT NULL DEFAULT 'USD',
    external_customer_ref text,   -- Stripe customer id / ERP customer id / NetSuite / QuickBooks etc.
    tax_profile_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    invoicing_config_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    meta_json jsonb NOT NULL DEFAULT '{}'::jsonb,

    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pricing_billing_accounts_mode
    ON public.pricing_billing_accounts (billing_mode, status);

CREATE INDEX IF NOT EXISTS idx_pricing_billing_accounts_customer_ref
    ON public.pricing_billing_accounts (external_customer_ref);


-- =========================================================
-- 2) Billing account members
-- =========================================================
CREATE TABLE IF NOT EXISTS public.pricing_billing_account_members (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    billing_account_id uuid NOT NULL REFERENCES public.pricing_billing_accounts(id) ON DELETE CASCADE,
    user_id uuid NOT NULL,
    role text NOT NULL DEFAULT 'member' CHECK (role IN ('owner', 'finance_admin', 'member', 'viewer')),
    is_default boolean NOT NULL DEFAULT false,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
    meta_json jsonb NOT NULL DEFAULT '{}'::jsonb,

    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT uq_pricing_billing_account_members UNIQUE (billing_account_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_pricing_billing_account_members_user
    ON public.pricing_billing_account_members (user_id, status);

CREATE INDEX IF NOT EXISTS idx_pricing_billing_account_members_default
    ON public.pricing_billing_account_members (user_id, is_default, status);


-- =========================================================
-- 3) Invoices
-- =========================================================
CREATE TABLE IF NOT EXISTS public.pricing_invoices (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    billing_account_id uuid NOT NULL REFERENCES public.pricing_billing_accounts(id) ON DELETE RESTRICT,

    invoice_number text UNIQUE,
    period_start timestamptz NOT NULL,
    period_end timestamptz NOT NULL,

    currency text NOT NULL DEFAULT 'USD',
    status text NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'issued', 'partially_paid', 'paid', 'void', 'failed')),

    subtotal_money numeric(18,8) NOT NULL DEFAULT 0,
    discount_money numeric(18,8) NOT NULL DEFAULT 0,
    tax_money numeric(18,8) NOT NULL DEFAULT 0,
    total_money numeric(18,8) NOT NULL DEFAULT 0,

    issued_at timestamptz,
    due_at timestamptz,
    paid_at timestamptz,

    external_invoice_ref text,   -- Stripe invoice id / ERP invoice id / accounting system ref
    meta_json jsonb NOT NULL DEFAULT '{}'::jsonb,

    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT chk_pricing_invoices_period CHECK (period_end >= period_start)
);

CREATE INDEX IF NOT EXISTS idx_pricing_invoices_account_status
    ON public.pricing_invoices (billing_account_id, status, period_start, period_end);

CREATE INDEX IF NOT EXISTS idx_pricing_invoices_external_ref
    ON public.pricing_invoices (external_invoice_ref);


-- =========================================================
-- 4) Invoice lines
-- =========================================================
CREATE TABLE IF NOT EXISTS public.pricing_invoice_lines (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id uuid NOT NULL REFERENCES public.pricing_invoices(id) ON DELETE CASCADE,
    billing_account_id uuid NOT NULL REFERENCES public.pricing_billing_accounts(id) ON DELETE RESTRICT,

    ledger_event_id uuid REFERENCES public.pricing_credit_ledger_events(id) ON DELETE SET NULL,
    reservation_id uuid REFERENCES public.pricing_credit_reservations(id) ON DELETE SET NULL,
    studio_job_id uuid REFERENCES public.studio_jobs(id) ON DELETE SET NULL,

    user_id uuid,
    service_name text,
    service_action text,
    sku_code text,
    quantity numeric(18,8) NOT NULL DEFAULT 0,
    unit_amount_money numeric(18,8) NOT NULL DEFAULT 0,
    line_amount_money numeric(18,8) NOT NULL DEFAULT 0,
    currency text NOT NULL DEFAULT 'USD',
    description text,
    meta_json jsonb NOT NULL DEFAULT '{}'::jsonb,

    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pricing_invoice_lines_invoice
    ON public.pricing_invoice_lines (invoice_id);

CREATE INDEX IF NOT EXISTS idx_pricing_invoice_lines_billing_account
    ON public.pricing_invoice_lines (billing_account_id);

CREATE INDEX IF NOT EXISTS idx_pricing_invoice_lines_ledger_event
    ON public.pricing_invoice_lines (ledger_event_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_pricing_invoice_lines_ledger_event
    ON public.pricing_invoice_lines (ledger_event_id)
    WHERE ledger_event_id IS NOT NULL;


-- =========================================================
-- 5) Minimal additive columns to existing pricing tables
-- =========================================================

-- ---------------------------------
-- pricing_credit_accounts
-- credit wallet owner / prepaid bucket
-- ---------------------------------
ALTER TABLE public.pricing_credit_accounts
    ADD COLUMN IF NOT EXISTS billing_account_id uuid REFERENCES public.pricing_billing_accounts(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS settlement_mode text NOT NULL DEFAULT 'prepaid'
        CHECK (settlement_mode IN ('prepaid', 'postpaid', 'hybrid'));

CREATE INDEX IF NOT EXISTS idx_pricing_credit_accounts_billing_account
    ON public.pricing_credit_accounts (billing_account_id);

-- ---------------------------------
-- pricing_credit_reservations
-- reservation owner + invoicing linkage
-- ---------------------------------
ALTER TABLE public.pricing_credit_reservations
    ADD COLUMN IF NOT EXISTS billing_account_id uuid REFERENCES public.pricing_billing_accounts(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS settlement_mode text NOT NULL DEFAULT 'prepaid'
        CHECK (settlement_mode IN ('prepaid', 'postpaid', 'hybrid')),
    ADD COLUMN IF NOT EXISTS service_name text,
    ADD COLUMN IF NOT EXISTS service_action text,
    ADD COLUMN IF NOT EXISTS sku_code text,
    ADD COLUMN IF NOT EXISTS invoice_id uuid REFERENCES public.pricing_invoices(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS invoiced_at timestamptz,
    ADD COLUMN IF NOT EXISTS invoice_line_count integer NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_pricing_credit_reservations_billing_account
    ON public.pricing_credit_reservations (billing_account_id, status, created_at);

CREATE INDEX IF NOT EXISTS idx_pricing_credit_reservations_invoice
    ON public.pricing_credit_reservations (invoice_id);

CREATE INDEX IF NOT EXISTS idx_pricing_credit_reservations_service
    ON public.pricing_credit_reservations (service_name, service_action, sku_code);

-- ---------------------------------
-- pricing_credit_ledger_events
-- the core billable usage event table
-- ---------------------------------
ALTER TABLE public.pricing_credit_ledger_events
    ADD COLUMN IF NOT EXISTS billing_account_id uuid REFERENCES public.pricing_billing_accounts(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS settlement_mode text NOT NULL DEFAULT 'prepaid'
        CHECK (settlement_mode IN ('prepaid', 'postpaid', 'hybrid')),
    ADD COLUMN IF NOT EXISTS reservation_id uuid REFERENCES public.pricing_credit_reservations(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS studio_job_id uuid REFERENCES public.studio_jobs(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS service_name text,
    ADD COLUMN IF NOT EXISTS service_action text,
    ADD COLUMN IF NOT EXISTS invoice_id uuid REFERENCES public.pricing_invoices(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS invoice_line_id uuid REFERENCES public.pricing_invoice_lines(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS invoiced_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_pricing_credit_ledger_events_billing_account
    ON public.pricing_credit_ledger_events (billing_account_id, event_type, created_at);

CREATE INDEX IF NOT EXISTS idx_pricing_credit_ledger_events_invoice
    ON public.pricing_credit_ledger_events (invoice_id, invoiced_at);

CREATE INDEX IF NOT EXISTS idx_pricing_credit_ledger_events_reservation
    ON public.pricing_credit_ledger_events (reservation_id);

CREATE INDEX IF NOT EXISTS idx_pricing_credit_ledger_events_job
    ON public.pricing_credit_ledger_events (studio_job_id);

CREATE INDEX IF NOT EXISTS idx_pricing_credit_ledger_events_service
    ON public.pricing_credit_ledger_events (service_name, service_action);

-- ---------------------------------
-- Optional: billing account on entitlement rows
-- lets enterprise plans be account-owned instead of only user-owned
-- ---------------------------------
ALTER TABLE public.pricing_user_entitlements
    ADD COLUMN IF NOT EXISTS billing_account_id uuid REFERENCES public.pricing_billing_accounts(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_pricing_user_entitlements_billing_account
    ON public.pricing_user_entitlements (billing_account_id, user_id);


-- =========================================================
-- 6) Backfill default billing accounts for existing users
-- =========================================================
WITH distinct_users AS (
    SELECT user_id
    FROM public.pricing_credit_accounts
),
ins_accounts AS (
    INSERT INTO public.pricing_billing_accounts (
        account_code,
        account_type,
        display_name,
        billing_mode,
        default_currency,
        meta_json
    )
    SELECT
        'user:' || user_id::text,
        'individual',
        'User ' || user_id::text,
        'prepaid',
        'USD',
        jsonb_build_object('bootstrap_source', 'pricing_credit_accounts')
    FROM distinct_users
    ON CONFLICT (account_code) DO NOTHING
    RETURNING id, account_code
)
INSERT INTO public.pricing_billing_account_members (
    billing_account_id,
    user_id,
    role,
    is_default,
    status,
    meta_json
)
SELECT
    ba.id,
    du.user_id,
    'owner',
    true,
    'active',
    jsonb_build_object('bootstrap_source', 'pricing_credit_accounts')
FROM distinct_users du
JOIN public.pricing_billing_accounts ba
  ON ba.account_code = 'user:' || du.user_id::text
ON CONFLICT (billing_account_id, user_id) DO NOTHING;

UPDATE public.pricing_credit_accounts pca
SET billing_account_id = ba.id
FROM public.pricing_billing_accounts ba
WHERE pca.billing_account_id IS NULL
  AND ba.account_code = 'user:' || pca.user_id::text;

COMMIT;