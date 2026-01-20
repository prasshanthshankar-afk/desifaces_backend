from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import asyncpg

from app.config import settings
from app.domain.enums import StepCode
from app.domain.models import FusionJobCreate
from app.services.idempotency_service import request_hash, provider_idempotency_key
from app.services.providers.heygen.av4_payload import build_av4_payload
from app.services.providers.heygen.client import HeyGenAV4Client, HeyGenApiError
from app.services.artifact_service import ArtifactService
from app.repos.fusion_jobs_repo import FusionJobsRepo
from app.repos.provider_runs_repo import ProviderRunsRepo
from app.repos.steps_repo import StepsRepo
from app.repos.artifacts_repo import ArtifactsRepo
from app.repos.digital_performances_repo import DigitalPerformancesRepo
from app.services.providers.heygen.assets import HeyGenAssetsClient

logger = logging.getLogger("fusion_orchestrator")


def _classify_error(e: Exception) -> str:
    msg = str(e).lower()
    if isinstance(e, HeyGenApiError):
        if "voice not found" in msg:
            return "HEYGEN_VOICE_NOT_FOUND"
        if "empty_body" in msg or "invalid_json" in msg:
            return "HEYGEN_TRANSIENT_EMPTY_BODY"
        if "timed out" in msg or "timeout" in msg:
            return "HEYGEN_TIMEOUT"
        return "HEYGEN_API_ERROR"
    if "requires" in msg and ("face" in msg or "audio" in msg):
        return "INVALID_REQUEST"
    return "FUSION_FAILED"


def _url_base(u: Optional[str]) -> Optional[str]:
    """
    Make request_hash stable when caller supplies SAS URLs.
    Drops querystring; keeps scheme+host+path.
    """
    if not u:
        return None
    s = str(u).strip()
    if not s:
        return None
    try:
        p = urlparse(s)
        if not p.scheme or not p.netloc:
            return s
        return f"{p.scheme}://{p.netloc}{p.path}"
    except Exception:
        return s.split("?", 1)[0]


class FusionOrchestrator:
    """
    Deterministic HeyGen AV4 orchestration.

    Key behavior for end-to-end with svc-face + svc-audio:
      - UI should pass artifact IDs (face_artifact_id, audio_artifact_id)
      - Fusion mints fresh SAS at run time to avoid expired SAS URLs
      - Still supports legacy URLs (face_image_url and voice_audio.audio_url)
    """

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self.jobs = FusionJobsRepo(pool)
        self.steps = StepsRepo(pool)
        self.runs = ProviderRunsRepo(pool)
        self.artifacts = ArtifactsRepo(pool)
        self.perfs = DigitalPerformancesRepo(pool)

        self.provider = HeyGenAV4Client()
        self.assets = HeyGenAssetsClient()
        self.artifact_service = ArtifactService()

    def _sas_ttl_hours(self) -> int:
        """
        TTL for minted SAS links used by HeyGen to fetch assets.
        Prefer a configured value if available; otherwise 4 hours.
        """
        # If you standardize this later, great; for now keep it simple.
        ttl = getattr(settings, "AZURE_SAS_EXPIRY_HOURS", None)
        try:
            if ttl:
                return max(1, int(ttl))
        except Exception:
            pass
        return 4

    async def create_job(self, user_id: str, req: FusionJobCreate) -> str:
        """
        Create a new fusion job.

        IMPORTANT:
        - Prefer stable IDs (artifact IDs / heygen_talking_photo_id / image_key) for hashing.
        - If a URL is provided (SAS), strip query string for stability.
        """
        face_artifact_id = getattr(req, "face_artifact_id", None)

        # audio artifact id is nested under voice_audio in your models
        voice_audio = getattr(req, "voice_audio", None)
        audio_artifact_id = getattr(voice_audio, "audio_artifact_id", None) if voice_audio else None

        stable_spec: Dict[str, Any] = {
            "provider": req.provider,
            "voice_mode": req.voice_mode.value,

            # Face stable identifiers (prefer these)
            "face_artifact_id": str(face_artifact_id) if face_artifact_id else None,
            "heygen_talking_photo_id": (req.heygen_talking_photo_id.strip() if req.heygen_talking_photo_id else None),
            "image_key": (req.image_key.strip() if req.image_key else None),

            # Audio stable identifiers (prefer these)
            "audio_artifact_id": str(audio_artifact_id) if audio_artifact_id else None,

            # Back-compat: if URLs are used, strip SAS query string so hash is stable
            "face_image_url_base": _url_base(str(req.face_image_url)) if getattr(req, "face_image_url", None) else None,
            "voice_audio_url_base": (
                _url_base(str(voice_audio.audio_url)) if (voice_audio and voice_audio.audio_url) else None
            ),

            # TTS mode stable fields
            "voice_id": req.voice_tts.voice_id if req.voice_tts else None,
            "script": req.voice_tts.script if req.voice_tts else None,

            # Video settings + payload version
            "video": req.video.model_dump(),
            "payload_version": settings.HEYGEN_AV4_PAYLOAD_VERSION,
        }

        stable_spec = {k: v for k, v in stable_spec.items() if v is not None}

        req_hash = request_hash(stable_spec)

        job_id = await self.jobs.insert_job(
            user_id=user_id,
            request_hash=req_hash,
            payload=req.model_dump(),
        )
        return job_id

    async def _resolve_face_url(self, job_id: str, req: FusionJobCreate) -> str:
        """
        Resolve face input to a fetchable URL/SAS for HeyGen asset upload.

        Priority:
          1) req.face_image_url (legacy/direct; may already be SAS)
          2) req.face_artifact_id -> mint fresh SAS from artifacts table (preferred)
        """
        if getattr(req, "face_image_url", None):
            return str(req.face_image_url)

        face_artifact_id = getattr(req, "face_artifact_id", None)
        if not face_artifact_id:
            raise ValueError("Provide face_image_url or face_artifact_id")

        row = await self.artifacts.get_artifact_by_id(str(face_artifact_id))
        if not row:
            raise ValueError(f"face_artifact_not_found: {face_artifact_id}")

        # Optional sanity check (don’t hard fail on older kinds; just warn)
        kind = str(row.get("kind") or "")
        if kind and kind not in ("face", "image", "face_image"):
            logger.warning("face_artifact_kind_unexpected", extra={"job_id": job_id, "kind": kind, "artifact_id": str(face_artifact_id)})

        face_url = await self.artifact_service.mint_read_sas_for_artifact(dict(row), ttl_hours=self._sas_ttl_hours())

        # Keep audit trail (helpful in debugging)
        await self.artifacts.add_artifact(
            job_id,
            "resolved_face_sas_url",
            face_url,
            content_type="text/uri-list",
            meta_json={"source": "artifact_id", "artifact_id": str(face_artifact_id)},
        )
        return str(face_url)



    async def _resolve_audio_url(self, job_id: str, req: FusionJobCreate) -> str:
        """
        Resolve audio input to a fetchable Azure Blob SAS URL.

        For voice_mode=audio:
        1) req.voice_audio.audio_url (direct)
        2) req.voice_audio.audio_artifact_id (preferred stable id -> mint SAS)
        """
        if req.voice_audio and req.voice_audio.audio_url:
            return str(req.voice_audio.audio_url)

        audio_artifact_id = None
        if req.voice_audio and getattr(req.voice_audio, "audio_artifact_id", None):
            audio_artifact_id = str(req.voice_audio.audio_artifact_id)

        if not audio_artifact_id:
            raise ValueError("voice_mode=audio requires voice_audio.audio_url or voice_audio.audio_artifact_id")

        row = await self.artifacts.get_artifact_by_id(audio_artifact_id)  # type: ignore[attr-defined]
        if not row:
            raise ValueError(f"audio_artifact_not_found: {audio_artifact_id}")

        audio_url = await self.artifact_service.mint_read_sas_for_artifact(dict(row), ttl_hours=4)

        await self.artifacts.add_artifact(
            job_id,
            "resolved_audio_sas_url",
            audio_url,
            content_type="text/uri-list",
            meta_json={"source": "voice_audio.audio_artifact_id", "artifact_id": audio_artifact_id},
        )
        return str(audio_url)



    async def run_job(self, job_id: str) -> None:
        job = await self.jobs.get_job(job_id)
        if not job:
            logger.warning("job_not_found", extra={"job_id": job_id})
            return

        status = str(job.get("status") or "")
        if status in ("succeeded", "failed", "canceled"):
            logger.info("job_terminal_skip", extra={"job_id": job_id, "status": status})
            return

        payload_json = job["payload_json"]
        if isinstance(payload_json, str):
            import json as _json
            payload_json = _json.loads(payload_json)
        if not isinstance(payload_json, dict):
            raise ValueError(f"Unexpected payload_json type: {type(payload_json)}")

        req = FusionJobCreate.model_validate(payload_json)

        provider_name = "heygen_av4"
        req_hash = str(job["request_hash"])
        idem = provider_idempotency_key(provider_name, settings.HEYGEN_AV4_PAYLOAD_VERSION, req_hash)

        run_id: Optional[str] = None
        provider_job_id: Optional[str] = None
        image_key: Optional[str] = None
        audio_url_to_use: Optional[str] = None
        last_poll = None
        performance_id: Optional[str] = None

        # keep this stable for perf/meta
        user_id = str(job.get("user_id") or "").strip()

        try:
            # -------------------------
            # STEP 1: Uploads + Submit
            # -------------------------
            await self.steps.upsert_step(job_id, StepCode.provider_submit.value, "running", attempt=0)

            # -------------------------
            # FACE: resolve HeyGen talking photo id / image_key
            # -------------------------
            # Priority:
            # 1) req.heygen_talking_photo_id (client pre-upload)
            # 2) upload from resolved face SAS URL
            if getattr(req, "heygen_talking_photo_id", None):
                image_key = str(req.heygen_talking_photo_id).strip()
                if not image_key:
                    raise ValueError("heygen_talking_photo_id is empty after strip")

                # audit artifact (helps debugging and later reuse)
                await self.artifacts.add_artifact(
                    job_id,
                    "heygen_talking_photo_id",
                    image_key,
                    content_type="text/plain",
                    meta_json={"source": "request_payload"},
                )
            else:
                # Resolve face URL (direct or artifact-id -> fresh SAS)
                face_url = await self._resolve_face_url(job_id, req)

                # Keep audit trail: resolved SAS used for upload
                await self.artifacts.add_artifact(
                    job_id,
                    "resolved_face_sas_url",
                    face_url,
                    content_type="text/uri-list",
                    meta_json={"source": "resolve_face"},
                )

                # Upload face image -> image_key (HeyGen /v1/asset)
                img_upload = await self.assets.upload_image_asset_from_url(face_url)

                image_key = (img_upload.get("data") or {}).get("image_key") or img_upload.get("image_key")
                if not image_key:
                    raise ValueError(f"Image upload missing image_key: {img_upload}")
                image_key = str(image_key)

                await self.artifacts.add_artifact(
                    job_id,
                    "heygen_image_key",
                    image_key,
                    content_type="text/plain",
                    meta_json={"provider": "heygen", "upload": img_upload},
                )

            # -------------------------
            # AUDIO: resolve runtime audio URL if voice_mode=audio
            # -------------------------
            if req.voice_mode.value == "audio":
                audio_url_to_use = await self._resolve_audio_url(job_id, req)

                # audit artifact: the SAS we passed to HeyGen
                await self.artifacts.add_artifact(
                    job_id,
                    "resolved_audio_sas_url",
                    audio_url_to_use,
                    content_type="text/uri-list",
                    meta_json={"source": "resolve_audio"},
                )

                # (optional legacy kind you already used)
                await self.artifacts.add_artifact(
                    job_id,
                    "heygen_audio_url",
                    audio_url_to_use,
                    content_type="text/uri-list",
                    meta_json={"provider": "azure_blob"},
                )

            # -------------------------
            # Build + Submit AV4
            # -------------------------
            video_title = f"desifaces_fusion_{job_id}"
            av4_payload = build_av4_payload(
                req,
                talking_photo_id=image_key,
                video_title=video_title,
                audio_url_override=audio_url_to_use,
            )

            # Best-effort debug meta (keep it small; payload itself is in provider_runs)
            try:
                await self.steps.upsert_step(
                    job_id,
                    StepCode.provider_submit.value,
                    "running",
                    attempt=0,
                    meta_json={
                        "image_key": image_key,
                        "audio_url_present": bool(audio_url_to_use),
                        "idempotency_key": idem,
                    },
                )
            except Exception:
                pass

            # Idempotent run reuse
            existing = await self.runs.get_by_idempotency_key(idem)
            if existing and existing.get("provider_job_id"):
                provider_job_id = str(existing["provider_job_id"])
                run_id = str(existing["id"])
                logger.info(
                    "reuse_provider_job",
                    extra={"job_id": job_id, "provider_job_id": provider_job_id, "idempotency_key": idem},
                )
            else:
                run_id = await self.runs.create_run(
                    job_id=job_id,
                    provider=provider_name,
                    idempotency_key=idem,
                    request_json=av4_payload,
                )
                submit_res = await self.provider.submit(av4_payload, idem)
                provider_job_id = submit_res.provider_job_id
                await self.runs.mark_submitted(run_id, provider_job_id, submit_res.raw_response)

            if not provider_job_id:
                raise HeyGenApiError("provider_job_id missing after submit/reuse")

            await self.steps.upsert_step(
                job_id,
                StepCode.provider_submit.value,
                "succeeded",
                attempt=0,
                meta_json={"provider_job_id": provider_job_id, "idempotency_key": idem, "image_key": image_key},
            )

            # -------------------------
            # Canonical Digital Performance (processing)
            # NOTE: requires DigitalPerformancesRepo to accept user_id::uuid (repo SQL fix).
            # -------------------------
            performance_id = await self.perfs.upsert_performance(
                user_id=str(job["user_id"]), 
                provider=provider_name,
                provider_job_id=provider_job_id,
                status="processing",
                share_url=None,
                meta_json={
                    "job_id": job_id,
                    "request_hash": req_hash,
                    "idempotency_key": idem,
                    "image_key": image_key,
                    "voice_mode": req.voice_mode.value,
                    "payload_version": settings.HEYGEN_AV4_PAYLOAD_VERSION,
                },
            )
            await self.perfs.upsert_fusion_job_output(job_id, performance_id)

            # -------------------------
            # STEP 2: Poll
            # -------------------------
            await self.steps.upsert_step(
                job_id,
                StepCode.provider_poll.value,
                "running",
                attempt=0,
                meta_json={"provider_job_id": provider_job_id},
            )

            started = asyncio.get_running_loop().time()
            last_status: Optional[str] = None

            while True:
                if (asyncio.get_running_loop().time() - started) > settings.JOB_POLL_MAX_SECONDS:
                    raise HeyGenApiError("Provider polling timed out")

                poll = await self.provider.poll(provider_job_id)
                last_poll = poll

                # update provider run status when it changes
                if run_id and poll.status != last_status:
                    last_status = poll.status
                    try:
                        await self.runs.update_status(
                            run_id,
                            poll.status,
                            meta_json={"raw": poll.raw_response, "provider_job_id": provider_job_id},
                        )
                    except Exception:
                        logger.warning("provider_run_status_update_failed", extra={"job_id": job_id, "run_id": run_id})

                if poll.status == "processing":
                    await asyncio.sleep(settings.JOB_POLL_INTERVAL_SECONDS)
                    continue

                if poll.status == "failed":
                    if run_id:
                        await self.runs.update_status(
                            run_id,
                            "failed",
                            meta_json={"error": poll.error_message, "raw": poll.raw_response},
                        )
                    raise HeyGenApiError(poll.error_message or "Provider failed")

                # succeeded
                if not poll.video_url:
                    if run_id:
                        await self.runs.update_status(
                            run_id,
                            "failed",
                            meta_json={"error": "Provider success but missing video_url", "raw": poll.raw_response},
                        )
                    raise HeyGenApiError("Provider returned succeeded but video_url is missing")

                if run_id:
                    await self.runs.update_status(
                        run_id,
                        "succeeded",
                        meta_json={"raw": poll.raw_response, "provider_job_id": provider_job_id},
                    )

                await self.steps.upsert_step(job_id, StepCode.provider_poll.value, "succeeded", attempt=0)
                break

            # -------------------------
            # STEP 3: Finalize
            # -------------------------
            await self.steps.upsert_step(job_id, StepCode.finalize.value, "running", attempt=0)

            final_video_url = await self.artifact_service.persist_video_artifact(
                last_poll.video_url,
                user_id=str(job["user_id"]),
                job_id=job_id,
                provider_job_id=provider_job_id,
            )

            await self.artifacts.add_artifact(
                job_id,
                "video",
                final_video_url,
                content_type="video/mp4",
                meta_json={"provider": provider_name, "provider_job_id": provider_job_id, "image_key": image_key},
            )

            share_url_val: Optional[str] = None
            try:
                get_share = getattr(self.provider, "get_share_url", None)
                if callable(get_share):
                    share = await get_share(provider_job_id)
                    share_url = (share or {}).get("share_url")
                    if share_url:
                        share_url_val = str(share_url)
                        await self.artifacts.add_artifact(
                            job_id,
                            "share_url",
                            share_url_val,
                            content_type="text/uri-list",
                            meta_json={
                                "provider": provider_name,
                                "provider_job_id": provider_job_id,
                                "raw": (share or {}).get("raw"),
                            },
                        )
            except Exception as e:
                logger.warning(
                    "share_url_failed",
                    extra={"job_id": job_id, "provider_job_id": provider_job_id, "error": str(e)},
                )

            if performance_id:
                try:
                    await self.perfs.mark_ready(
                        performance_id,
                        share_url=share_url_val,
                        meta_json={
                            "job_id": job_id,
                            "provider_job_id": provider_job_id,
                            "video_url": final_video_url,
                            "status": "ready",
                            "user_id": str(job["user_id"]),
                        },
                    )
                except Exception:
                    logger.warning("perf_mark_ready_failed", extra={"job_id": job_id, "performance_id": performance_id})

            await self.steps.upsert_step(job_id, StepCode.finalize.value, "succeeded", attempt=0)
            await self.jobs.set_status(job_id, "succeeded")
            return

        except Exception as e:
            msg = str(e)
            code = _classify_error(e)
            logger.exception(
                "fusion_job_failed",
                extra={
                    "job_id": job_id,
                    "error_code": code,
                    "error": msg,
                    "provider_job_id": provider_job_id,
                    "run_id": run_id,
                    "image_key": image_key,
                    "performance_id": performance_id,
                },
            )

            try:
                if run_id:
                    await self.runs.update_status(
                        run_id,
                        "failed",
                        meta_json={"error_code": code, "error": msg, "provider_job_id": provider_job_id, "user_id": str(job["user_id"])},
                    )
            except Exception:
                pass

            await self.jobs.set_status(job_id, "failed", error_code=code, error_message=msg)

            try:
                if not performance_id and provider_job_id:
                    performance_id = await self.perfs.upsert_performance(
                        user_id=str(job["user_id"]), 
                        provider=provider_name,
                        provider_job_id=provider_job_id,
                        status="failed",
                        share_url=None,
                        meta_json={"job_id": job_id, "request_hash": req_hash, "idempotency_key": idem, "user_id": str(job["user_id"])},
                    )
                    await self.perfs.upsert_fusion_job_output(job_id, performance_id)

                if performance_id:
                    await self.perfs.mark_failed(
                        performance_id,
                        error_code=code,
                        error_message=msg,
                        meta_json={"job_id": job_id, "user_id": str(job["user_id"])},
                    )
            except Exception:
                logger.warning("perf_mark_failed_failed", extra={"job_id": job_id, "performance_id": performance_id})

            try:
                await self.steps.fail_step(job_id, StepCode.provider_poll.value, attempt=0, error_code=code, error_message=msg)
            except Exception:
                pass
            try:
                await self.steps.fail_step(job_id, StepCode.finalize.value, attempt=0, error_code=code, error_message=msg)
            except Exception:
                pass
            return