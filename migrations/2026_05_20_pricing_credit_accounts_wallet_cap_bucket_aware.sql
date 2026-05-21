-- Bucket-aware replacement for pricing_credit_accounts_wallet_cap_biu()
-- Purpose:
--   pricing_credit_accounts.balance_credits is the total cached spendable balance
--   across included + purchased + promo buckets. It must NOT be capped by a
--   plan's included-credit cap. Wallet cap applies only to wallet/purchased lots.
--
-- Safe to run repeatedly.

create or replace function public.pricing_credit_accounts_wallet_cap_biu()
returns trigger
language plpgsql
as $function$
declare
  v_plan_code text;
  v_wallet_cap numeric(18,4);
  v_enforce boolean;
  v_wallet_balance numeric(18,4) := 0;
  v_wallet_reserved numeric(18,4) := 0;
  v_wallet_total numeric(18,4) := 0;
begin
  -- Always enforce non-negative account cache values.
  if NEW.balance_credits is not null and NEW.balance_credits < 0 then
    raise exception 'pricing_credit_accounts_negative_balance:user=% balance=%',
      NEW.user_id, NEW.balance_credits;
  end if;

  if NEW.reserved_credits is not null and NEW.reserved_credits < 0 then
    raise exception 'pricing_credit_accounts_negative_reserved:user=% reserved=%',
      NEW.user_id, NEW.reserved_credits;
  end if;

  -- Enterprise/postpaid or rows without user ownership should not be blocked by
  -- prepaid wallet caps here. Billing-account-level caps need a separate policy.
  if NEW.user_id is null then
    return NEW;
  end if;

  select _normalize_plan_code_from_entitlement(be.tier_code, be.plan_code)
    into v_plan_code
  from public.billing_entitlements be
  where be.user_id = NEW.user_id
    and be.effective_from <= now()
    and (be.effective_to is null or be.effective_to > now())
  order by be.effective_from desc, be.updated_at desc
  limit 1;

  if v_plan_code is null then
    v_plan_code := 'free';
  end if;

  select wallet_credit_cap, enforce_wallet_cap
    into v_wallet_cap, v_enforce
  from public.pricing_plan_credit_guardrails
  where plan_code = v_plan_code
    and is_active = true
  limit 1;

  -- IMPORTANT:
  -- NEW.balance_credits is total remaining across all active buckets. Do not compare
  -- it to wallet_credit_cap. Included plan credits are controlled by the transition
  -- engine when creating included lots. Wallet cap applies to purchased wallet lots.
  if coalesce(v_enforce, false) and v_wallet_cap is not null then
    select
      coalesce(sum(l.remaining_amount) filter (where l.status = 'active' and l.bucket_type = 'purchased'), 0),
      coalesce(sum(l.reserved_amount) filter (where l.status = 'active' and l.bucket_type = 'purchased'), 0)
      into v_wallet_balance, v_wallet_reserved
    from public.pricing_credit_lots l
    where l.user_id = NEW.user_id;

    v_wallet_total := coalesce(v_wallet_balance, 0) + coalesce(v_wallet_reserved, 0);

    if v_wallet_total > v_wallet_cap then
      raise exception 'pricing_credit_accounts_wallet_cap_exceeded:user=% plan=% purchased_wallet_total=% cap=% purchased_balance=% purchased_reserved=%',
        NEW.user_id, v_plan_code, v_wallet_total, v_wallet_cap, v_wallet_balance, v_wallet_reserved;
    end if;
  end if;

  return NEW;
end;
$function$;
