begin;

create extension if not exists pgcrypto;

begin;

drop view if exists v_dashboard_runway_mode_costs;

create view v_dashboard_runway_mode_costs as
with active_modes as (
  select
    drm.id,
    drm.studio,
    drm.mode,
    drm.label,
    lower(drm.display_unit) as display_unit,
    drm.baseline_display_qty::numeric(18,6) as baseline_display_qty,
    drm.variant_code,
    drm.sort_order
  from dashboard_runway_modes drm
  where drm.is_active = true
),
active_variants as (
  select
    pv.code,
    pv.name,
    pv.category
  from pricing_variants pv
  where pv.is_active = true
),
active_skus as (
  select
    ps.code,
    ps.name,
    lower(ps.unit) as sku_unit,
    ps.category as sku_category,
    ps.default_unit_credits::numeric(18,6) as default_unit_credits
  from pricing_skus ps
  where ps.status = 'active'
    and ps.effective_from <= now()
    and (ps.effective_to is null or ps.effective_to > now())
),
joined as (
  select
    am.id,
    am.studio,
    am.mode,
    am.label,
    am.display_unit,
    am.baseline_display_qty,
    am.variant_code,
    av.name as variant_name,
    av.category as variant_category,
    pvl.sku_code,
    lower(pvl.qty_mode) as qty_mode,
    coalesce(pvl.qty_value, 1)::numeric(18,6) as qty_value,
    lower(coalesce(pvl.qty_param, '')) as qty_param,
    asku.default_unit_credits,
    asku.sku_unit,
    asku.sku_category,
    am.sort_order
  from active_modes am
  join active_variants av
    on av.code = am.variant_code
  join pricing_variant_lines pvl
    on pvl.variant_code = am.variant_code
  join active_skus asku
    on asku.code = pvl.sku_code
),
normalized as (
  select
    j.*,
    case
      -- fixed/base charge
      when j.qty_mode = 'fixed' then
        j.qty_value

      -- explicit run/request/job/image/edit semantics
      when j.qty_mode in ('param', 'metered')
       and j.qty_param in (
         'request', 'requests', 'run', 'runs', 'job', 'jobs',
         'image', 'images', 'edit', 'edits',
         'image_run', 'edit_run'
       ) then
        case
          when j.display_unit = 'runs' then j.baseline_display_qty * j.qty_value
          else null
        end

      -- explicit seconds semantics
      when j.qty_mode in ('param', 'metered')
       and j.qty_param in (
         'second', 'seconds', 'sec',
         'duration_sec', 'seconds_requested'
       ) then
        case
          when j.display_unit = 'seconds' then j.baseline_display_qty * j.qty_value
          when j.display_unit = 'minutes' then (j.baseline_display_qty * 60.0) * j.qty_value
          else null
        end

      -- explicit minutes semantics
      when j.qty_mode in ('param', 'metered')
       and j.qty_param in (
         'minute', 'minutes', 'min',
         'duration_min', 'duration_minutes'
       ) then
        case
          when j.display_unit = 'minutes' then j.baseline_display_qty * j.qty_value
          when j.display_unit = 'seconds' then (j.baseline_display_qty / 60.0) * j.qty_value
          else null
        end

      -- explicit character semantics
      when j.qty_mode in ('param', 'metered')
       and j.qty_param in (
         'char', 'chars', 'character', 'characters',
         'kchar', 'kchars', '1k_char', '1k_chars',
         'thousand_chars', 'chars_1k'
       ) then
        case
          when j.qty_param in ('char', 'chars', 'character', 'characters') then
            case
              when j.display_unit = 'chars'  then (j.baseline_display_qty * j.qty_value) / 1000.0
              when j.display_unit = 'kchars' then ((j.baseline_display_qty * 1000.0) * j.qty_value) / 1000.0
              else null
            end
          else
            case
              when j.display_unit = 'kchars' then j.baseline_display_qty * j.qty_value
              when j.display_unit = 'chars'  then (j.baseline_display_qty / 1000.0) * j.qty_value
              else null
            end
        end

      -- fallback from sku.unit
      when j.qty_mode in ('param', 'metered')
       and j.sku_unit in (
         'request', 'requests', 'run', 'runs', 'job', 'jobs',
         'image', 'images', 'edit', 'edits'
       ) then
        case
          when j.display_unit = 'runs' then j.baseline_display_qty * j.qty_value
          else null
        end

      when j.qty_mode in ('param', 'metered')
       and j.sku_unit in ('second', 'seconds', 'sec') then
        case
          when j.display_unit = 'seconds' then j.baseline_display_qty * j.qty_value
          when j.display_unit = 'minutes' then (j.baseline_display_qty * 60.0) * j.qty_value
          else null
        end

      when j.qty_mode in ('param', 'metered')
       and j.sku_unit in ('minute', 'minutes', 'min') then
        case
          when j.display_unit = 'minutes' then j.baseline_display_qty * j.qty_value
          when j.display_unit = 'seconds' then (j.baseline_display_qty / 60.0) * j.qty_value
          else null
        end

      when j.qty_mode in ('param', 'metered')
       and j.sku_unit in (
         'char', 'chars', 'character', 'characters',
         'kchar', 'kchars', '1k_char', '1k_chars',
         'thousand_chars', 'chars_1k'
       ) then
        case
          when j.sku_unit in ('char', 'chars', 'character', 'characters') then
            case
              when j.display_unit = 'chars'  then (j.baseline_display_qty * j.qty_value) / 1000.0
              when j.display_unit = 'kchars' then ((j.baseline_display_qty * 1000.0) * j.qty_value) / 1000.0
              else null
            end
          else
            case
              when j.display_unit = 'kchars' then j.baseline_display_qty * j.qty_value
              when j.display_unit = 'chars'  then (j.baseline_display_qty / 1000.0) * j.qty_value
              else null
            end
        end

      -- strong compatibility fallback using actual live sku_code naming
      when j.qty_mode in ('param', 'metered')
       and upper(j.sku_code) like '%RUN%' then
        case
          when j.display_unit = 'runs' then j.baseline_display_qty * j.qty_value
          else null
        end

      when j.qty_mode in ('param', 'metered')
       and (
         upper(j.sku_code) like '%1K%CHAR%'
         or upper(j.sku_code) like '%KCHAR%'
         or upper(j.sku_code) like '%CHAR%'
       ) then
        case
          when j.display_unit = 'kchars' then j.baseline_display_qty * j.qty_value
          when j.display_unit = 'chars'  then (j.baseline_display_qty / 1000.0) * j.qty_value
          else null
        end

      else null
    end as effective_units_for_baseline
  from joined j
)
select
  n.id,
  n.studio,
  n.mode,
  n.label,
  n.display_unit,
  n.baseline_display_qty,
  n.variant_code,
  max(n.variant_name) as variant_name,
  max(n.variant_category) as variant_category,
  round(
    coalesce(
      sum(n.default_unit_credits * n.effective_units_for_baseline)
      filter (where n.effective_units_for_baseline is not null),
      0
    ),
    4
  ) as estimated_credits_for_baseline_qty,
  round(
    case
      when max(n.baseline_display_qty) > 0 then
        coalesce(
          sum(n.default_unit_credits * n.effective_units_for_baseline)
          filter (where n.effective_units_for_baseline is not null),
          0
        ) / max(n.baseline_display_qty)
      else null
    end,
    4
  ) as estimated_credits_per_display_unit,
  count(*) filter (where n.effective_units_for_baseline is not null) as supported_line_count,
  count(*) filter (where n.effective_units_for_baseline is null) as unsupported_line_count,
  string_agg(distinct n.sku_code, ', ' order by n.sku_code) as source_sku_codes,
  max(n.sort_order) as sort_order
from normalized n
group by
  n.id,
  n.studio,
  n.mode,
  n.label,
  n.display_unit,
  n.baseline_display_qty,
  n.variant_code;

commit;

-- Validation:
-- select studio, mode, label, display_unit, baseline_display_qty, variant_code
-- from dashboard_runway_modes
-- order by sort_order, studio, mode;
--
-- select studio, mode, variant_code, estimated_credits_for_baseline_qty,
--        estimated_credits_per_display_unit, supported_line_count,
--        unsupported_line_count, source_sku_codes
-- from v_dashboard_runway_mode_costs
-- order by sort_order, studio, mode;