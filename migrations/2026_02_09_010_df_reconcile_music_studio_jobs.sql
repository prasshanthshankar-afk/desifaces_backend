begin;

create or replace function public.df_reconcile_music_studio_jobs()
returns integer
language plpgsql
as $$
declare
  n integer := 0;
begin
  if to_regclass('public.studio_jobs') is null then
    return 0;
  end if;

  update public.studio_jobs sj
  set
    status = mv.status,
    updated_at = now(),
    meta_json = coalesce(sj.meta_json,'{}'::jsonb)
              || jsonb_build_object('reconciled_at', now(), 'svc', 'svc-music')
  from public.music_video_jobs mv
  where mv.id = sj.id
    and sj.studio_type in ('music','MUSIC','music_studio','MUSIC_STUDIO','svc_music','SVC_MUSIC')
    and sj.status in ('running','RUNNING','queued','QUEUED')
    and mv.status in ('succeeded','failed');

  get diagnostics n = row_count;
  return n;
end;
$$;

commit;