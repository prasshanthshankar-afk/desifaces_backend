# services/svc-marketing/app/app/services/orchestration/recipes/runner.py
from __future__ import annotations

from typing import Any, Dict

from app.domain.enums import RecipeKind
from app.domain.models import MarketingRunIn, UseCaseSpec
from app.services.orchestration.run_context import RunContext
from app.services.orchestration.recipes.face_audio_video import FaceAudioVideoRecipe
from app.services.orchestration.recipes.commerce_catalog_promo import CommerceCatalogPromoRecipe


class RecipeRunner:
    def __init__(self, *, face_audio_video: FaceAudioVideoRecipe, commerce_catalog: CommerceCatalogPromoRecipe):
        self.face_audio_video = face_audio_video
        self.commerce_catalog = commerce_catalog

    async def run(
        self,
        *,
        recipe: RecipeKind,
        ctx: RunContext,
        inp: MarketingRunIn,
        use_case: UseCaseSpec,
        run_seed: int,
        request_nonce: str,
    ) -> Dict[str, Any]:
        rv = recipe.value if hasattr(recipe, "value") else str(recipe)

        if rv == "FACE_CATALOG_PRODUCT_PROMO":
            return await self.commerce_catalog.run(ctx=ctx, use_case=use_case, inp=inp, run_seed=run_seed, request_nonce=request_nonce)

        # FACE_MUSIC_MUSICVIDEO currently falls back to face+audio+fusion unless you explicitly route it elsewhere
        return await self.face_audio_video.run(ctx=ctx, use_case=use_case, inp=inp, run_seed=run_seed, request_nonce=request_nonce)