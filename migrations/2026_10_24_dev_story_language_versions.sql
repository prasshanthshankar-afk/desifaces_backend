-- DEV candidate only. Apply to DEV after review and backup; never run on production.
-- Separate language versions, no mutation of completed source workflow/assets.
BEGIN;

CREATE TABLE IF NOT EXISTS public.v3_story_language_versions (
  language_version_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_workflow_id uuid NOT NULL REFERENCES public.v3_studio_workflows(workflow_id) ON DELETE RESTRICT,
  story_id uuid NOT NULL REFERENCES public.v3_stories(story_id) ON DELETE RESTRICT,
  account_id uuid NOT NULL,
  target_locale text NOT NULL,
  source_fingerprint text NOT NULL,
  version_no integer NOT NULL,
  state text NOT NULL DEFAULT 'draft',
  final_media_id uuid REFERENCES public.media_assets(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_v3_language_version_number CHECK (version_no > 0),
  CONSTRAINT ck_v3_language_version_state CHECK (state IN ('draft','awaiting_hitl','approved','generating','completed','failed')),
  UNIQUE (source_workflow_id,target_locale,version_no)
);
CREATE INDEX IF NOT EXISTS ix_v3_story_language_versions_account
 ON public.v3_story_language_versions(account_id,source_workflow_id,created_at DESC);

CREATE TABLE IF NOT EXISTS public.v3_story_language_turns (
  language_version_id uuid NOT NULL REFERENCES public.v3_story_language_versions(language_version_id) ON DELETE CASCADE,
  source_turn_id uuid NOT NULL REFERENCES public.v3_dialogue_turns(turn_id) ON DELETE RESTRICT,
  speaker_participant_id uuid NOT NULL REFERENCES public.v3_participants(participant_id) ON DELETE RESTRICT,
  source_text text NOT NULL,
  localized_text text,
  state text NOT NULL DEFAULT 'draft',
  approved_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(language_version_id,source_turn_id),
  CONSTRAINT ck_v3_language_turn_state CHECK (state IN ('draft','awaiting_hitl','approved')),
  CONSTRAINT ck_v3_language_turn_source_text CHECK (length(btrim(source_text)) > 0),
  CONSTRAINT ck_v3_language_turn_approved_text CHECK (state <> 'approved' OR (localized_text IS NOT NULL AND length(btrim(localized_text)) > 0))
);
COMMIT;
