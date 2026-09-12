from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx

from desifaces_shared.v3.studio_workflow_store import CanonicalStudioWorkflowStore, StudioWorkflowError


class ParticipantAudioBridgeError(RuntimeError):
    pass


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    try:
        return dict(value)
    except Exception:
        return {}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _explicit_gender(*, persona: dict[str, Any], participant_metadata: dict[str, Any]) -> str:
    """Resolve only explicit durable gender presentation; never infer from identity cues."""
    constraints = _as_dict(participant_metadata.get("explicit_face_constraints"))
    raw = _clean(constraints.get("gender") or constraints.get("gender_presentation"))
    if not raw:
        raw = _clean(persona.get("gender") or persona.get("gender_presentation"))
    normalized = {
        "man": "male",
        "male": "male",
        "woman": "female",
        "female": "female",
        "neutral": "neutral",
        "nonbinary": "neutral",
        "non-binary": "neutral",
        "unspecified": "unspecified",
    }.get(raw.casefold())
    if not normalized:
        raise ParticipantAudioBridgeError("audio_explicit_gender_required")
    return normalized


@dataclass(frozen=True)
class AudioStageContext:
    workflow_id: UUID
    stage_run_id: UUID
    account_id: UUID
    owner_user_id: UUID
    project_id: UUID
    story_id: UUID | None
    stage_state: str
    stage_metadata: dict[str, Any]
    dialogue_turn_id: UUID
    scene_id: UUID
    participant_id: UUID
    display_name: str
    text: str
    target_locale: str
    emotion_code: str | None
    delivery: dict[str, Any]
    participant_persona: dict[str, Any]
    participant_metadata: dict[str, Any]
    voice_profile_ref: str | None
    voice_locale: str | None


async def load_audio_stage_context(
    conn,
    *,
    account_id: UUID,
    workflow_id: UUID,
    stage_run_id: UUID,
) -> AudioStageContext:
    row = await conn.fetchrow(
        """
        select
          s.stage_run_id,s.workflow_id,s.stage_type,s.scope_type,s.state,s.dialogue_turn_id,
          s.metadata_json as stage_metadata,
          w.account_id,w.owner_user_id,w.project_id,w.story_id,w.current_stage,
          dt.scene_id,dt.speaker_participant_id,dt.text_value,dt.locale as turn_locale,
          dt.emotion_code,dt.delivery_json,
          p.display_name,p.default_locale,p.voice_profile_ref,p.voice_locale,
          p.persona_json,p.metadata_json as participant_metadata,
          st.default_locale as story_locale
        from public.v3_studio_stage_runs s
        join public.v3_studio_workflows w on w.workflow_id=s.workflow_id
        join public.v3_dialogue_turns dt on dt.turn_id=s.dialogue_turn_id
        join public.v3_participants p on p.participant_id=dt.speaker_participant_id
        left join public.v3_stories st on st.story_id=w.story_id
        where s.stage_run_id=$1 and s.workflow_id=$2 and w.account_id=$3
        """,
        stage_run_id,
        workflow_id,
        account_id,
    )
    if not row:
        raise ParticipantAudioBridgeError("audio_stage_not_found_or_account_mismatch")
    if _clean(row["stage_type"]) != "audio" or _clean(row["scope_type"]) != "dialogue_turn":
        raise ParticipantAudioBridgeError("audio_stage_type_scope_mismatch")
    if _clean(row["current_stage"]) not in {"audio", "fusion"}:
        raise ParticipantAudioBridgeError("audio_stage_not_current")

    text = _clean(row["text_value"])
    if not text:
        raise ParticipantAudioBridgeError("audio_dialogue_text_required")
    target_locale = _clean(
        row["turn_locale"] or row["voice_locale"] or row["default_locale"] or row["story_locale"]
    )
    if not target_locale:
        raise ParticipantAudioBridgeError("audio_target_locale_required")

    return AudioStageContext(
        workflow_id=UUID(str(row["workflow_id"])),
        stage_run_id=UUID(str(row["stage_run_id"])),
        account_id=UUID(str(row["account_id"])),
        owner_user_id=UUID(str(row["owner_user_id"])),
        project_id=UUID(str(row["project_id"])),
        story_id=UUID(str(row["story_id"])) if row["story_id"] else None,
        stage_state=_clean(row["state"]),
        stage_metadata=_as_dict(row["stage_metadata"]),
        dialogue_turn_id=UUID(str(row["dialogue_turn_id"])),
        scene_id=UUID(str(row["scene_id"])),
        participant_id=UUID(str(row["speaker_participant_id"])),
        display_name=_clean(row["display_name"]) or "Character",
        text=text,
        target_locale=target_locale,
        emotion_code=_clean(row["emotion_code"]) or None,
        delivery=_as_dict(row["delivery_json"]),
        participant_persona=_as_dict(row["persona_json"]),
        participant_metadata=_as_dict(row["participant_metadata"]),
        voice_profile_ref=_clean(row["voice_profile_ref"]) or None,
        voice_locale=_clean(row["voice_locale"]) or None,
    )


def compile_context_audio_input(context: AudioStageContext) -> dict[str, Any]:
    gender = _explicit_gender(
        persona=context.participant_persona,
        participant_metadata=context.participant_metadata,
    )
    conversation_settings = _as_dict(context.stage_metadata.get("conversation_settings"))
    resolved_target_locale = _clean(conversation_settings.get("target_locale")) or context.target_locale
    translate = bool(conversation_settings.get("translate"))
    source_language = _clean(conversation_settings.get("source_language")) or "auto"
    studio_input: dict[str, Any] = {
        "text": context.text,
        "target_locale": resolved_target_locale,
        "translate": translate,
        "speaker_gender": gender,
        "voice_gender": gender,
        "context": (
            f"story_dialogue workflow_id={context.workflow_id} "
            f"participant_id={context.participant_id} "
            f"dialogue_turn_id={context.dialogue_turn_id} "
            f"scene_id={context.scene_id}"
            + (f" emotion_code={context.emotion_code}" if context.emotion_code else "")
        ),
    }
    if translate:
        studio_input["source_language"] = source_language
    if context.voice_profile_ref:
        studio_input["voice_id"] = context.voice_profile_ref
    if context.voice_locale:
        studio_input["voice_locale"] = context.voice_locale
    # Only forward durable delivery controls that svc-audio already understands.
    for key in ("style", "style_degree", "rate", "pitch", "volume"):
        value = context.delivery.get(key)
        if value is not None and str(value).strip() != "":
            studio_input[key] = value
    return studio_input


class AudioStudioClient:
    def __init__(self, *, base_url: str, timeout_seconds: float = 40.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)

    async def preview_pricing(self, *, headers: dict[str, str], studio_input: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=self.timeout_seconds) as client:
            response = await client.post("/api/audio/tts/pricing/preview", json=studio_input)
        if response.status_code != 200:
            raise ParticipantAudioBridgeError(
                f"audio_pricing_preview_failed:{response.status_code}:{response.text[:1200]}"
            )
        return response.json()

    async def create_job(
        self,
        *,
        headers: dict[str, str],
        studio_input: dict[str, Any],
        quote_id: str,
        preview_fingerprint: str | None,
    ) -> str:
        payload = dict(studio_input)
        payload["pricing_confirmation"] = {
            "quote_id": quote_id,
            "preview_fingerprint": preview_fingerprint,
        }
        async with httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=self.timeout_seconds) as client:
            response = await client.post("/api/audio/tts", json=payload)
        if response.status_code not in {200, 201, 202}:
            raise ParticipantAudioBridgeError(
                f"audio_generate_failed:{response.status_code}:{response.text[:1200]}"
            )
        job_id = _clean(response.json().get("job_id"))
        if not job_id:
            raise ParticipantAudioBridgeError(f"audio_generate_missing_job_id:{response.text[:1200]}")
        return job_id

    async def status(self, *, headers: dict[str, str], job_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=self.timeout_seconds) as client:
            response = await client.get(f"/api/audio/jobs/{job_id}/status")
        if response.status_code != 200:
            raise ParticipantAudioBridgeError(
                f"audio_status_failed:{response.status_code}:{response.text[:1200]}"
            )
        return response.json()

    async def canonical_output(
        self,
        *,
        headers: dict[str, str],
        job_id: str,
        project_id: UUID,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=self.timeout_seconds) as client:
            response = await client.get(
                f"/api/audio/jobs/{job_id}/canonical-output",
                params={"project_id": str(project_id)},
            )
        if response.status_code != 200:
            raise ParticipantAudioBridgeError(
                f"audio_canonical_output_failed:{response.status_code}:{response.text[:1200]}"
            )
        return response.json()

    async def read_url(self, *, headers: dict[str, str], media_id: UUID) -> str:
        async with httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=self.timeout_seconds) as client:
            response = await client.get(f"/api/audio/assets/{media_id}/read-url")
        if response.status_code != 200:
            raise ParticipantAudioBridgeError(
                f"audio_read_url_failed:{response.status_code}:{response.text[:1200]}"
            )
        url = _clean(response.json().get("read_url"))
        if not url:
            raise ParticipantAudioBridgeError("audio_read_url_missing")
        return url


async def _latest_output_review(conn, *, stage_run_id: UUID):
    return await conn.fetchrow(
        """
        select o.media_id,r.review_item_id,r.decision
        from public.v3_studio_stage_outputs o
        left join public.v3_studio_review_items r
          on r.stage_run_id=o.stage_run_id and r.media_id=o.media_id
        where o.stage_run_id=$1 and o.is_active=true
        order by o.created_at desc limit 1
        """,
        stage_run_id,
    )


async def _latest_attempt(conn, *, stage_run_id: UUID):
    return await conn.fetchrow(
        """
        select attempt_id,attempt_no,attempt_kind,state,provider_job_ref,media_id,
               pricing_quote_id,preview_fingerprint,error_code,error_message,metadata_json
        from public.v3_studio_stage_attempts
        where stage_run_id=$1 order by attempt_no desc limit 1
        """,
        stage_run_id,
    )


class ParticipantAudioExecutionService:
    """Director-owned Audio execution bridge; svc-audio remains synthesis owner."""

    def __init__(self, *, audio_base_url: str, store: CanonicalStudioWorkflowStore | None = None) -> None:
        self.client = AudioStudioClient(base_url=audio_base_url)
        self.store = store or CanonicalStudioWorkflowStore()

    async def preview(
        self,
        conn,
        *,
        account_id: UUID,
        workflow_id: UUID,
        stage_run_id: UUID,
        headers: dict[str, str],
    ) -> tuple[AudioStageContext, dict[str, Any], dict[str, Any]]:
        context = await load_audio_stage_context(
            conn,
            account_id=account_id,
            workflow_id=workflow_id,
            stage_run_id=stage_run_id,
        )
        if context.stage_state not in {"pending", "ready", "failed", "rejected"}:
            raise ParticipantAudioBridgeError(f"audio_stage_not_priceable:{context.stage_state}")
        await self.store.assert_startable(conn, stage_run_id=stage_run_id)
        studio_input = compile_context_audio_input(context)
        pricing = await self.client.preview_pricing(headers=headers, studio_input=studio_input)
        return context, studio_input, pricing

    async def dispatch(
        self,
        pool,
        *,
        account_id: UUID,
        workflow_id: UUID,
        stage_run_id: UUID,
        headers: dict[str, str],
        quote_id: str,
        preview_fingerprint: str | None,
    ) -> tuple[AudioStageContext, str, int, str, UUID]:
        quote_id = _clean(quote_id)
        if not quote_id:
            raise ParticipantAudioBridgeError("audio_pricing_confirmation_required")

        async with pool.acquire() as conn:
            async with conn.transaction():
                context = await load_audio_stage_context(
                    conn,
                    account_id=account_id,
                    workflow_id=workflow_id,
                    stage_run_id=stage_run_id,
                )
                prior_state = context.stage_state
                if prior_state not in {"pending", "ready", "failed", "rejected"}:
                    latest = await _latest_attempt(conn, stage_run_id=stage_run_id)
                    if prior_state == "generating" and latest and latest["provider_job_ref"]:
                        return (
                            context,
                            str(latest["provider_job_ref"]),
                            int(latest["attempt_no"]),
                            str(latest["attempt_kind"]),
                            UUID(str(latest["attempt_id"])),
                        )
                    raise ParticipantAudioBridgeError(f"audio_stage_not_dispatchable:{prior_state}")

                await self.store.assert_startable(conn, stage_run_id=stage_run_id)
                attempt_no = int(
                    await conn.fetchval(
                        "select coalesce(max(attempt_no),0)+1 from public.v3_studio_stage_attempts where stage_run_id=$1",
                        stage_run_id,
                    )
                    or 1
                )
                attempt_kind = "initial" if attempt_no == 1 else (
                    "regenerate" if prior_state == "rejected" else "retry"
                )
                attempt = await conn.fetchrow(
                    """
                    insert into public.v3_studio_stage_attempts(
                      stage_run_id,attempt_no,attempt_kind,state,provider_service,
                      pricing_quote_id,preview_fingerprint,metadata_json
                    ) values($1,$2,$3,'dispatching','svc-audio',$4,$5,$6::jsonb)
                    returning attempt_id
                    """,
                    stage_run_id,
                    attempt_no,
                    attempt_kind,
                    quote_id,
                    preview_fingerprint,
                    json.dumps({"dispatch_outcome": "dispatching"}),
                )
                attempt_id = UUID(str(attempt["attempt_id"]))
                await self.store.mark_generating(conn, stage_run_id=stage_run_id)
                await conn.execute(
                    """
                    update public.v3_studio_stage_runs
                    set metadata_json=coalesce(metadata_json,'{}'::jsonb) || $2::jsonb,updated_at=now()
                    where stage_run_id=$1
                    """,
                    stage_run_id,
                    json.dumps(
                        {
                            "audio_attempt_id": str(attempt_id),
                            "audio_attempt_no": attempt_no,
                            "audio_attempt_kind": attempt_kind,
                            "audio_quote_id": quote_id,
                            "audio_preview_fingerprint": preview_fingerprint,
                        }
                    ),
                )
                studio_input = compile_context_audio_input(context)

        try:
            job_id = await self.client.create_job(
                headers=headers,
                studio_input=studio_input,
                quote_id=quote_id,
                preview_fingerprint=preview_fingerprint,
            )
        except Exception as exc:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    update public.v3_studio_stage_attempts
                    set state='failed',error_code='audio_dispatch_failed',error_message=$2,
                        completed_at=coalesce(completed_at,now()),updated_at=now()
                    where attempt_id=$1
                    """,
                    attempt_id,
                    str(exc)[:4000],
                )
                await self.store.mark_failed(conn, stage_run_id=stage_run_id, error=str(exc))
            raise

        async with pool.acquire() as conn:
            await conn.execute(
                """
                update public.v3_studio_stage_attempts
                set state='running',provider_job_ref=$2,
                    metadata_json=coalesce(metadata_json,'{}'::jsonb) || $3::jsonb,updated_at=now()
                where attempt_id=$1
                """,
                attempt_id,
                job_id,
                json.dumps({"dispatch_outcome": "accepted"}),
            )
            await conn.execute(
                """
                update public.v3_studio_stage_runs
                set metadata_json=coalesce(metadata_json,'{}'::jsonb) || $2::jsonb,updated_at=now()
                where stage_run_id=$1
                """,
                stage_run_id,
                json.dumps({"compatibility_audio_job_id": job_id}),
            )
        return context, job_id, attempt_no, attempt_kind, attempt_id

    async def sync(
        self,
        pool,
        *,
        account_id: UUID,
        workflow_id: UUID,
        stage_run_id: UUID,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        async with pool.acquire() as conn:
            context = await load_audio_stage_context(
                conn,
                account_id=account_id,
                workflow_id=workflow_id,
                stage_run_id=stage_run_id,
            )
            existing = await _latest_output_review(conn, stage_run_id=stage_run_id)
            latest = await _latest_attempt(conn, stage_run_id=stage_run_id)
            if existing and context.stage_state in {"awaiting_review", "approved", "rejected"}:
                existing_media_id = UUID(str(existing["media_id"]))
                existing_job_id = (
                    str(latest["provider_job_ref"]) if latest and latest["provider_job_ref"] else None
                )
                existing_review_id = (
                    str(existing["review_item_id"]) if existing["review_item_id"] else None
                )
                existing_review_decision = (
                    str(existing["decision"]) if existing["decision"] else None
                )
            else:
                existing_media_id = None
                existing_job_id = None
                existing_review_id = None
                existing_review_decision = None
            if existing_media_id is not None:
                attempt_meta = _as_dict(latest["metadata_json"]) if latest else {}
                # Always mint a fresh owner-service read URL; never persist or reuse an
                # expired SAS as the canonical identity of the Audio output.
                audio_url = await self.client.read_url(headers=headers, media_id=existing_media_id)
                return {
                    "workflow_id": str(workflow_id),
                    "stage_run_id": str(stage_run_id),
                    "dialogue_turn_id": str(context.dialogue_turn_id),
                    "participant_id": str(context.participant_id),
                    "display_name": context.display_name,
                    "provider_state": "succeeded",
                    "stage_state": context.stage_state,
                    "audio_job_id": existing_job_id,
                    "media_asset_id": str(existing_media_id),
                    "audio_url": audio_url,
                    "review_item_id": existing_review_id,
                    "review_decision": existing_review_decision,
                    "pricing": attempt_meta.get("pricing"),
                    "pricing_summary": attempt_meta.get("pricing_summary"),
                }
            if not latest or not latest["provider_job_ref"]:
                return {
                    "workflow_id": str(workflow_id),
                    "stage_run_id": str(stage_run_id),
                    "dialogue_turn_id": str(context.dialogue_turn_id),
                    "participant_id": str(context.participant_id),
                    "display_name": context.display_name,
                    "provider_state": None,
                    "stage_state": context.stage_state,
                    "audio_job_id": None,
                    "media_asset_id": None,
                    "review_item_id": None,
                    "review_decision": None,
                }
            attempt_id = UUID(str(latest["attempt_id"]))
            job_id = str(latest["provider_job_ref"])

        payload = await self.client.status(headers=headers, job_id=job_id)
        provider_state = _clean(payload.get("status")).lower()
        if provider_state in {"queued", "running", "processing", "pricing_pending", "reserved"}:
            return {
                "workflow_id": str(workflow_id),
                "stage_run_id": str(stage_run_id),
                "dialogue_turn_id": str(context.dialogue_turn_id),
                "participant_id": str(context.participant_id),
                "display_name": context.display_name,
                "provider_state": provider_state,
                "stage_state": "generating",
                "audio_job_id": job_id,
                "media_asset_id": None,
                "review_item_id": None,
                "review_decision": None,
            }
        if provider_state in {"failed", "canceled", "cancelled"}:
            message = _clean(payload.get("error_message") or payload.get("error_code") or provider_state)
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    update public.v3_studio_stage_attempts
                    set state='failed',error_code=$2,error_message=$3,
                        completed_at=coalesce(completed_at,now()),updated_at=now()
                    where attempt_id=$1
                    """,
                    attempt_id,
                    _clean(payload.get("error_code")) or "audio_generation_failed",
                    message[:4000],
                )
                await self.store.mark_failed(conn, stage_run_id=stage_run_id, error=message)
            raise ParticipantAudioBridgeError(f"audio_job_terminal_failure:{job_id}:{message}")
        if provider_state != "succeeded":
            return {
                "workflow_id": str(workflow_id),
                "stage_run_id": str(stage_run_id),
                "dialogue_turn_id": str(context.dialogue_turn_id),
                "participant_id": str(context.participant_id),
                "display_name": context.display_name,
                "provider_state": provider_state or "unknown",
                "stage_state": "generating",
                "audio_job_id": job_id,
                "media_asset_id": None,
                "review_item_id": None,
                "review_decision": None,
            }

        canonical = await self.client.canonical_output(
            headers=headers,
            job_id=job_id,
            project_id=context.project_id,
        )
        media_id = UUID(str(canonical.get("media_id")))
        audio_url = _clean(canonical.get("audio_url"))
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Re-check ownership and exact stage scope before accepting the owner-service media.
                media = await conn.fetchrow(
                    "select id,account_id,project_id,user_id from public.media_assets where id=$1",
                    media_id,
                )
                if not media or str(media["account_id"]) != str(account_id):
                    raise ParticipantAudioBridgeError("audio_media_account_mismatch")
                if media["project_id"] and str(media["project_id"]) != str(context.project_id):
                    raise ParticipantAudioBridgeError("audio_media_project_mismatch")

                review_id = await self.store.attach_output(
                    conn,
                    stage_run_id=stage_run_id,
                    media_id=media_id,
                    output_role="audio_candidate",
                )
                await conn.execute(
                    """
                    update public.v3_studio_stage_attempts
                    set state='succeeded',media_id=$2,
                        completed_at=coalesce(completed_at,now()),
                        metadata_json=coalesce(metadata_json,'{}'::jsonb) || $3::jsonb,updated_at=now()
                    where attempt_id=$1
                    """,
                    attempt_id,
                    media_id,
                    json.dumps({
                        "audio_url": audio_url,
                        "pricing": payload.get("pricing"),
                        "pricing_summary": payload.get("pricing_summary"),
                    }),
                )
                await conn.execute(
                    """
                    update public.v3_studio_stage_runs
                    set metadata_json=coalesce(metadata_json,'{}'::jsonb) || $2::jsonb,updated_at=now()
                    where stage_run_id=$1
                    """,
                    stage_run_id,
                    json.dumps({
                        "audio_url": audio_url,
                        "canonical_audio_media_id": str(media_id),
                        "pricing": payload.get("pricing"),
                        "pricing_summary": payload.get("pricing_summary"),
                    }),
                )

        return {
            "workflow_id": str(workflow_id),
            "stage_run_id": str(stage_run_id),
            "dialogue_turn_id": str(context.dialogue_turn_id),
            "participant_id": str(context.participant_id),
            "display_name": context.display_name,
            "provider_state": "succeeded",
            "stage_state": "awaiting_review",
            "audio_job_id": job_id,
            "media_asset_id": str(media_id),
            "audio_url": audio_url,
            "review_item_id": str(review_id),
            "review_decision": "pending",
            "pricing": payload.get("pricing"),
            "pricing_summary": payload.get("pricing_summary"),
        }


__all__ = [
    "AudioStageContext",
    "AudioStudioClient",
    "ParticipantAudioBridgeError",
    "ParticipantAudioExecutionService",
    "compile_context_audio_input",
    "load_audio_stage_context",
]
