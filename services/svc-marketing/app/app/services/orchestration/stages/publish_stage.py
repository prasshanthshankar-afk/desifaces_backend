# services/svc-marketing/app/app/services/orchestration/stages/publish_stage.py
from __future__ import annotations

import asyncio
from typing import Any, Dict, List
from uuid import UUID

from app.config import settings
from app.domain.models import MarketingRunIn, UseCaseSpec
from app.repos.marketing_platform_posts_repo import MarketingPlatformPostsRepo
from app.repos.marketing_platform_accounts_repo import MarketingPlatformAccountsRepo
from app.services.secrets.secret_provider import DefaultSecretProvider
from app.services.publishers.instagram_publisher import InstagramPublisher
from app.services.publishers.youtube_publisher import YouTubePublisher
from app.services.orchestration.errors import MarketingRunFailed
from app.services.orchestration.utils.config import cfg_bool


class PublishStage:
    def __init__(
        self,
        *,
        platform_posts: MarketingPlatformPostsRepo,
        platform_accounts: MarketingPlatformAccountsRepo,
        secrets: DefaultSecretProvider,
    ):
        self.platform_posts = platform_posts
        self.platform_accounts = platform_accounts
        self.secrets = secrets

    def _publish_is_done(self, out: Dict[str, Any], target: str) -> bool:
        if not isinstance(out, dict):
            return False
        if target.startswith("instagram"):
            ig = out.get("instagram")
            return isinstance(ig, dict) and ig.get("ok") is True
        if target.startswith("youtube"):
            yt = out.get("youtube")
            return isinstance(yt, dict) and yt.get("ok") is True
        return False

    def _caption(self, use_case: UseCaseSpec) -> str:
        lines = [use_case.hook_text, ""]
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
            "DM “desifaces.ai” for early access.",
            "",
            "#AICreators #SmallBusinessMarketing #ContentCreation #ReelsTips #desifaces.ai",
        ]
        return "\n".join(lines)

    def _aset(self, output: Dict[str, Any], key: str, patch: Dict[str, Any]) -> None:
        """
        artifact_status helper:
        output["artifact_status"][key] = { ...patch... }
        """
        if not isinstance(output, dict):
            return
        a = output.get("artifact_status")
        if not isinstance(a, dict):
            a = {}
            output["artifact_status"] = a
        cur = a.get(key)
        if not isinstance(cur, dict):
            cur = {}
            a[key] = cur
        cur.update(patch)

    async def run(
        self,
        *,
        run_id: UUID,
        inp: MarketingRunIn,
        use_case: UseCaseSpec,
        fmt: str,
        publish_targets: List[str],
        output: Dict[str, Any],
        timeout_s: int,
    ) -> Dict[str, Any]:
        publish_strict = cfg_bool("MARKETING_PUBLISH_STRICT", False)
        reel_url = output.get("reel_url")

        async def _do_publish() -> None:
            # ----------------
            # Instagram
            # ----------------
            do_ig = ("instagram_reel" in publish_targets) or ("instagram" in publish_targets)
            if do_ig and not self._publish_is_done(output, "instagram"):
                self._aset(output, "instagram", {"state": "running", "ok": None, "error": None})

                if not getattr(settings, "ENABLE_PUBLISH_IG", False):
                    output["instagram"] = {"enabled": False, "reason": "ENABLE_PUBLISH_IG=false"}
                    self._aset(output, "instagram", {"state": "skipped", "ok": False, "error": "disabled"})
                elif not reel_url:
                    output["instagram"] = {"enabled": True, "ok": False, "error": "Missing reel_url"}
                    self._aset(output, "instagram", {"state": "failed", "ok": False, "error": "Missing reel_url"})
                else:
                    pub = InstagramPublisher()
                    res = await pub.publish_reel(video_url=reel_url, caption=self._caption(use_case))
                    output["instagram"] = {
                        "enabled": True,
                        "ok": res.ok,
                        "media_id": res.media_id,
                        "permalink": getattr(res, "permalink", None),
                        "error": res.error,
                    }
                    self._aset(
                        output,
                        "instagram",
                        {
                            "state": "succeeded" if res.ok else "failed",
                            "ok": bool(res.ok),
                            "media_id": str(res.media_id) if res.media_id else None,
                            "permalink": getattr(res, "permalink", None),
                            "error": res.error,
                        },
                    )

                # upsert
                try:
                    ig = output.get("instagram") or {}
                    if isinstance(ig, dict) and ig.get("enabled") and ig.get("ok") and ig.get("media_id"):
                        await self.platform_posts.upsert_post(
                            run_id=run_id,
                            platform="instagram",
                            media_id=str(ig.get("media_id")),
                            permalink=ig.get("permalink"),
                            status="published",
                            payload_json=ig,
                        )
                except Exception:
                    pass

                if publish_strict:
                    ig = output.get("instagram") or {}
                    if isinstance(ig, dict) and ig.get("enabled") and not ig.get("ok"):
                        raise MarketingRunFailed(
                            "INSTAGRAM_PUBLISH_FAILED",
                            str(ig.get("error") or "unknown"),
                            stage="publish",
                        )

            # ----------------
            # YouTube
            # ----------------
            do_yt_short = ("youtube_short" in publish_targets) or ("yt_short" in publish_targets)
            do_yt_long = ("youtube_long" in publish_targets) or ("yt_long" in publish_targets)
            if (do_yt_short or do_yt_long) and not self._publish_is_done(output, "youtube"):
                self._aset(output, "youtube", {"state": "running", "ok": None, "error": None})

                if not reel_url:
                    output["youtube"] = {"ok": False, "error": "Missing reel_url for YouTube upload"}
                    self._aset(output, "youtube", {"state": "failed", "ok": False, "error": "Missing reel_url"})
                    if publish_strict:
                        raise MarketingRunFailed("YOUTUBE_PUBLISH_FAILED", "Missing reel_url", stage="publish")
                    return

                yt_privacy = (inp.inputs or {}).get("youtube_privacy") or "public"
                yt = YouTubePublisher(accounts_repo=self.platform_accounts, secrets=self.secrets)
                title = use_case.hook_text
                desc = self._caption(use_case)

                if do_yt_long or fmt == "yt_long":
                    yt_res = await yt.publish_long(
                        platform_account_id=None,
                        video_source_url=reel_url,
                        title=title,
                        description=desc,
                        tags=["desifaces", "ai", "creators"],
                        privacy_status=yt_privacy,
                        thumbnail_path=None,
                    )
                else:
                    yt_res = await yt.publish_short(
                        platform_account_id=None,
                        video_source_url=reel_url,
                        title=title,
                        description=desc,
                        tags=["desifaces", "ai", "shorts"],
                        privacy_status=yt_privacy,
                        thumbnail_path=None,
                    )

                output["youtube"] = {"ok": yt_res.ok, "video_id": yt_res.video_id, "url": yt_res.url, "error": yt_res.error}
                self._aset(
                    output,
                    "youtube",
                    {
                        "state": "succeeded" if yt_res.ok else "failed",
                        "ok": bool(yt_res.ok),
                        "video_id": str(yt_res.video_id) if yt_res.video_id else None,
                        "url": str(yt_res.url) if yt_res.url else None,
                        "error": yt_res.error,
                    },
                )

                try:
                    if yt_res.ok and yt_res.video_id:
                        await self.platform_posts.upsert_post(
                            run_id=run_id,
                            platform="youtube",
                            media_id=str(yt_res.video_id),
                            permalink=str(yt_res.url) if yt_res.url else None,
                            status="published",
                            payload_json=output["youtube"],
                        )
                except Exception:
                    pass

                if publish_strict and not yt_res.ok:
                    raise MarketingRunFailed("YOUTUBE_PUBLISH_FAILED", str(yt_res.error or "unknown"), stage="publish")

        try:
            await asyncio.wait_for(_do_publish(), timeout=float(timeout_s))
        except asyncio.TimeoutError:
            output["publish_timeout"] = f"publish exceeded {timeout_s}s"
            # surface per-target timeout state too (useful for UI)
            self._aset(output, "instagram", {"state": "timeout"})
            self._aset(output, "youtube", {"state": "timeout"})
            if publish_strict:
                raise MarketingRunFailed("PUBLISH_TIMEOUT", output["publish_timeout"], stage="publish")

        return output