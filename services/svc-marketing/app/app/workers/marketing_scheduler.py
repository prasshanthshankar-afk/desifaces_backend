from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional, Any, Dict
from uuid import UUID

from app.config import settings
from app.db import get_pool
from app.domain.models import MarketingRunIn
from app.domain.enums import MarketingRunMode, RecipeKind, Persona
from app.repos.schedules_repo import SchedulesRepo
from app.repos.marketing_runs_repo import MarketingRunsRepo
from app.services.admin.admin_resolver import maybe_set_admin_marketing_user_id

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("svc-marketing-scheduler")


def _dow(now_utc: datetime) -> str:
    return ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][now_utc.weekday()]


def _is_due(row, now_utc: datetime) -> bool:
    if not row["enabled"]:
        return False

    if row["freq"] == "daily":
        if now_utc.hour != row["hour"] or now_utc.minute != row["minute"]:
            return False
    elif row["freq"] == "weekly":
        if not row["dow"]:
            return False
        allowed = [x.strip().lower() for x in row["dow"].split(",") if x.strip()]
        if _dow(now_utc) not in allowed:
            return False
        if now_utc.hour != row["hour"] or now_utc.minute != row["minute"]:
            return False
    else:
        return False

    last = row["last_run_at"]
    if last and isinstance(last, datetime):
        if abs((now_utc - last.replace(tzinfo=timezone.utc)).total_seconds()) < 60:
            return False
    return True


def _as_dict_loose(v: Any) -> Dict[str, Any]:
    """
    Schedules.inputs_json may come back as:
      - dict (jsonb)
      - string like '{}' (json cast or text column)
      - None
    Normalize to dict for MarketingRunIn.inputs.
    """
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return {}
        try:
            x = json.loads(s)
            return x if isinstance(x, dict) else {}
        except Exception:
            return {}
    # asyncpg json sometimes returns as list/tuple; try best-effort
    try:
        if hasattr(v, "items"):
            return dict(v)  # type: ignore
    except Exception:
        pass
    return {}


async def main() -> None:
    pool = await get_pool()
    schedules = SchedulesRepo(pool)
    runs = MarketingRunsRepo(pool)

    admin_uid: Optional[UUID] = await maybe_set_admin_marketing_user_id(pool)
    if not admin_uid:
        logger.warning(
            "Marketing admin not resolved. Set ADMIN_MARKETING_EMAIL (recommended) or ADMIN_MARKETING_USER_ID. "
            "Scheduler will not enqueue runs until resolved."
        )
    else:
        logger.info("Marketing admin resolved: %s", admin_uid)

    logger.info("scheduler started tick=10s")
    while True:
        now_utc = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        # Re-try resolving periodically (in case user is created after startup)
        if not admin_uid:
            admin_uid = await maybe_set_admin_marketing_user_id(pool)

        for row in await schedules.due_schedules():
            try:
                if not _is_due(row, now_utc):
                    continue
                if not admin_uid:
                    continue

                inputs_dict = _as_dict_loose(row.get("inputs_json") if isinstance(row, dict) else row["inputs_json"])

                inp = MarketingRunIn(
                    mode=MarketingRunMode(row["mode"]),
                    recipe=RecipeKind(row["recipe"]),
                    persona=Persona(row["persona"]) if row["persona"] else None,
                    industry=row["industry"],
                    tags=row["tags"] or [],
                    season_event=row["season_event"],
                    offer=row["offer"],
                    language_hint=row["language_hint"] or "en",
                    inputs=inputs_dict,
                    target_seconds=row["target_seconds"],
                )

                run_id = await runs.create_run(
                    run_as_user_id=admin_uid,
                    bearer_token=None,
                    mode=inp.mode,
                    recipe=inp.recipe,
                    cost_bucket=settings.DEFAULT_COST_BUCKET,
                    cost_category=inp.recipe.value,
                    input_json=inp.model_dump(),
                )
                await schedules.mark_ran(row["schedule_id"])
                logger.info("enqueued run_id=%s schedule_id=%s", run_id, row["schedule_id"])

            except Exception as e:
                # Never crash the scheduler; log and continue
                logger.exception("schedule processing failed schedule_id=%s err=%s", row.get("schedule_id"), str(e))

        await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(main())