from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import httpx

from desifaces_shared.v3.studio_workflow_store import CanonicalStudioWorkflowStore

from .audio_execution import AudioStudioClient


class SceneFusionBridgeError(RuntimeError):
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


@dataclass(frozen=True)
class SceneTurnInput:
    dialogue_turn_id: UUID
    sequence_no: int
    participant_id: UUID
    display_name: str
    face_media_id: UUID
    audio_media_id: UUID
    emotion_code: str | None
    duration_hint_ms: int | None


@dataclass(frozen=True)
class FusionSceneContext:
    workflow_id: UUID
    stage_run_id: UUID
    account_id: UUID
    owner_user_id: UUID
    project_id: UUID
    story_id: UUID | None
    scene_id: UUID
    stage_state: str
    stage_metadata: dict[str, Any]
    scene_title: str | None
    scene_summary: str | None
    scene_direction: dict[str, Any]
    turns: tuple[SceneTurnInput, ...]


async def load_fusion_scene_context(
    conn,
    *,
    account_id: UUID,
    workflow_id: UUID,
    stage_run_id: UUID,
) -> FusionSceneContext:
    stage = await conn.fetchrow(
        """
        select s.stage_run_id,s.workflow_id,s.stage_type,s.scope_type,s.state,s.scene_id,
               s.metadata_json as stage_metadata,w.account_id,w.owner_user_id,w.project_id,
               w.story_id,w.current_stage,sc.title,sc.summary,sc.direction_json
        from public.v3_studio_stage_runs s
        join public.v3_studio_workflows w on w.workflow_id=s.workflow_id
        join public.v3_scenes sc on sc.scene_id=s.scene_id
        where s.stage_run_id=$1 and s.workflow_id=$2 and w.account_id=$3
        """,
        stage_run_id,
        workflow_id,
        account_id,
    )
    if not stage:
        raise SceneFusionBridgeError("fusion_stage_not_found_or_account_mismatch")
    if _clean(stage["stage_type"]) != "fusion" or _clean(stage["scope_type"]) != "scene":
        raise SceneFusionBridgeError("fusion_stage_type_scope_mismatch")
    if _clean(stage["current_stage"]) != "fusion":
        raise SceneFusionBridgeError("fusion_stage_not_current")

    turn_rows = await conn.fetch(
        """
        select dt.turn_id,dt.sequence_no,dt.speaker_participant_id,dt.emotion_code,
               dt.duration_hint_ms,p.display_name,p.primary_face_media_id,
               a.stage_run_id as audio_stage_run_id,ao.media_id as audio_media_id
        from public.v3_dialogue_turns dt
        join public.v3_participants p on p.participant_id=dt.speaker_participant_id
        join public.v3_studio_stage_runs a
          on a.workflow_id=$1 and a.stage_type='audio' and a.scope_type='dialogue_turn'
         and a.dialogue_turn_id=dt.turn_id and a.state='approved'
        join public.v3_studio_stage_outputs ao
          on ao.stage_run_id=a.stage_run_id and ao.is_active=true
        join public.v3_studio_review_items ar
          on ar.stage_run_id=a.stage_run_id and ar.media_id=ao.media_id and ar.decision='approved'
        where dt.scene_id=$2 and dt.turn_kind='speech'
        order by dt.sequence_no,dt.turn_id
        """,
        workflow_id,
        stage["scene_id"],
    )
    if not turn_rows:
        raise SceneFusionBridgeError("fusion_scene_requires_approved_dialogue_audio")

    turns: list[SceneTurnInput] = []
    for row in turn_rows:
        if not row["primary_face_media_id"]:
            raise SceneFusionBridgeError(
                f"fusion_speaker_face_not_approved:{row['speaker_participant_id']}"
            )
        turns.append(
            SceneTurnInput(
                dialogue_turn_id=UUID(str(row["turn_id"])),
                sequence_no=int(row["sequence_no"]),
                participant_id=UUID(str(row["speaker_participant_id"])),
                display_name=_clean(row["display_name"]) or "Character",
                face_media_id=UUID(str(row["primary_face_media_id"])),
                audio_media_id=UUID(str(row["audio_media_id"])),
                emotion_code=_clean(row["emotion_code"]) or None,
                duration_hint_ms=(int(row["duration_hint_ms"]) if row["duration_hint_ms"] is not None else None),
            )
        )

    return FusionSceneContext(
        workflow_id=UUID(str(stage["workflow_id"])),
        stage_run_id=UUID(str(stage["stage_run_id"])),
        account_id=UUID(str(stage["account_id"])),
        owner_user_id=UUID(str(stage["owner_user_id"])),
        project_id=UUID(str(stage["project_id"])),
        story_id=UUID(str(stage["story_id"])) if stage["story_id"] else None,
        scene_id=UUID(str(stage["scene_id"])),
        stage_state=_clean(stage["state"]),
        stage_metadata=_as_dict(stage["stage_metadata"]),
        scene_title=_clean(stage["title"]) or None,
        scene_summary=_clean(stage["summary"]) or None,
        scene_direction=_as_dict(stage["direction_json"]),
        turns=tuple(turns),
    )


class FaceAssetClient:
    def __init__(self, *, base_url: str, timeout_seconds: float = 35.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)

    async def read_url(self, *, headers: dict[str, str], media_id: UUID) -> str:
        async with httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=self.timeout_seconds) as client:
            response = await client.get(f"/api/face/assets/{media_id}/read-url")
        if response.status_code != 200:
            raise SceneFusionBridgeError(
                f"fusion_face_read_url_failed:{response.status_code}:{response.text[:1200]}"
            )
        url = _clean(response.json().get("read_url"))
        if not url:
            raise SceneFusionBridgeError("fusion_face_read_url_missing")
        return url


class FusionStudioClient:
    def __init__(self, *, base_url: str, timeout_seconds: float = 45.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)

    async def preview_pricing(self, *, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=self.timeout_seconds) as client:
            response = await client.post("/jobs/pricing/preview", json=payload)
        if response.status_code != 200:
            raise SceneFusionBridgeError(
                f"fusion_pricing_preview_failed:{response.status_code}:{response.text[:1200]}"
            )
        return response.json()

    async def create_job(
        self,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
        quote_id: str,
        preview_fingerprint: str | None,
    ) -> str:
        body = dict(payload)
        body["pricing_confirmation"] = {
            "quote_id": quote_id,
            "preview_fingerprint": preview_fingerprint,
            "user_confirmed": True,
        }
        async with httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=self.timeout_seconds) as client:
            response = await client.post("/jobs", json=body)
        if response.status_code not in {200, 201, 202}:
            raise SceneFusionBridgeError(
                f"fusion_generate_failed:{response.status_code}:{response.text[:1200]}"
            )
        job_id = _clean(response.json().get("job_id") or response.json().get("id"))
        if not job_id:
            raise SceneFusionBridgeError(f"fusion_generate_missing_job_id:{response.text[:1200]}")
        return job_id

    async def status(self, *, headers: dict[str, str], job_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=self.timeout_seconds) as client:
            response = await client.get(f"/jobs/{job_id}/status")
        if response.status_code != 200:
            raise SceneFusionBridgeError(
                f"fusion_status_failed:{response.status_code}:{response.text[:1200]}"
            )
        return response.json()


class SceneStitchClient:
    def __init__(self, *, base_url: str, timeout_seconds: float = 240.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)

    async def stitch(
        self,
        *,
        headers: dict[str, str],
        project_id: UUID,
        workflow_id: UUID,
        stage_run_id: UUID,
        attempt_id: UUID,
        segment_urls: list[str],
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=self.timeout_seconds) as client:
            response = await client.post(
                "/api/longform/v3/scene-stitch",
                json={
                    "project_id": str(project_id),
                    "workflow_id": str(workflow_id),
                    "stage_run_id": str(stage_run_id),
                    "attempt_id": str(attempt_id),
                    "segment_urls": segment_urls,
                },
            )
        if response.status_code != 200:
            raise SceneFusionBridgeError(
                f"fusion_scene_stitch_failed:{response.status_code}:{response.text[:1600]}"
            )
        return response.json()

    async def read_url(self, *, headers: dict[str, str], media_id: UUID) -> str:
        async with httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=self.timeout_seconds) as client:
            response = await client.get(f"/api/longform/v3/assets/{media_id}/read-url")
        if response.status_code != 200:
            raise SceneFusionBridgeError(
                f"fusion_scene_read_url_failed:{response.status_code}:{response.text[:1200]}"
            )
        url = _clean(response.json().get("read_url"))
        if not url:
            raise SceneFusionBridgeError("fusion_scene_read_url_missing")
        return url


def _scene_prompt(context: FusionSceneContext) -> str | None:
    parts: list[str] = []
    if context.scene_summary:
        parts.append(context.scene_summary)
    for key in ("performance", "camera", "motion", "tone", "style"):
        value = context.scene_direction.get(key)
        if value is not None and str(value).strip():
            parts.append(f"{key}: {str(value).strip()}")
    text = ". ".join(parts).strip()
    return text[:3500] or None


async def _compile_children(
    *,
    context: FusionSceneContext,
    face_client: FaceAssetClient,
    audio_client: AudioStudioClient,
    headers: dict[str, str],
    external_provider_ok: bool,
    request_nonce_by_turn: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    prompt = _scene_prompt(context)
    children: list[dict[str, Any]] = []
    for turn in context.turns:
        face_url = await face_client.read_url(headers=headers, media_id=turn.face_media_id)
        audio_url = await audio_client.read_url(headers=headers, media_id=turn.audio_media_id)
        video: dict[str, Any] = {}
        if turn.duration_hint_ms and turn.duration_hint_ms > 0:
            video["duration_sec"] = max(1, min(30, int(round(turn.duration_hint_ms / 1000.0))))
        if turn.emotion_code:
            video["emotion"] = turn.emotion_code
        turn_key = str(turn.dialogue_turn_id)
        request_nonce = _clean((request_nonce_by_turn or {}).get(turn_key))
        if prompt:
            # svc-fusion's canonical contract carries creative direction under
            # video.prompt; unknown top-level prompt fields are not authoritative.
            video["prompt"] = prompt
        payload: dict[str, Any] = {
            "face_image_url": face_url,
            "provider": "veed_fabric",
            "voice_mode": "audio",
            "voice_audio": {"type": "audio", "audio_url": audio_url},
            "consent": {"external_provider_ok": bool(external_provider_ok)},
            "video": video,
            "tags": {
                "v3_orchestrated": True,
                "workflow_id": str(context.workflow_id),
                "scene_id": str(context.scene_id),
                "stage_run_id": str(context.stage_run_id),
                "dialogue_turn_id": str(turn.dialogue_turn_id),
                "participant_id": str(turn.participant_id),
                "segment_sequence": turn.sequence_no,
            },
        }
        if request_nonce:
            # This nonce is part of both the pricing-preview payload and the later
            # confirmed dispatch payload. It permits intentional regeneration while
            # keeping replay of one confirmed quote idempotent.
            payload["provider_options"] = {"v3_request_nonce": request_nonce}
        children.append(
            {
                "dialogue_turn_id": str(turn.dialogue_turn_id),
                "participant_id": str(turn.participant_id),
                "display_name": turn.display_name,
                "sequence_no": turn.sequence_no,
                "face_media_id": str(turn.face_media_id),
                "audio_media_id": str(turn.audio_media_id),
                "payload": payload,
            }
        )
    return children


def _video_url_from_status(payload: dict[str, Any]) -> str:
    for key in ("final_video_url", "output_video_url", "share_url", "video_url"):
        url = _clean(payload.get(key))
        if url:
            return url
    artifacts = list(payload.get("artifacts") or [])
    for artifact in artifacts:
        item = _as_dict(artifact)
        if "video" in _clean(item.get("kind")).casefold() and _clean(item.get("url")):
            return _clean(item.get("url"))
    for artifact in artifacts:
        item = _as_dict(artifact)
        if _clean(item.get("url")):
            return _clean(item.get("url"))
    return ""


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


class SceneFusionExecutionService:
    """Render one story scene as ordered single-speaker Fusion shots, then stitch.

    svc-fusion retains child render ownership. svc-fusion-extension owns stitching.
    Director owns only the logical scene attempt, dependency/HITL state and lineage.
    """

    def __init__(
        self,
        *,
        face_base_url: str,
        audio_base_url: str,
        fusion_base_url: str,
        fusion_extension_base_url: str,
        store: CanonicalStudioWorkflowStore | None = None,
    ) -> None:
        self.face_client = FaceAssetClient(base_url=face_base_url)
        self.audio_client = AudioStudioClient(base_url=audio_base_url)
        self.fusion_client = FusionStudioClient(base_url=fusion_base_url)
        self.stitch_client = SceneStitchClient(base_url=fusion_extension_base_url)
        self.store = store or CanonicalStudioWorkflowStore()

    async def preview(
        self,
        conn,
        *,
        account_id: UUID,
        workflow_id: UUID,
        stage_run_id: UUID,
        headers: dict[str, str],
        external_provider_ok: bool,
    ) -> tuple[FusionSceneContext, list[dict[str, Any]]]:
        context = await load_fusion_scene_context(
            conn,
            account_id=account_id,
            workflow_id=workflow_id,
            stage_run_id=stage_run_id,
        )
        if context.stage_state not in {"pending", "ready", "failed", "rejected"}:
            raise SceneFusionBridgeError(f"fusion_stage_not_priceable:{context.stage_state}")
        await self.store.assert_startable(conn, stage_run_id=stage_run_id)
        request_nonce_by_turn = {str(turn.dialogue_turn_id): uuid4().hex for turn in context.turns}
        children = await _compile_children(
            context=context,
            face_client=self.face_client,
            audio_client=self.audio_client,
            headers=headers,
            external_provider_ok=external_provider_ok,
            request_nonce_by_turn=request_nonce_by_turn,
        )
        quotes: list[dict[str, Any]] = []
        for child in children:
            preview = await self.fusion_client.preview_pricing(
                headers=headers,
                payload=child["payload"],
            )
            quote_id = _clean(preview.get("quote_id") or _as_dict(preview.get("pricing")).get("quote_id"))
            if not quote_id:
                raise SceneFusionBridgeError(
                    f"fusion_pricing_preview_missing_quote_id:{child['dialogue_turn_id']}"
                )
            quotes.append(
                {
                    "dialogue_turn_id": child["dialogue_turn_id"],
                    "participant_id": child["participant_id"],
                    "display_name": child["display_name"],
                    "sequence_no": child["sequence_no"],
                    "request_nonce": request_nonce_by_turn[child["dialogue_turn_id"]],
                    "quote_id": quote_id,
                    "preview_fingerprint": preview.get("preview_fingerprint")
                    or _as_dict(preview.get("pricing")).get("preview_fingerprint"),
                    "pricing": preview.get("pricing") or {},
                    "pricing_summary": preview.get("pricing_summary") or {},
                    "message": preview.get("message"),
                }
            )
        return context, quotes

    async def dispatch(
        self,
        pool,
        *,
        account_id: UUID,
        workflow_id: UUID,
        stage_run_id: UUID,
        headers: dict[str, str],
        confirmations: list[dict[str, Any]],
        external_provider_ok: bool,
    ) -> tuple[FusionSceneContext, UUID, int, str, list[dict[str, Any]]]:
        if not external_provider_ok:
            raise SceneFusionBridgeError("fusion_external_provider_consent_required")
        confirmation_by_turn = {
            _clean(item.get("dialogue_turn_id")): item for item in confirmations if _clean(item.get("dialogue_turn_id"))
        }

        async with pool.acquire() as conn:
            async with conn.transaction():
                context = await load_fusion_scene_context(
                    conn,
                    account_id=account_id,
                    workflow_id=workflow_id,
                    stage_run_id=stage_run_id,
                )
                prior_state = context.stage_state
                if prior_state not in {"pending", "ready", "failed", "rejected"}:
                    latest = await _latest_attempt(conn, stage_run_id=stage_run_id)
                    if prior_state == "generating" and latest:
                        return (
                            context,
                            UUID(str(latest["attempt_id"])),
                            int(latest["attempt_no"]),
                            str(latest["attempt_kind"]),
                            list(_as_dict(latest["metadata_json"]).get("children") or []),
                        )
                    raise SceneFusionBridgeError(f"fusion_stage_not_dispatchable:{prior_state}")
                await self.store.assert_startable(conn, stage_run_id=stage_run_id)
                expected_turns = {str(turn.dialogue_turn_id) for turn in context.turns}
                if set(confirmation_by_turn) != expected_turns:
                    raise SceneFusionBridgeError("fusion_pricing_confirmation_bundle_mismatch")
                for turn_id, item in confirmation_by_turn.items():
                    if not _clean(item.get("quote_id")):
                        raise SceneFusionBridgeError(f"fusion_quote_required:{turn_id}")
                    if not _clean(item.get("request_nonce")):
                        raise SceneFusionBridgeError(f"fusion_request_nonce_required:{turn_id}")

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
                row = await conn.fetchrow(
                    """
                    insert into public.v3_studio_stage_attempts(
                      stage_run_id,attempt_no,attempt_kind,state,provider_service,provider_job_ref,metadata_json
                    ) values($1,$2,$3,'dispatching','svc-director',$4,$5::jsonb)
                    returning attempt_id
                    """,
                    stage_run_id,
                    attempt_no,
                    attempt_kind,
                    f"scene-fusion:{stage_run_id}:{attempt_no}",
                    json.dumps({"children": [], "pricing_confirmations": confirmations}),
                )
                attempt_id = UUID(str(row["attempt_id"]))
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
                            "fusion_attempt_id": str(attempt_id),
                            "fusion_attempt_no": attempt_no,
                            "fusion_attempt_kind": attempt_kind,
                            "render_strategy": "dialogue_turn_segments_then_stitch",
                        }
                    ),
                )

        children = await _compile_children(
            context=context,
            face_client=self.face_client,
            audio_client=self.audio_client,
            headers=headers,
            external_provider_ok=external_provider_ok,
            request_nonce_by_turn={
                turn_id: _clean(item.get("request_nonce"))
                for turn_id, item in confirmation_by_turn.items()
            },
        )
        dispatched: list[dict[str, Any]] = []
        try:
            for child in children:
                confirmation = confirmation_by_turn[child["dialogue_turn_id"]]
                job_id = await self.fusion_client.create_job(
                    headers=headers,
                    payload=child["payload"],
                    quote_id=_clean(confirmation.get("quote_id")),
                    preview_fingerprint=_clean(confirmation.get("preview_fingerprint")) or None,
                )
                child_record = {
                    k: v for k, v in child.items() if k != "payload"
                }
                child_record.update(
                    {
                        "fusion_job_id": job_id,
                        "status": "queued",
                        "quote_id": _clean(confirmation.get("quote_id")),
                        "preview_fingerprint": _clean(confirmation.get("preview_fingerprint")) or None,
                    }
                )
                dispatched.append(child_record)
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        update public.v3_studio_stage_attempts
                        set metadata_json=coalesce(metadata_json,'{}'::jsonb) || $2::jsonb,updated_at=now()
                        where attempt_id=$1
                        """,
                        attempt_id,
                        json.dumps({"children": dispatched, "dispatch_outcome": "partial"}),
                    )
        except Exception as exc:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    update public.v3_studio_stage_attempts
                    set state='failed',error_code='fusion_child_dispatch_failed',error_message=$2,
                        metadata_json=coalesce(metadata_json,'{}'::jsonb) || $3::jsonb,updated_at=now()
                    where attempt_id=$1
                    """,
                    attempt_id,
                    str(exc)[:4000],
                    json.dumps({"children": dispatched, "dispatch_outcome": "failed"}),
                )
                await self.store.mark_failed(conn, stage_run_id=stage_run_id, error=str(exc))
            raise

        async with pool.acquire() as conn:
            await conn.execute(
                """
                update public.v3_studio_stage_attempts
                set state='running',metadata_json=coalesce(metadata_json,'{}'::jsonb) || $2::jsonb,updated_at=now()
                where attempt_id=$1
                """,
                attempt_id,
                json.dumps({"children": dispatched, "dispatch_outcome": "accepted"}),
            )
        return context, attempt_id, attempt_no, attempt_kind, dispatched

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
            context = await load_fusion_scene_context(
                conn,
                account_id=account_id,
                workflow_id=workflow_id,
                stage_run_id=stage_run_id,
            )
            existing = await _latest_output_review(conn, stage_run_id=stage_run_id)
            latest = await _latest_attempt(conn, stage_run_id=stage_run_id)
            if existing and context.stage_state in {"awaiting_review", "approved", "rejected"}:
                media_id = UUID(str(existing["media_id"]))
                video_url = await self.stitch_client.read_url(headers=headers, media_id=media_id)
                return {
                    "workflow_id": str(workflow_id),
                    "stage_run_id": str(stage_run_id),
                    "scene_id": str(context.scene_id),
                    "provider_state": "succeeded",
                    "stage_state": context.stage_state,
                    "media_asset_id": str(media_id),
                    "video_url": video_url,
                    "review_item_id": str(existing["review_item_id"]) if existing["review_item_id"] else None,
                    "review_decision": str(existing["decision"]) if existing["decision"] else None,
                    "children": list(_as_dict(latest["metadata_json"]).get("children") or []) if latest else [],
                }
            if not latest:
                return {
                    "workflow_id": str(workflow_id),
                    "stage_run_id": str(stage_run_id),
                    "scene_id": str(context.scene_id),
                    "provider_state": None,
                    "stage_state": context.stage_state,
                    "media_asset_id": None,
                    "video_url": None,
                    "review_item_id": None,
                    "review_decision": None,
                    "children": [],
                }
            attempt_id = UUID(str(latest["attempt_id"]))
            metadata = _as_dict(latest["metadata_json"])
            children = list(metadata.get("children") or [])
            if not children:
                raise SceneFusionBridgeError("fusion_attempt_has_no_child_jobs")

        refreshed: list[dict[str, Any]] = []
        any_failed = False
        all_succeeded = True
        for child in children:
            item = dict(child)
            job_id = _clean(item.get("fusion_job_id"))
            if not job_id:
                any_failed = True
                all_succeeded = False
                item["status"] = "failed"
                item["error"] = "missing_fusion_job_id"
                refreshed.append(item)
                continue
            payload = await self.fusion_client.status(headers=headers, job_id=job_id)
            state = _clean(payload.get("status")).lower()
            item["status"] = state or "unknown"
            item["error_code"] = payload.get("error_code")
            item["error_message"] = payload.get("error_message")
            if state in {"failed", "canceled", "cancelled"}:
                any_failed = True
                all_succeeded = False
            elif state in {"succeeded", "completed", "complete", "ready"}:
                video_url = _video_url_from_status(payload)
                if not video_url:
                    any_failed = True
                    all_succeeded = False
                    item["status"] = "failed"
                    item["error"] = "fusion_succeeded_without_video_url"
                else:
                    item["video_url"] = video_url
            else:
                all_succeeded = False
            refreshed.append(item)

        async with pool.acquire() as conn:
            await conn.execute(
                """
                update public.v3_studio_stage_attempts
                set metadata_json=coalesce(metadata_json,'{}'::jsonb) || $2::jsonb,updated_at=now()
                where attempt_id=$1
                """,
                attempt_id,
                json.dumps({"children": refreshed}),
            )

        if any_failed:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    update public.v3_studio_stage_attempts
                    set state='failed',error_code='fusion_child_failed',error_message='one_or_more_child_fusion_jobs_failed',updated_at=now()
                    where attempt_id=$1
                    """,
                    attempt_id,
                )
                await self.store.mark_failed(
                    conn,
                    stage_run_id=stage_run_id,
                    error="one_or_more_child_fusion_jobs_failed",
                )
            raise SceneFusionBridgeError("fusion_child_job_failed")

        if not all_succeeded:
            return {
                "workflow_id": str(workflow_id),
                "stage_run_id": str(stage_run_id),
                "scene_id": str(context.scene_id),
                "provider_state": "running",
                "stage_state": "generating",
                "media_asset_id": None,
                "video_url": None,
                "review_item_id": None,
                "review_decision": None,
                "children": refreshed,
            }

        ordered_urls = [
            _clean(item.get("video_url"))
            for item in sorted(refreshed, key=lambda x: int(x.get("sequence_no") or 0))
        ]
        stitch = await self.stitch_client.stitch(
            headers=headers,
            project_id=context.project_id,
            workflow_id=context.workflow_id,
            stage_run_id=context.stage_run_id,
            attempt_id=attempt_id,
            segment_urls=ordered_urls,
        )
        media_id = UUID(str(stitch.get("media_id")))
        video_url = _clean(stitch.get("video_url"))

        async with pool.acquire() as conn:
            async with conn.transaction():
                media = await conn.fetchrow(
                    "select id,account_id,project_id,user_id from public.media_assets where id=$1",
                    media_id,
                )
                if not media or str(media["account_id"]) != str(account_id):
                    raise SceneFusionBridgeError("fusion_final_media_account_mismatch")
                if media["project_id"] and str(media["project_id"]) != str(context.project_id):
                    raise SceneFusionBridgeError("fusion_final_media_project_mismatch")

                review_id = await self.store.attach_output(
                    conn,
                    stage_run_id=stage_run_id,
                    media_id=media_id,
                    output_role="scene_video_candidate",
                )
                # Explicitly bind approved Face/Audio lineage into the logical scene stage.
                for turn in context.turns:
                    face_stage_id = await conn.fetchval(
                        """
                        select stage_run_id from public.v3_studio_stage_runs
                        where workflow_id=$1 and stage_type='face' and scope_type='participant'
                          and participant_id=$2 and state='approved'
                        order by created_at desc limit 1
                        """,
                        workflow_id,
                        turn.participant_id,
                    )
                    audio_stage_id = await conn.fetchval(
                        """
                        select stage_run_id from public.v3_studio_stage_runs
                        where workflow_id=$1 and stage_type='audio' and scope_type='dialogue_turn'
                          and dialogue_turn_id=$2 and state='approved'
                        order by created_at desc limit 1
                        """,
                        workflow_id,
                        turn.dialogue_turn_id,
                    )
                    if face_stage_id:
                        await self.store.bind_approved_input(
                            conn,
                            stage_run_id=stage_run_id,
                            media_id=turn.face_media_id,
                            input_role="approved_speaker_face",
                            source_stage_run_id=UUID(str(face_stage_id)),
                        )
                    if audio_stage_id:
                        await self.store.bind_approved_input(
                            conn,
                            stage_run_id=stage_run_id,
                            media_id=turn.audio_media_id,
                            input_role="approved_dialogue_audio",
                            source_stage_run_id=UUID(str(audio_stage_id)),
                        )
                await conn.execute(
                    """
                    update public.v3_studio_stage_attempts
                    set state='succeeded',media_id=$2,
                        metadata_json=coalesce(metadata_json,'{}'::jsonb) || $3::jsonb,updated_at=now()
                    where attempt_id=$1
                    """,
                    attempt_id,
                    media_id,
                    json.dumps({"children": refreshed, "stitched_video_url": video_url}),
                )
                await conn.execute(
                    """
                    update public.v3_studio_stage_runs
                    set metadata_json=coalesce(metadata_json,'{}'::jsonb) || $2::jsonb,updated_at=now()
                    where stage_run_id=$1
                    """,
                    stage_run_id,
                    json.dumps({"canonical_scene_video_media_id": str(media_id)}),
                )

        return {
            "workflow_id": str(workflow_id),
            "stage_run_id": str(stage_run_id),
            "scene_id": str(context.scene_id),
            "provider_state": "succeeded",
            "stage_state": "awaiting_review",
            "media_asset_id": str(media_id),
            "video_url": video_url,
            "review_item_id": str(review_id),
            "review_decision": "pending",
            "children": refreshed,
        }


__all__ = [
    "FusionSceneContext",
    "SceneFusionBridgeError",
    "SceneFusionExecutionService",
    "SceneTurnInput",
    "load_fusion_scene_context",
]
