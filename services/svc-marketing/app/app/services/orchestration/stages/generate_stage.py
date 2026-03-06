# services/svc-marketing/app/app/services/orchestration/stages/generate_stage.py
from __future__ import annotations

import asyncio
from typing import Any, Dict

from app.domain.enums import RecipeKind
from app.domain.models import MarketingRunIn, UseCaseSpec
from app.services.orchestration.errors import MarketingRunFailed
from app.services.orchestration.recipes.runner import RecipeRunner
from app.services.orchestration.utils.media_extract import extract_media
from app.services.orchestration.utils.jsonx import truncate_json, deep_find_url


class GenerateStage:
    def __init__(self, runner: RecipeRunner):
        self.runner = runner

    async def run(
        self,
        *,
        recipe: RecipeKind,
        ctx,
        inp: MarketingRunIn,
        use_case: UseCaseSpec,
        output: Dict[str, Any],
        timeout_s: int,
        run_seed: int,
        request_nonce: str,
    ) -> Dict[str, Any]:
        if output.get("recipe_result"):
            return output

        try:
            result = await asyncio.wait_for(
                self.runner.run(recipe=recipe, ctx=ctx, inp=inp, use_case=use_case, run_seed=run_seed, request_nonce=request_nonce),
                timeout=float(timeout_s),
            )
        except asyncio.TimeoutError:
            raise MarketingRunFailed("GENERATE_TIMEOUT", f"generate timed out after {timeout_s}s", stage="generate")

        if not isinstance(result, dict):
            raise MarketingRunFailed("GENERATE_BAD_RESULT", f"recipe returned {type(result)}", stage="generate")

        output["recipe_result"] = result

        # ensure reel_url if present in result
        media = extract_media(result)
        if media.get("video_url"):
            output["reel_url"] = media["video_url"]
        if not output.get("reel_url"):
            output["reel_url"] = result.get("reel_url") or deep_find_url(result)

        fmt = str((inp.inputs or {}).get("format_hint") or "reel").strip().lower()
        if fmt in ("reel", "yt_short", "yt_long") and not output.get("reel_url"):
            raise MarketingRunFailed("MISSING_REEL_URL", f"No reel_url. hint={truncate_json(result, 1600)}", stage="generate")

        return output