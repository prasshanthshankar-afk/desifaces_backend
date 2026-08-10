BEGIN;

DO $$
DECLARE
    v_changes bigint := 0;
BEGIN
    ALTER TABLE public.face_generation_regions
        DISABLE TRIGGER trg_face_generation_regions_bump;

    UPDATE public.face_generation_regions
    SET is_active = TRUE
    WHERE left(code, 4) = 'geo_'
      AND geography_type IN ('country', 'subdivision')
      AND is_active = FALSE;

    GET DIAGNOSTICS v_changes = ROW_COUNT;

    ALTER TABLE public.face_generation_regions
        ENABLE TRIGGER trg_face_generation_regions_bump;

    IF v_changes > 0 THEN
        PERFORM public.bump_masterdata_revision('face');
    END IF;
END
$$;

COMMIT;
