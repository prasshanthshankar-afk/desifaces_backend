-- seed_commerce_component_combinations.sql
-- Seeds DB-driven garment/component combinations + pack requirements in constraints_json.
-- Assumes commerce_garment_components already contains the base component codes.

BEGIN;

-- -----------------------------
-- Guard rails: required codes
-- -----------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM commerce_garment_components WHERE code = 'saree') THEN
    RAISE EXCEPTION 'Missing commerce_garment_components.code = saree';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM commerce_garment_components WHERE code = 'blouse') THEN
    RAISE EXCEPTION 'Missing commerce_garment_components.code = blouse';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM commerce_garment_components WHERE code = 'shirt') THEN
    RAISE EXCEPTION 'Missing commerce_garment_components.code = shirt';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM commerce_garment_components WHERE code = 'pants') THEN
    RAISE EXCEPTION 'Missing commerce_garment_components.code = pants';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM commerce_garment_components WHERE code = 'dress') THEN
    RAISE EXCEPTION 'Missing commerce_garment_components.code = dress';
  END IF;
END $$;

-- -----------------------------
-- Saree set: Recommended pack
-- -----------------------------
INSERT INTO commerce_component_combinations
  (combo_code, display_name, primary_component_code, component_codes, constraints_json)
VALUES
  (
    'saree_set:recommended',
    'Saree Set (Recommended)',
    'saree',
    ARRAY['saree','blouse'],
    jsonb_build_object(
      'outfit_kind', 'saree_set',
      'pack_level', 'recommended',

      -- Optional garment/accessory components that may be present in a listing:
      'optional_component_codes', ARRAY['jewelry_necklace','jewelry_earrings','handbag','shoes'],

      -- What the merchant should ideally provide as *asset roles* for best drape quality:
      -- These map to commerce_product_assets.asset_type (role) and/or media_assets.meta_json.role
      'required_asset_roles', ARRAY['saree_full','pallu_full','border_closeup'],
      'optional_asset_roles', ARRAY['texture_closeup','blouse_piece','worn_ref_front'],

      -- Drape styles the pipeline can support for this outfit kind
      'allowed_drape_styles', ARRAY['nivi','bengali','gujarati','lehenga'],

      -- Notes for UI/vendor guidance
      'notes', 'Recommended: full saree + pallu + border. Optional texture/blouse piece. Worn ref improves realism.'
    )
  )
ON CONFLICT (combo_code) DO NOTHING;

-- -----------------------------
-- Saree set: Best pack
-- -----------------------------
INSERT INTO commerce_component_combinations
  (combo_code, display_name, primary_component_code, component_codes, constraints_json)
VALUES
  (
    'saree_set:best',
    'Saree Set (Best)',
    'saree',
    ARRAY['saree','blouse','jewelry_necklace','jewelry_earrings'],
    jsonb_build_object(
      'outfit_kind', 'saree_set',
      'pack_level', 'best',

      'optional_component_codes', ARRAY['handbag','shoes'],

      -- Best pack expects at least one worn reference (front) if available.
      'required_asset_roles', ARRAY['saree_full','pallu_full','border_closeup','worn_ref_front'],
      'optional_asset_roles', ARRAY['texture_closeup','blouse_piece','worn_ref_side','worn_ref_back','drape_ref_style'],

      'allowed_drape_styles', ARRAY['nivi','bengali','gujarati','lehenga'],

      'notes', 'Best: includes worn_ref_front + optional side/back refs for stronger drape realism.'
    )
  )
ON CONFLICT (combo_code) DO NOTHING;

-- -----------------------------
-- Shirt + Pants: Basic
-- -----------------------------
INSERT INTO commerce_component_combinations
  (combo_code, display_name, primary_component_code, component_codes, constraints_json)
VALUES
  (
    'shirt_pants:basic',
    'Shirt + Pants (Basic)',
    'shirt',
    ARRAY['shirt','pants'],
    jsonb_build_object(
      'outfit_kind', 'shirt_pants_set',
      'pack_level', 'basic',
      'optional_component_codes', ARRAY['shoes','handbag'],
      'required_asset_roles', ARRAY['top_full','bottom_full'],
      'optional_asset_roles', ARRAY['fabric_closeup','worn_ref_front'],
      'notes', 'Basic western set template.'
    )
  )
ON CONFLICT (combo_code) DO NOTHING;

-- -----------------------------
-- Dress: Basic
-- -----------------------------
INSERT INTO commerce_component_combinations
  (combo_code, display_name, primary_component_code, component_codes, constraints_json)
VALUES
  (
    'dress:basic',
    'Dress (Basic)',
    'dress',
    ARRAY['dress'],
    jsonb_build_object(
      'outfit_kind', 'dress',
      'pack_level', 'basic',
      'optional_component_codes', ARRAY['shoes','handbag','jewelry_necklace','jewelry_earrings'],
      'required_asset_roles', ARRAY['dress_full'],
      'optional_asset_roles', ARRAY['fabric_closeup','worn_ref_front'],
      'notes', 'Dress template.'
    )
  )
ON CONFLICT (combo_code) DO NOTHING;

COMMIT;

-- Verify seeded data
select combo_code, primary_component_code, component_codes, constraints_json->>'pack_level' as pack_level
from commerce_component_combinations
order by combo_code;