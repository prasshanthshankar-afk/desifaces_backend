BEGIN;

-- Fix face_profiles.user_id to UUID
ALTER TABLE face_profiles ADD COLUMN user_uuid uuid;
DELETE FROM face_profiles;
ALTER TABLE face_profiles DROP COLUMN user_id;
ALTER TABLE face_profiles RENAME COLUMN user_uuid TO user_id;
ALTER TABLE face_profiles ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE face_profiles ADD CONSTRAINT face_profiles_user_id_fkey FOREIGN KEY (user_id) REFERENCES core.users(id) ON DELETE CASCADE;
CREATE INDEX idx_face_profiles_user_created ON face_profiles(user_id, created_at DESC);
CREATE INDEX idx_face_profiles_user_status ON face_profiles(user_id, status);

-- Fix audio_clips.user_id to UUID
ALTER TABLE audio_clips ADD COLUMN user_uuid uuid;
DELETE FROM audio_clips;
ALTER TABLE audio_clips DROP COLUMN user_id;
ALTER TABLE audio_clips RENAME COLUMN user_uuid TO user_id;
ALTER TABLE audio_clips ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE audio_clips ADD CONSTRAINT audio_clips_user_id_fkey FOREIGN KEY (user_id) REFERENCES core.users(id) ON DELETE CASCADE;
CREATE INDEX idx_audio_clips_user_created ON audio_clips(user_id, created_at DESC);

COMMIT;