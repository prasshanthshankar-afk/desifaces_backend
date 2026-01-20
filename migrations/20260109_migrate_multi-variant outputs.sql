begin;

-- Face outputs must support multiple variants per job
alter table public.face_job_outputs
  add column if not exists variant_number integer,
  add column if not exists prompt_used text,
  add column if not exists negative_prompt text,
  add column if not exists technical_specs jsonb not null default '{}'::jsonb,
  add column if not exists creative_variations jsonb not null default '{}'::jsonb,
  add column if not exists source_asset_id uuid,
  add column if not exists identity_score double precision,
  add column if not exists identity_verified boolean;

-- Backfill existing rows (if any)
update public.face_job_outputs
set variant_number = 1
where variant_number is null;

-- Enforce not null
alter table public.face_job_outputs
  alter column variant_number set not null;

-- Replace UNIQUE(job_id) with UNIQUE(job_id, variant_number)
alter table public.face_job_outputs
  drop constraint if exists uq_face_job_outputs_job;

alter table public.face_job_outputs
  add constraint uq_face_job_outputs_job_variant unique (job_id, variant_number);

-- Indexes
create index if not exists idx_face_job_outputs_job
  on public.face_job_outputs(job_id);

create index if not exists idx_face_job_outputs_job_variant
  on public.face_job_outputs(job_id, variant_number);

create index if not exists idx_face_job_outputs_source_asset
  on public.face_job_outputs(source_asset_id);

-- FK for source_asset_id (only if not already present)
do $$
begin
  if not exists (
    select 1
    from information_schema.table_constraints
    where table_schema='public'
      and table_name='face_job_outputs'
      and constraint_name='face_job_outputs_source_asset_id_fkey'
  ) then
    alter table public.face_job_outputs
      add constraint face_job_outputs_source_asset_id_fkey
      foreign key (source_asset_id) references public.media_assets(id) on delete set null;
  end if;
end $$;

create index if not exists idx_face_job_outputs_profile_created
  on public.face_job_outputs(face_profile_id, created_at desc);

commit;