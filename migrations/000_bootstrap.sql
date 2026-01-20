-- 000_bootstrap.sql
-- DesiFaces Phase-1 Bootstrap
-- PURPOSE:
--   • Create schemas
--   • Enable required extensions
--   • NOTHING ELSE
--
-- This file MUST be the first migration applied.
-- Safe to re-run. No side effects.

BEGIN;

-- =====================================================
-- Extensions (idempotent)
-- =====================================================
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS citext;     -- optional future email case-insensitive support

-- =====================================================
-- Schemas (idempotent)
-- =====================================================
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS face;
CREATE SCHEMA IF NOT EXISTS fusion;
CREATE SCHEMA IF NOT EXISTS billing;
CREATE SCHEMA IF NOT EXISTS dashboard;
CREATE SCHEMA IF NOT EXISTS tips;
CREATE SCHEMA IF NOT EXISTS admin;

COMMIT;