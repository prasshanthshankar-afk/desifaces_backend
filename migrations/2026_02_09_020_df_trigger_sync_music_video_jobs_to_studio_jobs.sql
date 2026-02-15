begin;

create or replace function public.df_sync_studio_job_from_music_video_job()
returns trigger
language plpgsql
as $$
declare
  u uuid;
  meta jsonb;
begin
  if to_regclass('public.studio_jobs') is null then
    return new;
  end if;

  -- Resolve user_id from music_projects
  select mp.user_id into u
  from public.music_projects mp
  where mp.id = new.project_id;

  if u is null then
    return new;
  end if;

  meta := jsonb_build_object(
    'source','svc-music',
    'music_project_id', new.project_id::text,
    'request_type','music_video'
  );

  insert into public.studio_jobs(id, studio_type, status, request_hash, payload_json, meta_json, user_id)
  values(
    new.id,
    'music',
    new.status,
    md5(new.id::text || ':music'),
    coalesce(new.input_json,'{}'::jsonb),
    meta,
    u
  )
  on conflict (id) do update
  set
    status = excluded.status,
    updated_at = now(),
    payload_json = excluded.payload_json,
    meta_json = coalesce(public.studio_jobs.meta_json,'{}'::jsonb) || excluded.meta_json
  where public.studio_jobs.studio_type in ('music','MUSIC','music_studio','MUSIC_STUDIO','svc_music','SVC_MUSIC');

  return new;
end;
$$;

drop trigger if exists trg_df_sync_music_video_jobs_to_studio_jobs on public.music_video_jobs;

create trigger trg_df_sync_music_video_jobs_to_studio_jobs
after insert or update of status, input_json
on public.music_video_jobs
for each row
execute function public.df_sync_studio_job_from_music_video_job();

commit;