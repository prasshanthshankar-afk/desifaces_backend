-- desifaces-v3 Multi-Person + Story foundation.
--
-- Cardinality rule: the canonical domain is 1..N participants. Legacy
-- single_person/two_people/group labels remain UI/provider compatibility
-- projections and are not persisted as the V3 domain model.

BEGIN;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS public.v3_projects (
  project_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id uuid NOT NULL REFERENCES public.pricing_billing_accounts(id) ON DELETE RESTRICT,
  owner_user_id uuid NOT NULL,
  title text NOT NULL DEFAULT 'Untitled Project',
  description text,
  lifecycle_state text NOT NULL DEFAULT 'active',
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_v3_project_title_nonempty CHECK (length(btrim(title)) > 0),
  CONSTRAINT ck_v3_project_lifecycle CHECK (lifecycle_state IN ('active','archived','deleted')),
  UNIQUE(project_id, account_id)
);
CREATE INDEX IF NOT EXISTS idx_v3_projects_account_updated
  ON public.v3_projects(account_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_v3_projects_owner_updated
  ON public.v3_projects(owner_user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS public.v3_participants (
  participant_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id uuid NOT NULL,
  project_id uuid NOT NULL,
  participant_kind text NOT NULL DEFAULT 'person',
  display_name text,
  description text,
  default_locale text,
  primary_face_media_id uuid REFERENCES public.media_assets(id) ON DELETE SET NULL,
  voice_profile_ref text,
  voice_locale text,
  persona_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  continuity_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  lifecycle_state text NOT NULL DEFAULT 'active',
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT fk_v3_participant_project_account
    FOREIGN KEY(project_id, account_id)
    REFERENCES public.v3_projects(project_id, account_id) ON DELETE CASCADE,
  CONSTRAINT ck_v3_participant_kind CHECK (participant_kind IN ('person','character','pet','other')),
  CONSTRAINT ck_v3_participant_lifecycle CHECK (lifecycle_state IN ('active','archived','deleted'))
);
CREATE INDEX IF NOT EXISTS idx_v3_participants_project_order
  ON public.v3_participants(project_id, created_at, participant_id);
CREATE INDEX IF NOT EXISTS idx_v3_participants_account_updated
  ON public.v3_participants(account_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS public.v3_participant_media (
  participant_id uuid NOT NULL REFERENCES public.v3_participants(participant_id) ON DELETE CASCADE,
  media_id uuid NOT NULL REFERENCES public.media_assets(id) ON DELETE RESTRICT,
  relation text NOT NULL DEFAULT 'reference_face',
  sequence_no integer NOT NULL DEFAULT 0,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(participant_id, media_id, relation),
  CONSTRAINT ck_v3_participant_media_relation CHECK (
    relation IN ('primary_face','reference_face','voice_reference','other')
  ),
  CONSTRAINT ck_v3_participant_media_sequence CHECK (sequence_no >= 0)
);
CREATE INDEX IF NOT EXISTS idx_v3_participant_media_media
  ON public.v3_participant_media(media_id, relation, created_at);

CREATE TABLE IF NOT EXISTS public.v3_stories (
  story_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id uuid NOT NULL,
  project_id uuid NOT NULL,
  title text NOT NULL DEFAULT 'Untitled Story',
  synopsis text,
  default_locale text,
  state text NOT NULL DEFAULT 'draft',
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT fk_v3_story_project_account
    FOREIGN KEY(project_id, account_id)
    REFERENCES public.v3_projects(project_id, account_id) ON DELETE CASCADE,
  CONSTRAINT ck_v3_story_title_nonempty CHECK (length(btrim(title)) > 0),
  CONSTRAINT ck_v3_story_state CHECK (state IN ('draft','ready','generating','succeeded','failed','archived'))
);
CREATE INDEX IF NOT EXISTS idx_v3_stories_project_updated
  ON public.v3_stories(project_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS public.v3_story_participants (
  story_id uuid NOT NULL REFERENCES public.v3_stories(story_id) ON DELETE CASCADE,
  participant_id uuid NOT NULL REFERENCES public.v3_participants(participant_id) ON DELETE RESTRICT,
  sequence_no integer NOT NULL DEFAULT 0,
  role_label text,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(story_id, participant_id),
  CONSTRAINT ck_v3_story_participant_sequence CHECK (sequence_no >= 0)
);
CREATE INDEX IF NOT EXISTS idx_v3_story_participants_order
  ON public.v3_story_participants(story_id, sequence_no, participant_id);

CREATE TABLE IF NOT EXISTS public.v3_scenes (
  scene_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  story_id uuid NOT NULL REFERENCES public.v3_stories(story_id) ON DELETE CASCADE,
  sequence_no integer NOT NULL,
  title text,
  summary text,
  setting_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  direction_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  duration_hint_ms integer,
  state text NOT NULL DEFAULT 'draft',
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_v3_scene_sequence CHECK (sequence_no >= 0),
  CONSTRAINT ck_v3_scene_duration CHECK (duration_hint_ms IS NULL OR duration_hint_ms >= 0),
  CONSTRAINT ck_v3_scene_state CHECK (state IN ('draft','ready','generating','succeeded','failed')),
  UNIQUE(story_id, sequence_no)
);
CREATE INDEX IF NOT EXISTS idx_v3_scenes_story_order
  ON public.v3_scenes(story_id, sequence_no, scene_id);

CREATE TABLE IF NOT EXISTS public.v3_scene_participants (
  scene_id uuid NOT NULL REFERENCES public.v3_scenes(scene_id) ON DELETE CASCADE,
  participant_id uuid NOT NULL REFERENCES public.v3_participants(participant_id) ON DELETE RESTRICT,
  sequence_no integer NOT NULL DEFAULT 0,
  role_label text,
  placement_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  performance_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(scene_id, participant_id),
  CONSTRAINT ck_v3_scene_participant_sequence CHECK (sequence_no >= 0)
);
CREATE INDEX IF NOT EXISTS idx_v3_scene_participants_order
  ON public.v3_scene_participants(scene_id, sequence_no, participant_id);

CREATE TABLE IF NOT EXISTS public.v3_dialogue_turns (
  turn_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scene_id uuid NOT NULL REFERENCES public.v3_scenes(scene_id) ON DELETE CASCADE,
  sequence_no integer NOT NULL,
  turn_kind text NOT NULL DEFAULT 'speech',
  speaker_participant_id uuid REFERENCES public.v3_participants(participant_id) ON DELETE RESTRICT,
  text_value text NOT NULL,
  locale text,
  emotion_code text,
  delivery_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  start_offset_ms integer,
  duration_hint_ms integer,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_v3_dialogue_sequence CHECK (sequence_no >= 0),
  CONSTRAINT ck_v3_dialogue_kind CHECK (turn_kind IN ('speech','narration','action')),
  CONSTRAINT ck_v3_dialogue_text_nonempty CHECK (length(btrim(text_value)) > 0),
  CONSTRAINT ck_v3_dialogue_speech_speaker CHECK (turn_kind <> 'speech' OR speaker_participant_id IS NOT NULL),
  CONSTRAINT ck_v3_dialogue_start CHECK (start_offset_ms IS NULL OR start_offset_ms >= 0),
  CONSTRAINT ck_v3_dialogue_duration CHECK (duration_hint_ms IS NULL OR duration_hint_ms >= 0),
  UNIQUE(scene_id, sequence_no)
);
CREATE INDEX IF NOT EXISTS idx_v3_dialogue_scene_order
  ON public.v3_dialogue_turns(scene_id, sequence_no, turn_id);
CREATE INDEX IF NOT EXISTS idx_v3_dialogue_speaker
  ON public.v3_dialogue_turns(speaker_participant_id, created_at)
  WHERE speaker_participant_id IS NOT NULL;

-- Cross-aggregate integrity: a participant attached to a story/scene must belong
-- to the same canonical project, and a dialogue speaker must be part of the story.
CREATE OR REPLACE FUNCTION public.df_v3_validate_story_participant()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_story_project uuid;
  v_participant_project uuid;
BEGIN
  SELECT project_id INTO v_story_project FROM public.v3_stories WHERE story_id=NEW.story_id;
  SELECT project_id INTO v_participant_project FROM public.v3_participants WHERE participant_id=NEW.participant_id;
  IF v_story_project IS NULL OR v_participant_project IS NULL OR v_story_project <> v_participant_project THEN
    RAISE EXCEPTION 'v3_story_participant_project_mismatch:story=% participant=%', NEW.story_id, NEW.participant_id;
  END IF;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_df_v3_story_participant_validate ON public.v3_story_participants;
CREATE TRIGGER trg_df_v3_story_participant_validate
BEFORE INSERT OR UPDATE OF story_id,participant_id ON public.v3_story_participants
FOR EACH ROW EXECUTE FUNCTION public.df_v3_validate_story_participant();

CREATE OR REPLACE FUNCTION public.df_v3_validate_scene_participant()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_story_id uuid;
BEGIN
  SELECT story_id INTO v_story_id FROM public.v3_scenes WHERE scene_id=NEW.scene_id;
  IF v_story_id IS NULL OR NOT EXISTS (
    SELECT 1 FROM public.v3_story_participants sp
    WHERE sp.story_id=v_story_id AND sp.participant_id=NEW.participant_id
  ) THEN
    RAISE EXCEPTION 'v3_scene_participant_not_in_story:scene=% participant=%', NEW.scene_id, NEW.participant_id;
  END IF;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_df_v3_scene_participant_validate ON public.v3_scene_participants;
CREATE TRIGGER trg_df_v3_scene_participant_validate
BEFORE INSERT OR UPDATE OF scene_id,participant_id ON public.v3_scene_participants
FOR EACH ROW EXECUTE FUNCTION public.df_v3_validate_scene_participant();

CREATE OR REPLACE FUNCTION public.df_v3_validate_dialogue_speaker()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_story_id uuid;
BEGIN
  IF NEW.speaker_participant_id IS NULL THEN
    RETURN NEW;
  END IF;
  SELECT story_id INTO v_story_id FROM public.v3_scenes WHERE scene_id=NEW.scene_id;
  IF v_story_id IS NULL OR NOT EXISTS (
    SELECT 1 FROM public.v3_story_participants sp
    WHERE sp.story_id=v_story_id AND sp.participant_id=NEW.speaker_participant_id
  ) THEN
    RAISE EXCEPTION 'v3_dialogue_speaker_not_in_story:scene=% participant=%', NEW.scene_id, NEW.speaker_participant_id;
  END IF;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_df_v3_dialogue_speaker_validate ON public.v3_dialogue_turns;
CREATE TRIGGER trg_df_v3_dialogue_speaker_validate
BEFORE INSERT OR UPDATE OF scene_id,speaker_participant_id ON public.v3_dialogue_turns
FOR EACH ROW EXECUTE FUNCTION public.df_v3_validate_dialogue_speaker();

CREATE OR REPLACE FUNCTION public.df_v3_validate_participant_media_account()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_participant_account uuid;
  v_media_account uuid;
BEGIN
  SELECT account_id INTO v_participant_account FROM public.v3_participants WHERE participant_id=NEW.participant_id;
  SELECT account_id INTO v_media_account FROM public.media_assets WHERE id=NEW.media_id;
  IF v_participant_account IS NULL OR v_media_account IS NULL OR v_participant_account <> v_media_account THEN
    RAISE EXCEPTION 'v3_participant_media_account_mismatch:participant=% media=%', NEW.participant_id, NEW.media_id;
  END IF;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_df_v3_participant_media_validate ON public.v3_participant_media;
CREATE TRIGGER trg_df_v3_participant_media_validate
BEFORE INSERT OR UPDATE OF participant_id,media_id ON public.v3_participant_media
FOR EACH ROW EXECUTE FUNCTION public.df_v3_validate_participant_media_account();

-- C5 integration: a generation can now be traced to the Story/Scene that caused
-- it. Existing compatibility generation rows remain valid because these are null.
ALTER TABLE public.v3_generation_requests
  ADD COLUMN IF NOT EXISTS story_id uuid,
  ADD COLUMN IF NOT EXISTS scene_id uuid;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_v3_generation_story') THEN
    ALTER TABLE public.v3_generation_requests
      ADD CONSTRAINT fk_v3_generation_story
      FOREIGN KEY(story_id) REFERENCES public.v3_stories(story_id) ON DELETE SET NULL;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_v3_generation_scene') THEN
    ALTER TABLE public.v3_generation_requests
      ADD CONSTRAINT fk_v3_generation_scene
      FOREIGN KEY(scene_id) REFERENCES public.v3_scenes(scene_id) ON DELETE SET NULL;
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_v3_generation_requests_story_created
  ON public.v3_generation_requests(story_id, created_at DESC) WHERE story_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_v3_generation_requests_scene_created
  ON public.v3_generation_requests(scene_id, created_at DESC) WHERE scene_id IS NOT NULL;

CREATE OR REPLACE VIEW public.v3_story_graph_summary AS
SELECT
  s.story_id,
  s.account_id,
  s.project_id,
  s.title,
  s.state,
  count(DISTINCT sp.participant_id)::integer AS participant_count,
  count(DISTINCT sc.scene_id)::integer AS scene_count,
  count(DISTINCT dt.turn_id)::integer AS dialogue_turn_count,
  s.updated_at
FROM public.v3_stories s
LEFT JOIN public.v3_story_participants sp ON sp.story_id=s.story_id
LEFT JOIN public.v3_scenes sc ON sc.story_id=s.story_id
LEFT JOIN public.v3_dialogue_turns dt ON dt.scene_id=sc.scene_id
GROUP BY s.story_id,s.account_id,s.project_id,s.title,s.state,s.updated_at;

COMMIT;
