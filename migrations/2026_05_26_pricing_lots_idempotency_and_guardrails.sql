-- DesiFaces pricing/payment lifecycle hardening
-- Purpose:
-- 1) keep pricing_credit_lots idempotent for wallet top-ups and plan-cycle true-ups
-- 2) preserve purchased/top-up credits across plan changes
-- 3) keep plan caps separate from top-up wallet credits

begin;

-- One provider-verified wallet order / plan true-up source_ref should create at most one active lot per user.
-- This protects Google/Apple/Stripe retry paths and plan reconciliation retries.
create unique index if not exists pricing_credit_lots_user_bucket_source_ref_uidx
on public.pricing_credit_lots(user_id, bucket_type, source_type, source_ref)
where source_ref is not null;

-- Wallet/top-up caps are not plan included-credit caps. Verified paid top-ups must not be blocked
-- by Free/Pro/Business plan caps. Plan included caps remain in included_credit_cap.
update public.pricing_plan_credit_guardrails
set
  allow_topups = true,
  enforce_wallet_cap = false,
  wallet_credit_cap = null,
  metadata_json = coalesce(metadata_json, '{}'::jsonb)
    || jsonb_build_object(
      'wallet_cap_policy', 'do_not_apply_plan_cap_to_verified_paid_topups',
      'included_cap_policy', 'plan_cycle_entitlement_cap_only',
      'updated_reason', 'canonical_pricing_reconciliation_e2e'
    ),
  updated_at = now()
where plan_code in (
  'free',
  'pro_monthly_v1',
  'pro_yearly_v1',
  'business_monthly_v1',
  'business_yearly_v1'
);

commit;
