CREATE TABLE IF NOT EXISTS public.face_generation_subject_compositions (
  code text PRIMARY KEY,
  display_name jsonb NOT NULL DEFAULT '{}'::jsonb,
  subject_count int NOT NULL CHECK (subject_count >= 1 AND subject_count <= 12),
  pairing text NOT NULL DEFAULT 'any', -- any|mf|mm|ff|mixed|group
  relationship_type text NOT NULL DEFAULT 'any', -- any|couple|friends|acquaintances|family|team|wedding...
  prompt_tokens text NOT NULL DEFAULT '',
  negative_tokens text NOT NULL DEFAULT '',
  meta_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  sort_order int,
  is_active boolean NOT NULL DEFAULT true
);

-- Phase-1 seed rows (you can tweak names/tokens later)
INSERT INTO public.face_generation_subject_compositions
(code, display_name, subject_count, pairing, relationship_type, prompt_tokens, negative_tokens, sort_order, is_active)
VALUES
('single_person', '{"en":"Single person"}', 1, 'any', 'any',
 'one person, solo portrait, one face', 'two people, group, extra faces', 10, true),

('two_people_any', '{"en":"Two people (any)"}', 2, 'any', 'any',
 'two people in the same frame, both fully visible, two distinct faces', 'single person, merged faces, duplicate face', 20, true),

('two_people_mf', '{"en":"Two people (M+F)"}', 2, 'mf', 'any',
 'one man and one woman in the same frame, both fully visible, two distinct faces', 'single person, merged faces, duplicate face', 21, true),

('two_people_mm', '{"en":"Two people (M+M)"}', 2, 'mm', 'any',
 'two men in the same frame, both fully visible, two distinct faces', 'single person, merged faces, duplicate face', 22, true),

('two_people_ff', '{"en":"Two people (F+F)"}', 2, 'ff', 'any',
 'two women in the same frame, both fully visible, two distinct faces', 'single person, merged faces, duplicate face', 23, true)
ON CONFLICT (code) DO NOTHING;

-- Optional: fast filtering for UI
CREATE INDEX IF NOT EXISTS ix_face_gen_subject_comp_active
ON public.face_generation_subject_compositions (is_active, sort_order);

CREATE INDEX IF NOT EXISTS ix_studio_jobs_payload_subject_comp
ON public.studio_jobs ((payload_json->>'subject_composition_code'));

CREATE INDEX IF NOT EXISTS ix_studio_jobs_meta_request_type
ON public.studio_jobs ((meta_json->>'request_type'));

ALTER TABLE fusion_job_outputs
ADD CONSTRAINT uq_fusion_job_outputs_job_id UNIQUE (job_id);

ALTER TABLE studio_jobs ALTER COLUMN user_prompt TYPE text;