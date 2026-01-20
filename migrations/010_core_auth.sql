-- 010_core_auth.sql
-- Core auth + sessions + password reset + admin RBAC (Phase-1)
-- Raw SQL, asyncpg-friendly.
-- Assumes schema `core` exists.

BEGIN;

-- Useful crypto functions
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid(), crypt(), gen_salt()

-- 1) Users
CREATE TABLE IF NOT EXISTS core.users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email text NOT NULL,
  password_hash text NOT NULL,
  full_name text NOT NULL DEFAULT '',
  tier text NOT NULL DEFAULT 'free' CHECK (tier IN ('free','pro','enterprise')),
  is_active boolean NOT NULL DEFAULT true,
  is_email_verified boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT users_email_lower_unique UNIQUE (email)
);

-- Normalize email uniqueness to lower(email)
-- (We keep `email` stored as original case if you want display fidelity.)
CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email_lower
  ON core.users (lower(email));

-- 2) Roles (simple + extendable)
CREATE TABLE IF NOT EXISTS core.roles (
  id bigserial PRIMARY KEY,
  role_key text NOT NULL UNIQUE, -- e.g. 'admin', 'user'
  description text NOT NULL DEFAULT ''
);

INSERT INTO core.roles(role_key, description)
VALUES ('user','Standard user'), ('admin','Administrator')
ON CONFLICT (role_key) DO NOTHING;

CREATE TABLE IF NOT EXISTS core.user_roles (
  user_id uuid NOT NULL REFERENCES core.users(id) ON DELETE CASCADE,
  role_id bigint NOT NULL REFERENCES core.roles(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, role_id)
);

-- 3) Sessions (refresh tokens)
-- Store only refresh_token_hash (NEVER the raw refresh token)
CREATE TABLE IF NOT EXISTS core.sessions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES core.users(id) ON DELETE CASCADE,

  -- hashed refresh token (HMAC/sha256 output hex)
  refresh_token_hash text NOT NULL,

  -- optional device binding (Phase-2 ready)
  device_id text NULL,
  client_type text NULL CHECK (client_type IN ('web','ios','android')),

  ip text NULL,
  user_agent text NULL,

  created_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz NULL,

  CONSTRAINT sessions_refresh_hash_unique UNIQUE (refresh_token_hash)
);

CREATE INDEX IF NOT EXISTS ix_sessions_user_id ON core.sessions(user_id);
CREATE INDEX IF NOT EXISTS ix_sessions_expires_at ON core.sessions(expires_at);
CREATE INDEX IF NOT EXISTS ix_sessions_revoked_at ON core.sessions(revoked_at);

-- 4) Password reset tokens
-- Store only token_hash (HMAC/sha256) with expiration + single-use semantics.
CREATE TABLE IF NOT EXISTS core.password_reset_tokens (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES core.users(id) ON DELETE CASCADE,
  token_hash text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  used_at timestamptz NULL,
  request_ip text NULL,
  request_user_agent text NULL
);

CREATE INDEX IF NOT EXISTS ix_prt_user_id ON core.password_reset_tokens(user_id);
CREATE INDEX IF NOT EXISTS ix_prt_expires_at ON core.password_reset_tokens(expires_at);

-- 5) Optional: account lockouts / login attempts (minimal now; can expand)
CREATE TABLE IF NOT EXISTS core.login_attempts (
  id bigserial PRIMARY KEY,
  email_lower text NOT NULL,
  success boolean NOT NULL,
  ip text NULL,
  user_agent text NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_login_attempts_email_created
  ON core.login_attempts(email_lower, created_at DESC);

COMMIT;