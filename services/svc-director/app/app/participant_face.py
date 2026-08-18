from __future__ import annotations

import asyncio
import json
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
    "account", "billing", "credential", "email", "password", "payment",
    "phone", "secret", "token", "user",
}
_ALLOWED_GENDERS = {"male", "female"}
_VISUAL_PRIORITY = (
    "identity type",
    "identity brief",
    "presentation",
    "portrait framing",
    "expression",
    "face shape",
    "brows",
    "eyes",
    "eye colour",
    "eye color",
    "nose",
    "lips",
    "jaw and chin",
    "hair styling",
    "hair",
    "lighting",
    "distinguishing cues",
    "rendering",
    "photorealism",
    "wardrobe",
    "body reference",
    "resemblance constraint",
    "identity independence",
)
_CONTINUITY_PRIORITY = ("identity", "identity lock", "wardrobe", "wardrobe lock")


def _key_is_sensitive(key: str) -> bool:
    tokens = {
        token for token in re.split(r"[^a-z0-9]+", str(key or "").strip().lower()) if token
    }
    return bool(tokens & _SENSITIVE_KEY_TOKENS) or "id" in tokens


def _normalized_key(value: str) -> str:
    return re.sub(r"[_-]+", " ", str(value or "").strip().lower()).strip()


def _safe_creative_map(value: dict[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw_key, raw_value in (value or {}).items():
        key = str(raw_key or "").strip()
        if not key or _key_is_sensitive(key) or raw_value is None:
            continue
        text = str(raw_value).strip()
        if text:
            out[_normalized_key(key)] = text[:420]
    return out


def _explicit_gender(participant_hint: dict[str, Any] | None) -> str | None:
    hint = participant_hint or {}
    value = str(hint.get("gender") or hint.get("gender_presentation") or "").strip().lower()
    return value if value in _ALLOWED_GENDERS else None


def _explicit_age(participant_hint: dict[str, Any] | None) -> str | None:
    hint = participant_hint or {}
    for key in ("age", "age_range", "age_range_code", "age_presentation"):
        value = hint.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()[:80]
    return None


def _append_prompt(parts: list[str], sentence: str, *, max_chars: int = 1500) -> None:
    sentence = " ".join(str(sentence or "").split()).strip()
    if not sentence:
        return
    candidate = " ".join([*parts, sentence])
    if len(candidate) <= max_chars:
        parts.append(sentence)


def compile_participant_face_studio_input(
    *,
    participant: PlannedParticipant,
    participant_hint: dict[str, Any] | None = None,
    language: str = "en",
    num_variants: int = 1,
) -> dict[str, Any]:
    """Compile approved Director identity design into Face Studio input.

    The provider prompt is deliberately identity-first. Story mechanics do not
    consume the limited Face prompt budget. Director field names are normalized
    so snake_case and human-readable keys compile identically, while sensitive
    account/user/payment identifiers are excluded. The prompt is built from
    complete prioritized sentences rather than blindly truncated.
    """
    hint = dict(participant_hint or {})
    gender = _explicit_gender(hint)
    age = _explicit_age(hint)
    visual = _safe_creative_map(participant.visual_direction)
    continuity = _safe_creative_map(participant.continuity)

    parts: list[str] = []
    _append_prompt(
        parts,
        f"Create exactly one photorealistic identity-reference portrait for {participant.display_name}.",
    )
    if age:
        _append_prompt(parts, f"Explicit user age: {age}.")
    _append_prompt(
        parts,
        "Do not infer ethnicity, skin tone, religion, attire, occupation, socioeconomic status, facial anatomy, or personality from geography, locale, name, or family relationship.",
    )
    _append_prompt(parts, "No other story participant may appear in the image.")

    for key in _VISUAL_PRIORITY:
        value = visual.get(key)
        if value:
            _append_prompt(parts, f"{key.title()}: {value}.")
    for key in _CONTINUITY_PRIORITY:
        value = continuity.get(key)
        if value:
            _append_prompt(parts, f"{key.title()}: {value}.")

    _append_prompt(
        parts,
        "Treat the resulting face as a durable recurring-character identity reference; preserve natural pores, believable eyes, fine hair detail, realistic age detail, and non-synthetic texture.",
    )

    studio_input: dict[str, Any] = {
        "mode": "text-to-image",
        "language": language or "en",
        "subject_composition_code": "single_person",
        "num_variants": max(1, min(int(num_variants), 4)),
        "aspect_ratio": "9:16",
        "user_prompt": " ".join(parts),
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
    """HTTP adapter over the existing authenticated Face Studio API."""

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
                    if not list(payload.get("variants") or []):
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
            headers=headers, studio_input=studio_input, pricing_preview=pricing_preview,
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
    """Attach a generated Face candidate to its Participant and HITL Face stage.

    A generated candidate is *not* the canonical primary face until HITL approval.
    This distinction prevents UI/Assistant/downstream code from mistaking an
    unreviewed image for an accepted character identity.
    """

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
            """select s.stage_run_id,s.workflow_id,s.stage_type,s.scope_type,s.participant_id,w.account_id
            from public.v3_studio_stage_runs s
            join public.v3_studio_workflows w on w.workflow_id=s.workflow_id
            where s.stage_run_id=$1""",
            stage_run_id,
        )
        if not stage or str(stage["account_id"]) != str(account_id):
            raise ParticipantFaceBridgeError("face_stage_not_found_or_account_mismatch")
        if str(stage["stage_type"]) != "face" or str(stage["scope_type"]) != "participant":
            raise ParticipantFaceBridgeError("face_stage_type_scope_mismatch")
        if str(stage["participant_id"]) != str(participant_id):
            raise ParticipantFaceBridgeError("face_stage_participant_mismatch")

        media = await conn.fetchrow("select id,account_id from public.media_assets where id=$1", media_asset_id)
        if not media or str(media["account_id"]) != str(account_id):
            raise ParticipantFaceBridgeError("face_media_account_mismatch")

        candidate_meta = json.dumps({
            "source": "svc-face",
            "candidate_state": "awaiting_review",
            "compatibility_face_job_id": face_job_id,
            "face_profile_id": face_profile_id,
        }, ensure_ascii=False)
        await conn.execute(
            """insert into public.v3_participant_media(participant_id,media_id,relation,sequence_no,metadata_json)
            values($1,$2,'reference_face',0,$3::jsonb)
            on conflict(participant_id,media_id,relation)
            do update set sequence_no=excluded.sequence_no,metadata_json=excluded.metadata_json""",
            participant_id, media_asset_id, candidate_meta,
        )
        await conn.execute(
            """update public.v3_studio_stage_runs
            set metadata_json=coalesce(metadata_json,'{}'::jsonb) || $2::jsonb,updated_at=now()
            where stage_run_id=$1""",
            stage_run_id,
            json.dumps({
                "compatibility_face_job_id": face_job_id,
                "face_profile_id": face_profile_id,
                "prompt_used": prompt_used[:1500],
            }, ensure_ascii=False),
        )
        return await self.store.attach_output(
            conn, stage_run_id=stage_run_id, media_id=media_asset_id, output_role="face_candidate",
        )


async def promote_approved_face_candidate(
    conn,
    *,
    account_id: UUID,
    stage_run_id: UUID,
    media_asset_id: UUID,
) -> UUID:
    """Promote an approved Face candidate into the Participant's canonical identity."""
    row = await conn.fetchrow(
        """select s.participant_id,s.stage_type,s.scope_type,s.state,w.account_id
        from public.v3_studio_stage_runs s
        join public.v3_studio_workflows w on w.workflow_id=s.workflow_id
        where s.stage_run_id=$1""",
        stage_run_id,
    )
    if not row or str(row["account_id"]) != str(account_id):
        raise ParticipantFaceBridgeError("face_promotion_stage_not_found_or_account_mismatch")
    if str(row["stage_type"]) != "face" or str(row["scope_type"]) != "participant":
        raise ParticipantFaceBridgeError("face_promotion_stage_type_scope_mismatch")
    if str(row["state"]) != "approved":
        raise ParticipantFaceBridgeError("face_promotion_requires_approved_stage")

    approved = await conn.fetchval(
        """select exists(
          select 1 from public.v3_studio_review_items r
          join public.v3_studio_stage_outputs o
            on o.stage_run_id=r.stage_run_id and o.media_id=r.media_id
          where r.stage_run_id=$1 and r.media_id=$2 and r.decision='approved' and o.is_active=true
        )""",
        stage_run_id, media_asset_id,
    )
    if not approved:
        raise ParticipantFaceBridgeError("face_promotion_requires_active_approved_output")

    participant_id = UUID(str(row["participant_id"]))
    await conn.execute(
        "delete from public.v3_participant_media where participant_id=$1 and relation='primary_face'",
        participant_id,
    )
    await conn.execute(
        """insert into public.v3_participant_media(participant_id,media_id,relation,sequence_no,metadata_json)
        values($1,$2,'primary_face',0,$3::jsonb)
        on conflict(participant_id,media_id,relation)
        do update set sequence_no=excluded.sequence_no,metadata_json=excluded.metadata_json""",
        participant_id,
        media_asset_id,
        json.dumps({"source": "svc-face", "selected_by": "hitl_review"}, ensure_ascii=False),
    )
    await conn.execute(
        """update public.v3_participants
        set primary_face_media_id=$2,updated_at=now(),
            metadata_json=coalesce(metadata_json,'{}'::jsonb) || $3::jsonb
        where participant_id=$1 and account_id=$4""",
        participant_id,
        media_asset_id,
        json.dumps({"face_selection_state": "approved"}, ensure_ascii=False),
        account_id,
    )
    await conn.execute(
        """update public.v3_participant_media
        set metadata_json=coalesce(metadata_json,'{}'::jsonb) || $3::jsonb
        where participant_id=$1 and media_id=$2 and relation='reference_face'""",
        participant_id,
        media_asset_id,
        json.dumps({"candidate_state": "approved"}, ensure_ascii=False),
    )
    return participant_id
