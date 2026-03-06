# services/svc-marketing/app/app/repos/festival_calendar_repo.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import asyncpg


@dataclass(frozen=True)
class FestivalPick:
    festival_id: str
    scope_id: str
    occurrence_id: str

    slug: str
    name: str

    country_code: str
    region_code: Optional[str]
    religion: Optional[str]
    locale: Optional[str]
    observance_variant: Optional[str]
    timezone: str

    category: str
    lead_days: int
    lag_days: int
    priority: int

    festival_date: date

    motifs: Dict[str, Any]
    sources: List[Dict[str, Any]]


class FestivalCalendarRepo:
    """
    Scope-aware, production-safe selector:
      - Never returns festivals "after the fact" unless lag_days allows it (default 0).
      - Prefers exact matches for region/religion/locale/variant.
      - Uses lead_days window to ensure promos are generated before/on festival day.
    """

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def pick_best_for_day(
        self,
        *,
        today: date,
        country_code: str,
        region_code: Optional[str] = None,
        religion: Optional[str] = None,
        locale: Optional[str] = None,
        observance_variant: Optional[str] = None,
        lookahead_days: int = 10,
        lookback_days: int = 3,
        strict_when_none: bool = True,
    ) -> Optional[FestivalPick]:
        """
        strict_when_none=True:
          - if region_code is None -> only consider scopes where region_code IS NULL (same for religion/locale/variant)
        If False:
          - when region_code is None, scopes with region_code NULL OR non-NULL are considered (but NULL is preferred).
        """

        cc = (country_code or "").strip().upper()
        if not cc:
            return None

        # window around today so we can match lead/lag with small scan
        start_date = today - timedelta(days=int(lookback_days))
        end_date = today + timedelta(days=int(lookahead_days))

        q = """
        with c as (
          select
            d.festival_id::text as festival_id,
            s.scope_id::text as scope_id,
            o.occurrence_id::text as occurrence_id,

            d.slug as slug,
            d.name as name,

            s.country_code as country_code,
            s.region_code as region_code,
            s.religion as religion,
            s.locale as locale,
            s.observance_variant as observance_variant,
            s.timezone as timezone,

            d.category as category,
            d.lead_days as lead_days,
            d.lag_days as lag_days,
            d.priority as priority,

            o.festival_date as festival_date,
            d.motifs_json as motifs,
            o.sources_json as sources,

            -- eligibility: today must be within [festival_date - lead_days, festival_date + lag_days]
            ( $7::date between (o.festival_date - (d.lead_days::int * interval '1 day'))
                         and (o.festival_date + (d.lag_days::int * interval '1 day')) ) as is_eligible,

            -- prefer not-past if multiple eligible (festival_date < today)
            case when o.festival_date < $7::date then 1 else 0 end as is_past,

            -- specificity scoring: higher is better
            (case when $2::text is not null and s.region_code = $2::text then 16 else 0 end) +
            (case when $3::text is not null and s.religion = $3::text then 8 else 0 end) +
            (case when $4::text is not null and s.locale = $4::text then 4 else 0 end) +
            (case when $5::text is not null and s.observance_variant = $5::text then 2 else 0 end) +
            (case when s.region_code is null then 1 else 0 end) +
            (case when s.religion is null then 1 else 0 end) +
            (case when s.locale is null then 1 else 0 end) +
            (case when s.observance_variant is null then 1 else 0 end)
            as specificity

          from marketing_festival_occurrences o
          join marketing_festival_definitions d on d.festival_id = o.festival_id
          join marketing_festival_scopes s on s.scope_id = o.scope_id
          where d.enabled = true
            and s.enabled = true
            and s.country_code = $1
            and o.festival_date >= $6::date
            and o.festival_date <= $8::date

            -- region filter behavior
            and (
              case
                when $2::text is null and $9::bool then s.region_code is null
                when $2::text is null and not $9::bool then true
                else (s.region_code is null or s.region_code = $2::text)
              end
            )

            -- religion filter behavior
            and (
              case
                when $3::text is null and $9::bool then s.religion is null
                when $3::text is null and not $9::bool then true
                else (s.religion is null or s.religion = $3::text)
              end
            )

            -- locale filter behavior
            and (
              case
                when $4::text is null and $9::bool then s.locale is null
                when $4::text is null and not $9::bool then true
                else (s.locale is null or s.locale = $4::text)
              end
            )

            -- variant filter behavior
            and (
              case
                when $5::text is null and $9::bool then s.observance_variant is null
                when $5::text is null and not $9::bool then true
                else (s.observance_variant is null or s.observance_variant = $5::text)
              end
            )
        )
        select *
        from c
        where is_eligible = true
        order by
          is_past asc,
          festival_date asc,
          priority desc,
          specificity desc,
          name asc
        limit 1
        """

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                q,
                cc,
                region_code,
                religion,
                locale,
                observance_variant,
                start_date,
                today,
                end_date,
                bool(strict_when_none),
            )

        if not row:
            return None

        return FestivalPick(
            festival_id=str(row["festival_id"]),
            scope_id=str(row["scope_id"]),
            occurrence_id=str(row["occurrence_id"]),
            slug=str(row["slug"]),
            name=str(row["name"]),
            country_code=str(row["country_code"]),
            region_code=row["region_code"],
            religion=row["religion"],
            locale=row["locale"],
            observance_variant=row["observance_variant"],
            timezone=str(row["timezone"]),
            category=str(row["category"]),
            lead_days=int(row["lead_days"] or 0),
            lag_days=int(row["lag_days"] or 0),
            priority=int(row["priority"] or 0),
            festival_date=row["festival_date"],
            motifs=dict(row["motifs"] or {}),
            sources=list(row["sources"] or []),
        )