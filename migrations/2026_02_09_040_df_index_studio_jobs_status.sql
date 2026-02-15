create index if not exists idx_studio_jobs_type_status_updated_at
on public.studio_jobs(studio_type, status, updated_at desc);