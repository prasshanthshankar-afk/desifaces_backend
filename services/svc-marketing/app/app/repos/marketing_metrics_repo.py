# services/svc-marketing/app/app/repos/marketing_metrics_repo.py
from __future__ import annotations

import json
from datetime import date
from typing import Any, Dict, List
from uuid import UUID

import asyncpg


class MarketingMetricsRepo:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def upsert_metrics(
        self,
        platform_post_id: UUID,
        metric_date: date,
        metrics: Dict[str, Any],
        raw_json: Dict[str, Any],
    ) -> None:
        q = """
        insert into marketing_post_metrics (
          platform_post_id, metric_date,
          impressions, reach, plays, likes, comments, shares, saves,
          profile_visits, follows, watch_time_ms,
          raw_json
        ) values (
          $1, $2,
          $3, $4, $5, $6, $7, $8, $9,
          $10, $11, $12,
          $13::jsonb
        )
        on conflict (platform_post_id, metric_date)
        do update set
          impressions=excluded.impressions,
          reach=excluded.reach,
          plays=excluded.plays,
          likes=excluded.likes,
          comments=excluded.comments,
          shares=excluded.shares,
          saves=excluded.saves,
          profile_visits=excluded.profile_visits,
          follows=excluded.follows,
          watch_time_ms=excluded.watch_time_ms,
          raw_json=excluded.raw_json,
          created_at=now()
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                q,
                platform_post_id,
                metric_date,
                metrics.get("impressions"),
                metrics.get("reach"),
                metrics.get("plays"),
                metrics.get("likes"),
                metrics.get("comments"),
                metrics.get("shares"),
                metrics.get("saves"),
                metrics.get("profile_visits"),
                metrics.get("follows"),
                metrics.get("watch_time_ms"),
                json.dumps(raw_json or {}),
            )

    async def aggregate_usecase_metrics(self, lookback_days: int) -> List[asyncpg.Record]:
        # Joins:
        # marketing_runs -> planning_json.use_case.use_case_id -> platform_posts -> metrics
        q = f"""
        with run_usecases as (
          select
            run_id,
            (planning_json->'use_case'->>'use_case_id')::uuid as use_case_id
          from marketing_runs
          where planning_json ? 'use_case'
            and (planning_json->'use_case' ? 'use_case_id')
        ),
        posts as (
          select p.platform_post_id, ru.use_case_id, p.run_id
          from marketing_platform_posts p
          join run_usecases ru on ru.run_id = p.run_id
        ),
        m as (
          select
            posts.use_case_id,
            sum(coalesce(impressions,0)) as impressions,
            sum(coalesce(reach,0)) as reach,
            sum(coalesce(plays,0)) as plays,
            sum(coalesce(likes,0)) as likes,
            sum(coalesce(comments,0)) as comments,
            sum(coalesce(shares,0)) as shares,
            sum(coalesce(saves,0)) as saves,
            sum(coalesce(profile_visits,0)) as profile_visits,
            sum(coalesce(follows,0)) as follows,
            sum(coalesce(watch_time_ms,0)) as watch_time_ms
          from posts
          join marketing_post_metrics mm on mm.platform_post_id = posts.platform_post_id
          where mm.metric_date >= (current_date - $1::int)
          group by posts.use_case_id
        ),
        cost as (
          select
            ru.use_case_id,
            sum(coalesce(ol.cost_usd,0)) as cost_usd
          from ops_cost_ledger ol
          join run_usecases ru on ru.run_id = ol.run_id
          where ol.created_at >= (now() - ($1::int || ' days')::interval)
          group by ru.use_case_id
        )
        select
          m.use_case_id,
          m.impressions, m.reach, m.plays, m.likes, m.comments, m.shares, m.saves, m.profile_visits, m.follows, m.watch_time_ms,
          coalesce(cost.cost_usd, 0) as cost_usd
        from m
        left join cost on cost.use_case_id = m.use_case_id
        """
        async with self.pool.acquire() as conn:
            return await conn.fetch(q, lookback_days)