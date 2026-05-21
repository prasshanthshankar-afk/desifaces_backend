ALTER TABLE notification_deliveries
  ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE notification_deliveries
  ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 4;

ALTER TABLE notification_deliveries
  ADD COLUMN IF NOT EXISTS processing_started_at TIMESTAMPTZ NULL;

ALTER TABLE notification_deliveries
  ADD COLUMN IF NOT EXISTS terminal_reason TEXT NULL;

CREATE INDEX IF NOT EXISTS ix_notification_deliveries_due
  ON notification_deliveries (status, next_attempt_at, created_at);

CREATE INDEX IF NOT EXISTS ix_notification_deliveries_processing_started
  ON notification_deliveries (status, processing_started_at);