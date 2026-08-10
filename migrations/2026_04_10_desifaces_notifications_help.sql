CREATE EXTENSION IF NOT EXISTS pgcrypto;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'notification_category') THEN
    CREATE TYPE notification_category AS ENUM (
      'jobs',
      'billing',
      'account',
      'support',
      'announcements'
    );
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'notification_priority') THEN
    CREATE TYPE notification_priority AS ENUM (
      'critical',
      'important',
      'info'
    );
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'notification_channel') THEN
    CREATE TYPE notification_channel AS ENUM (
      'in_app',
      'push',
      'email'
    );
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'notification_delivery_status') THEN
    CREATE TYPE notification_delivery_status AS ENUM (
      'queued',
      'processing',
      'delivered',
      'failed',
      'skipped'
    );
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'support_request_status') THEN
    CREATE TYPE support_request_status AS ENUM (
      'open',
      'waiting_on_support',
      'waiting_on_customer',
      'resolved',
      'closed'
    );
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'support_sender_role') THEN
    CREATE TYPE support_sender_role AS ENUM (
      'user',
      'support',
      'system'
    );
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS notification_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  event_type TEXT NOT NULL,
  category notification_category NOT NULL,
  priority notification_priority NOT NULL DEFAULT 'info',
  source_service TEXT NOT NULL,
  source_ref_type TEXT NULL,
  source_ref_id TEXT NULL,
  actor_user_id UUID NULL REFERENCES core.users(id) ON DELETE SET NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  action_route TEXT NULL,
  action_label TEXT NULL,
  image_url TEXT NULL,
  payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  dedupe_key TEXT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_notification_events_dedupe UNIQUE (dedupe_key)
);

CREATE INDEX IF NOT EXISTS ix_notification_events_created_at
  ON notification_events (created_at DESC);

CREATE INDEX IF NOT EXISTS ix_notification_events_source
  ON notification_events (source_service, source_ref_type, source_ref_id);

CREATE TABLE IF NOT EXISTS notification_user_items (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id UUID NOT NULL REFERENCES notification_events(id) ON DELETE CASCADE,
  user_id UUID NOT NULL REFERENCES core.users(id) ON DELETE CASCADE,
  category notification_category NOT NULL,
  priority notification_priority NOT NULL,
  event_type TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  action_route TEXT NULL,
  action_label TEXT NULL,
  image_url TEXT NULL,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  is_read BOOLEAN NOT NULL DEFAULT FALSE,
  read_at TIMESTAMPTZ NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (event_id, user_id)
);

CREATE INDEX IF NOT EXISTS ix_notification_user_items_user_created
  ON notification_user_items (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_notification_user_items_user_unread
  ON notification_user_items (user_id, is_read, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_notification_user_items_user_category
  ON notification_user_items (user_id, category, created_at DESC);

CREATE TABLE IF NOT EXISTS notification_deliveries (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id UUID NOT NULL REFERENCES notification_events(id) ON DELETE CASCADE,
  user_id UUID NOT NULL REFERENCES core.users(id) ON DELETE CASCADE,
  channel notification_channel NOT NULL,
  destination TEXT NULL,
  provider TEXT NULL,
  provider_message_id TEXT NULL,
  status notification_delivery_status NOT NULL DEFAULT 'queued',
  attempt_count INTEGER NOT NULL DEFAULT 0,
  last_attempt_at TIMESTAMPTZ NULL,
  delivered_at TIMESTAMPTZ NULL,
  error_code TEXT NULL,
  error_message TEXT NULL,
  payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (event_id, user_id, channel)
);

CREATE INDEX IF NOT EXISTS ix_notification_deliveries_status_created
  ON notification_deliveries (status, created_at);

CREATE INDEX IF NOT EXISTS ix_notification_deliveries_user_channel
  ON notification_deliveries (user_id, channel, created_at DESC);

CREATE TABLE IF NOT EXISTS notification_preferences (
  user_id UUID NOT NULL REFERENCES core.users(id) ON DELETE CASCADE,
  category notification_category NOT NULL,
  in_app_enabled BOOLEAN NOT NULL DEFAULT TRUE,
  push_enabled BOOLEAN NOT NULL DEFAULT TRUE,
  email_enabled BOOLEAN NOT NULL DEFAULT TRUE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, category)
);

CREATE TABLE IF NOT EXISTS notification_devices (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES core.users(id) ON DELETE CASCADE,
  platform TEXT NOT NULL CHECK (platform IN ('ios', 'android', 'web')),
  expo_push_token TEXT NOT NULL,
  device_name TEXT NULL,
  app_version TEXT NULL,
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, expo_push_token)
);

CREATE INDEX IF NOT EXISTS ix_notification_devices_user_active
  ON notification_devices (user_id, is_active);

CREATE TABLE IF NOT EXISTS support_requests (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NULL REFERENCES core.users(id) ON DELETE SET NULL,
  name TEXT NOT NULL,
  email TEXT NOT NULL,
  topic TEXT NOT NULL,
  product_area TEXT NOT NULL,
  priority TEXT NOT NULL CHECK (priority IN ('low', 'normal', 'high')),
  subject TEXT NOT NULL,
  status support_request_status NOT NULL DEFAULT 'open',
  tier_code TEXT NULL,
  assigned_to_user_id UUID NULL REFERENCES core.users(id) ON DELETE SET NULL,
  latest_message_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_support_requests_user_created
  ON support_requests (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_support_requests_status_latest
  ON support_requests (status, latest_message_at DESC);

CREATE TABLE IF NOT EXISTS support_messages (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  request_id UUID NOT NULL REFERENCES support_requests(id) ON DELETE CASCADE,
  sender_role support_sender_role NOT NULL,
  sender_user_id UUID NULL REFERENCES core.users(id) ON DELETE SET NULL,
  sender_email TEXT NULL,
  body TEXT NOT NULL,
  attachments_json JSONB NOT NULL DEFAULT '[]'::jsonb,
  is_internal BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_support_messages_request_created
  ON support_messages (request_id, created_at ASC);

CREATE TABLE IF NOT EXISTS help_categories (
  key TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT NULL,
  sort_order INTEGER NOT NULL DEFAULT 100,
  is_active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS help_articles (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  slug TEXT NOT NULL UNIQUE,
  category_key TEXT NOT NULL REFERENCES help_categories(key) ON DELETE RESTRICT,
  title TEXT NOT NULL,
  summary TEXT NULL,
  body_markdown TEXT NOT NULL,
  keywords TEXT[] NOT NULL DEFAULT '{}'::text[],
  is_faq BOOLEAN NOT NULL DEFAULT FALSE,
  is_published BOOLEAN NOT NULL DEFAULT TRUE,
  sort_order INTEGER NOT NULL DEFAULT 100,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_help_articles_category_sort
  ON help_articles (category_key, is_published, sort_order, created_at DESC);