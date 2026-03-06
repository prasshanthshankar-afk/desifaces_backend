-- services/svc-marketing/app/app/db/migrations/005_seed_schedules_cadence.sql
-- Cadence:
--   Mon/Wed/Fri: Reel (Face-first)
--   Tue/Thu:     Story (Face-first)
--   Sun:         Carousel (Face-first)
--
-- Time:
--   10:00 AM America/New_York ≈ 15:00 UTC (standard time)
-- NOTE: Your scheduler compares UTC hour/minute, so this is correct for Feb 2026.

insert into marketing_schedules (
  schedule_id, name, enabled,
  freq, hour, minute, dow,
  mode, recipe, persona, industry, tags, season_event, offer, language_hint,
  inputs_json, target_seconds
) values

-- -------------------------
-- Mon/Wed/Fri: REELS (Face-first)
-- -------------------------

(
  gen_random_uuid(),
  'Cadence Reel (Face) - Mon/Wed/Fri 10am ET',
  true,
  'weekly', 15, 0, 'mon,wed,fri',
  'stage',
  'FACE_AUDIO_VIDEO',
  'creator',
  null,
  array['face_studio','format_reel','creator'],
  null,
  null,
  'en',
  -- inputs_json: keep empty; svc-marketing will choose a specific approved usecase by tags
  '{}'::jsonb,
  10
),

-- -------------------------
-- Tue/Thu: STORIES (Face-first)
-- -------------------------

(
  gen_random_uuid(),
  'Cadence Story (Face) - Tue/Thu 10am ET',
  true,
  'weekly', 15, 0, 'tue,thu',
  'stage',
  'FACE_AUDIO_VIDEO',
  'user',
  null,
  array['face_studio','format_story','user'],
  null,
  null,
  'en',
  '{}'::jsonb,
  8
),

-- -------------------------
-- Sun: CAROUSEL (1–2 slides) (Face-first)
-- -------------------------

(
  gen_random_uuid(),
  'Cadence Carousel (Face) - Sun 10am ET',
  true,
  'weekly', 15, 0, 'sun',
  'stage',
  'FACE_AUDIO_VIDEO',
  'creator',
  null,
  array['face_studio','format_carousel','creator'],
  null,
  null,
  'en',
  '{}'::jsonb,
  10
);

-- --------------------------------------------------------------------
-- OPTIONAL “next phase” schedules (disabled by default) to turn on later
-- Fusion then VTON. Flip enabled=true when you’re ready.
-- --------------------------------------------------------------------

insert into marketing_schedules (
  schedule_id, name, enabled,
  freq, hour, minute, dow,
  mode, recipe, persona, industry, tags, season_event, offer, language_hint,
  inputs_json, target_seconds
) values

-- 1x/week Fusion talking reel (disabled initially)
(
  gen_random_uuid(),
  'Cadence Reel (Fusion) - Wed 10am ET (disabled)',
  false,
  'weekly', 15, 0, 'wed',
  'stage',
  'FACE_AUDIO_VIDEO',
  'smb',
  null,
  array['fusion_studio','format_reel','smb'],
  null,
  null,
  'en',
  '{}'::jsonb,
  10
),

-- 1x/week VTON catalog promo reel (disabled initially)
(
  gen_random_uuid(),
  'Cadence Reel (VTON) - Fri 10am ET (disabled)',
  false,
  'weekly', 15, 0, 'fri',
  'stage',
  'FACE_CATALOG_PRODUCT_PROMO',
  'smb',
  'apparel',
  array['commerce','vton','catalog_reels','format_reel','smb'],
  null,
  'New arrivals',
  'en',
  '{}'::jsonb,
  10
);