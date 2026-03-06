-- services/svc-marketing/app/app/db/migrations/005_seed_schedules_cadence.sql
-- Cadence:
--   Mon/Wed/Fri: Reel (Face-first)
--   Tue/Thu:     Story (Face-first)
--   Sun:         Carousel (1–2 slides) (Face-first)
--
-- Time:
--   10:00 AM America/New_York ≈ 15:00 UTC (standard time)

insert into marketing_schedules (
  schedule_id, name, enabled,
  freq, hour, minute, dow,
  mode, recipe, persona, industry, tags, season_event, offer, language_hint,
  inputs_json, target_seconds
) values

-- Mon/Wed/Fri: REELS
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
  '{"format_hint":"reel"}'::jsonb,
  10
),

-- Tue/Thu: STORIES
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
  '{"format_hint":"story"}'::jsonb,
  8
),

-- Sun: CAROUSEL (1–2 slides)
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
  '{"format_hint":"carousel","carousel_slides":2}'::jsonb,
  10
);

-- Optional next-phase schedules (disabled by default)

insert into marketing_schedules (
  schedule_id, name, enabled,
  freq, hour, minute, dow,
  mode, recipe, persona, industry, tags, season_event, offer, language_hint,
  inputs_json, target_seconds
) values

-- Fusion reel (disabled initially)
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
  '{"format_hint":"reel"}'::jsonb,
  10
),

-- VTON catalog reel (disabled initially)
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
  '{"format_hint":"reel"}'::jsonb,
  10
);