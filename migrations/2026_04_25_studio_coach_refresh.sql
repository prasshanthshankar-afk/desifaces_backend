-- Safe, additive migration for DesiFaces Studio Coach serving/audit/LLM refresh.
-- It does not drop or rewrite existing data.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS public.studio_coach_tips (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    studio text NOT NULL,
    mode text NULL,
    locale text NULL DEFAULT 'en',
    title text NOT NULL,
    body text NOT NULL,
    tone text NOT NULL DEFAULT 'neutral',
    priority integer NOT NULL DEFAULT 0,
    source text NULL DEFAULT 'seed',
    targeting_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    tags_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    is_active boolean NOT NULL DEFAULT true,
    expires_at timestamptz NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.studio_coach_tips ADD COLUMN IF NOT EXISTS mode text NULL;
ALTER TABLE public.studio_coach_tips ADD COLUMN IF NOT EXISTS locale text NULL DEFAULT 'en';
ALTER TABLE public.studio_coach_tips ADD COLUMN IF NOT EXISTS source text NULL DEFAULT 'seed';
ALTER TABLE public.studio_coach_tips ADD COLUMN IF NOT EXISTS targeting_json jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE public.studio_coach_tips ADD COLUMN IF NOT EXISTS tags_json jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE public.studio_coach_tips ADD COLUMN IF NOT EXISTS expires_at timestamptz NULL;
ALTER TABLE public.studio_coach_tips ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE public.studio_coach_tips ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

CREATE INDEX IF NOT EXISTS idx_studio_coach_tips_active_lookup
ON public.studio_coach_tips (studio, mode, locale, is_active, priority DESC);

CREATE UNIQUE INDEX IF NOT EXISTS uq_studio_coach_tips_content
ON public.studio_coach_tips (
    studio,
    COALESCE(mode, ''),
    COALESCE(locale, 'en'),
    lower(trim(title)),
    lower(trim(body))
);

CREATE TABLE IF NOT EXISTS public.studio_coach_tip_audit (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NULL,
    studio text NOT NULL,
    mode text NULL,
    locale text NULL DEFAULT 'en',
    source text NULL,
    fallback_used boolean NOT NULL DEFAULT false,
    rotation_key text NULL,
    tip_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    tips jsonb NOT NULL DEFAULT '[]'::jsonb,
    context_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    form_state_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    request_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    response_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    served_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.studio_coach_tip_audit ADD COLUMN IF NOT EXISTS user_id uuid NULL;
ALTER TABLE public.studio_coach_tip_audit ADD COLUMN IF NOT EXISTS mode text NULL;
ALTER TABLE public.studio_coach_tip_audit ADD COLUMN IF NOT EXISTS locale text NULL DEFAULT 'en';
ALTER TABLE public.studio_coach_tip_audit ADD COLUMN IF NOT EXISTS source text NULL;
ALTER TABLE public.studio_coach_tip_audit ADD COLUMN IF NOT EXISTS fallback_used boolean NOT NULL DEFAULT false;
ALTER TABLE public.studio_coach_tip_audit ADD COLUMN IF NOT EXISTS rotation_key text NULL;
ALTER TABLE public.studio_coach_tip_audit ADD COLUMN IF NOT EXISTS tip_ids jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE public.studio_coach_tip_audit ADD COLUMN IF NOT EXISTS tips jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE public.studio_coach_tip_audit ADD COLUMN IF NOT EXISTS context_json jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE public.studio_coach_tip_audit ADD COLUMN IF NOT EXISTS form_state_json jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE public.studio_coach_tip_audit ADD COLUMN IF NOT EXISTS request_json jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE public.studio_coach_tip_audit ADD COLUMN IF NOT EXISTS response_json jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE public.studio_coach_tip_audit ADD COLUMN IF NOT EXISTS served_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE public.studio_coach_tip_audit ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();

CREATE INDEX IF NOT EXISTS idx_studio_coach_tip_audit_lookup
ON public.studio_coach_tip_audit (studio, mode, locale, served_at DESC);

CREATE TABLE IF NOT EXISTS public.studio_coach_refresh_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    studio text NOT NULL,
    mode text NULL,
    locale text NULL DEFAULT 'en',
    status text NOT NULL DEFAULT 'running',
    source text NULL DEFAULT 'llm_refresh',
    provider text NULL,
    model text NULL,
    input_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    output_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_count integer NOT NULL DEFAULT 0,
    updated_count integer NOT NULL DEFAULT 0,
    rejected_count integer NOT NULL DEFAULT 0,
    message text NULL,
    error_message text NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.studio_coach_refresh_runs ADD COLUMN IF NOT EXISTS mode text NULL;
ALTER TABLE public.studio_coach_refresh_runs ADD COLUMN IF NOT EXISTS locale text NULL DEFAULT 'en';
ALTER TABLE public.studio_coach_refresh_runs ADD COLUMN IF NOT EXISTS source text NULL DEFAULT 'llm_refresh';
ALTER TABLE public.studio_coach_refresh_runs ADD COLUMN IF NOT EXISTS provider text NULL;
ALTER TABLE public.studio_coach_refresh_runs ADD COLUMN IF NOT EXISTS model text NULL;
ALTER TABLE public.studio_coach_refresh_runs ADD COLUMN IF NOT EXISTS input_json jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE public.studio_coach_refresh_runs ADD COLUMN IF NOT EXISTS output_json jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE public.studio_coach_refresh_runs ADD COLUMN IF NOT EXISTS created_count integer NOT NULL DEFAULT 0;
ALTER TABLE public.studio_coach_refresh_runs ADD COLUMN IF NOT EXISTS updated_count integer NOT NULL DEFAULT 0;
ALTER TABLE public.studio_coach_refresh_runs ADD COLUMN IF NOT EXISTS rejected_count integer NOT NULL DEFAULT 0;
ALTER TABLE public.studio_coach_refresh_runs ADD COLUMN IF NOT EXISTS message text NULL;
ALTER TABLE public.studio_coach_refresh_runs ADD COLUMN IF NOT EXISTS error_message text NULL;
ALTER TABLE public.studio_coach_refresh_runs ADD COLUMN IF NOT EXISTS started_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE public.studio_coach_refresh_runs ADD COLUMN IF NOT EXISTS finished_at timestamptz NULL;
ALTER TABLE public.studio_coach_refresh_runs ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE public.studio_coach_refresh_runs ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

CREATE INDEX IF NOT EXISTS idx_studio_coach_refresh_runs_lookup
ON public.studio_coach_refresh_runs (studio, mode, locale, created_at DESC);
