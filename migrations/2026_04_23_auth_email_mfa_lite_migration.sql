BEGIN;

ALTER TABLE core.users
    ADD COLUMN IF NOT EXISTS email_verified_at timestamptz;

CREATE TABLE IF NOT EXISTS core.auth_email_challenges (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NULL REFERENCES core.users(id) ON DELETE CASCADE,
    email text NOT NULL,
    purpose text NOT NULL CHECK (purpose IN ('register_verify', 'password_change')),
    code_hash text NOT NULL,
    status text NOT NULL CHECK (status IN ('pending', 'consumed', 'expired', 'locked')),
    attempt_count integer NOT NULL DEFAULT 0,
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    request_ip text NULL,
    request_user_agent text NULL
);

CREATE INDEX IF NOT EXISTS idx_auth_email_challenges_email_purpose_created
    ON core.auth_email_challenges (lower(email), purpose, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_auth_email_challenges_user_purpose_created
    ON core.auth_email_challenges (user_id, purpose, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_auth_email_challenges_pending_expiry
    ON core.auth_email_challenges (status, expires_at);

COMMIT;
