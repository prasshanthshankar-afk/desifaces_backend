-- SUPERSEDED / INTENTIONAL NO-OP
--
-- Do not use this V1 Stripe LIVE mapping migration.
-- It has been superseded by:
--   migrations/2026_09_13_stripe_live_launch_price_mappings_v2.sql
--
-- V2 narrows mutation authority to exactly 8 recurring plan rows and 6 credit-pack
-- rows, with one exact UPDATE per certified launch catalog entry and immutable
-- customer-price/credit fingerprints.
--
-- This file remains as a no-op marker so automated migration discovery cannot
-- accidentally execute the superseded V1 implementation.

BEGIN;
DO $$
BEGIN
  RAISE NOTICE 'Stripe LIVE mapping V1 skipped; use 2026_09_13_stripe_live_launch_price_mappings_v2.sql';
END $$;
COMMIT;
