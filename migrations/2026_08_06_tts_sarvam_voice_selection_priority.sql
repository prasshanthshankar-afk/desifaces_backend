BEGIN;

UPDATE public.tts_voice_locale_capabilities vl
SET selection_priority = 0,
    updated_at = now()
FROM public.tts_voices v
WHERE vl.voice_id = v.id
  AND v.provider = 'sarvam';

WITH priorities(locale, voice_name, priority) AS (
VALUES
('hi-IN','shubh',100),('hi-IN','ashutosh',90),
('hi-IN','priya',100),('hi-IN','suhani',90),

('te-IN','shubh',100),('te-IN','ratan',90),
('te-IN','neha',100),('te-IN','priya',90),

('kn-IN','shubh',100),('kn-IN','ratan',90),
('kn-IN','neha',100),('kn-IN','ishita',90),

('bn-IN','rehan',100),
('bn-IN','roopa',100),('bn-IN','suhani',90),

('ta-IN','ratan',100),('ta-IN','rohan',90),
('ta-IN','ishita',100),('ta-IN','ritu',90),

('or-IN','shubh',100),
('or-IN','ritu',100),('or-IN','pooja',90),

('ml-IN','shubh',100),('ml-IN','pooja',100),

('mr-IN','ratan',100),
('mr-IN','priya',100),('mr-IN','ritu',90),

('pa-IN','mani',100),
('pa-IN','roopa',100),('pa-IN','suhani',90),

('gu-IN','ratan',100),
('gu-IN','priya',100),('gu-IN','ritu',90),

('en-IN','ratan',100),
('en-IN','priya',100),('en-IN','ishita',90)
)
UPDATE public.tts_voice_locale_capabilities vl
SET selection_priority = p.priority,
    updated_at = now()
FROM priorities p
JOIN public.tts_voices v
  ON v.provider='sarvam'
 AND lower(v.voice_name)=lower(p.voice_name)
WHERE vl.voice_id=v.id
  AND vl.locale=p.locale;

UPDATE public.masterdata_revision
SET revision=GREATEST(revision,11)
WHERE domain='tts';

COMMIT;
