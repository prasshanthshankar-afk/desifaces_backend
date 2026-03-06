# services/svc-marketing/app/app/services/orchestration/stages/compose_stage.py
from __future__ import annotations

import asyncio
import json
import os
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import UUID

from app.config import settings
from app.domain.enums import AssetKind, MarketingRunMode, RecipeKind
from app.domain.models import MarketingRunIn, UseCaseSpec
from app.repos.marketing_assets_repo import MarketingAssetsRepo
from app.services.orchestration.qc.output_gates import assert_required_outputs_or_fail, compose_is_done
from app.services.orchestration.utils.config import cfg_bool, cfg_str
from app.services.rendering.image_composer import compose_slide
from app.services.storage.blob_uploader import BlobUploader


def _asset_kind(name: str, fallback: str) -> str:
    e = getattr(AssetKind, name, None)
    try:
        return e.value if e is not None else fallback
    except Exception:
        return fallback


def _json_default(o: Any) -> Any:
    """
    Make manifest writing bulletproof.
    Handles UUID, datetime, Decimal, Enum, and pydantic-like objects.
    """
    if isinstance(o, UUID):
        return str(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, Enum):
        return getattr(o, "value", str(o))
    # Pydantic v2 models often have model_dump()
    if hasattr(o, "model_dump") and callable(getattr(o, "model_dump")):
        try:
            return o.model_dump()
        except Exception:
            pass
    # last resort: stringify to avoid hard-crash
    return str(o)


def _write_json_file(path: str, obj: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=_json_default)


def _dedupe_line(text: str, phrase: str) -> str:
    """
    Ensure a CTA doesn't appear twice in captions.
    """
    if not isinstance(text, str) or not text.strip():
        return text
    if not phrase:
        return text
    lines = [ln.strip() for ln in text.splitlines()]
    out: List[str] = []
    seen_phrase = False
    for ln in lines:
        if not ln:
            out.append("")
            continue
        if phrase.lower() in ln.lower():
            if seen_phrase:
                continue
            seen_phrase = True
        out.append(ln)
    # cleanup multiple blank lines
    cleaned: List[str] = []
    prev_blank = False
    for ln in out:
        blank = (ln.strip() == "")
        if blank and prev_blank:
            continue
        cleaned.append(ln)
        prev_blank = blank
    return "\n".join(cleaned).strip()


class ComposeStage:
    def __init__(self, assets: MarketingAssetsRepo, uploader: BlobUploader):
        self.assets = assets
        self.uploader = uploader

    async def _upload(self, local_path: str, blob_path: str, content_type: str):
        return await asyncio.to_thread(self.uploader.upload_file, local_path, blob_path, content_type)

    async def _compose_slide(self, **kwargs):
        return await asyncio.to_thread(compose_slide, **kwargs)

    def _write_text(self, path: str, text: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def _carousel_slides(self, inp: MarketingRunIn) -> int:
        v = (inp.inputs or {}).get("carousel_slides")
        try:
            n = int(v) if v is not None else 2
        except Exception:
            n = 2
        return 1 if n <= 1 else 2

    def _cta_text(self) -> str:
        # Make CTA configurable + consistent across outputs
        return cfg_str("MARKETING_CTA_TEXT", "DM desifaces and I will show you how I do it").strip()

    def _cta_on_cover_enabled(self) -> bool:
        # IMPORTANT: default False to avoid the “said twice” complaint
        return cfg_bool("MARKETING_CTA_INCLUDE_ON_COVER", False)

    def caption(self, use_case: UseCaseSpec) -> str:
        cta = self._cta_text()

        lines: List[str] = [use_case.hook_text, ""]
        lines.append(f"Persona: {use_case.persona.value.upper()} • Industry: {use_case.industry}")
        if use_case.season_event:
            lines.append(f"Season: {use_case.season_event}")
        if use_case.offer:
            lines.append(f"Offer: {use_case.offer}")
        if use_case.product_anchor:
            lines.append(f"Use case: {use_case.product_anchor}")

        lines += [
            "",
            "desifaces.ai: Face • Talking Video • Music • Promo — ready to post.",
            cta,
            "",
            "#AICreators #SmallBusinessMarketing #ContentCreation #ReelsTips #desifaces.ai",
        ]

        text = "\n".join(lines)
        # Ensure CTA appears only once (even if upstream already included it)
        return _dedupe_line(text, cta)

    async def run(
        self,
        *,
        run_id: UUID,
        mode: MarketingRunMode,
        recipe: RecipeKind,
        fmt: str,
        publish_targets: List[str],
        inp: MarketingRunIn,
        use_case: UseCaseSpec,
        output: Dict[str, Any],
        timeout_s: int,
    ) -> Dict[str, Any]:
        out_dir = os.path.join(settings.OUTPUT_DIR, str(run_id))
        os.makedirs(out_dir, exist_ok=True)
        slides_n = self._carousel_slides(inp)

        if compose_is_done(fmt, output, slides_n):
            return output

        cta = self._cta_text()
        cta_on_cover = self._cta_on_cover_enabled()

        # caption
        if not output.get("caption_url"):
            caption_path = os.path.join(out_dir, "caption.txt")
            await asyncio.to_thread(self._write_text, caption_path, self.caption(use_case))
            up = await asyncio.wait_for(
                self._upload(caption_path, f"{run_id}/caption.txt", "text/plain"),
                timeout=float(timeout_s),
            )
            await self.assets.add_asset(
                run_id,
                _asset_kind("caption_txt", "caption_txt"),
                up.url,
                "text/plain",
                None,
                None,
                None,
                {},
            )
            output["caption_url"] = up.url

        # cover/story/carousel
        if fmt in ("reel", "yt_short", "yt_long"):
            if not output.get("reel_cover_url"):
                cover_lines = [*(use_case.onscreen_lines[:2] or []), "Face • Talk • Video"]
                # Avoid repeating CTA on cover by default
                if cta_on_cover:
                    cover_lines.append(cta)
                else:
                    cover_lines.append("desifaces.ai")

                cover = await asyncio.wait_for(
                    self._compose_slide(
                        out_path=os.path.join(out_dir, "reel_cover.png"),
                        headline=use_case.hook_text,
                        lines=cover_lines,
                        size=(1080, 1920),
                    ),
                    timeout=float(timeout_s),
                )
                up = await asyncio.wait_for(
                    self._upload(cover.path, f"{run_id}/reel_cover.png", "image/png"),
                    timeout=float(timeout_s),
                )
                await self.assets.add_asset(
                    run_id,
                    _asset_kind("reel_cover_png", "reel_cover_png"),
                    up.url,
                    "image/png",
                    cover.width,
                    cover.height,
                    None,
                    {},
                )
                output["reel_cover_url"] = up.url

        elif fmt == "story":
            if not output.get("story_url"):
                story_lines = [*(use_case.onscreen_lines[:2] or []), "Swipe/DM for demo"]
                if cta_on_cover:
                    story_lines.append(cta)

                story = await asyncio.wait_for(
                    self._compose_slide(
                        out_path=os.path.join(out_dir, "story.png"),
                        headline=use_case.hook_text,
                        lines=story_lines,
                        size=(1080, 1920),
                    ),
                    timeout=float(timeout_s),
                )
                up = await asyncio.wait_for(
                    self._upload(story.path, f"{run_id}/story.png", "image/png"),
                    timeout=float(timeout_s),
                )
                await self.assets.add_asset(
                    run_id,
                    _asset_kind("story_png", "story_png"),
                    up.url,
                    "image/png",
                    story.width,
                    story.height,
                    None,
                    {},
                )
                output["story_url"] = up.url

        else:  # carousel
            if not output.get("slide_01_url"):
                s1 = await asyncio.wait_for(
                    self._compose_slide(
                        out_path=os.path.join(out_dir, "slide_01.png"),
                        headline=use_case.hook_text,
                        lines=use_case.onscreen_lines[:4],
                        size=(1080, 1350),
                    ),
                    timeout=float(timeout_s),
                )
                up = await asyncio.wait_for(
                    self._upload(s1.path, f"{run_id}/slide_01.png", "image/png"),
                    timeout=float(timeout_s),
                )
                await self.assets.add_asset(
                    run_id,
                    _asset_kind("slide_01_png", "slide_01_png"),
                    up.url,
                    "image/png",
                    s1.width,
                    s1.height,
                    None,
                    {},
                )
                output["slide_01_url"] = up.url

            if slides_n >= 2 and not output.get("slide_02_url"):
                lines2 = ["Fast • Consistent • Post-ready"]
                if cta_on_cover:
                    lines2.append(cta)
                else:
                    lines2.append("desifaces.ai")

                s2 = await asyncio.wait_for(
                    self._compose_slide(
                        out_path=os.path.join(out_dir, "slide_02.png"),
                        headline="How it helps",
                        lines=lines2,
                        size=(1080, 1350),
                    ),
                    timeout=float(timeout_s),
                )
                up = await asyncio.wait_for(
                    self._upload(s2.path, f"{run_id}/slide_02.png", "image/png"),
                    timeout=float(timeout_s),
                )
                await self.assets.add_asset(
                    run_id,
                    _asset_kind("slide_02_png", "slide_02_png"),
                    up.url,
                    "image/png",
                    s2.width,
                    s2.height,
                    None,
                    {},
                )
                output["slide_02_url"] = up.url

        # manifest with optional raw pruning
        include_raw = cfg_bool("MARKETING_MANIFEST_INCLUDE_RAW", False)
        recipe_result: Any = output.get("recipe_result")
        if isinstance(recipe_result, dict) and not include_raw:
            pruned = dict(recipe_result)
            for k in ("face_raw", "audio_raw", "fusion_raw", "commerce_raw"):
                if k in pruned:
                    pruned[k] = {"_pruned": True}
            recipe_result = pruned

        if not output.get("manifest_url"):
            manifest = {
                "run_id": str(run_id),
                "mode": mode.value,
                "recipe": recipe.value,
                "format_hint": fmt,
                "publish_targets": publish_targets,
                "use_case": use_case.model_dump(),
                "recipe_result": recipe_result,
                "output": output,  # may contain UUIDs -> handled by _json_default
            }
            path = os.path.join(out_dir, "post_manifest.json")
            await asyncio.to_thread(_write_json_file, path, manifest)
            up = await asyncio.wait_for(
                self._upload(path, f"{run_id}/post_manifest.json", "application/json"),
                timeout=float(timeout_s),
            )
            await self.assets.add_asset(
                run_id,
                _asset_kind("manifest_json", "manifest_json"),
                up.url,
                "application/json",
                None,
                None,
                None,
                {},
            )
            output["manifest_url"] = up.url

        assert_required_outputs_or_fail(fmt, output)
        return output