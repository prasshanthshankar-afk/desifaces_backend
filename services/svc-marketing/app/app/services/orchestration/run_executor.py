# services/svc-marketing/app/app/services/orchestration/run_executor.py
from __future__ import annotations

import logging
import os
import re
import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Tuple
from uuid import UUID

import asyncpg
import httpx

from app.domain.enums import MarketingRunMode, RecipeKind
from app.domain.models import MarketingRunIn
from app.repos.marketing_assets_repo import MarketingAssetsRepo
from app.repos.marketing_platform_accounts_repo import MarketingPlatformAccountsRepo
from app.repos.marketing_platform_posts_repo import MarketingPlatformPostsRepo
from app.repos.marketing_runs_repo import MarketingRunsRepo
from app.repos.marketing_use_cases_repo import MarketingUseCasesRepo
from app.repos.ops_cost_ledger_repo import OpsCostLedgerRepo
from app.services.orchestration.downstream_clients import (
    SvcAudioClient,
    SvcCommerceClient,
    SvcFaceClient,
    SvcFusionClient,
    SvcMusicClient,
)
from app.services.orchestration.errors import MarketingRunFailed
from app.services.orchestration.recipes.commerce_catalog_promo import CommerceCatalogPromoRecipe
from app.services.orchestration.recipes.face_audio_video import FaceAudioVideoRecipe
from app.services.orchestration.recipes.runner import RecipeRunner
from app.services.orchestration.run_context import RunContext
from app.services.orchestration.stages.branding_stage import BrandingStage
from app.services.orchestration.stages.compose_stage import ComposeStage
from app.services.orchestration.stages.generate_stage import GenerateStage
from app.services.orchestration.stages.planning_stage import PlanningStage
from app.services.orchestration.stages.publish_stage import PublishStage
from app.services.orchestration.utils.config import cfg_bool, cfg_int, cfg_str
from app.services.orchestration.utils.determinism import stable_u32_from_run_id
from app.services.orchestration.utils.jsonx import as_dict
from app.services.secrets.secret_provider import DefaultSecretProvider
from app.services.storage.blob_uploader import BlobUploader

logger = logging.getLogger("svc-marketing-executor")

# quiet extremely noisy Azure request/response logging (409 ContainerAlreadyExists is not fatal)
logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)
logging.getLogger("azure.storage.blob").setLevel(logging.WARNING)

# -------------------------
# helpers
# -------------------------


def _normalize_format_hint(inp: MarketingRunIn) -> str:
    fmt = (inp.inputs or {}).get("format_hint") or "reel"
    fmt = str(fmt).strip().lower()
    if fmt not in ("reel", "story", "carousel", "yt_short", "yt_long"):
        fmt = "reel"
    return fmt


def _publish_targets(inp: MarketingRunIn) -> List[str]:
    v = (inp.inputs or {}).get("publish_targets") or []
    if isinstance(v, str):
        return [v.strip().lower()] if v.strip() else []
    if isinstance(v, list):
        out: List[str] = []
        for x in v:
            s = str(x).strip().lower()
            if s:
                out.append(s)
        return out
    return []


def _default_publish_targets(fmt: str) -> List[str]:
    if fmt in ("reel", "yt_short"):
        return ["instagram_reel", "youtube_short"]
    if fmt == "yt_long":
        return ["youtube_long"]
    return ["instagram_reel"]


_SCRIPT_PREFIX_RE = re.compile(
    r"^\s*(script|voiceover|narration|audio script|tts|speaker)\s*[:\-]\s*",
    re.IGNORECASE,
)


def _coerce_voice_text(x: Any) -> str:
    if x is None:
        return ""
    if isinstance(x, str):
        s = x.strip()
    elif isinstance(x, dict):
        for k in ("voiceover_text", "voiceover", "narration", "audio_script", "script", "text", "tts_text"):
            v = x.get(k)
            if isinstance(v, str) and v.strip():
                s = v.strip()
                break
        else:
            s = str(x)
    else:
        s = str(x)

    s = re.sub(r"^```.*?$|```$", "", s, flags=re.MULTILINE).strip()
    s = re.sub(r"^\s*#+\s*", "", s).strip()
    s = re.sub(r"^\s*[\-\*\u2022]\s+", "", s).strip()
    s = _SCRIPT_PREFIX_RE.sub("", s).strip()
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _sanitize_use_case_in_place(use_case: Any) -> None:
    candidate_fields = ("voiceover_text", "audio_script", "script", "tts_text", "narration")
    for f in candidate_fields:
        try:
            if hasattr(use_case, f):
                v = getattr(use_case, f)
                if v is not None:
                    setattr(use_case, f, _coerce_voice_text(v))
        except Exception:
            pass

    for f in ("computed", "meta", "extras"):
        try:
            if hasattr(use_case, f):
                d = getattr(use_case, f)
                if isinstance(d, dict):
                    for k in ("voiceover_text", "audio_script", "script", "tts_text", "narration"):
                        if k in d:
                            d[k] = _coerce_voice_text(d.get(k))
        except Exception:
            pass


def _normalize_bearer_token(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    if s.lower().startswith("bearer "):
        tok = s.split(None, 1)[1].strip()
        return f"Bearer {tok}" if tok else ""
    return f"Bearer {s}"


def _branding_enabled() -> bool:
    return bool(cfg_bool("MARKETING_BRAND_ENABLE", False) or cfg_bool("MARKETING_BRAND_LOGO_ENABLE", True))


def _pick_reel_url_from_output(output: Dict[str, Any]) -> str:
    if not isinstance(output, dict):
        return ""
    for k in ("reel_url", "reel_mp4_url", "video_url", "mp4_url", "final_url", "output_url", "url"):
        v = output.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
    computed = output.get("computed")
    if isinstance(computed, dict):
        for k in ("reel_url", "video_url", "final_url", "url"):
            v = computed.get(k)
            if isinstance(v, str) and v.startswith("http"):
                return v
    return ""


def _merge_branded_url_into_output(output: Dict[str, Any], branded_url: str) -> Dict[str, Any]:
    if not branded_url or not branded_url.startswith("http"):
        return output
    out = dict(output or {})
    out["reel_url"] = branded_url
    out["video_url"] = branded_url
    out["final_url"] = branded_url
    if isinstance(out.get("computed"), dict):
        c = dict(out["computed"])
        c["reel_url"] = branded_url
        c["video_url"] = branded_url
        c["final_url"] = branded_url
        out["computed"] = c
    return out


def _dedupe_repeated_phrase(text: str, phrase: str) -> str:
    if not text or not phrase:
        return text
    pat = re.compile(rf"({re.escape(phrase)})(?:\s*[\.\!\?\-–—,:;]*\s*\1)+", re.IGNORECASE)
    text2 = pat.sub(r"\1", text)
    lines = [ln.strip() for ln in text2.splitlines() if ln.strip()]
    out_lines: List[str] = []
    seen = set()
    for ln in lines:
        key = ln.lower()
        if key in seen:
            continue
        seen.add(key)
        out_lines.append(ln)
    return "\n".join(out_lines).strip()


def _json_sanitize(x: Any) -> Any:
    if x is None:
        return None
    if isinstance(x, (str, int, float, bool)):
        return x
    if isinstance(x, UUID):
        return str(x)
    if isinstance(x, (datetime, date)):
        return x.isoformat()
    if isinstance(x, Decimal):
        return float(x)
    if isinstance(x, bytes):
        try:
            return x.decode("utf-8", "ignore")
        except Exception:
            return str(x)
    if isinstance(x, dict):
        return {str(k): _json_sanitize(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_json_sanitize(v) for v in x]
    if isinstance(x, tuple):
        return [_json_sanitize(v) for v in x]
    return str(x)


def _deep_merge_dict(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deep merge where 'extra' wins.
    Used to preserve keys created by PublishStage while still updating computed artifact_status.
    """
    out: Dict[str, Any] = dict(base or {})
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge_dict(out[k], v)  # type: ignore[arg-type]
        else:
            out[k] = v
    return out


def _artifact_status_from_output(stage: str, output: Dict[str, Any]) -> Dict[str, Any]:
    """
    Best-effort artifact/progress map shown in /status output_json.
    Lives inside output_json["artifact_status"] (no schema change).
    """
    o = output or {}
    face_ok = bool(isinstance(o.get("face_image_url"), str) and o["face_image_url"].startswith("http"))
    audio_ok = bool(isinstance(o.get("voice_audio_url"), str) and o["voice_audio_url"].startswith("http"))
    reel_ok = bool(isinstance(o.get("reel_url"), str) and o["reel_url"].startswith("http"))

    fusion_raw = o.get("fusion_raw")
    fusion_status = ""
    fusion_error = ""
    if isinstance(fusion_raw, dict):
        fusion_status = str(fusion_raw.get("status") or fusion_raw.get("state") or "")
        fusion_error = str(fusion_raw.get("error_message") or fusion_raw.get("error") or "")
        if not fusion_error and isinstance(fusion_raw.get("error_detail"), str):
            fusion_error = str(fusion_raw.get("error_detail") or "")

    ig = o.get("instagram")
    yt = o.get("youtube")
    ig_ok = bool(isinstance(ig, dict) and ig.get("ok") is True)
    yt_ok = bool(isinstance(yt, dict) and yt.get("ok") is True)

    st = str(stage or "").lower()
    after_generate = st in ("generate", "branding", "compose", "publish", "done", "succeeded")
    after_branding = st in ("branding", "compose", "publish", "done", "succeeded")
    after_compose = st in ("compose", "publish", "done", "succeeded")

    return {
        "stage": st,
        "face": {"status": "succeeded" if face_ok else ("in_progress" if after_generate else "pending")},
        "audio": {"status": "succeeded" if audio_ok else ("in_progress" if after_generate else "pending")},
        "video": {
            "status": "succeeded"
            if reel_ok
            else ("failed" if fusion_error else ("in_progress" if after_generate else "pending")),
            "fusion_status": fusion_status,
            "fusion_error": fusion_error,
        },
        "branding": {
            "status": "succeeded"
            if (after_branding and reel_ok)
            else ("in_progress" if st == "branding" else ("pending" if not after_branding else "in_progress"))
        },
        "compose": {
            "status": "succeeded"
            if (after_compose and reel_ok)
            else ("in_progress" if st == "compose" else ("pending" if not after_compose else "in_progress"))
        },
        "publish": {"status": "succeeded" if (ig_ok or yt_ok) else ("in_progress" if st == "publish" else "pending")},
    }


# -------------------------
# service-account auth (NO DB persistence of tokens)
# -------------------------

_SERVICE_AUTH_CACHE: Dict[str, Any] = {"bearer": "", "user_id": "", "exp": 0.0}


async def _get_service_auth() -> Tuple[str, str]:
    """
    Logs into svc-core using DF_SERVICE_EMAIL / DF_SERVICE_PASSWORD.
    Keeps token only in-memory with a short TTL; never persisted to DB.
    """
    use_service = cfg_bool("MARKETING_USE_SERVICE_ACCOUNT", True)
    if not use_service:
        return "", ""

    email = (os.getenv("DF_SERVICE_EMAIL") or cfg_str("DF_SERVICE_EMAIL", "")).strip()
    password = (os.getenv("DF_SERVICE_PASSWORD") or cfg_str("DF_SERVICE_PASSWORD", "")).strip()
    if not email or not password:
        return "", ""

    now = time.time()
    ttl_s = int(cfg_int("MARKETING_SERVICE_TOKEN_TTL_S", 45 * 60))
    if _SERVICE_AUTH_CACHE.get("bearer") and float(_SERVICE_AUTH_CACHE.get("exp") or 0.0) > (now + 30):
        return str(_SERVICE_AUTH_CACHE["bearer"]), str(_SERVICE_AUTH_CACHE.get("user_id") or "")

    core_url = cfg_str("CORE_URL", "http://svc-core:8000").rstrip("/")
    login_path = cfg_str("MARKETING_CORE_LOGIN_PATH", "/api/auth/login")
    url = core_url + login_path

    payloads = [
        {"email": email, "password": password},
        {"username": email, "password": password},
    ]

    last_err = ""
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
        for body in payloads:
            try:
                r = await client.post(url, json=body)
                r.raise_for_status()
                js = r.json() if r.content else {}
                tok = js.get("access_token") or js.get("token") or js.get("bearer_token") or js.get("jwt") or ""
                bearer = _normalize_bearer_token(str(tok))
                uid = str(js.get("user_id") or js.get("x_user_id") or js.get("id") or "").strip()

                if bearer:
                    _SERVICE_AUTH_CACHE["bearer"] = bearer
                    _SERVICE_AUTH_CACHE["user_id"] = uid
                    _SERVICE_AUTH_CACHE["exp"] = now + float(ttl_s)
                    return bearer, uid
                last_err = f"missing token in response keys={list(js.keys())}"
            except Exception as e:
                last_err = str(e)

    logger.warning("service login failed: %s", last_err)
    return "", ""


# -------------------------
# executor
# -------------------------


class RunExecutor:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self.runs = MarketingRunsRepo(pool)
        self.assets = MarketingAssetsRepo(pool)
        self.usecases = MarketingUseCasesRepo(pool)
        self.cost = OpsCostLedgerRepo(pool)
        self.platform_posts = MarketingPlatformPostsRepo(pool)
        self.platform_accounts = MarketingPlatformAccountsRepo(pool)
        self.secrets = DefaultSecretProvider()

        # downstream clients
        self.face = SvcFaceClient()
        self.audio = SvcAudioClient()
        self.fusion = SvcFusionClient()
        self.music = SvcMusicClient()
        self.commerce = SvcCommerceClient()

        self.uploader = BlobUploader()

        # ---- planning stage (festival calendar wired safely) ----
        festival_repo = None
        try:
            # Keep this import INSIDE __init__ so missing repo never breaks import-time startup
            from app.repos.festival_calendar_repo import FestivalCalendarRepo  # type: ignore

            festival_repo = FestivalCalendarRepo(self.pool)
        except Exception as e:
            logger.info("FestivalCalendarRepo not enabled/available: %s", str(e))

        # Support both PlanningStage signatures:
        #   PlanningStage(runs, usecases)
        #   PlanningStage(runs, usecases, festival_repo=...)
        #   PlanningStage(runs, usecases, festival_repo)
        try:
            self.planning_stage = PlanningStage(self.runs, self.usecases, festival_repo=festival_repo)  # type: ignore[arg-type]
        except TypeError:
            try:
                self.planning_stage = PlanningStage(self.runs, self.usecases, festival_repo)  # type: ignore[arg-type]
            except TypeError:
                self.planning_stage = PlanningStage(self.runs, self.usecases)

        runner = RecipeRunner(
            face_audio_video=FaceAudioVideoRecipe(
                face_client=self.face,
                audio_client=self.audio,
                fusion_client=self.fusion,
                uploader=self.uploader,
            ),
            commerce_catalog=CommerceCatalogPromoRecipe(self.commerce),
        )
        self.generate_stage = GenerateStage(runner)
        self.branding_stage = BrandingStage(self.assets, self.uploader)
        self.compose_stage = ComposeStage(self.assets, self.uploader)
        self.publish_stage = PublishStage(
            platform_posts=self.platform_posts,
            platform_accounts=self.platform_accounts,
            secrets=self.secrets,
        )

    async def _persist_output(self, run_id: UUID, out: Dict[str, Any], stage: str = "") -> None:
        try:
            out2 = dict(out or {})

            # Merge artifact_status (DO NOT overwrite what PublishStage wrote)
            if stage:
                base = _artifact_status_from_output(stage, out2)
                existing = out2.get("artifact_status")
                if isinstance(existing, dict):
                    out2["artifact_status"] = _deep_merge_dict(base, existing)
                else:
                    out2["artifact_status"] = base

            # NEVER persist bearer tokens
            out2.pop("bearer_token", None)
            auth = out2.get("auth")
            if isinstance(auth, dict):
                auth.pop("bearer_token", None)
                auth.pop("access_token", None)

            safe = _json_sanitize(out2)
            await self.runs.set_output_json(run_id, safe)
        except Exception as e:
            logger.warning("run=%s persist_output failed: %s", str(run_id), str(e))

    async def _safe_update_stage(self, run_id: UUID, stage: str) -> None:
        try:
            await self.runs.update_stage(run_id, stage)
        except Exception:
            pass

    async def publish_only(self, run_id: UUID) -> None:
        """
        Used by /runs/{run_id}/publish endpoint. Assumes compose is already done and reel_url exists.
        """
        row = await self.runs.get_run_row(run_id)
        if not row:
            return

        output: Dict[str, Any] = as_dict(row.get("output_json"))
        input_json = as_dict(row.get("input_json"))
        planning_json = as_dict(row.get("planning_json"))

        merged = dict(input_json or {})
        merged["inputs"] = as_dict(merged.get("inputs"))
        inp = MarketingRunIn(**merged)

        use_case_dict = (planning_json or {}).get("use_case") if isinstance(planning_json, dict) else None
        if not isinstance(use_case_dict, dict):
            raise MarketingRunFailed("MISSING_USE_CASE", "planning_json.use_case missing", stage="publish")

        # service-account auth required
        svc_bearer, svc_uid = await _get_service_auth()
        if cfg_bool("MARKETING_USE_SERVICE_ACCOUNT", True) and not svc_bearer:
            raise MarketingRunFailed(
                "SERVICE_AUTH_FAILED",
                "Service account auth required. Set DF_SERVICE_EMAIL and DF_SERVICE_PASSWORD (and ensure CORE_URL is reachable).",
                stage="auth",
            )

        run_as_user_id = row.get("run_as_user_id")
        if svc_uid:
            try:
                run_as_user_id = UUID(str(svc_uid))
            except Exception:
                pass

        _ = RunContext(
            run_id=run_id,
            run_as_user_id=run_as_user_id,
            bearer_token=svc_bearer,
            cost_bucket=str(row.get("cost_bucket") or ""),
            cost_category=str(row.get("cost_category") or ""),
        )

        fmt = _normalize_format_hint(inp)
        publish_targets = _publish_targets(inp) or _default_publish_targets(fmt)

        from app.domain.models import UseCaseSpec  # local import to avoid cycles in some builds

        use_case = UseCaseSpec(**use_case_dict)
        _sanitize_use_case_in_place(use_case)

        publish_timeout = cfg_int("MARKETING_PUBLISH_TIMEOUT_S", 900)

        await self._safe_update_stage(run_id, "publish")
        output = await self.publish_stage.run(
            run_id=run_id,
            inp=inp,
            use_case=use_case,
            fmt=fmt,
            publish_targets=publish_targets,
            output=output,
            timeout_s=publish_timeout,
        )
        await self._persist_output(run_id, output, stage="publish")

    async def execute(self, run_id: UUID) -> None:
        row = await self.runs.get_run_row(run_id)
        if not row:
            return

        status = str(row.get("status") or "").lower()
        if status in ("succeeded", "failed", "canceled", "cancelled"):
            return

        input_json = as_dict(row.get("input_json"))
        planning_json = as_dict(row.get("planning_json"))
        output: Dict[str, Any] = as_dict(row.get("output_json"))

        # parse enums
        try:
            mode = MarketingRunMode(str(row["mode"]))
        except Exception:
            await self.runs.mark_failed(run_id, "worker", "INVALID_MODE", f"Invalid mode='{row.get('mode')}'")
            return
        try:
            recipe = RecipeKind(str(row["recipe"]))
        except Exception:
            await self.runs.mark_failed(run_id, "worker", "INVALID_RECIPE", f"Invalid recipe='{row.get('recipe')}'")
            return

        merged = dict(input_json or {})
        merged.setdefault("mode", mode.value)
        merged.setdefault("recipe", recipe.value)
        merged["inputs"] = as_dict(merged.get("inputs"))
        for k in ("persona", "industry", "season_event", "offer", "language_hint", "target_seconds", "use_case_id"):
            if merged.get(k) is None and k in merged["inputs"]:
                merged[k] = merged["inputs"].get(k)

        inp = MarketingRunIn(**merged)

        try:
            run_seed = int(float(input_json.get("seed") or 0))
        except Exception:
            run_seed = 0
        if run_seed <= 0:
            run_seed = stable_u32_from_run_id(run_id)

        request_nonce = str(input_json.get("request_nonce") or "").strip() or str(run_id)

        # choose publish targets
        fmt = _normalize_format_hint(inp)
        publish_targets = _publish_targets(inp)
        if mode == MarketingRunMode.publish and not publish_targets:
            publish_targets = _default_publish_targets(fmt)

        output["format_hint"] = fmt
        output["publish_targets"] = publish_targets
        output.setdefault("run_seed", int(run_seed))
        output.setdefault("request_nonce", str(request_nonce))
        output.setdefault("artifact_status", {})

        # -------- auth: service account only (tokens never persisted) --------
        svc_bearer, svc_uid = await _get_service_auth()
        if cfg_bool("MARKETING_USE_SERVICE_ACCOUNT", True) and not svc_bearer:
            await self._persist_output(
                run_id,
                {
                    **output,
                    "auth": {"mode": "service_account", "ok": False},
                    "error": "Service account auth required. Set DF_SERVICE_EMAIL and DF_SERVICE_PASSWORD (and ensure CORE_URL is reachable).",
                },
                stage="auth",
            )
            await self.runs.mark_failed(
                run_id,
                "auth",
                "SERVICE_AUTH_FAILED",
                "Service account auth required. Set DF_SERVICE_EMAIL and DF_SERVICE_PASSWORD (and ensure CORE_URL is reachable).",
            )
            return

        run_as_user_id = row.get("run_as_user_id")
        if svc_uid:
            try:
                run_as_user_id = UUID(str(svc_uid))
            except Exception:
                pass

        output["auth"] = {"mode": "service_account", "ok": True, "run_as_user_id": str(run_as_user_id)}
        await self._persist_output(run_id, output, stage="start")

        ctx = RunContext(
            run_id=run_id,
            run_as_user_id=run_as_user_id,
            bearer_token=svc_bearer,
            cost_bucket=str(row.get("cost_bucket") or ""),
            cost_category=str(row.get("cost_category") or ""),
        )

        planning_timeout = cfg_int("MARKETING_PLANNING_TIMEOUT_S", 120)
        generate_timeout = cfg_int("MARKETING_GENERATE_TIMEOUT_S", 1800)
        compose_timeout = cfg_int("MARKETING_COMPOSE_TIMEOUT_S", 180)
        publish_timeout = cfg_int("MARKETING_PUBLISH_TIMEOUT_S", 900)

        t0 = time.time()
        current_stage = str(row.get("stage") or "start").lower()

        try:
            # planning
            current_stage = "planning"
            await self._safe_update_stage(run_id, current_stage)
            use_case = await self.planning_stage.run(
                run_id=run_id,
                inp=inp,
                planning_json=planning_json,
                timeout_s=planning_timeout,
            )
            _sanitize_use_case_in_place(use_case)

            # generate
            current_stage = "generate"
            await self._safe_update_stage(run_id, current_stage)
            output = await self.generate_stage.run(
                recipe=recipe,
                ctx=ctx,
                inp=inp,
                use_case=use_case,
                output=output,
                timeout_s=generate_timeout,
                run_seed=int(run_seed),
                request_nonce=str(request_nonce),
            )
            await self._persist_output(run_id, output, stage=current_stage)

            # branding (must NOT crash the run)
            if _branding_enabled():
                current_stage = "branding"
                await self._safe_update_stage(run_id, current_stage)

                reel_url = _pick_reel_url_from_output(output)
                branded = await self.branding_stage.run(
                    run_id=run_id,
                    reel_url=reel_url,
                    fmt="mp4",
                    output=output,
                )

                if isinstance(branded, dict):
                    output = as_dict(branded)
                elif isinstance(branded, str) and branded:
                    output = _merge_branded_url_into_output(output, branded)

                await self._persist_output(run_id, output, stage=current_stage)

            # compose
            current_stage = "compose"
            await self._safe_update_stage(run_id, current_stage)
            output = await self.compose_stage.run(
                run_id=run_id,
                mode=mode,
                recipe=recipe,
                fmt=fmt,
                publish_targets=publish_targets,
                inp=inp,
                use_case=use_case,
                output=output,
                timeout_s=compose_timeout,
            )

            # dedupe CTA line if it appears twice
            cta = "DM desifaces and I will show you how I do it"
            for k in ("caption", "caption_text", "voiceover_text", "script", "narration"):
                v = output.get(k)
                if isinstance(v, str) and v:
                    output[k] = _dedupe_repeated_phrase(v, cta)

            await self._persist_output(run_id, output, stage=current_stage)

            # publish
            if mode == MarketingRunMode.publish and publish_targets:
                current_stage = "publish"
                await self._safe_update_stage(run_id, current_stage)
                output = await self.publish_stage.run(
                    run_id=run_id,
                    inp=inp,
                    use_case=use_case,
                    fmt=fmt,
                    publish_targets=publish_targets,
                    output=output,
                    timeout_s=publish_timeout,
                )
                await self._persist_output(run_id, output, stage=current_stage)

            output.setdefault("timing", {})
            output["timing"]["run_total_s"] = round(time.time() - t0, 3)
            await self._persist_output(run_id, output, stage="succeeded")

            await self.runs.mark_succeeded(run_id)

        except MarketingRunFailed as e:
            await self._persist_output(run_id, output, stage=current_stage)
            logger.exception("run=%s failed stage=%s err=%s", str(run_id), e.stage, str(e))
            await self.runs.mark_failed(run_id, e.stage or current_stage, e.code, e.message)

        except Exception as e:
            await self._persist_output(run_id, output, stage=current_stage)
            logger.exception("run=%s failed stage=%s err=%s", str(run_id), current_stage, str(e))
            await self.runs.mark_failed(run_id, current_stage or "error", "MARKETING_RUN_FAILED", str(e))