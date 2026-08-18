from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx

from df_contracts.v3.director import PlannedParticipant
from desifaces_shared.v3.studio_workflow_store import CanonicalStudioWorkflowStore


class ParticipantFaceBridgeError(RuntimeError):
    pass


_SENSITIVE_KEY_TOKENS = {
    "account",
    "billing",
    "credential",
    "email",
    "password",
    "payment",
    "phone",
    "secret",
    "token",
    "user",
}

_ALLOWED_GENDERS = {"male", "female"}


def _key_is_sensitive(key: str) -> bool:
    tokens = {
        token
        for token in re.split(r"[^a-z0-9]+", str(key or "").strip().lower())
        if token
    }
    return bool(tokens & _SENSITIVE_KEY_TOKENS) or "id" in tokens


def _safe_creative_map(value: dict[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw_key, raw_value in sorted((value or {}).items(), key=lambda item: str(item[0])):
        key = str(raw_key or "").strip()
        if not key or _key_is_sensitive(key):
            continue
        if raw_value is None:
            continue
        text = str(raw_value).strip()
        if not text:
            continue
        out[key] = text[:600]
    return out


def _compact_map(label: str, value: dict[str, Any] | None) -> str | None:
    safe = _safe_creative_map(value)
    if not safe:
        return None
    body = "; ".join(f"{key}: {text}" for key, text in safe.items())
    return f"{label}: {body}"


def _explicit_gender(participant_hint: dict[str, Any] | None) -> str | None:
    hint = participant_hint or {}
    value = str(hint.get("gender") or hint.get("gender_presentation") or "").strip().lower()
    return value if value in _ALLOWED_GENDERS else None


def _explicit_age(participant_hint: dict[str, Any] | None) -> str | None:
    hint = participant_hint or {}
    for key in ("age", "age_range", "age_range_code", "age_presentation"):
        value = hint.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()[:100]
    return None


def compile_participant_face_studio_input(
    *,
    participant: PlannedParticipant,
    participant_hint: dict[str, Any] | None = None,
    language: str = "en",
    num_variants: int = 1,
) -> dict[str, Any]:
    """Compile an approved Director participant into the existing Face Studio contract.

    This is intentionally deterministic. The LLM may propose creative attributes in
    ``persona`` / ``visual_direction`` / ``continuity``, but this compiler owns the
    provider-facing request shape. It never infers gender from a participant name,
    relationship role, geography or locale, and it strips obvious sensitive-key
    material before provider submission.
    """

    hint = dict(participant_hint or {})
    gender = _explicit_gender(hint)
    age = _explicit_age(hint)

    parts: list[str] = [
        f"Create a single-person identity reference portrait for the character {participant.display_name}.",
    ]
    if participant.role:
        parts.append(f"Story role: {participant.role}.")
    if age:
        parts.append(f"Explicit age guidance from the user: {age}.")

    for section in (
        _compact_map("Persona", participant.persona),
        _compact_map("Approved visual direction", participant.visual_direction),
        _compact_map("Continuity requirements", participant.continuity),
    ):
        if section:
            parts.append(section + ".")

    parts.extend(
        [
            "Show exactly one person; do not include any other story participant.",
            "Create a clean, high-quality portrait that can serve as the durable visual identity reference for later scenes.",
            "Do not infer ethnicity, skin tone, religion, attire, occupation, socioeconomic status, facial anatomy, or personality from geography, locale, name, or relationship role.",
            "Use only the explicit user constraints and approved creative direction above; otherwise choose neutral, non-stereotyped details.",
        ]
    )

    prompt = " ".join(parts)
    if len(prompt) > 1500:
        prompt = prompt[:1497].rstrip() + "..."

    studio_input: dict[str, Any] = {
        "mode": "text-to-image",
        "language": language or "en",
        "subject_composition_code": "single_person",
        "num_variants": max(1, min(int(num_variants), 4)),
        "aspect_ratio": "9:16",
        "user_prompt": prompt,
    }
    if gender:
        studio_input["gender"] = gender
    return studio_input


@dataclass(frozen=True)
class FaceGenerationResult:
    job_id: str
    media_asset_id: UUID
    face_profile_id: str
    image_url: str
    prompt_used: str
    status_payload: dict[str, Any]
    pricing_preview: dict[str, Any]


class FaceStudioClient:
    """Small HTTP adapter over the existing authenticated Face Studio API."""

    def __init__(self, *, base_url: str, timeout_seconds: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)

    async def preview_pricing(self, *, headers: dict[str, str], studio_input: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=self.timeout_seconds) as client:
            response = await client.post(
                "/api/face/creator/pricing/preview",
                json={"studio": "face", "action": "generate", "studio_input": studio_input},
            )
        if response.status_code != 200:
            raise ParticipantFaceBridgeError(
                f"face_pricing_preview_failed:{response.status_code}:{response.text[:1200]}"
            )
        return response.json()

    async def create_job(
        self,
        *,
        headers: dict[str, str],
        studio_input: dict[str, Any],
        pricing_preview: dict[str, Any],
    ) -> str:
        quote_id = str(pricing_preview.get("quote_id") or "").strip()
        if not quote_id:
            raise ParticipantFaceBridgeError("face_pricing_preview_missing_quote_id")
        payload = {
            "studio": "face",
            "studio_input": studio_input,
            "pricing_confirmation": {
                "quote_id": quote_id,
                "preview_fingerprint": pricing_preview.get("preview_fingerprint"),
                "user_confirmed": True,
            },
        }
        async with httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=self.timeout_seconds) as client:
            response = await client.post("/api/face/creator/generate", json=payload)
        if response.status_code not in {200, 201, 202}:
            raise ParticipantFaceBridgeError(
                f"face_generate_failed:{response.status_code}:{response.text[:1200]}"
            )
        job_id = str(response.json().get("job_id") or "").strip()
        if not job_id:
            raise ParticipantFaceBridgeError(f"face_generate_missing_job_id:{response.text[:1200]}")
        return job_id

    async def wait_for_first_variant(
        self,
        *,
        headers: dict[str, str],
        job_id: str,
        timeout_seconds: int = 600,
        poll_seconds: float = 2.0,
        state_callback=None,
    ) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + int(timeout_seconds)
        last_state = None
        async with httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=self.timeout_seconds) as client:
            while asyncio.get_running_loop().time() < deadline:
                response = await client.get(f"/api/face/creator/jobs/{job_id}/status")
                if response.status_code != 200:
                    raise ParticipantFaceBridgeError(
                        f"face_status_failed:{response.status_code}:{response.text[:1200]}"
                    )
                payload = response.json()
                state = str(payload.get("status") or "").strip().lower()
                if state != last_state and state_callback is not None:
                    state_callback(state)
                    last_state = state
                if state == "succeeded":
                    variants = list(payload.get("variants") or [])
                    if not variants:
                        raise ParticipantFaceBridgeError(f"face_succeeded_without_variants:{job_id}")
                    return payload
                if state in {"failed", "cancelled", "canceled"}:
                    raise ParticipantFaceBridgeError(
                        f"face_job_terminal_failure:{job_id}:{payload.get('error') or payload}"
                    )
                await asyncio.sleep(float(poll_seconds))
        raise ParticipantFaceBridgeError(f"face_job_timeout:{job_id}")

    async def generate_one(
        self,
        *,
        headers: dict[str, str],
        studio_input: dict[str, Any],
        pricing_preview: dict[str, Any],
        timeout_seconds: int = 600,
        state_callback=None,
    ) -> FaceGenerationResult:
        job_id = await self.create_job(
            headers=headers,
            studio_input=studio_input,
            pricing_preview=pricing_preview,
        )
        status_payload = await self.wait_for_first_variant(
            headers=headers,
            job_id=job_id,
            timeout_seconds=timeout_seconds,
            state_callback=state_callback,
        )
        variant = dict((status_payload.get("variants") or [])[0])
        return FaceGenerationResult(
            job_id=job_id,
            media_asset_id=UUID(str(variant["media_asset_id"])),
            face_profile_id=str(variant.get("face_profile_id") or ""),
            image_url=str(variant.get("image_url") or ""),
            prompt_used=str(variant.get("prompt_used") or ""),
            status_payload=status_payload,
            pricing_preview=pricing_preview,
        )


class ParticipantFaceBinder:
    """Bind a generated Face MediaAsset to a canonical Participant and HITL Face stage."""

    def __init__(self, *, store: CanonicalStudioWorkflowStore | None = None) -> None:
        self.store = store or CanonicalStudioWorkflowStore()

    async def bind_generated_face(
        self,
        conn,
        *,
        account_id: UUID,
        participant_id: UUID,
        stage_run_id: UUID,
        media_asset_id: UUID,
        face_job_id: str,
        face_profile_id: str,
        prompt_used: str,
    ) -> UUID:
        stage = await conn.fetchrow(
            """
            select s.stage_run_id,s.workflow_id,s.stage_type,s.scope_type,s.participant_id,w.account_id
            from public.v3_studio_stage_runs s
            join public.v3_studio_workflows w on w.workflow_id=s.workflow_id
            where s.stage_run_id=$1
            """,
            stage_run_id,
        )
        if not stage or str(stage["account_id"]) != str(account_id):
            raise ParticipantFaceBridgeError("face_stage_not_found_or_account_mismatch")
        if str(stage["stage_type"]) != "face" or str(stage["scope_type"]) != "participant":
            raise ParticipantFaceBridgeError("face_stage_type_scope_mismatch")
        if str(stage["participant_id"]) != str(participant_id):
            raise ParticipantFaceBridgeError("face_stage_participant_mismatch")

        media = await conn.fetchrow(
            "select id,account_id from public.media_assets where id=$1",
            media_asset_id,
        )
        if not media or str(media["account_id"]) != str(account_id):
            raise ParticipantFaceBridgeError("face_media_account_mismatch")

        await conn.execute(
            "delete from public.v3_participant_media where participant_id=$1 and relation='primary_face'",
            participant_id,
        )
        await conn.execute(
            """
            insert into public.v3_participant_media(participant_id,media_id,relation,sequence_no,metadata_json)
            values($1,$2,'primary_face',0,$3::jsonb)
            on conflict(participant_id,media_id,relation)
            do update set sequence_no=excluded.sequence_no,metadata_json=excluded.metadata_json
            """,
            participant_id,
            media_asset_id,
            {
                "source": "svc-face",
                "compatibility_face_job_id": face_job_id,
                "face_profile_id": face_profile_id,
            },
        )
        await conn.execute(
            """
            update public.v3_participants
            set primary_face_media_id=$2,updated_at=now(),
                metadata_json=coalesce(metadata_json,'{}'::jsonb) || $3::jsonb
            where participant_id=$1 and account_id=$4
            """,
            participant_id,
            media_asset_id,
            {
                "face_source": "svc-face",
                "face_job_id": face_job_id,
                "face_profile_id": face_profile_id,
            },
            account_id,
        )
        await conn.execute(
            """
            update public.v3_studio_stage_runs
            set metadata_json=coalesce(metadata_json,'{}'::jsonb) || $2::jsonb,updated_at=now()
            where stage_run_id=$1
            """,
            stage_run_id,
            {
                "compatibility_face_job_id": face_job_id,
                "face_profile_id": face_profile_id,
                "prompt_used": prompt_used[:1500],
            },
        )
        return await self.store.attach_output(
            conn,
            stage_run_id=stage_run_id,
            media_id=media_asset_id,
            output_role="primary_face",
        )
