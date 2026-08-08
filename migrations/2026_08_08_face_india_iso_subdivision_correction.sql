BEGIN;

DO $$
DECLARE
    v_rows bigint;
    v_changes bigint := 0;
BEGIN
    ALTER TABLE public.face_generation_regions
        DISABLE TRIGGER trg_face_generation_regions_bump;

    DELETE FROM public.face_generation_regions
    WHERE code IN (
        'geo_in_ct',
        'geo_in_or',
        'geo_in_tg',
        'geo_in_ut'
    )
      AND country_code = 'IN'
      AND geography_type = 'subdivision'
      AND is_active = false;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_changes := v_changes + v_rows;

    UPDATE public.face_generation_regions r
    SET subdivision_code = m.subdivision_code
    FROM (
        VALUES
          ('chhattisgarh', 'IN-CT'),
          ('odisha',       'IN-OR'),
          ('telangana',    'IN-TG'),
          ('uttarakhand',  'IN-UT')
    ) AS m(code, subdivision_code)
    WHERE r.code = m.code
      AND r.country_code = 'IN'
      AND r.geography_type = 'subdivision'
      AND r.subdivision_code IS DISTINCT FROM m.subdivision_code;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_changes := v_changes + v_rows;

    ALTER TABLE public.face_generation_regions
        ENABLE TRIGGER trg_face_generation_regions_bump;

    IF v_changes > 0 THEN
        PERFORM public.bump_masterdata_revision('face');
    END IF;
END
$$;

COMMIT;
