-- V3-C6: subscription-cycle credit integrity and billing-account ownership.
--
-- This migration does not grant a new subscription period. It enforces the
-- invariant that every credit lot belonging to a known user/account is linked
-- to that billing account, including future renewal lots created by legacy
-- Apple/Google/Stripe paths.

BEGIN;

CREATE OR REPLACE FUNCTION public.df_v3_resolve_user_billing_account_id(p_user_id uuid)
RETURNS uuid
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
  v_account_id uuid;
BEGIN
  SELECT bam.billing_account_id
  INTO v_account_id
  FROM public.pricing_billing_account_members bam
  JOIN public.pricing_billing_accounts ba ON ba.id=bam.billing_account_id
  WHERE bam.user_id=p_user_id
    AND bam.status='active'
    AND ba.status='active'
  ORDER BY bam.is_default DESC,
           CASE bam.role WHEN 'owner' THEN 0 WHEN 'finance_admin' THEN 1 ELSE 2 END,
           bam.created_at ASC
  LIMIT 1;

  IF v_account_id IS NOT NULL THEN
    RETURN v_account_id;
  END IF;

  SELECT pca.billing_account_id
  INTO v_account_id
  FROM public.pricing_credit_accounts pca
  WHERE pca.user_id=p_user_id AND pca.billing_account_id IS NOT NULL
  LIMIT 1;

  IF v_account_id IS NOT NULL THEN
    RETURN v_account_id;
  END IF;

  SELECT ba.id
  INTO v_account_id
  FROM public.pricing_billing_accounts ba
  WHERE ba.account_code='user:' || p_user_id::text
    AND ba.status='active'
  LIMIT 1;

  RETURN v_account_id;
END;
$$;

CREATE OR REPLACE FUNCTION public.df_v3_credit_lot_account_context()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.billing_account_id IS NULL AND NEW.user_id IS NOT NULL THEN
    NEW.billing_account_id := public.df_v3_resolve_user_billing_account_id(NEW.user_id);
  END IF;
  IF NEW.billing_account_id IS NULL AND NEW.user_id IS NOT NULL THEN
    RAISE EXCEPTION 'v3_credit_lot_billing_account_missing:user_id=%', NEW.user_id;
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_df_v3_credit_lot_account_context ON public.pricing_credit_lots;
CREATE TRIGGER trg_df_v3_credit_lot_account_context
BEFORE INSERT OR UPDATE OF user_id, billing_account_id ON public.pricing_credit_lots
FOR EACH ROW EXECUTE FUNCTION public.df_v3_credit_lot_account_context();

UPDATE public.pricing_credit_lots l
SET billing_account_id=public.df_v3_resolve_user_billing_account_id(l.user_id),
    updated_at=now()
WHERE l.billing_account_id IS NULL
  AND l.user_id IS NOT NULL
  AND public.df_v3_resolve_user_billing_account_id(l.user_id) IS NOT NULL;

-- Durable audit of periodic integrity reconciliation. Provider webhook tables
-- remain provider-specific; this table records the canonical post-provider
-- credit-cycle repair result.
CREATE TABLE IF NOT EXISTS public.v3_subscription_credit_reconciliation (
  reconciliation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL,
  billing_account_id uuid NOT NULL REFERENCES public.pricing_billing_accounts(id) ON DELETE RESTRICT,
  gateway_provider text NOT NULL,
  gateway_subscription_id text NOT NULL,
  cycle_key text NOT NULL,
  plan_code text NOT NULL,
  action text NOT NULL,
  result_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_v3_sub_credit_provider_nonempty CHECK (length(btrim(gateway_provider))>0),
  CONSTRAINT ck_v3_sub_credit_gateway_id_nonempty CHECK (length(btrim(gateway_subscription_id))>0),
  CONSTRAINT ck_v3_sub_credit_cycle_nonempty CHECK (length(btrim(cycle_key))>0),
  UNIQUE(user_id,gateway_provider,gateway_subscription_id,cycle_key,action)
);

CREATE INDEX IF NOT EXISTS idx_v3_subscription_credit_reconcile_account_created
  ON public.v3_subscription_credit_reconciliation(billing_account_id,created_at DESC);

COMMIT;
