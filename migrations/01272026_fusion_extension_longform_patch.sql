alter table public.longform_jobs
  add column if not exists voice_gender_mode text not null default 'auto',
  add column if not exists voice_gender text null;

do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conname = 'ck_longform_jobs_voice_gender_mode'
  ) then
    alter table public.longform_jobs
      add constraint ck_longform_jobs_voice_gender_mode
      check (voice_gender_mode in ('auto','manual'));
  end if;

  if not exists (
    select 1 from pg_constraint
    where conname = 'ck_longform_jobs_voice_gender'
  ) then
    alter table public.longform_jobs
      add constraint ck_longform_jobs_voice_gender
      check (voice_gender is null or voice_gender in ('male','female'));
  end if;
end $$;

alter table public.longform_jobs
  add column if not exists voice_gender_mode text,
  add column if not exists voice_gender text;

-- optional: defaults
update public.longform_jobs
set voice_gender_mode = coalesce(voice_gender_mode, 'auto')
where voice_gender_mode is null;

alter table public.longform_jobs
  alter column voice_gender_mode set default 'auto';

-- optional: constraints
alter table public.longform_jobs
  add constraint chk_longform_voice_gender_mode
    check (voice_gender_mode in ('auto','manual'));

alter table public.longform_jobs
  add constraint chk_longform_voice_gender
    check (voice_gender is null or voice_gender in ('male','female'));