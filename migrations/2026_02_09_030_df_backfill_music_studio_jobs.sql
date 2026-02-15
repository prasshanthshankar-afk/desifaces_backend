begin;

-- 1) Insert missing studio_jobs rows for existing music_video_jobs
insert into public.studio_jobs(id, studio_type, status, request_hash, payload_json, meta_json, user_id)
select
  mv.id,
  'music',
  mv.status,
  md5(mv.id::text || ':music'),
  coalesce(mv.input_json,'{}'::jsonb),
  jsonb_build_object('source','svc-music','music_project_id', mv.project_id::text, 'request_type','music_video'),
  mp.user_id
from public.music_video_jobs mv
join public.music_projects mp on mp.id = mv.project_id
where not exists (
  select 1 from public.studio_jobs sj
  where sj.id = mv.id
);

-- 2) Reconcile any mismatched statuses (running/queued vs succeeded/failed)
select public.df_reconcile_music_studio_jobs();

commit;