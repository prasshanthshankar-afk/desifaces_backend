BEGIN;

-- Normalize historical version-qualified OmniHuman provider identifiers to the
-- stable product/runtime identity "omnihuman". This is data compatibility only.
-- New runtime code writes only the stable identity.

UPDATE public.studio_jobs
SET
  payload_json = CASE
    WHEN COALESCE(payload_json->>'provider','') LIKE 'omnihuman_%'
      THEN jsonb_set(COALESCE(payload_json,'{}'::jsonb), '{provider}', '"omnihuman"'::jsonb, true)
    ELSE payload_json
  END,
  meta_json = CASE
    WHEN COALESCE(meta_json->>'provider','') LIKE 'omnihuman_%'
      THEN jsonb_set(COALESCE(meta_json,'{}'::jsonb), '{provider}', '"omnihuman"'::jsonb, true)
    ELSE meta_json
  END,
  updated_at = now()
WHERE studio_type='fusion'
  AND (
    COALESCE(payload_json->>'provider','') LIKE 'omnihuman_%'
    OR COALESCE(meta_json->>'provider','') LIKE 'omnihuman_%'
  );

UPDATE public.provider_runs
SET provider='omnihuman',
    updated_at=now()
WHERE provider LIKE 'omnihuman_%';

UPDATE public.digital_performances
SET provider='omnihuman',
    updated_at=now()
WHERE provider LIKE 'omnihuman_%';

COMMIT;
