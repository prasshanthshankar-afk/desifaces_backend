from __future__ import annotations

import asyncio
from typing import Any

import asyncpg

from app.config import settings
from app.repos.creator_config_repo import CreatorPlatformConfigRepo


class StrictCreatorPlatformConfigRepo(CreatorPlatformConfigRepo):
    """Creator config repository that fails instead of returning raw dicts."""

    @staticmethod
    def _safe_model(model_cls, data: dict[str, Any]):
        try:
            return model_cls(**data)
        except Exception as exc:
            code = str(data.get("code") or data.get("platform_code") or "<unknown>")
            raise RuntimeError(
                "MPS2_CONFIG_HYDRATION_FAIL="
                f"model={getattr(model_cls, '__name__', str(model_cls))}:"
                f"code={code}:error={exc}"
            ) from exc


async def main() -> None:
    pool = await asyncpg.create_pool(settings.DATABASE_URL, min_size=1, max_size=2)
    try:
        repo = StrictCreatorPlatformConfigRepo(pool)
        checks = (
            ("image_formats", repo.get_image_formats),
            ("use_cases", repo.get_use_cases),
            ("age_ranges", repo.get_age_ranges),
            ("regions", repo.get_regions),
            ("skin_tones", repo.get_skin_tones),
        )
        total = 0
        for label, getter in checks:
            try:
                rows = await getter()
            except Exception as exc:
                raise RuntimeError(f"MPS2_CONFIG_HYDRATION_FAIL=catalog={label}:{exc}") from exc
            raw_dicts = [row for row in rows if isinstance(row, dict)]
            if raw_dicts:
                codes = [str(row.get("code") or "<unknown>") for row in raw_dicts]
                raise RuntimeError(
                    f"MPS2_CONFIG_HYDRATION_FAIL=catalog={label}:raw_dicts={codes}"
                )
            total += len(rows)
            print(f"MPS2_CONFIG_HYDRATION_CATALOG=PASS:{label}:rows={len(rows)}")
        print(f"MPS2_CONFIG_HYDRATION=PASS:typed_rows={total}")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
