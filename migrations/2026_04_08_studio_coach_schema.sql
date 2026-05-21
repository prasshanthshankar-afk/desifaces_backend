-- Studio Coach tip library for DesiFaces.ai
-- DB-backed, request-time ranked, LLM-refreshable offline.

create table if not exists studio_coach_tips (
  id uuid primary key default gen_random_uuid(),
  studio text not null check (studio in ('face','audio','fusion')),
  mode text null,
  locale text not null default 'en',
  title text not null,
  body text not null,
  tone text not null default 'neutral' check (tone in ('neutral','success','warning','premium')),
  priority numeric(8,2) not null default 0,
  source text not null default 'human' check (source in ('human','seed','llm_refresh')),
  targeting_json jsonb not null default '{}'::jsonb,
  tags_json jsonb not null default '{}'::jsonb,
  is_active boolean not null default true,
  expires_at timestamptz null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_studio_coach_tips_active
  on studio_coach_tips (studio, locale, is_active, priority desc);

create index if not exists idx_studio_coach_tips_mode
  on studio_coach_tips (studio, mode, locale)
  where is_active = true;

create index if not exists idx_studio_coach_tips_expires
  on studio_coach_tips (expires_at)
  where expires_at is not null;

create index if not exists gin_studio_coach_tips_targeting
  on studio_coach_tips using gin (targeting_json);

create index if not exists gin_studio_coach_tips_tags
  on studio_coach_tips using gin (tags_json);

create or replace function set_studio_coach_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists trg_studio_coach_updated_at on studio_coach_tips;
create trigger trg_studio_coach_updated_at
before update on studio_coach_tips
for each row execute function set_studio_coach_updated_at();

create table if not exists studio_coach_refresh_runs (
  id uuid primary key default gen_random_uuid(),
  studio text not null check (studio in ('face','audio','fusion')),
  locale text not null default 'en',
  status text not null check (status in ('started','succeeded','failed')),
  model text null,
  prompt_version text null,
  generated_count integer not null default 0,
  accepted_count integer not null default 0,
  rejected_count integer not null default 0,
  notes_json jsonb not null default '{}'::jsonb,
  started_at timestamptz not null default now(),
  completed_at timestamptz null
);

create table if not exists studio_coach_tip_audit (
  id uuid primary key default gen_random_uuid(),
  tip_id uuid null references studio_coach_tips(id) on delete set null,
  studio text not null check (studio in ('face','audio','fusion')),
  action text not null check (action in ('served','clicked_refresh','accepted','dismissed','expired','deactivated')),
  user_id uuid null,
  session_key text null,
  request_features_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists idx_studio_coach_tip_audit_tip
  on studio_coach_tip_audit (tip_id, created_at desc);

create index if not exists idx_studio_coach_tip_audit_user
  on studio_coach_tip_audit (user_id, created_at desc)
  where user_id is not null;

-- Initial seeds: keep concise, high-quality, strongly targeted.
insert into studio_coach_tips (studio, mode, locale, title, body, tone, priority, source, targeting_json, tags_json)
values
('face', 'text-to-image', 'en', 'Lock the framing', 'Use one framing term like headshot, medium shot, or full-body so composition stays consistent.', 'premium', 100, 'seed', '{"missing_fields_any": ["shot_type_code"]}'::jsonb, '{"category":"composition"}'::jsonb),
('face', 'text-to-image', 'en', 'Name the setting', 'Add a clear environment so the background feels intentional instead of generic.', 'neutral', 90, 'seed', '{"missing_fields_any": ["context_code"]}'::jsonb, '{"category":"scene"}'::jsonb),
('face', 'image-to-image', 'en', 'Preserve identity cleanly', 'With identity lock, ask for styling, lighting, attire, and background changes instead of changing the person.', 'success', 110, 'seed', '{"image_safety_state":"passed"}'::jsonb, '{"category":"identity_lock"}'::jsonb),
('face', null, 'en', 'Save credits while testing', 'Try 2 to 4 variants first, then scale up only after the prompt direction feels right.', 'warning', 85, 'seed', '{"plan_in": ["free"], "num_variants_gte": 5}'::jsonb, '{"category":"cost"}'::jsonb),
('audio', null, 'en', 'Prefer shorter sentences', 'Shorter sentences usually sound cleaner and more natural in TTS.', 'premium', 100, 'seed', '{"prompt_length_bucket":"long"}'::jsonb, '{"category":"delivery"}'::jsonb),
('audio', null, 'en', 'Watch character-based cost', 'Enhancement can increase script length. Review the refreshed estimate before you apply the new script.', 'warning', 120, 'seed', '{"has_prompt": true}'::jsonb, '{"category":"pricing"}'::jsonb),
('fusion', null, 'en', 'Keep one emotional arc', 'Give the performer one clear emotional direction instead of stacking multiple moods.', 'premium', 100, 'seed', '{}'::jsonb, '{"category":"performance"}'::jsonb)
on conflict do nothing;
