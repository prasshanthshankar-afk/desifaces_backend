-- services/svc-pricing/app/app/migrations/0005_pricing_feature_flags_seed.sql
-- Default GTM gating policies.

BEGIN;

-- Global: Pricing engine enabled for web/mobile billing
INSERT INTO pricing_feature_flags
  (code, scope, country_code, tier_code, channel, enabled, billing_mode, priority, metadata_json)
VALUES
  ('pricing.core',   'global', '', '', '',      true, 'bill', 1000, '{"note":"core pricing enabled"}'),

  -- Modules: enabled + billed by default (you can override below)
  ('module.face',    'global', '', '', '',      true, 'bill', 900,  '{"note":"Face Studio live"}'),
  ('module.audio',   'global', '', '', '',      true, 'bill', 900,  '{"note":"Audio Studio live"}'),
  ('module.fusion',  'global', '', '', '',      true, 'bill', 900,  '{"note":"Fusion Studio live"}'),
  ('module.commerce','global', '', '', '',      true, 'bill', 900,  '{"note":"Commerce Studio live"}'),

  -- Music: choose ONE default:
  -- Option A (recommended): live but SHADOW for 3-6 months until you finalize packaging
  ('module.music',   'global', '', '', '',      true, 'shadow', 900, '{"note":"Music Studio allowed but not charged yet (shadow)"}'),

  -- API channel: disabled until developer/API launch
  ('channel.api',    'global', '', '', 'api',   false, 'disabled', 1000, '{"note":"API pricing OFF until API launch"}')
ON CONFLICT (code) DO UPDATE
SET enabled = EXCLUDED.enabled,
    billing_mode = EXCLUDED.billing_mode,
    priority = EXCLUDED.priority,
    metadata_json = EXCLUDED.metadata_json;

-- Example: allow API shadow internally for developer tier only (optional)
-- Uncomment when you're ready to test API in production without charging customers:
-- INSERT INTO pricing_feature_flags
--   (code, scope, country_code, tier_code, channel, enabled, billing_mode, priority, metadata_json)
-- VALUES
--   ('channel.api', 'tier', '', 'developer', 'api', true, 'shadow', 2000, '{"note":"Developer tier API shadow testing"}')
-- ON CONFLICT (code) DO UPDATE
-- SET enabled = EXCLUDED.enabled,
--     billing_mode = EXCLUDED.billing_mode,
--     priority = EXCLUDED.priority,
--     metadata_json = EXCLUDED.metadata_json;

COMMIT;