CREATE OR REPLACE FUNCTION public.billing_entitlements_guardrails_biu()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
declare
  v_plan_code text;
  v_guard pricing_plan_credit_guardrails%rowtype;
  v_free_cap numeric;
begin
  NEW.tier_code := lower(coalesce(trim(NEW.tier_code), 'free'));
  NEW.plan_code := case
    when coalesce(trim(NEW.plan_code), '') = '' then null
    else lower(trim(NEW.plan_code))
  end;

  if NEW.billing_mode is null or trim(NEW.billing_mode) = '' then
    NEW.billing_mode := case when NEW.tier_code = 'free' then 'free' else 'subscription' end;
  else
    NEW.billing_mode := lower(trim(NEW.billing_mode));
  end if;

  if NEW.settlement_mode is null or trim(NEW.settlement_mode) = '' then
    NEW.settlement_mode := 'credits';
  else
    NEW.settlement_mode := lower(trim(NEW.settlement_mode));
  end if;

  v_plan_code := _normalize_plan_code_from_entitlement(NEW.tier_code, NEW.plan_code);

  select *
  into v_guard
  from pricing_plan_credit_guardrails
  where plan_code = v_plan_code
    and is_active = true
  limit 1;

  /*
    Free plan guardrail:
    Previously this trigger forcibly zeroed all free entitlements:
      plan_code = null
      included_credits_total = 0
      included_credits_remaining = 0

    That broke free signup bootstrap because svc-pricing correctly created a
    100-credit free grant, then this BEFORE trigger zeroed the entitlement,
    and the AFTER sync trigger expired the included lot.

    Correct behavior:
      - Free entitlement remains plan_code='free'
      - Free entitlement may carry included credits, e.g. 100 signup credits
      - Remaining is clamped to [0, total], not forcibly zeroed
      - Spent users can still have remaining=0 while total=100
  */
  if not found then
    if NEW.tier_code = 'free' or NEW.billing_mode = 'free' or coalesce(NEW.plan_code, '') = 'free' then
      NEW.tier_code := 'free';
      NEW.plan_code := 'free';
      NEW.billing_mode := 'free';
      NEW.settlement_mode := 'credits';

      NEW.included_credits_total := greatest(coalesce(NEW.included_credits_total, 0), 0);

      if NEW.included_credits_remaining is null then
        NEW.included_credits_remaining := NEW.included_credits_total;
      end if;

      if NEW.included_credits_remaining > NEW.included_credits_total then
        NEW.included_credits_remaining := NEW.included_credits_total;
      end if;

      if NEW.included_credits_remaining < 0 then
        NEW.included_credits_remaining := 0;
      end if;

      if NEW.wallet_topup_allowed is null then
        NEW.wallet_topup_allowed := true;
      end if;

      NEW.overage_allowed := false;
      NEW.hard_stop_on_insufficient_balance := true;
      return NEW;
    end if;

    raise exception 'billing_entitlements_guardrails_missing_plan_cap:%', v_plan_code;
  end if;

  if v_guard.tier_code = 'free' or NEW.billing_mode = 'free' or coalesce(NEW.plan_code, '') = 'free' then
    NEW.tier_code := 'free';
    NEW.plan_code := 'free';
    NEW.billing_mode := 'free';
    NEW.settlement_mode := 'credits';

    v_free_cap := greatest(coalesce(v_guard.included_credit_cap, 0), 0);

    if NEW.included_credits_total is null then
      NEW.included_credits_total := v_free_cap;
    end if;

    if v_free_cap > 0 and NEW.included_credits_total > v_free_cap then
      NEW.included_credits_total := v_free_cap;
    end if;

    if NEW.included_credits_total < 0 then
      NEW.included_credits_total := 0;
    end if;

    if NEW.included_credits_remaining is null then
      NEW.included_credits_remaining := NEW.included_credits_total;
    end if;

    if NEW.included_credits_remaining > NEW.included_credits_total then
      NEW.included_credits_remaining := NEW.included_credits_total;
    end if;

    if NEW.included_credits_remaining < 0 then
      NEW.included_credits_remaining := 0;
    end if;

    if NEW.wallet_topup_allowed is null then
      NEW.wallet_topup_allowed := coalesce(v_guard.allow_topups, true);
    end if;

    NEW.overage_allowed := false;
    NEW.hard_stop_on_insufficient_balance := true;
    return NEW;
  end if;

  NEW.tier_code := v_guard.tier_code;
  NEW.plan_code := v_guard.plan_code;

  if NEW.included_credits_total is null then
    NEW.included_credits_total := v_guard.included_credit_cap;
  end if;
  if NEW.included_credits_total > v_guard.included_credit_cap then
    NEW.included_credits_total := v_guard.included_credit_cap;
  end if;
  if NEW.included_credits_total < 0 then
    NEW.included_credits_total := 0;
  end if;

  if NEW.included_credits_remaining is null then
    NEW.included_credits_remaining := NEW.included_credits_total;
  end if;
  if NEW.included_credits_remaining > NEW.included_credits_total then
    NEW.included_credits_remaining := NEW.included_credits_total;
  end if;
  if NEW.included_credits_remaining < 0 then
    NEW.included_credits_remaining := 0;
  end if;

  if NEW.wallet_topup_allowed is null then
    NEW.wallet_topup_allowed := v_guard.allow_topups;
  end if;

  return NEW;
end;
$function$;