alter table public.music_style_presets
  add column if not exists tags text[],

  add column if not exists scene_primary_tag text,
  add column if not exists scene_secondary_tags text[],

  add column if not exists mood_tag text,
  add column if not exists energy_tag text,

  add column if not exists face_mode text,     -- mixed|performance|broll_only|no_face|lyric|abstract
  add column if not exists grade text,         -- canonical grade string (maps to LUT/prompt style)

  add column if not exists shot_cookbook_version int not null default 0,
  add column if not exists shot_cookbook_json jsonb;

create index if not exists idx_music_style_presets_tags
  on public.music_style_presets using gin (tags);

create index if not exists idx_music_style_presets_shot_cookbook_json
  on public.music_style_presets using gin (shot_cookbook_json);