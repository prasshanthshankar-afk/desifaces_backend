from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Tuple

import asyncpg

from app.services.tts_service import TTSService
from app.services.azure_storage_service import AzureStorageService

logger = logging.getLogger("tts_orchestrator")


def _jsonb_to_dict(val: Any) -> Dict[str, Any]:
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return {}
        try:
            obj = json.loads(s)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    try:
        d = dict(val)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _safe_float(val: Any, default: float) -> float:
    """
    Accepts None/""/"1.2"/1.2 safely.
    """
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return default
        try:
            return float(s)
        except Exception:
            return default
    try:
        return float(val)
    except Exception:
        return default


def _upload_fields(upload: Any) -> Tuple[str, str, str, int]:
    """
    Returns: (sas_url, storage_path, sha256, bytes)
    Works with either dict or UploadBytesResult-like objects.
    """
    if isinstance(upload, dict):
        sas_url = str(upload.get("sas_url") or upload.get("url") or "").strip()
        storage_path = str(upload.get("storage_path") or upload.get("blob_key") or "").strip()
        sha256 = str(upload.get("sha256") or "").strip()
        b = upload.get("bytes")
        try:
            nbytes = int(b) if b is not None else 0
        except Exception:
            nbytes = 0
        return sas_url, storage_path, sha256, nbytes

    sas_url = str(getattr(upload, "sas_url", None) or getattr(upload, "url", None) or "").strip()
    storage_path = str(
        getattr(upload, "storage_path", None) or getattr(upload, "blob_key", None) or ""
    ).strip()
    sha256 = str(getattr(upload, "sha256", None) or "").strip()
    b = getattr(upload, "bytes", None)
    try:
        nbytes = int(b) if b is not None else 0
    except Exception:
        nbytes = 0
    return sas_url, storage_path, sha256, nbytes


class TTSOrchestrator:
    STEP_CODE = "tts"
    STUDIO_TYPE = "audio"

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self.tts = TTSService(pool)
        self.storage = AzureStorageService()

    async def process_job(self, job_id: str) -> None:
        """
        Flow:
          1) claim phase (tx): validate, mark running, upsert step running, increment attempt
          2) execution: synthesize + upload (no DB conn held)
          3) finalize (tx): insert artifact, mark succeeded
          4) on error: mark failed + step failed
        """

        # ---------------------------
        # Claim phase
        # ---------------------------
        user_id: Optional[str] = None
        payload: Dict[str, Any] = {}
        text = ""
        target_locale = ""
        attempt_i = 0

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                job = await conn.fetchrow(
                    """
                    SELECT id::text,
                           user_id::text,
                           studio_type,
                           status,
                           payload_json,
                           attempt_count
                      FROM studio_jobs
                     WHERE id=$1::uuid
                    """,
                    job_id,
                )
                if not job:
                    return

                studio_type = (job["studio_type"] or "").strip()
                if studio_type != self.STUDIO_TYPE:
                    await self._fail_job_and_step(
                        conn,
                        job_id,
                        "wrong_studio_type",
                        f"expected studio_type={self.STUDIO_TYPE}, got={studio_type}",
                        attempt=0,
                    )
                    return

                status = (job["status"] or "").lower()
                if status in ("succeeded", "failed", "cancelled"):
                    return

                payload = _jsonb_to_dict(job["payload_json"])
                user_id = (job["user_id"] or "").strip()

                text = (payload.get("text") or "").strip()
                target_locale = (payload.get("target_locale") or "").strip()

                if not user_id:
                    await self._fail_job_and_step(conn, job_id, "missing_user_id", "job.user_id is required", attempt=0)
                    return
                if not text:
                    await self._fail_job_and_step(conn, job_id, "missing_text", "payload.text is required", attempt=0)
                    return
                if not target_locale:
                    await self._fail_job_and_step(conn, job_id, "missing_target_locale", "payload.target_locale is required", attempt=0)
                    return

                # Update job to running and bump attempt_count atomically
                attempt_i = int(
                    await conn.fetchval(
                        """
                        UPDATE studio_jobs
                           SET status='running',
                               updated_at=now(),
                               attempt_count=attempt_count+1
                         WHERE id=$1::uuid
                         RETURNING attempt_count
                        """,
                        job_id,
                    )
                    or 0
                )

                await conn.execute(
                    """
                    INSERT INTO studio_job_steps(job_id, step_code, status, attempt, error_code, error_message, meta_json)
                    VALUES($1::uuid, $2::text, 'running', $3::int, NULL, NULL, '{}'::jsonb)
                    ON CONFLICT (job_id, step_code)
                    DO UPDATE SET
                      status='running',
                      attempt=EXCLUDED.attempt,
                      error_code=NULL,
                      error_message=NULL,
                      updated_at=now()
                    """,
                    job_id,
                    self.STEP_CODE,
                    attempt_i,
                )

        # ---------------------------
        # Execution phase
        # ---------------------------
        try:
            input_language = (payload.get("input_language") or payload.get("source_language") or "en")
            output_format = (payload.get("output_format") or "mp3")

            rate = _safe_float(payload.get("speed") or payload.get("rate"), 1.0)
            pitch = _safe_float(payload.get("pitch"), 0.0)

            audio_bytes, final_text, chosen_voice, content_type, ext, meta = await self.tts.synthesize(
                text=text,
                input_language=input_language,
                target_locale=target_locale,
                voice=payload.get("voice"),
                style=payload.get("style"),
                emotion=payload.get("emotion"),
                rate=rate,
                pitch=pitch,
                translate=bool(payload.get("translate", True)),
                output_format=output_format,
            )

            upload = await self.storage.upload_bytes(
                data=audio_bytes,
                user_id=user_id,
                job_id=job_id,
                variant=1,
                ext=ext,
                content_type=content_type,
            )

            sas_url, storage_path, sha256, nbytes = _upload_fields(upload)

            if not sas_url:
                raise RuntimeError(f"upload_missing_sas_url: type={type(upload)} upload={upload!r}")

            # ---------------------------
            # Finalize (persist resolved voice + final text into payload_json)
            # ---------------------------
            translated_text = None
            if isinstance(meta, dict):
                translated_text = meta.get("translated_text") or meta.get("translation") or None

            # Make payload reflect what actually happened (important for /status)
            payload_updates: Dict[str, Any] = {
                "voice": chosen_voice,                 # resolved voice
                "final_synthesis_text": final_text,    # text used for synthesis (possibly translated)
            }
            if translated_text:
                payload_updates["translated_text"] = translated_text

            # Optional: keep provider/meta summary in payload (lightweight)
            if isinstance(meta, dict) and meta:
                payload_updates["tts_meta"] = meta

            # Merge back into existing payload
            payload_merged = dict(payload)
            payload_merged.update(payload_updates)

            artifact_meta = {
                "variant": 1,
                "ext": ext,
                "storage_path": storage_path,
                "voice": chosen_voice,
                "final_text": final_text,
                "target_locale": target_locale,
                "attempt": attempt_i,
            }
            if isinstance(meta, dict) and meta:
                artifact_meta.update(meta)

            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    # Persist updated payload_json so status endpoint shows resolved voice/text
                    await conn.execute(
                        """
                        UPDATE studio_jobs
                           SET payload_json=$2::jsonb,
                               updated_at=now()
                         WHERE id=$1::uuid
                        """,
                        job_id,
                        json.dumps(payload_merged),
                    )

                    await conn.execute(
                        """
                        INSERT INTO artifacts(job_id, kind, url, content_type, sha256, bytes, meta_json)
                        VALUES($1::uuid, 'audio', $2::text, $3::text, $4::text, $5::bigint, $6::jsonb)
                        """,
                        job_id,
                        sas_url,
                        content_type,
                        sha256,
                        nbytes,
                        json.dumps(artifact_meta),
                    )

                    await conn.execute(
                        """
                        UPDATE studio_jobs
                           SET status='succeeded',
                               updated_at=now(),
                               error_code=NULL,
                               error_message=NULL
                         WHERE id=$1::uuid
                        """,
                        job_id,
                    )

                    await conn.execute(
                        """
                        UPDATE studio_job_steps
                           SET status='succeeded',
                               error_code=NULL,
                               error_message=NULL,
                               updated_at=now()
                         WHERE job_id=$1::uuid AND step_code=$2::text
                        """,
                        job_id,
                        self.STEP_CODE,
                    )

        except Exception as e:
            msg = str(e)
            logger.exception("TTS job failed job_id=%s err=%s", job_id, msg)

            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """
                        UPDATE studio_jobs
                           SET status='failed',
                               updated_at=now(),
                               error_code='tts_failed',
                               error_message=$2::text
                         WHERE id=$1::uuid
                        """,
                        job_id,
                        msg,
                    )

                    await conn.execute(
                        """
                        INSERT INTO studio_job_steps(job_id, step_code, status, attempt, error_code, error_message, meta_json)
                        VALUES($1::uuid, $2::text, 'failed', $3::int, 'tts_failed', $4::text,
                               jsonb_build_object('error', $4::text))
                        ON CONFLICT (job_id, step_code)
                        DO UPDATE SET
                          status='failed',
                          attempt=EXCLUDED.attempt,
                          error_code='tts_failed',
                          error_message=EXCLUDED.error_message,
                          meta_json=studio_job_steps.meta_json || EXCLUDED.meta_json,
                          updated_at=now()
                        """,
                        job_id,
                        self.STEP_CODE,
                        attempt_i,
                        msg,
                    )
            raise

    async def _fail_job_and_step(
        self,
        conn: asyncpg.Connection,
        job_id: str,
        code: str,
        message: str,
        *,
        attempt: int,
    ) -> None:
        await conn.execute(
            """
            UPDATE studio_jobs
               SET status='failed',
                   updated_at=now(),
                   error_code=$2::text,
                   error_message=$3::text
             WHERE id=$1::uuid
            """,
            job_id,
            code,
            message,
        )

        await conn.execute(
            """
            INSERT INTO studio_job_steps(job_id, step_code, status, attempt, error_code, error_message, meta_json)
            VALUES($1::uuid, $2::text, 'failed', $3::int, $4::text, $5::text,
                   jsonb_build_object('error', $5::text))
            ON CONFLICT (job_id, step_code)
            DO UPDATE SET
              status='failed',
              attempt=EXCLUDED.attempt,
              error_code=EXCLUDED.error_code,
              error_message=EXCLUDED.error_message,
              meta_json=studio_job_steps.meta_json || EXCLUDED.meta_json,
              updated_at=now()
            """,
            job_id,
            self.STEP_CODE,
            int(attempt),
            code,
            message,
        )