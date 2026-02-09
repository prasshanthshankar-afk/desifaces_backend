-- 2026-02-06__support_admin_views_and_verification.sql
-- Adds admin view + helper functions for verifying hash-chain integrity.
-- Safe to run multiple times.

BEGIN;

-- 1) Admin-friendly view (if you already created it, this just replaces definition)
CREATE OR REPLACE VIEW public.v_support_events_admin AS
SELECT
  e.id,
  e.session_id,
  s.user_id          AS session_user_id,
  s.project_id,
  s.job_id,
  s.surface,
  s.status           AS session_status,

  e.actor_type,
  e.actor_user_id,
  e.actor_admin_id,
  e.impersonated_user_id,

  e.kind,
  e.payload,

  e.request_id,
  e.ip,
  e.user_agent,

  e.retention_until,
  encode(e.prev_hash, 'hex')  AS prev_hash_hex,
  encode(e.event_hash, 'hex') AS event_hash_hex,

  e.created_at
FROM public.support_events e
JOIN public.support_sessions s ON s.id = e.session_id;

-- 2) Fast lookup indexes for common admin queries (only create if missing)
-- NOTE: You already have most of these. Keeping them here is harmless.
CREATE INDEX IF NOT EXISTS support_sessions_surface_project_idx
  ON public.support_sessions(surface, project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS support_sessions_surface_user_idx
  ON public.support_sessions(surface, user_id, created_at DESC);

-- 3) Chain verification helper: returns the first broken link (if any) for a session.
--    This is for admin audits / investigations.
CREATE OR REPLACE FUNCTION public.verify_support_event_chain(p_session_id uuid)
RETURNS TABLE(
  ok boolean,
  broken_event_id uuid,
  reason text
) AS $$
DECLARE
  r record;
  prev bytea := NULL;
BEGIN
  FOR r IN
    SELECT id, prev_hash, event_hash
    FROM public.support_events
    WHERE session_id = p_session_id
    ORDER BY created_at ASC, id ASC
  LOOP
    -- First event: prev_hash should be NULL (or empty)
    IF prev IS NULL THEN
      IF r.prev_hash IS NOT NULL THEN
        ok := false;
        broken_event_id := r.id;
        reason := 'first_event_prev_hash_not_null';
        RETURN NEXT;
        RETURN;
      END IF;
    ELSE
      -- Every next event: prev_hash must equal previous event_hash
      IF r.prev_hash IS DISTINCT FROM prev THEN
        ok := false;
        broken_event_id := r.id;
        reason := 'prev_hash_mismatch';
        RETURN NEXT;
        RETURN;
      END IF;
    END IF;

    prev := r.event_hash;
  END LOOP;

  ok := true;
  broken_event_id := NULL;
  reason := NULL;
  RETURN NEXT;
END;
$$ LANGUAGE plpgsql STABLE;

COMMIT;

-- Usage:
--   SELECT * FROM public.v_support_events_admin
--   WHERE project_id='...' ORDER BY created_at DESC LIMIT 200;
--
--   SELECT * FROM public.verify_support_event_chain('<SESSION_UUID>');