-- V3 Multi-Person + Story hardening.
-- Enforce account ownership for Participant.primary_face_media_id even when a
-- caller does not wrap persistence in a larger application transaction.

BEGIN;

CREATE OR REPLACE FUNCTION public.df_v3_validate_participant_primary_face_account()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_media_account uuid;
BEGIN
  IF NEW.primary_face_media_id IS NULL THEN
    RETURN NEW;
  END IF;

  SELECT account_id INTO v_media_account
  FROM public.media_assets
  WHERE id=NEW.primary_face_media_id;

  IF v_media_account IS NULL OR v_media_account <> NEW.account_id THEN
    RAISE EXCEPTION 'v3_participant_primary_face_account_mismatch:participant=% media=%',
      NEW.participant_id, NEW.primary_face_media_id;
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_df_v3_participant_primary_face_validate ON public.v3_participants;
CREATE TRIGGER trg_df_v3_participant_primary_face_validate
BEFORE INSERT OR UPDATE OF account_id,primary_face_media_id ON public.v3_participants
FOR EACH ROW EXECUTE FUNCTION public.df_v3_validate_participant_primary_face_account();

COMMIT;
