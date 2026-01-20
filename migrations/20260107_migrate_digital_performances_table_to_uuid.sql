docker compose --env-file ./infra/.env exec -T desifaces-db psql -U desifaces_admin -d desifaces << 'EOF'
BEGIN;

-- Add new UUID column
ALTER TABLE digital_performances ADD COLUMN user_uuid uuid;

-- Delete all existing test data (clean slate)
DELETE FROM digital_performances;

-- Drop the old integer column
ALTER TABLE digital_performances DROP COLUMN user_id;

-- Rename the new column
ALTER TABLE digital_performances RENAME COLUMN user_uuid TO user_id;

-- Make it NOT NULL
ALTER TABLE digital_performances ALTER COLUMN user_id SET NOT NULL;

-- Add foreign key
ALTER TABLE digital_performances 
ADD CONSTRAINT digital_performances_user_id_fkey 
FOREIGN KEY (user_id) REFERENCES core.users(id) ON DELETE CASCADE;

-- Recreate indexes
DROP INDEX IF EXISTS idx_digital_performances_user_created;
DROP INDEX IF EXISTS idx_digital_performances_user_provider_job;
DROP INDEX IF EXISTS idx_digital_performances_user_status;

CREATE INDEX idx_digital_performances_user_created ON digital_performances(user_id, created_at DESC);
CREATE INDEX idx_digital_performances_user_provider_job ON digital_performances(user_id, provider, provider_job_id) WHERE provider_job_id IS NOT NULL;
CREATE INDEX idx_digital_performances_user_status ON digital_performances(user_id, status);

-- Verify
SELECT column_name, data_type FROM information_schema.columns 
WHERE table_name = 'digital_performances' AND column_name = 'user_id';

COMMIT;