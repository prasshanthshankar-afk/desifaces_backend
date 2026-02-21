alter table public.music_style_presets
  add column if not exists shot_cookbook_version int not null default 0,
  add column if not exists shot_cookbook_json jsonb;

create index if not exists idx_music_style_presets_shot_cookbook_json
  on public.music_style_presets using gin (shot_cookbook_json);

create index if not exists idx_music_style_presets_shot_cookbook_version
  on public.music_style_presets (shot_cookbook_version);