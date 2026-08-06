BEGIN;

CREATE TEMP TABLE sv_voice(
  voice_name text PRIMARY KEY,
  gender text NOT NULL,
  is_default boolean NOT NULL
) ON COMMIT DROP;

INSERT INTO sv_voice VALUES
('shubh','male',true),
('aditya','male',false),
('rahul','male',false),
('rohan','male',false),
('amit','male',false),
('dev','male',false),
('ratan','male',false),
('varun','male',false),
('manan','male',false),
('sumit','male',false),
('kabir','male',false),
('aayan','male',false),
('ashutosh','male',false),
('advait','male',false),
('anand','male',false),
('tarun','male',false),
('sunny','male',false),
('mani','male',false),
('gokul','male',false),
('vijay','male',false),
('mohit','male',false),
('rehan','male',false),
('soham','male',false),
('ritu','female',false),
('priya','female',false),
('neha','female',false),
('pooja','female',false),
('simran','female',false),
('kavya','female',false),
('ishita','female',false),
('shreya','female',false),
('roopa','female',false),
('tanya','female',false),
('shruti','female',false),
('suhani','female',false),
('kavitha','female',false),
('rupali','female',false);

CREATE TEMP TABLE sv_locale(locale text PRIMARY KEY) ON COMMIT DROP;
INSERT INTO sv_locale VALUES ('en-IN'), ('hi-IN'), ('bn-IN'), ('ta-IN'), ('te-IN'), ('kn-IN'), ('ml-IN'), ('mr-IN'), ('gu-IN'), ('pa-IN'), ('or-IN');

CREATE TEMP TABLE sv_recommended(
  locale text NOT NULL,
  voice_name text NOT NULL,
  PRIMARY KEY(locale,voice_name)
) ON COMMIT DROP;

INSERT INTO sv_recommended VALUES
('hi-IN','shubh'),
('hi-IN','ashutosh'),
('hi-IN','priya'),
('hi-IN','suhani'),
('te-IN','shubh'),
('te-IN','ratan'),
('te-IN','neha'),
('te-IN','priya'),
('kn-IN','shubh'),
('kn-IN','ratan'),
('kn-IN','neha'),
('kn-IN','ishita'),
('bn-IN','rehan'),
('bn-IN','roopa'),
('bn-IN','suhani'),
('ta-IN','ratan'),
('ta-IN','rohan'),
('ta-IN','ishita'),
('ta-IN','ritu'),
('or-IN','shubh'),
('or-IN','ritu'),
('or-IN','pooja'),
('ml-IN','shubh'),
('ml-IN','pooja'),
('mr-IN','ratan'),
('mr-IN','priya'),
('mr-IN','ritu'),
('pa-IN','mani'),
('pa-IN','roopa'),
('pa-IN','suhani'),
('gu-IN','ratan'),
('gu-IN','priya'),
('gu-IN','ritu'),
('en-IN','ratan'),
('en-IN','priya'),
('en-IN','ishita');

UPDATE public.tts_voices v
SET gender=s.gender,
    locale=NULL,
    voice_type='natural',
    is_default=s.is_default,
    supports_styles=false,
    meta_json=COALESCE(v.meta_json,'{}'::jsonb) ||
      jsonb_build_object(
        'provider_model','bulbul_v3',
        'provider_model_id','bulbul:v3',
        'catalog_source','official_docs',
        'catalog_version','2026-08',
        'multilingual',true
      ),
    updated_at=now()
FROM sv_voice s
WHERE v.provider='sarvam'
  AND lower(v.voice_name)=lower(s.voice_name);

INSERT INTO public.tts_voices(
  provider,voice_name,locale,gender,voice_type,
  is_default,supports_styles,meta_json
)
SELECT
  'sarvam',s.voice_name,NULL,s.gender,'natural',
  s.is_default,false,
  jsonb_build_object(
    'provider_model','bulbul_v3',
    'provider_model_id','bulbul:v3',
    'catalog_source','official_docs',
    'catalog_version','2026-08',
    'multilingual',true
  )
FROM sv_voice s
WHERE NOT EXISTS(
  SELECT 1 FROM public.tts_voices v
  WHERE v.provider='sarvam'
    AND lower(v.voice_name)=lower(s.voice_name)
);

INSERT INTO public.tts_voice_model_capabilities(
  provider_code,voice_id,model_code,
  is_enabled,is_approved,
  supports_styles,supports_emotions,supports_streaming,
  source,source_version,discovered_at,last_seen_at,meta_json
)
SELECT
  'sarvam',v.id,'bulbul_v3',
  true,true,
  false,false,true,
  'official_docs','2026-08',now(),now(),
  jsonb_build_object('provider_model_id','bulbul:v3')
FROM public.tts_voices v
JOIN sv_voice s ON lower(s.voice_name)=lower(v.voice_name)
WHERE v.provider='sarvam'
ON CONFLICT(provider_code,voice_id,model_code)
DO UPDATE SET
  is_enabled=true,
  is_approved=true,
  supports_streaming=true,
  source='official_docs',
  source_version='2026-08',
  last_seen_at=now(),
  updated_at=now();

UPDATE public.tts_voice_locale_capabilities vl
SET is_enabled=true,
    is_approved=true,
    is_native_fit=false,
    is_recommended=EXISTS(
      SELECT 1 FROM sv_recommended r
      WHERE r.locale=sl.locale
        AND lower(r.voice_name)=lower(s.voice_name)
    ),
    quality_score=CASE WHEN EXISTS(
      SELECT 1 FROM sv_recommended r
      WHERE r.locale=sl.locale
        AND lower(r.voice_name)=lower(s.voice_name)
    ) THEN 0.95 ELSE 0.80 END,
    source='official_docs',
    source_version='2026-08',
    last_seen_at=now(),
    meta_json=jsonb_build_object(
      'provider','sarvam',
      'model','bulbul_v3',
      'multilingual',true
    ),
    updated_at=now()
FROM public.tts_voices v
JOIN sv_voice s ON lower(s.voice_name)=lower(v.voice_name)
CROSS JOIN sv_locale sl
WHERE v.provider='sarvam'
  AND vl.voice_id=v.id
  AND vl.locale=sl.locale
  AND vl.accent_code='';

INSERT INTO public.tts_voice_locale_capabilities(
  voice_id,locale,accent_code,
  is_native_fit,is_recommended,
  is_enabled,is_approved,quality_score,
  source,source_version,last_seen_at,meta_json
)
SELECT
  v.id,
  sl.locale,
  '',
  false,
  EXISTS(
    SELECT 1 FROM sv_recommended r
    WHERE r.locale=sl.locale
      AND lower(r.voice_name)=lower(s.voice_name)
  ),
  true,
  true,
  CASE WHEN EXISTS(
    SELECT 1 FROM sv_recommended r
    WHERE r.locale=sl.locale
      AND lower(r.voice_name)=lower(s.voice_name)
  ) THEN 0.95 ELSE 0.80 END,
  'official_docs',
  '2026-08',
  now(),
  jsonb_build_object(
    'provider','sarvam',
    'model','bulbul_v3',
    'multilingual',true
  )
FROM public.tts_voices v
JOIN sv_voice s ON lower(s.voice_name)=lower(v.voice_name)
CROSS JOIN sv_locale sl
WHERE v.provider='sarvam'
AND NOT EXISTS(
  SELECT 1
  FROM public.tts_voice_locale_capabilities x
  WHERE x.voice_id=v.id
    AND x.locale=sl.locale
    AND x.accent_code=''
);

UPDATE public.masterdata_revision
SET revision=GREATEST(revision,3)
WHERE domain='tts';

COMMIT;
