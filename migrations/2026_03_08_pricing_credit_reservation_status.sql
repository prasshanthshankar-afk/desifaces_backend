ALTER TABLE public.pricing_credit_reservations
DROP CONSTRAINT IF EXISTS ck_pricing_res_status;

ALTER TABLE public.pricing_credit_reservations
ADD CONSTRAINT ck_pricing_res_status
CHECK (
  status IN (
    'reserved',
    'committed',
    'released',
    'expired',
    'cancelled',
    'failed'
  )
);