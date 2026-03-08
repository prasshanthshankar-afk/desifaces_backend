BEGIN;

-- Keep ck_pricing_variant_lines_qty_mode; drop the duplicates if they exist
DO $$
BEGIN
  IF EXISTS (select 1 from pg_constraint where conname = 'ck_pricing_variant_qty_mode') THEN
    ALTER TABLE pricing_variant_lines DROP CONSTRAINT ck_pricing_variant_qty_mode;
  END IF;

  IF EXISTS (select 1 from pg_constraint where conname = 'pricing_variant_lines_qty_mode_check') THEN
    ALTER TABLE pricing_variant_lines DROP CONSTRAINT pricing_variant_lines_qty_mode_check;
  END IF;
END $$;

COMMIT;