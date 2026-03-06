from __future__ import annotations

import json
from typing import List, Optional
from uuid import UUID, uuid4

import asyncpg

from app.domain.models import ScheduleIn, ScheduleOut


class SchedulesRepo:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def create(self, inp: ScheduleIn) -> UUID:
        schedule_id = uuid4()
        q = """
        insert into marketing_schedules (
          schedule_id, name, enabled,
          freq, hour, minute, dow,
          mode, recipe, persona, industry, tags, season_event, offer, language_hint,
          inputs_json, target_seconds
        ) values (
          $1, $2, $3,
          $4, $5, $6, $7,
          $8, $9, $10, $11, $12::text[], $13, $14, $15,
          $16::jsonb, $17
        )
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                q,
                schedule_id,
                inp.name,
                inp.enabled,
                inp.freq,
                inp.hour,
                inp.minute,
                inp.dow,
                inp.mode.value,
                inp.recipe.value,
                inp.persona.value if inp.persona else None,
                inp.industry,
                inp.tags,
                inp.season_event,
                inp.offer,
                inp.language_hint,
                json.dumps(inp.inputs),
                inp.target_seconds,
            )
        return schedule_id

    async def get(self, schedule_id: UUID) -> Optional[asyncpg.Record]:
        q = "select * from marketing_schedules where schedule_id=$1"
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(q, schedule_id)

    async def list_all(self) -> List[asyncpg.Record]:
        q = "select * from marketing_schedules order by created_at desc"
        async with self.pool.acquire() as conn:
            return await conn.fetch(q)

    async def set_enabled(self, schedule_id: UUID, enabled: bool) -> None:
        q = "update marketing_schedules set enabled=$2, updated_at=now() where schedule_id=$1"
        async with self.pool.acquire() as conn:
            await conn.execute(q, schedule_id, enabled)

    async def due_schedules(self) -> List[asyncpg.Record]:
        q = "select * from marketing_schedules where enabled=true"
        async with self.pool.acquire() as conn:
            return await conn.fetch(q)

    async def mark_ran(self, schedule_id: UUID) -> None:
        q = "update marketing_schedules set last_run_at=now(), updated_at=now() where schedule_id=$1"
        async with self.pool.acquire() as conn:
            await conn.execute(q, schedule_id)

    def to_out(self, row: asyncpg.Record) -> ScheduleOut:
        return ScheduleOut(
            schedule_id=row["schedule_id"],
            name=row["name"],
            enabled=row["enabled"],
            freq=row["freq"],
            hour=row["hour"],
            minute=row["minute"],
            dow=row["dow"],
            last_run_at=row["last_run_at"].isoformat() if row["last_run_at"] else None,
            created_at=row["created_at"].isoformat(),
            updated_at=row["updated_at"].isoformat(),
        )