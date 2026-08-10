-- DesiFaces svc-core auth OTP reset schema migration
-- Supports one shared email challenge table for registration, password reset,
-- and logged-in password change OTP verification.

BEGIN;

ALTER TABLE core.auth_email_challenges
  DROP CONSTRAINT IF EXISTS auth_email_challenges_purpose_check;

ALTER TABLE core.auth_email_challenges
  ADD CONSTRAINT auth_email_challenges_purpose_check
  CHECK (purpose IN ('register_verify', 'password_change', 'password_reset'));

CREATE INDEX IF NOT EXISTS ix_auth_email_challenges_email_purpose_created
  ON core.auth_email_challenges (lower(email), purpose, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_auth_email_challenges_user_purpose_created
  ON core.auth_email_challenges (user_id, purpose, created_at DESC);

COMMIT;
