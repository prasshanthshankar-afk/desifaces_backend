begin;

-- -------------------------
-- 1) Cache table (one row per user)
-- -------------------------
create table if not exists public.dashboard_home_cache (
  user_id uuid primary key,
  updated_at timestamptz not null default now(),

  gauges_json jsonb not null default '{}'::jsonb,
  alerts_json jsonb not null default '[]'::jsonb,

  face_carousel_json jsonb not null default '[]'::jsonb,
  video_carousel_json jsonb not null default '[]'::jsonb,

  header_json jsonb not null default '{}'::jsonb
);

create index if not exists idx_dashboard_home_cache_updated_at
  on public.dashboard_home_cache(updated_at desc);

-- -------------------------
-- 2) Debounced refresh queue (one row per user)
-- -------------------------
create table if not exists public.dashboard_refresh_requests (
  user_id uuid primary key,
  requested_at timestamptz not null default now(),
  reason text not null default 'unknown'
);

create index if not exists idx_dashboard_refresh_requests_requested_at
  on public.dashboard_refresh_requests(requested_at asc);

-- -------------------------
-- 3) Provider health (optional)
-- -------------------------
create table if not exists public.dashboard_provider_health (
  provider_code text not null,
  region text not null default 'global',
  status text not null check (status in ('ok','degraded','down')),
  message text,
  updated_at timestamptz not null default now(),
  primary key (provider_code, region)
);

create index if not exists idx_dashboard_provider_health_status
  on public.dashboard_provider_health(status);

-- -------------------------
-- 4) Index hardening (prod)
-- -------------------------
create index if not exists idx_studio_jobs_user_status_updated
  on public.studio_jobs(user_id, status, updated_at desc);

create index if not exists idx_studio_jobs_user_type_status_updated
  on public.studio_jobs(user_id, studio_type, status, updated_at desc);

-- artifacts already has idx_artifacts_job_kind_created; keep a kind-wide index too
create index if not exists idx_artifacts_kind_created_at
  on public.artifacts(kind, created_at desc);

-- for dashboard temp + carousels
create index if not exists idx_artifacts_job_kind_created_asc
  on public.artifacts(job_id, kind, created_at);

-- for dashboard temp
do $$
begin
  if to_regclass('public.provider_runs') is not null then
    execute 'create index if not exists idx_provider_runs_job_created
             on public.provider_runs(job_id, created_at)';
  end if;
end;
$$;

-- ============================================================
-- FUNCTIONS
-- ============================================================

-- Enqueue refresh (upsert per user)
create or replace function public.fn_dashboard_enqueue_refresh(p_user_id uuid, p_reason text)
returns void
language plpgsql
as $$
begin
  insert into public.dashboard_refresh_requests(user_id, requested_at, reason)
  values (p_user_id, now(), coalesce(p_reason,'unknown'))
  on conflict (user_id)
  do update
     set requested_at = excluded.requested_at,
         reason      = excluded.reason;
end;
$$;

-- Credits remaining (best-effort; optional table)
create or replace function public.fn_dashboard_credits_remaining(p_user_id uuid)
returns numeric
language plpgsql
as $$
declare
  v numeric := 0;
begin
  if to_regclass('public.credit_ledger') is null then
    return 0;
  end if;

  execute
    'select coalesce(sum(l.delta_credits), 0) from public.credit_ledger l where l.user_id = $1'
  into v
  using p_user_id;

  return coalesce(v, 0);
exception when undefined_column then
  return 0;
end;
$$;

-- Compute gauges (vehicle cluster)
create or replace function public.fn_dashboard_compute_gauges(p_user_id uuid)
returns jsonb
language plpgsql
as $$
declare
  v_now timestamptz := now();

  -- live load
  v_running int := 0;
  v_queued int := 0;

  -- throughput
  v_face_done_15m int := 0;
  v_fusion_done_15m int := 0;
  v_face_done_60m int := 0;
  v_fusion_done_60m int := 0;

  -- activity timestamps (within last 24h to avoid "stale forever")
  v_last_done_at timestamptz := null;
  v_last_job_touch_at timestamptz := null;

  -- health windows
  v_success_24h int := 0;
  v_failed_24h int := 0;
  v_failed_2h int := 0;

  -- temp
  v_p95_latency_sec numeric := 0;
  v_latency_cap_sec numeric := 7200; -- mapping cap for curve

  -- fuel
  v_credits_remaining numeric := 0;
  v_credit_cap numeric := null;

  -- provider health (optional)
  v_provider_min text := 'ok';
  v_health_score numeric := 1.0;

  -- normalized 0..1
  n_velocity numeric := 0;
  n_rpm numeric := 0;
  n_fuel numeric := 0;
  n_temp numeric := 0;
  n_health numeric := 0;

  -- helpers
  v_velocity_raw numeric := 0;
  v_decay numeric := 1.0;
  v_idle_minutes numeric := 0;

begin
  -- -----------------------
  -- Live load (all studio types)
  -- -----------------------
  select count(*) into v_running
  from public.studio_jobs j
  where j.user_id = p_user_id and j.status = 'running';

  select count(*) into v_queued
  from public.studio_jobs j
  where j.user_id = p_user_id and j.status = 'queued';

  -- -----------------------
  -- Completions: last 15m / 60m
  -- -----------------------
  select count(*) into v_face_done_15m
  from public.studio_jobs j
  where j.user_id = p_user_id
    and j.status = 'succeeded'
    and j.studio_type = 'face'
    and j.updated_at >= (v_now - interval '15 minutes');

  select count(*) into v_fusion_done_15m
  from public.studio_jobs j
  where j.user_id = p_user_id
    and j.status = 'succeeded'
    and j.studio_type = 'fusion'
    and j.updated_at >= (v_now - interval '15 minutes');

  select count(*) into v_face_done_60m
  from public.studio_jobs j
  where j.user_id = p_user_id
    and j.status = 'succeeded'
    and j.studio_type = 'face'
    and j.updated_at >= (v_now - interval '60 minutes');

  select count(*) into v_fusion_done_60m
  from public.studio_jobs j
  where j.user_id = p_user_id
    and j.status = 'succeeded'
    and j.studio_type = 'fusion'
    and j.updated_at >= (v_now - interval '60 minutes');

  -- -----------------------
  -- Activity timestamps (recent-only)
  -- -----------------------
  select max(j.updated_at) into v_last_done_at
  from public.studio_jobs j
  where j.user_id = p_user_id
    and j.status in ('succeeded','failed')
    and j.updated_at >= (v_now - interval '24 hours');

  select max(j.updated_at) into v_last_job_touch_at
  from public.studio_jobs j
  where j.user_id = p_user_id
    and j.updated_at >= (v_now - interval '24 hours');

  -- -----------------------
  -- Health 24h
  -- -----------------------
  select count(*) into v_success_24h
  from public.studio_jobs j
  where j.user_id = p_user_id
    and j.status = 'succeeded'
    and j.updated_at >= (v_now - interval '24 hours');

  select count(*) into v_failed_24h
  from public.studio_jobs j
  where j.user_id = p_user_id
    and j.status = 'failed'
    and j.updated_at >= (v_now - interval '24 hours');

  select count(*) into v_failed_2h
  from public.studio_jobs j
  where j.user_id = p_user_id
    and j.status = 'failed'
    and j.updated_at >= (v_now - interval '2 hours');

  -- -----------------------
  -- TEMP (Engine Heat): USER-SCOPED p95 blended duration
  -- duration per job =
  --   (first_output_at OR job.updated_at) - (first_provider_run_at OR job.created_at)
  -- Works even when face/audio do not log provider_runs yet.
  -- -----------------------
  if to_regclass('public.artifacts') is not null then
    select coalesce(
      percentile_cont(0.95) within group (order by dur_sec),
      0
    )
    into v_p95_latency_sec
    from (
      with user_jobs as (
        select j.id, j.created_at, j.updated_at
        from public.studio_jobs j
        where j.user_id = p_user_id
          and j.status in ('succeeded','failed')
          and j.updated_at >= (v_now - interval '24 hours')
      ),
      pr as (
        select job_id, min(created_at) as first_provider_at
        from public.provider_runs
        where job_id in (select id from user_jobs)
        group by job_id
      ),
      out_art as (
        select job_id, min(created_at) as first_out_at
        from public.artifacts
        where job_id in (select id from user_jobs)
          and kind in ('face_image','video','audio')
        group by job_id
      )
      select
        greatest(
          0,
          extract(epoch from (
            coalesce(out_art.first_out_at, uj.updated_at)
            -
            coalesce(pr.first_provider_at, uj.created_at)
          ))
        ) as dur_sec
      from user_jobs uj
      left join pr on pr.job_id = uj.id
      left join out_art on out_art.job_id = uj.id
    ) t;
  else
    v_p95_latency_sec := 0;
  end if;

  -- add load contribution so temp moves under load
  v_p95_latency_sec := greatest(0, v_p95_latency_sec + (v_running * 3) + (v_queued * 1));

  -- cap raw display (avoid insane numbers in UI)
  v_p95_latency_sec := least(v_p95_latency_sec, 3600); -- show up to 1h

  -- Normalize temp with log curve + gamma (prevents pegging)
  n_temp := least(
    1,
    greatest(
      0,
      power(
        ln(1 + least(v_p95_latency_sec, v_latency_cap_sec)) / ln(1 + v_latency_cap_sec),
        1.7
      )
    )
  );

  -- -----------------------
  -- Fuel (best effort)
  -- -----------------------
  v_credits_remaining := public.fn_dashboard_credits_remaining(p_user_id);

  -- provider min (optional)
  if to_regclass('public.dashboard_provider_health') is not null then
    select
      case
        when exists(select 1 from public.dashboard_provider_health where status = 'down') then 'down'
        when exists(select 1 from public.dashboard_provider_health where status = 'degraded') then 'degraded'
        else 'ok'
      end
    into v_provider_min;
  else
    v_provider_min := 'ok';
  end if;

  -- -----------------------
  -- Velocity: throughput + live load, decays with idle time
  -- -----------------------
  v_velocity_raw :=
    (v_face_done_15m * 1.0) +
    (v_fusion_done_15m * 3.0) +
    (v_running * 2.0) +
    (v_queued * 0.5);

  if v_last_done_at is not null then
    v_idle_minutes := extract(epoch from (v_now - v_last_done_at)) / 60.0;
  else
    v_idle_minutes := 9999;
  end if;

  if v_idle_minutes <= 10 then
    v_decay := 1.0;
  else
    v_decay := greatest(0.05, 1.0 - ((v_idle_minutes - 10.0) / 110.0));
  end if;

  n_velocity := least(1, greatest(0, (v_velocity_raw / 25.0))) * v_decay;

  -- RPM normalize
  n_rpm := least(1, greatest(0, (v_running + (0.5*v_queued)) / 20.0));

  -- Fuel normalize
  if v_credit_cap is not null and v_credit_cap > 0 then
    n_fuel := least(1, greatest(0, v_credits_remaining / v_credit_cap));
  else
    n_fuel := least(1, greatest(0, v_credits_remaining / 1000.0));
  end if;

  -- Health: 24h success ratio, penalize recent failures + provider status
  if (v_success_24h + v_failed_24h) > 0 then
    v_health_score := v_success_24h::numeric / (v_success_24h + v_failed_24h);
  else
    v_health_score := 1.0;
  end if;

  if v_failed_2h > 0 then
    v_health_score := v_health_score - least(0.25, v_failed_2h * 0.05);
  end if;

  if v_provider_min = 'down' then
    v_health_score := v_health_score * 0.4;
  elsif v_provider_min = 'degraded' then
    v_health_score := v_health_score * 0.8;
  end if;

  n_health := least(1, greatest(0, v_health_score));

  return jsonb_build_object(
    'speedometer', jsonb_build_object(
      'label', 'Velocity',
      'faces_last_60m', v_face_done_60m,
      'videos_last_60m', v_fusion_done_60m,
      'raw', v_velocity_raw,
      'decay', v_decay,
      'last_done_at', v_last_done_at,
      'last_touch_at', v_last_job_touch_at,
      'value_norm', n_velocity
    ),
    'rpm', jsonb_build_object(
      'label', 'Intensity',
      'running', v_running,
      'queued', v_queued,
      'last_touch_at', v_last_job_touch_at,
      'value_norm', n_rpm
    ),
    'fuel', jsonb_build_object(
      'label', 'Credits',
      'credits_remaining', v_credits_remaining,
      'cap', v_credit_cap,
      'value_norm', n_fuel
    ),
    'temp', jsonb_build_object(
      'label', 'Engine Temp',
      'p95_latency_sec', v_p95_latency_sec,
      'last_touch_at', v_last_job_touch_at,
      'value_norm', n_temp
    ),
    'health', jsonb_build_object(
      'label', 'Health',
      'provider', v_provider_min,
      'success_24h', v_success_24h,
      'failed_24h', v_failed_24h,
      'failed_2h', v_failed_2h,
      'value_norm', n_health
    )
  );
end;
$$;

-- Carousels (aligned to your artifacts.kind)
create or replace function public.fn_dashboard_compute_carousels(p_user_id uuid, p_limit int default 5)
returns jsonb
language plpgsql
as $$
declare
  faces jsonb := '[]'::jsonb;
  videos jsonb := '[]'::jsonb;
begin
  -- Faces: face_image
  select coalesce(jsonb_agg(item), '[]'::jsonb) into faces
  from (
    select jsonb_build_object(
      'artifact_id', a.id,
      'image_url', a.url,
      'created_at', a.created_at,
      'meta', a.meta_json
    ) as item
    from public.artifacts a
    join public.studio_jobs j on j.id = a.job_id
    where j.user_id = p_user_id
      and j.studio_type = 'face'
      and a.kind = 'face_image'
    order by a.created_at desc
    limit p_limit
  ) s;

  -- Videos: video
  select coalesce(jsonb_agg(item), '[]'::jsonb) into videos
  from (
    select jsonb_build_object(
      'artifact_id', a.id,
      'video_url', a.url,
      'created_at', a.created_at,
      'meta', a.meta_json
    ) as item
    from public.artifacts a
    join public.studio_jobs j on j.id = a.job_id
    where j.user_id = p_user_id
      and j.studio_type = 'fusion'
      and a.kind = 'video'
    order by a.created_at desc
    limit p_limit
  ) s;

  return jsonb_build_object('faces', faces, 'videos', videos);
end;
$$;

create or replace function public.fn_dashboard_compute_alerts(p_gauges jsonb)
returns jsonb
language plpgsql
as $$
declare
  alerts jsonb := '[]'::jsonb;
  fuel_norm numeric := coalesce((p_gauges #>> '{fuel,value_norm}')::numeric, 1);
  health_norm numeric := coalesce((p_gauges #>> '{health,value_norm}')::numeric, 1);
  temp_norm numeric := coalesce((p_gauges #>> '{temp,value_norm}')::numeric, 0);
begin
  if fuel_norm < 0.15 then
    alerts := alerts || jsonb_build_object('code','low_fuel','severity','warn','message','Low credits remaining');
  end if;

  if health_norm < 0.85 then
    alerts := alerts || jsonb_build_object('code','engine_health','severity','warn','message','System health is degraded');
  end if;

  if temp_norm > 0.80 then
    alerts := alerts || jsonb_build_object('code','engine_temp','severity','warn','message','High latency detected');
  end if;

  return alerts;
end;
$$;

create or replace function public.fn_dashboard_refresh_home_cache(p_user_id uuid)
returns void
language plpgsql
as $$
declare
  g jsonb;
  c jsonb;
  a jsonb;
  header jsonb;
begin
  g := public.fn_dashboard_compute_gauges(p_user_id);
  c := public.fn_dashboard_compute_carousels(p_user_id, 5);
  a := public.fn_dashboard_compute_alerts(g);

  header := jsonb_build_object(
    'velocity', g->'speedometer',
    'fuel', g->'fuel',
    'health', g->'health'
  );

  insert into public.dashboard_home_cache(
    user_id, updated_at, gauges_json, alerts_json, face_carousel_json, video_carousel_json, header_json
  )
  values (
    p_user_id,
    now(),
    g,
    a,
    coalesce(c->'faces','[]'::jsonb),
    coalesce(c->'videos','[]'::jsonb),
    header
  )
  on conflict (user_id)
  do update set
    updated_at = excluded.updated_at,
    gauges_json = excluded.gauges_json,
    alerts_json = excluded.alerts_json,
    face_carousel_json = excluded.face_carousel_json,
    video_carousel_json = excluded.video_carousel_json,
    header_json = excluded.header_json;
end;
$$;

create or replace view public.v_dashboard_home as
select
  user_id,
  updated_at,
  gauges_json,
  alerts_json,
  face_carousel_json,
  video_carousel_json,
  header_json
from public.dashboard_home_cache;

-- ============================================================
-- TRIGGERS
-- ============================================================

-- studio_jobs → enqueue refresh
create or replace function public.trg_dashboard_on_studio_jobs()
returns trigger
language plpgsql
as $$
begin
  perform public.fn_dashboard_enqueue_refresh(new.user_id, 'studio_jobs');
  return new;
end;
$$;

drop trigger if exists trg_dashboard_studio_jobs on public.studio_jobs;
create trigger trg_dashboard_studio_jobs
after insert or update of status, updated_at on public.studio_jobs
for each row execute function public.trg_dashboard_on_studio_jobs();

-- artifacts → lookup job.user_id → enqueue refresh
create or replace function public.trg_dashboard_on_artifacts()
returns trigger
language plpgsql
as $$
declare
  v_user_id uuid;
begin
  select j.user_id into v_user_id
  from public.studio_jobs j
  where j.id = new.job_id;

  if v_user_id is not null then
    perform public.fn_dashboard_enqueue_refresh(v_user_id, 'artifacts');
  end if;

  return new;
end;
$$;

drop trigger if exists trg_dashboard_artifacts on public.artifacts;
create trigger trg_dashboard_artifacts
after insert on public.artifacts
for each row execute function public.trg_dashboard_on_artifacts();

-- credit_ledger → enqueue refresh (optional)
create or replace function public.trg_dashboard_on_credit_ledger()
returns trigger
language plpgsql
as $$
begin
  perform public.fn_dashboard_enqueue_refresh(new.user_id, 'credit_ledger');
  return new;
end;
$$;

do $$
begin
  if to_regclass('public.credit_ledger') is not null then
    execute 'drop trigger if exists trg_dashboard_credit_ledger on public.credit_ledger';
    execute 'create trigger trg_dashboard_credit_ledger
             after insert on public.credit_ledger
             for each row execute function public.trg_dashboard_on_credit_ledger()';
  end if;
end;
$$;

commit;