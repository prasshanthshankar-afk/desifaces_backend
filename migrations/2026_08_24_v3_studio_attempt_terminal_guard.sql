-- desifaces-v3 Studio attempt terminal-state guard
-- Canonical invariant: terminal attempts always carry completed_at.
-- Defensive DB enforcement complements explicit service-writer updates.

BEGIN;

CREATE OR REPLACE FUNCTION public.v3_studio_attempt_terminal_completed_at_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.state IN ('succeeded', 'failed', 'canceled', 'cancelled')
       AND NEW.completed_at IS NULL THEN
        NEW.completed_at := now();
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_v3_studio_attempt_terminal_completed_at
ON public.v3_studio_stage_attempts;

CREATE TRIGGER trg_v3_studio_attempt_terminal_completed_at
BEFORE INSERT OR UPDATE OF state, completed_at
ON public.v3_studio_stage_attempts
FOR EACH ROW
EXECUTE FUNCTION public.v3_studio_attempt_terminal_completed_at_guard();

COMMENT ON FUNCTION public.v3_studio_attempt_terminal_completed_at_guard() IS
'desifaces-v3 defensive invariant: terminal Studio attempts always receive completed_at before table constraints are evaluated.';

COMMIT;
