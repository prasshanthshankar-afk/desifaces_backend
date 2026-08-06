UPDATE public.masterdata_revision
SET revision = GREATEST(revision, 9)
WHERE domain = 'tts';
