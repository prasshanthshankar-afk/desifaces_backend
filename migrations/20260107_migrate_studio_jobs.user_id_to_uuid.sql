-- Migration: Change studio_jobs.user_id from integer to UUID
-- Date: 2026-01-07
-- Purpose: Align with core.users.id type and enable proper foreign key relationship

BEGIN;

-- Step 1: Check if there are any rows with user_id = 1 (test data)
-- These need to be mapped to actual UUIDs or deleted
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM studio_jobs WHERE user_id = 1) THEN
        RAISE NOTICE 'Found test jobs with user_id=1. These will be deleted.';
        -- Delete test jobs created during development
        DELETE FROM studio_jobs WHERE user_id = 1;
    END IF;
END $$;

-- Step 2: Add new UUID column
ALTER TABLE studio_jobs ADD COLUMN user_uuid uuid;

-- Step 3: For any remaining rows, try to map integer IDs to UUIDs
-- (This step depends on your data - adjust as needed)
-- If you have no production data, you can skip this

-- Step 4: Drop the old integer column
ALTER TABLE studio_jobs DROP COLUMN user_id;

-- Step 5: Rename the new column to user_id
ALTER TABLE studio_jobs RENAME COLUMN user_uuid TO user_id;

-- Step 6: Make it NOT NULL and add default
ALTER TABLE studio_jobs ALTER COLUMN user_id SET NOT NULL;

-- Step 7: Add foreign key constraint to core.users
ALTER TABLE studio_jobs 
ADD CONSTRAINT studio_jobs_user_id_fkey 
FOREIGN KEY (user_id) REFERENCES core.users(id) ON DELETE CASCADE;

-- Step 8: Add index for performance
CREATE INDEX IF NOT EXISTS idx_studio_jobs_user_id ON studio_jobs(user_id);

-- Verify the change
SELECT 
    column_name, 
    data_type, 
    is_nullable
FROM information_schema.columns 
WHERE table_name = 'studio_jobs' 
AND column_name = 'user_id';

COMMIT;