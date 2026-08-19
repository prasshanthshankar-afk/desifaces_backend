from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx

from df_contracts.v3.director import PlannedParticipant
from desifaces_shared.v3.studio_workflow_store import CanonicalStudioWorkflowStore, StudioWorkflowError

from .participant_face import (
    FaceStudioClient,
    ParticipantFaceBinder,
    ParticipantFaceBridgeError,
    compile_participant_face_studio_input,
)


@dataclass(frozen=True)
class FaceStageContext:
    workflow_id: UUID
    stage_run_id: UUID
    participant_id: UUID
    display_name: str
    planned_participant: PlannedParticipant
    stage_state: str
    metadata: dict[str, Any]


def _dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        return dict(value or {})
    except Exception:
        return {}


def _planned_from_row(row) -> PlannedParticipant:
    metadata = _dict(row["participant_metadata"])
    return PlannedParticipant(
        participant_id=UUID(str(row["participant_id"])),
        kind=str(row["participant_kind"]),
        display_name=str(row["display_name"]),
        role=str(row["description"]) if row["description"] else None,
        persona=_dict(row["persona_json"]),
        continuity=_dict(row["continuity_json"]),
        preferred_locale=str(row["default_locale"]) if row["default_locale"] else None,
        visual_direction=_dict(metadata.get("visual_direction")),
        voice_direction=_dict(metadata.get("voice_direction")),
    )


async def load_face_stage_context(
    conn,
    *,
    account_id: UUID,
    workflow_id: UUID,
    stage_run_id: UUID,
    for_update: bool = False,
) -> FaceStageContext:
    lock = " for update of s" if for_update else ""
    row = await conn.fetchrow(
        f"""
        select s.stage_run_id,s.workflow_id,s.participant_id,s.state,s.metadata_json,
               p.participant_kind,p.display_name,p.description,p.default_locale,
               p.persona_json,p.continuity_json,p.metadata_json as participant_metadata
        from public.v3_studio_stage_runs s
        join public.v3_studio_workflows w on w.workflow_id=s.workflow_id
        join public.v3_participants p on p.participant_id=s.participant_id
        where s.stage_run_id=$1 and s.workflow_id=$2 and w.account_id=$3
          and s.stage_type='face' and s.scope_type='participant'
        {lock}
        """,
        stage_run_id,
        workflow_id,
        account_id,
    )
    if not row:
        raise ParticipantFaceBridgeError("face_stage_not_found_or_account_mismatch")
    return FaceStageContext(
        workflow_id=UUID(str(row["workflow_id"])),
        stage_run_id=UUID(str(row["stage_run_id"])),
        participant_id=UUID(str(row["participant_id"])),
        display_name=str(row["display_name"]),
        planned_participant=_planned_from_row(row),
        stage_state=str(row["state"]),
        metadata=_dict(row["metadata_json"]),
    )


def compile_context_face_input(context: FaceStageContext) -> dict[str, Any]:
    # Explicit age/gender are only taken from a durable explicit constraint block
    # if present. The compiler does not infer them from name, geography or role.
    participant_metadata = _dict(context.planned_participant.model_dump().get("metadata"))
    hint = _dict(context.metadata.get("explicit_face_constraints"))
    if not hint:
        hint = _dict(participant_metadata.get("explicit_face_constraints"))
    return compile_participant_face_studio_input(
        participant=context.planned_participant,
        participant_hint=hint,
        language=(context.planned_participant.preferred_locale or "en").split("-")[0],
        num_variants=1,
    )


class ParticipantFaceExecutionService:
    """Nonblocking control-plane bridge for one Participant Face output slot.

    Billing remains authoritative in svc-face/svc-pricing. A dispatch creates one
    independent provider job. A failure is retryable without touching already
    approved sibling Face stages. A successful output is bound for HITL review;
    workflow progression remains governed by the complete Face cohort barrier.
    """

    def __init__(self, *, face_base_url: str, store: CanonicalStudioWorkflowStore | None = None) -> None:
        self.face_client = FaceStudioClient(base_url=face_base_url)
        self.face_base_url = face_base_url.rstrip("/")
        self.store = store or CanonicalStudioWorkflowStore()
        self.binder = ParticipantFaceBinder(store=self.store)

    async def preview(
        self,
        conn,
        *,
        account_id: UUID,
        workflow_id: UUID,
        stage_run_id: UUID,
        headers: dict[str, str],
    ) -> tuple[FaceStageContext, dict[str, Any], dict[str, Any]]:
        context = await load_face_stage_context(
            conn, account_id=account_id, workflow_id=workflow_id, stage_run_id=stage_run_id,
        )
        if context.stage_state in {"approved", "generating", "awaiting_review"}:
            raise ParticipantFaceBridgeError(f"face_stage_not_previewable:{context.stage_state}")
        studio_input = compile_context_face_input(context)
        pricing = await self.face_client.preview_pricing(headers=headers, studio_input=studio_input)
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
    ) -> tuple[FaceStageContext, str, int, str]:
        async with pool.acquire() as conn:
            async with conn.transaction():
                context = await load_face_stage_context(
                    conn,
                    account_id=account_id,
                    workflow_id=workflow_id,
                    stage_run_id=stage_run_id,
                    for_update=True,
                )
                prior_state = context.stage_state
                if prior_state not in {"pending", "ready", "failed", "rejected"}:
                    raise ParticipantFaceBridgeError(f"face_stage_not_dispatchable:{prior_state}")
                attempt_count = int(context.metadata.get("face_attempt_count") or 0) + 1
                attempt_kind = "initial" if attempt_count == 1 else (
                    "regenerate" if prior_state == "rejected" else "retry"
                )
                await self.store.mark_generating(conn, stage_run_id=stage_run_id)
                await conn.execute(
                    """update public.v3_studio_stage_runs
                    set metadata_json=coalesce(metadata_json,'{}'::jsonb) || $2::jsonb,updated_at=now()
                    where stage_run_id=$1""",
                    stage_run_id,
                    {
                        "face_attempt_count": attempt_count,
                        "face_attempt_kind": attempt_kind,
                        "face_quote_id": quote_id,
                        "face_preview_fingerprint": preview_fingerprint,
                        "compatibility_face_job_id": None,
                        "last_error": None,
                    },
                )

        studio_input = compile_context_face_input(context)
        try:
            job_id = await self.face_client.create_job(
                headers=headers,
                studio_input=studio_input,
                pricing_preview={
                    "quote_id": quote_id,
                    "preview_fingerprint": preview_fingerprint,
                },
            )
        except Exception as exc:
            async with pool.acquire() as conn:
                await self.store.mark_failed(conn, stage_run_id=stage_run_id, error=str(exc))
            raise

        async with pool.acquire() as conn:
            await conn.execute(
                """update public.v3_studio_stage_runs
                set metadata_json=coalesce(metadata_json,'{}'::jsonb) || $2::jsonb,updated_at=now()
                where stage_run_id=$1""",
                stage_run_id,
                {"compatibility_face_job_id": job_id},
            )
        return context, job_id, attempt_count, attempt_kind

    async def _status_once(self, *, headers: dict[str, str], job_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=self.face_base_url,
            headers=headers,
            timeout=30.0,
        ) as client:
            response = await client.get(f"/api/face/creator/jobs/{job_id}/status")
        if response.status_code != 200:
            raise ParticipantFaceBridgeError(
                f"face_status_failed:{response.status_code}:{response.text[:1200]}"
            )
        return response.json()

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
            context = await load_face_stage_context(
                conn, account_id=account_id, workflow_id=workflow_id, stage_run_id=stage_run_id,
            )
            job_id = str(context.metadata.get("compatibility_face_job_id") or "").strip()
            existing = await conn.fetchrow(
                """select o.media_id,r.review_item_id,r.decision
                from public.v3_studio_stage_outputs o
                left join public.v3_studio_review_items r
                  on r.stage_run_id=o.stage_run_id and r.media_id=o.media_id
                where o.stage_run_id=$1 and o.is_active=true
                order by o.created_at desc limit 1""",
                stage_run_id,
            )
        if not job_id:
            return {
                "provider_state": None,
                "stage_state": context.stage_state,
                "face_job_id": None,
                "media_asset_id": str(existing["media_id"]) if existing else None,
                "review_item_id": str(existing["review_item_id"]) if existing and existing["review_item_id"] else None,
                "review_decision": str(existing["decision"]) if existing and existing["decision"] else None,
            }

        status_payload = await self._status_once(headers=headers, job_id=job_id)
        provider_state = str(status_payload.get("status") or "").strip().lower()
        response: dict[str, Any] = {
            "provider_state": provider_state,
            "stage_state": context.stage_state,
            "face_job_id": job_id,
            "media_asset_id": str(existing["media_id"]) if existing else None,
            "review_item_id": str(existing["review_item_id"]) if existing and existing["review_item_id"] else None,
            "review_decision": str(existing["decision"]) if existing and existing["decision"] else None,
        }

        if provider_state == "succeeded":
            variants = list(status_payload.get("variants") or [])
            if not variants:
                raise ParticipantFaceBridgeError(f"face_succeeded_without_variants:{job_id}")
            variant = dict(variants[0])
            media_asset_id = UUID(str(variant["media_asset_id"]))
            if not existing or str(existing["media_id"]) != str(media_asset_id):
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        review_item_id = await self.binder.bind_generated_face(
                            conn,
                            account_id=account_id,
                            participant_id=context.participant_id,
                            stage_run_id=stage_run_id,
                            media_asset_id=media_asset_id,
                            face_job_id=job_id,
                            face_profile_id=str(variant.get("face_profile_id") or ""),
                            prompt_used=str(variant.get("prompt_used") or ""),
                        )
                response["review_item_id"] = str(review_item_id)
                response["review_decision"] = "pending"
            response.update({
                "stage_state": "awaiting_review",
                "media_asset_id": str(media_asset_id),
                "image_url": str(variant.get("image_url") or ""),
                "face_profile_id": str(variant.get("face_profile_id") or ""),
                "prompt_used": str(variant.get("prompt_used") or ""),
                "pricing": status_payload.get("pricing"),
            })
            return response

        if provider_state in {"failed", "cancelled", "canceled"}:
            error = status_payload.get("error") or status_payload.get("message") or "face_generation_failed"
            async with pool.acquire() as conn:
                await self.store.mark_failed(conn, stage_run_id=stage_run_id, error=str(error))
            response["stage_state"] = "failed"
            response["error"] = error
            response["pricing"] = status_payload.get("pricing")
            return response

        response["pricing"] = status_payload.get("pricing")
        return response
