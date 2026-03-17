CREATE UNIQUE INDEX IF NOT EXISTS uq_pricing_credit_reservations_user_idempotency
ON pricing_credit_reservations (user_id, idempotency_key);


CREATE UNIQUE INDEX IF NOT EXISTS uq_provider_runs_idempotency_key
ON public.provider_runs (idempotency_key);