BEGIN;

-- Launch invariant: the authenticated user's most recently resolved login country
-- is the canonical country used for customer-facing pricing/payment currency.
-- India => INR; every other country => USD.
ALTER TABLE core.users
  ADD COLUMN IF NOT EXISTS country_code text NULL;

UPDATE core.users
SET country_code = upper(btrim(country_code))
WHERE country_code IS NOT NULL
  AND country_code <> upper(btrim(country_code));

UPDATE core.users
SET country_code = NULL
WHERE country_code IS NOT NULL
  AND country_code !~ '^[A-Z]{2}$';

ALTER TABLE core.users
  DROP CONSTRAINT IF EXISTS ck_core_users_country_code_iso2;
ALTER TABLE core.users
  ADD CONSTRAINT ck_core_users_country_code_iso2
  CHECK (country_code IS NULL OR country_code ~ '^[A-Z]{2}$');

CREATE INDEX IF NOT EXISTS idx_core_users_country_code
  ON core.users(country_code)
  WHERE country_code IS NOT NULL;

-- Keep the account-level commercial default aligned wherever a canonical
-- country is already known. Login/register code repeats this sync so accounts
-- created after this migration remain aligned as well.
UPDATE public.pricing_billing_accounts ba
SET default_currency = CASE WHEN u.country_code = 'IN' THEN 'INR' ELSE 'USD' END,
    updated_at = now()
FROM public.pricing_billing_account_members bam
JOIN core.users u ON u.id = bam.user_id
WHERE bam.billing_account_id = ba.id
  AND bam.status = 'active'
  AND u.country_code IS NOT NULL
  AND ba.default_currency IS DISTINCT FROM CASE WHEN u.country_code = 'IN' THEN 'INR' ELSE 'USD' END;

COMMIT;
