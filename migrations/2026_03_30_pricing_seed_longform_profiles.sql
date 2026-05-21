-- pricing_skus
INSERT INTO pricing_skus (
  code,
  name,
  unit,
  category,
  provider_hint,
  default_unit_credits,
  status,
  metadata_json
)
VALUES
  (
    'LONGFORM_TALK_MIN',
    'Talking Video (per minute)',
    'minute',
    'fusion_extension',
    'omnihuman',
    100,
    'active',
    jsonb_build_object(
      'longform_profile', 'talking_video',
      'supports_aspect_ratios', jsonb_build_array('16:9', '9:16')
    )
  ),
  (
    'LONGFORM_CINEMATIC_MIN',
    'Cinematic Video Direction (per minute)',
    'minute',
    'fusion_extension',
    'omnihuman+luma+kling',
    300,
    'active',
    jsonb_build_object(
      'longform_profile', 'cinematic_video_direction',
      'supports_aspect_ratios', jsonb_build_array('16:9', '9:16')
    )
  )
ON CONFLICT (code) DO UPDATE SET
  name = EXCLUDED.name,
  unit = EXCLUDED.unit,
  category = EXCLUDED.category,
  provider_hint = EXCLUDED.provider_hint,
  default_unit_credits = EXCLUDED.default_unit_credits,
  status = EXCLUDED.status,
  metadata_json = EXCLUDED.metadata_json;

-- pricing_variants
INSERT INTO pricing_variants (
  code,
  name,
  category,
  is_active,
  metadata_json
)
VALUES
  (
    'TALKING_VIDEO',
    'Talking Video',
    'fusion_extension',
    true,
    jsonb_build_object(
      'longform_profile', 'talking_video',
      'premium', false,
      'supports_aspect_ratios', jsonb_build_array('16:9', '9:16')
    )
  ),
  (
    'CINEMATIC_VIDEO_DIRECTION',
    'Cinematic Video Direction',
    'fusion_extension',
    true,
    jsonb_build_object(
      'longform_profile', 'cinematic_video_direction',
      'premium', true,
      'supports_aspect_ratios', jsonb_build_array('16:9', '9:16')
    )
  )
ON CONFLICT (code) DO UPDATE SET
  name = EXCLUDED.name,
  category = EXCLUDED.category,
  is_active = EXCLUDED.is_active,
  metadata_json = EXCLUDED.metadata_json;

-- pricing_variant_lines
INSERT INTO pricing_variant_lines (
  variant_code,
  sku_code,
  qty_mode,
  qty_param,
  metadata_json
)
VALUES
  (
    'TALKING_VIDEO',
    'LONGFORM_TALK_MIN',
    'param',
    'minutes',
    jsonb_build_object(
      'longform_profile', 'talking_video'
    )
  ),
  (
    'CINEMATIC_VIDEO_DIRECTION',
    'LONGFORM_CINEMATIC_MIN',
    'param',
    'minutes',
    jsonb_build_object(
      'longform_profile', 'cinematic_video_direction'
    )
  )
ON CONFLICT (variant_code, sku_code, qty_mode, qty_param) DO UPDATE SET
  metadata_json = EXCLUDED.metadata_json;