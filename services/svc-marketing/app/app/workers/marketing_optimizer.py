# services/svc-marketing/app/app/workers/marketing_optimizer.py
from __future__ import annotations

import asyncio
import logging
from typing import Dict, Any

from app.config import settings
from app.db import get_pool
from app.repos.marketing_metrics_repo import MarketingMetricsRepo
from app.repos.marketing_use_cases_repo import MarketingUseCasesRepo
from app.services.metrics.weight_optimizer import compute_weight

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("svc-marketing-optimizer")


async def main() -> None:
    if not settings.ENABLE_OPTIMIZER:
        logger.warning("ENABLE_OPTIMIZER=false; optimizer will exit.")
        return

    pool = await get_pool()
    metrics_repo = MarketingMetricsRepo(pool)
    usecases_repo = MarketingUseCasesRepo(pool)

    logger.info("optimizer started interval=%ss lookback_days=%s", settings.OPTIMIZER_INTERVAL_SECONDS, settings.OPTIMIZER_LOOKBACK_DAYS)

    while True:
        try:
            rows = await metrics_repo.aggregate_usecase_metrics(settings.OPTIMIZER_LOOKBACK_DAYS)
            logger.info("optimizer: aggregated use_case rows=%d", len(rows))

            for r in rows:
                use_case_id = r["use_case_id"]
                metrics: Dict[str, Any] = dict(r)
                new_weight = compute_weight(metrics)

                await usecases_repo.update_weight_and_metrics(
                    use_case_id=use_case_id,
                    weight=float(new_weight),
                    last_metrics_json={
                        "lookback_days": settings.OPTIMIZER_LOOKBACK_DAYS,
                        "impressions": int(r["impressions"] or 0),
                        "reach": int(r["reach"] or 0),
                        "plays": int(r["plays"] or 0),
                        "likes": int(r["likes"] or 0),
                        "comments": int(r["comments"] or 0),
                        "shares": int(r["shares"] or 0),
                        "saves": int(r["saves"] or 0),
                        "profile_visits": int(r["profile_visits"] or 0),
                        "follows": int(r["follows"] or 0),
                        "watch_time_ms": int(r["watch_time_ms"] or 0),
                        "cost_usd": float(r["cost_usd"] or 0),
                        "computed_weight": float(new_weight),
                    },
                )

            logger.info("optimizer: weights updated.")
        except Exception as e:
            logger.exception("optimizer error: %s", e)

        await asyncio.sleep(settings.OPTIMIZER_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())