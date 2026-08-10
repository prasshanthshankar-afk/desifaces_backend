-- DesiFaces svc-core auth migration
-- Purpose: allow OTP-first password reset flow to reuse core.auth_email_challenges.
-- Safe to rerun.

BEGIN;

ALTER TABLE core.auth_email_challenges
  DROP CONSTRAINT IF EXISTS auth_email_challenges_purpose_check;

ALTER TABLE core.auth_email_challenges
  ADD CONSTRAINT auth_email_challenges_purpose_check
  CHECK (purpose IN ('register_verify', 'password_change', 'password_reset'));

COMMIT;
