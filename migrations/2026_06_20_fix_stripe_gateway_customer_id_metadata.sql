begin;

with canonical as (
  select distinct on (user_id)
    user_id,
    gateway_customer_id
  from public.payment_plan_subscriptions
  where gateway_provider = 'stripe'
    and gateway_customer_id like 'cus_%'
  order by user_id, updated_at desc
)
update public.billing_entitlements be
set metadata_json = jsonb_set(
      case
        when jsonb_typeof(coalesce(be.metadata_json, '{}'::jsonb)) = 'object'
        then coalesce(be.metadata_json, '{}'::jsonb)
        else '{}'::jsonb
      end,
      '{gateway_customer_id}',
      to_jsonb(c.gateway_customer_id::text),
      true
    ),
    updated_at = now()
from canonical c
where be.user_id = c.user_id
  and coalesce(be.metadata_json->>'gateway_customer_id', '') is distinct from c.gateway_customer_id;

with canonical as (
  select distinct on (user_id)
    user_id,
    gateway_customer_id
  from public.payment_plan_subscriptions
  where gateway_provider = 'stripe'
    and gateway_customer_id like 'cus_%'
  order by user_id, updated_at desc
)
update public.pricing_user_entitlements pue
set metadata_json = jsonb_set(
      case
        when jsonb_typeof(coalesce(pue.metadata_json, '{}'::jsonb)) = 'object'
        then coalesce(pue.metadata_json, '{}'::jsonb)
        else '{}'::jsonb
      end,
      '{gateway_customer_id}',
      to_jsonb(c.gateway_customer_id::text),
      true
    )
from canonical c
where pue.user_id = c.user_id
  and coalesce(pue.metadata_json->>'gateway_customer_id', '') is distinct from c.gateway_customer_id;

commit;
