from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from .fusion_execution import (
    SceneFusionBridgeError,
    SceneFusionExecutionService,
    _as_dict,
    _clean,
    _compile_children,
    _latest_attempt,
    load_fusion_scene_context,
)


def _completed_children(metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return reusable successful child renders keyed by dialogue turn.

    A failed scene retry must not re-render or re-charge successful dialogue children.
    Only children with an actual video URL are reusable. Merely seeing a provider
    success state is insufficient because stitching requires a concrete ordered URL.
    """
    out: dict[str, dict[str, Any]] = {}
    for raw in list(metadata.get("children") or []):
        item = dict(raw or {})
        turn_id = _clean(item.get("dialogue_turn_id"))
        state = _clean(item.get("status")).lower()
        video_url = _clean(item.get("video_url"))
        if turn_id and state in {"succeeded", "completed", "complete", "ready"} and video_url:
            item["status"] = "succeeded"
            item["reused_from_prior_attempt"] = True
            out[turn_id] = item
    return out


class ResilientSceneFusionExecutionService(SceneFusionExecutionService):
    """Scene Fusion execution with failed-child-only retry semantics.

    Initial generation and user-requested revision regenerate every dialogue child.
    A technical retry after a failed scene preserves successful child videos and asks
    pricing/dispatch only for missing or failed dialogue turns. This protects both
    user credits and approved upstream Face/Audio assets.
    """

    async def preview(
        self,
        conn,
        *,
        account_id: UUID,
        workflow_id: UUID,
        stage_run_id: UUID,
        headers: dict[str, str],
        external_provider_ok: bool,
    ):
        context = await load_fusion_scene_context(
            conn,
            account_id=account_id,
            workflow_id=workflow_id,
            stage_run_id=stage_run_id,
        )
        if context.stage_state not in {"pending", "ready", "failed", "rejected"}:
            raise SceneFusionBridgeError(f"fusion_stage_not_priceable:{context.stage_state}")
        await self.store.assert_startable(conn, stage_run_id=stage_run_id)

        preserved: dict[str, dict[str, Any]] = {}
        if context.stage_state == "failed":
            latest = await _latest_attempt(conn, stage_run_id=stage_run_id)
            if latest:
                preserved = _completed_children(_as_dict(latest["metadata_json"]))

        required_turn_ids = {
            str(turn.dialogue_turn_id)
            for turn in context.turns
            if str(turn.dialogue_turn_id) not in preserved
        }
        if not required_turn_ids:
            # Every child is already available. A prior attempt therefore failed
            # after child rendering (normally scene stitching). Re-pricing child
            # renders would double-charge the user, so an empty quote bundle is the
            # explicit signal for the no-charge stitch-only retry route.
            return context, []

        request_nonce_by_turn = {turn_id: uuid4().hex for turn_id in required_turn_ids}
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
            turn_id = child["dialogue_turn_id"]
            if turn_id not in required_turn_ids:
                continue
            preview = await self.fusion_client.preview_pricing(
                headers=headers,
                payload=child["payload"],
            )
            pricing = _as_dict(preview.get("pricing"))
            quote_id = _clean(preview.get("quote_id") or pricing.get("quote_id"))
            if not quote_id:
                raise SceneFusionBridgeError(
                    f"fusion_pricing_preview_missing_quote_id:{turn_id}"
                )
            quotes.append({
                "dialogue_turn_id": turn_id,
                "participant_id": child["participant_id"],
                "display_name": child["display_name"],
                "sequence_no": child["sequence_no"],
                "request_nonce": request_nonce_by_turn[turn_id],
                "quote_id": quote_id,
                "preview_fingerprint": preview.get("preview_fingerprint")
                or pricing.get("preview_fingerprint"),
                "pricing": pricing,
                "pricing_summary": preview.get("pricing_summary") or {},
                "message": preview.get("message"),
                "retry_scope": "failed_child_only" if preserved else "initial_scene",
                "preserved_child_count": len(preserved),
            })
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
    ):
        if not external_provider_ok:
            raise SceneFusionBridgeError("fusion_external_provider_consent_required")
        confirmation_by_turn = {
            _clean(item.get("dialogue_turn_id")): item
            for item in confirmations
            if _clean(item.get("dialogue_turn_id"))
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

                preserved: dict[str, dict[str, Any]] = {}
                if prior_state == "failed":
                    prior_attempt = await _latest_attempt(conn, stage_run_id=stage_run_id)
                    if prior_attempt:
                        preserved = _completed_children(_as_dict(prior_attempt["metadata_json"]))

                all_turn_ids = {str(turn.dialogue_turn_id) for turn in context.turns}
                expected_turns = all_turn_ids - set(preserved)
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
                    ) or 1
                )
                attempt_kind = "initial" if attempt_no == 1 else (
                    "regenerate" if prior_state == "rejected" else "retry"
                )
                preserved_children = [
                    preserved[turn_id]
                    for turn_id in sorted(
                        preserved,
                        key=lambda value: int(preserved[value].get("sequence_no") or 0),
                    )
                ]
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
                    json.dumps({
                        "children": preserved_children,
                        "pricing_confirmations": confirmations,
                        "preserved_child_count": len(preserved_children),
                        "retry_scope": "failed_child_only" if preserved_children else attempt_kind,
                    }),
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
                    json.dumps({
                        "fusion_attempt_id": str(attempt_id),
                        "fusion_attempt_no": attempt_no,
                        "fusion_attempt_kind": attempt_kind,
                        "render_strategy": "dialogue_turn_segments_then_stitch",
                        "retry_scope": "failed_child_only" if preserved_children else attempt_kind,
                    }),
                )

        if not expected_turns:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    update public.v3_studio_stage_attempts
                    set state='running',metadata_json=coalesce(metadata_json,'{}'::jsonb) || $2::jsonb,updated_at=now()
                    where attempt_id=$1
                    """,
                    attempt_id,
                    json.dumps({
                        "children": preserved_children,
                        "dispatch_outcome": "stitch_only_retry",
                    }),
                )
            return context, attempt_id, attempt_no, attempt_kind, preserved_children

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
        dispatched = list(preserved_children)
        try:
            for child in children:
                turn_id = child["dialogue_turn_id"]
                if turn_id not in expected_turns:
                    continue
                confirmation = confirmation_by_turn[turn_id]
                job_id = await self.fusion_client.create_job(
                    headers=headers,
                    payload=child["payload"],
                    quote_id=_clean(confirmation.get("quote_id")),
                    preview_fingerprint=_clean(confirmation.get("preview_fingerprint")) or None,
                )
                child_record = {k: v for k, v in child.items() if k != "payload"}
                child_record.update({
                    "fusion_job_id": job_id,
                    "status": "queued",
                    "quote_id": _clean(confirmation.get("quote_id")),
                    "preview_fingerprint": _clean(confirmation.get("preview_fingerprint")) or None,
                    "reused_from_prior_attempt": False,
                })
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
                    set state='failed',completed_at=coalesce(completed_at,now()),
                        error_code='fusion_child_dispatch_failed',error_message=$2,
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
        try:
            return await super().sync(
                pool,
                account_id=account_id,
                workflow_id=workflow_id,
                stage_run_id=stage_run_id,
                headers=headers,
            )
        except SceneFusionBridgeError as exc:
            code = str(exc)
            post_child_failure = (
                code.startswith("fusion_scene_stitch_failed:")
                or code.startswith("fusion_final_media_")
            )
            if not post_child_failure:
                raise

            # Base sync has already persisted refreshed child status/video URLs before
            # it calls deterministic stitching. Convert that late failure into a real
            # failed attempt/stage so the next preview can preserve every completed
            # child and offer a no-charge stitch-only recovery.
            async with pool.acquire() as conn:
                latest = await _latest_attempt(conn, stage_run_id=stage_run_id)
                if latest:
                    attempt_id = UUID(str(latest["attempt_id"]))
                    await conn.execute(
                        """
                        update public.v3_studio_stage_attempts
                        set state='failed',completed_at=coalesce(completed_at,now()),
                            error_code='fusion_scene_finalize_failed',error_message=$2,updated_at=now()
                        where attempt_id=$1 and state not in ('succeeded','canceled','cancelled')
                        """,
                        attempt_id,
                        code[:4000],
                    )
                await self.store.mark_failed(
                    conn,
                    stage_run_id=stage_run_id,
                    error="Scene assembly could not finish. Completed dialogue videos were preserved.",
                )
            raise SceneFusionBridgeError("fusion_scene_finalize_failed") from exc


__all__ = ["ResilientSceneFusionExecutionService"]
