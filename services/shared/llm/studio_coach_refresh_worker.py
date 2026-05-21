from __future__ import annotations

"""
Common DesiFaces Studio Coach refresh worker.

This worker is intentionally NOT owned by svc-audio, svc-face, or svc-fusion.
It hydrates shared DB-backed Studio Coach tips for all Studio surfaces:
  - face
  - audio
  - fusion

Recommended module path in repo:
  services/shared/llm/studio_coach_refresh_worker.py

Recommended runtime command:
  python -m desifaces_shared.llm.studio_coach_refresh_worker
"""

import asyncio
import json
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import asyncpg

# Runtime-safe import. This worker is shared and may be run either as:
#   python -m desifaces_shared.llm.studio_coach_refresh_worker
# or directly from a mounted shared llm directory:
#   python /app/shared_llm/studio_coach_refresh_worker.py
# Do not crash just because the service image has not installed the shared package.
try:
    from desifaces_shared.llm.studio_coach_refresh import RefreshConfig, refresh_one_context
except ModuleNotFoundError:
    _HERE = Path(__file__).resolve().parent
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))
    from studio_coach_refresh import RefreshConfig, refresh_one_context  # type: ignore


DEFAULT_CONTEXTS: List[Dict[str, Any]] = [
    {
        "studio": "face",
        "modes": ["text-to-image", "image-to-image"],
        "locales": ["en"],
    },
    {
        "studio": "audio",
        "modes": ["tts"],
        "locales": ["en"],
    },
    {
        "studio": "fusion",
        "modes": ["talking_video", "cinematic_video_direction"],
        "locales": ["en"],
    },
]


@dataclass(frozen=True)
class CoachRefreshContext:
    studio: str
    mode: str
    locale: str


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default))).strip())
    except Exception:
        return default


def _csv(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [x.strip() for x in value.split(",") if x.strip()]


def _unique_contexts(contexts: Iterable[CoachRefreshContext]) -> List[CoachRefreshContext]:
    seen: set[tuple[str, str, str]] = set()
    out: List[CoachRefreshContext] = []
    for ctx in contexts:
        key = (ctx.studio, ctx.mode, ctx.locale)
        if key in seen:
            continue
        seen.add(key)
        out.append(ctx)
    return out


def _contexts_from_rows(rows: Sequence[Dict[str, Any]]) -> List[CoachRefreshContext]:
    contexts: List[CoachRefreshContext] = []
    for row in rows:
        studio = str(row.get("studio") or "").strip()
        if studio not in {"face", "audio", "fusion"}:
            continue
        modes = row.get("modes") or row.get("mode") or []
        locales = row.get("locales") or row.get("locale") or ["en"]
        if isinstance(modes, str):
            modes = [modes]
        if isinstance(locales, str):
            locales = [locales]
        for mode in modes:
            mode_s = str(mode or "").strip()
            if not mode_s:
                continue
            for locale in locales:
                locale_s = str(locale or "en").strip() or "en"
                contexts.append(CoachRefreshContext(studio=studio, mode=mode_s, locale=locale_s))
    return _unique_contexts(contexts)


def load_refresh_contexts() -> List[CoachRefreshContext]:
    """
    Preferred configuration:
      DF_STUDIO_COACH_REFRESH_CONTEXTS_JSON='[
        {"studio":"face","modes":["text-to-image","image-to-image"],"locales":["en"]},
        {"studio":"audio","modes":["tts"],"locales":["en"]},
        {"studio":"fusion","modes":["talking_video","cinematic_video_direction"],"locales":["en"]}
      ]'

    Backward-compatible fallback:
      DF_STUDIO_COACH_REFRESH_STUDIOS=face,audio,fusion
      DF_STUDIO_COACH_REFRESH_LOCALES=en

    Note: the old global MODES env is deliberately avoided by default because it
    creates bad cross-products like face+tts or audio+cinematic_video_direction.
    """
    raw_json = os.getenv("DF_STUDIO_COACH_REFRESH_CONTEXTS_JSON")
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, list):
                contexts = _contexts_from_rows([x for x in parsed if isinstance(x, dict)])
                if contexts:
                    return contexts
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "event": "studio_coach_refresh_context_config_invalid",
                        "error": str(exc),
                    }
                ),
                flush=True,
            )

    studios = _csv(os.getenv("DF_STUDIO_COACH_REFRESH_STUDIOS"))
    locales = _csv(os.getenv("DF_STUDIO_COACH_REFRESH_LOCALES")) or ["en"]
    explicit_modes = _csv(os.getenv("DF_STUDIO_COACH_REFRESH_MODES"))

    base_rows = DEFAULT_CONTEXTS
    if studios:
        base_rows = [row for row in DEFAULT_CONTEXTS if row["studio"] in set(studios)]

    if explicit_modes and len(base_rows) == 1:
        # Safe only when the worker is scoped to one studio.
        base_rows = [{**base_rows[0], "modes": explicit_modes, "locales": locales}]
    else:
        base_rows = [{**row, "locales": locales} for row in base_rows]

    return _contexts_from_rows(base_rows)


async def _create_pool() -> asyncpg.Pool:
    dsn = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_DSN") or os.getenv("DB_DSN")
    if not dsn:
        raise RuntimeError("DATABASE_URL/POSTGRES_DSN/DB_DSN is required for Studio Coach refresh worker")
    return await asyncpg.create_pool(
        dsn=dsn,
        min_size=1,
        max_size=_env_int("DF_STUDIO_COACH_REFRESH_DB_POOL_MAX", 3),
    )


async def run_once(pool: asyncpg.Pool, *, config: RefreshConfig, contexts: Sequence[CoachRefreshContext]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for ctx in contexts:
        result = await refresh_one_context(
            pool,
            studio=ctx.studio,
            mode=ctx.mode,
            locale=ctx.locale,
            config=config,
        )
        results.append(result.model_dump())
    return results


async def main() -> None:
    config = RefreshConfig.from_env()
    interval = max(300, _env_int("DF_STUDIO_COACH_REFRESH_INTERVAL_SECONDS", 21600))
    run_once_only = str(os.getenv("DF_STUDIO_COACH_REFRESH_RUN_ONCE", "false")).lower() in {"1", "true", "yes", "on"}
    contexts = load_refresh_contexts()

    if not contexts:
        raise RuntimeError("No Studio Coach refresh contexts configured")

    print(
        json.dumps(
            {
                "event": "studio_coach_refresh_worker_start",
                "contexts": [ctx.__dict__ for ctx in contexts],
                "auto_activate": config.auto_activate,
                "provider": config.provider,
                "model": config.llm_model,
            },
            default=str,
        ),
        flush=True,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    pool = await _create_pool()
    try:
        while not stop.is_set():
            results = await run_once(pool, config=config, contexts=contexts)
            print(
                json.dumps(
                    {
                        "event": "studio_coach_refresh_complete",
                        "results": results,
                    },
                    default=str,
                ),
                flush=True,
            )
            if run_once_only:
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
