-- Migration: Add unique constraint on studio_job_steps (job_id, step_code)
-- Date: 2026-01-07
-- Purpose: Fix ON CONFLICT clause in steps_repo.py upsert_step method
-- This constraint ensures one step per job, allowing upserts to work correctly

-- Check if index exists and drop it
DROP INDEX IF EXISTS uq_studio_job_steps_job_step;

-- Add proper UNIQUE constraint
ALTER TABLE studio_job_steps 
ADD CONSTRAINT uq_studio_job_steps_job_step UNIQUE (job_id, step_code);

-- Verify constraint was added
SELECT constraint_name, constraint_type 
FROM information_schema.table_constraints 
WHERE table_name = 'studio_job_steps' 
AND constraint_name = 'uq_studio_job_steps_job_step';