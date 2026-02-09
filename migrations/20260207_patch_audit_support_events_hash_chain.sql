CREATE OR REPLACE FUNCTION public._support_events_hash_chain()
RETURNS trigger AS $$
DECLARE
  v_prev bytea;
  v_data text;
BEGIN
  -- ✅ Backfill legacy user_id for safety (before NOT NULL check)
  IF NEW.user_id IS NULL THEN
    IF NEW.actor_type = 'user' THEN
      NEW.user_id := NEW.actor_user_id;
    ELSIF NEW.actor_type = 'admin' THEN
      NEW.user_id := NEW.impersonated_user_id;
      IF NEW.user_id IS NULL THEN
        RAISE EXCEPTION 'admin event requires impersonated_user_id when support_events.user_id is NOT NULL';
      END IF;
    END IF;
  END IF;

  SELECT e.event_hash INTO v_prev
  FROM public.support_events e
  WHERE e.session_id = NEW.session_id
    AND e.event_hash IS NOT NULL
  ORDER BY e.created_at DESC, e.id DESC
  LIMIT 1;

  NEW.prev_hash = v_prev;

  v_data :=
      coalesce(encode(NEW.prev_hash, 'hex'), '') || '|' ||
      coalesce(NEW.session_id::text, '') || '|' ||
      coalesce(NEW.actor_type, '') || '|' ||
      coalesce(NEW.actor_user_id::text, '') || '|' ||
      coalesce(NEW.actor_admin_id::text, '') || '|' ||
      coalesce(NEW.impersonated_user_id::text, '') || '|' ||
      coalesce(NEW.kind, '') || '|' ||
      coalesce(NEW.request_id, '') || '|' ||
      coalesce(NEW.ip::text, '') || '|' ||
      coalesce(NEW.user_agent, '') || '|' ||
      coalesce(NEW.retention_until::text, '') || '|' ||
      coalesce(NEW.payload::text, '') || '|' ||
      coalesce(NEW.created_at::text, '');

  NEW.event_hash = digest(v_data, 'sha256');
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;


ALTER TABLE public.support_events
  ADD CONSTRAINT support_events_actor_user_required
  CHECK (actor_type <> 'user' OR actor_user_id IS NOT NULL) NOT VALID;

ALTER TABLE public.support_events
  ADD CONSTRAINT support_events_actor_admin_required
  CHECK (actor_type <> 'admin' OR actor_admin_id IS NOT NULL) NOT VALID;

-- If you’re keeping the "admin must impersonate" rule:
ALTER TABLE public.support_events
  ADD CONSTRAINT support_events_admin_must_impersonate
  CHECK (actor_type <> 'admin' OR impersonated_user_id IS NOT NULL) NOT VALID;

-- Validate after deploy (safe on large tables):
ALTER TABLE public.support_events VALIDATE CONSTRAINT support_events_actor_user_required;
ALTER TABLE public.support_events VALIDATE CONSTRAINT support_events_actor_admin_required;
ALTER TABLE public.support_events VALIDATE CONSTRAINT support_events_admin_must_impersonate;


ALTER TABLE public.support_events
  ADD CONSTRAINT support_events_actor_user_required
  CHECK (actor_type <> 'user' OR actor_user_id IS NOT NULL) NOT VALID;

ALTER TABLE public.support_events
  ADD CONSTRAINT support_events_actor_admin_required
  CHECK (actor_type <> 'admin' OR actor_admin_id IS NOT NULL) NOT VALID;

ALTER TABLE public.support_events
  ADD CONSTRAINT support_events_admin_must_impersonate
  CHECK (actor_type <> 'admin' OR impersonated_user_id IS NOT NULL) NOT VALID;

ALTER TABLE public.support_events VALIDATE CONSTRAINT support_events_actor_user_required;
ALTER TABLE public.support_events VALIDATE CONSTRAINT support_events_actor_admin_required;
ALTER TABLE public.support_events VALIDATE CONSTRAINT support_events_admin_must_impersonate;