-- services/svc-marketing/app/app/db/migrations/007_seed_schedules_youtube.sql
-- YouTube cadence:
--   Tue/Thu: Shorts (repurpose best Face/Fusion demos)
-- Time: 10:00 AM ET ≈ 15:00 UTC (standard time)
--
-- NOTE: Your scheduler only supports daily/weekly, not monthly. We'll add monthly later if you want.

insert into marketing_schedules (
  schedule_id, name, enabled,
  freq, hour, minute, dow,
  mode, recipe, persona, industry, tags, season_event, offer, language_hint,
  inputs_json, target_seconds
) values

-- Tue/Thu Shorts (disabled initially)
(
  gen_random_uuid(),
  'YouTube Shorts (Face-first) - Tue/Thu 10am ET (disabled)',
  false,
  'weekly', 15, 0, 'tue,thu',
  'publish',
  'FACE_AUDIO_VIDEO',
  'creator',
  null,
  array['face_studio','format_reel','yt_short','creator'],
  null,
  null,
  'en',
  '{
    "format_hint":"yt_short",
    "publish_targets":["youtube_short"],
    "youtube_privacy":"public"
  }'::jsonb,
  10
),

-- Optional: 1 weekly long-form compilation (disabled)
(
  gen_random_uuid(),
  'YouTube Long (Compilation) - Sun 10am ET (disabled)',
  false,
  'weekly', 15, 0, 'sun',
  'publish',
  'FACE_AUDIO_VIDEO',
  'creator',
  null,
  array['face_studio','yt_long','creator'],
  null,
  null,
  'en',
  '{
    "format_hint":"yt_long",
    "publish_targets":["youtube_long"],
    "youtube_privacy":"unlisted"
  }'::jsonb,
  60
);