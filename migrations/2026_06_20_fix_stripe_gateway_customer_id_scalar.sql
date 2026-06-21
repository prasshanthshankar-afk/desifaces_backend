begin;

with fixed as (
  select
    id,
    coalesce(
      substring(gateway_customer_id::text from '''id'': ''(cus_[^'']+)'''),
      substring(gateway_customer_id::text from '"id"[[:space:]]*:[[:space:]]*"(cus_[^"]+)"'),
      case
        when gateway_customer_id::text like 'cus_%' then gateway_customer_id::text
        else null
      end
    ) as extracted_gateway_customer_id
  from public.payment_plan_subscriptions
  where gateway_provider = 'stripe'
)
update public.payment_plan_subscriptions p
set gateway_customer_id = f.extracted_gateway_customer_id,
    updated_at = now()
from fixed f
where p.id = f.id
  and f.extracted_gateway_customer_id is not null
  and p.gateway_customer_id::text is distinct from f.extracted_gateway_customer_id;

commit;
