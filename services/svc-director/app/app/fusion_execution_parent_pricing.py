from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID, uuid4

import httpx

from .fusion_execution import (
    SceneFusionBridgeError,
    _as_dict,
    _clean,
    _compile_children,
    _latest_attempt,
    load_fusion_scene_context,
)
from .fusion_execution_performance import PerformantResilientSceneFusionExecutionService
from .fusion_execution_resilient import _completed_children


_INTERNAL_REASON = "child_job_of_billable_v3_scene"
_PROVIDER = "veed_fabric"


def _suppressed_pricing(parent_stage_run_id: UUID, dialogue_turn_id: str) -> dict[str, Any]:
    return {
        "enabled": False,
        "state": "suppressed",
        "suppressed": True,
        "pricing_suppressed": True,
        "suppress_pricing": True,
        "billing_mode": "internal",
        "settlement_mode": "internal",
        "service_name": "svc-fusion",
        "service_action": "fusion.video.generate.internal_child",
        "variant_code": "FUSION_INTERNAL_CHILD",
        "sku_code": "FUSION_INTERNAL_CHILD",
        "estimated_units": "0",
        "actual_units": None,
        "billed_units": "0",
        "amount": "0.00",
        "final_amount": "0.00",
        "currency": None,
        "quote_id": None,
        "reservation_id": None,
        "ledger_entry_id": None,
        "parent_service": "svc-fusion-extension",
        "parent_job_id": str(parent_stage_run_id),
        "parent_longform_job_id": str(parent_stage_run_id),
        "billing_parent_job_id": str(parent_stage_run_id),
        "segment_id": dialogue_turn_id,
        "reason": _INTERNAL_REASON,
    }


def _stamp_internal_child(payload: dict[str, Any], *, stage_run_id: UUID, dialogue_turn_id: str) -> dict[str, Any]:
    out = dict(payload or {})
    pricing = _suppressed_pricing(stage_run_id, dialogue_turn_id)
    billing_context = {
        "parent_service": "svc-fusion-extension",
        "parent_job_id": str(stage_run_id),
        "parent_longform_job_id": str(stage_run_id),
        "billing_parent_job_id": str(stage_run_id),
        "segment_id": dialogue_turn_id,
        "child_role": "dialogue_turn_render",
        "reason": _INTERNAL_REASON,
        "pricing_suppressed": True,
        "suppress_pricing": True,
        "internal_job": True,
        "child_job": True,
        "bill_to_parent": True,
    }

    for key in ("tags", "provider_options", "meta", "metadata"):
        nested = _as_dict(out.get(key))
        nested.update(
            {
                "pricing_suppressed": True,
                "suppress_pricing": True,
                "skip_pricing": True,
                "disable_pricing": True,
                "pricing_enabled": False,
                "internal_job": True,
                "child_job": True,
                "is_internal_child": True,
                "child_job_of_billable_longform_parent": True,
                "bill_to_parent": True,
                "parent_service": "svc-fusion-extension",
                "pricing": pricing,
                "pricing_context": billing_context,
                "billing_context": billing_context,
            }
        )
        out[key] = nested

    out.update(
        {
            "pricing_suppressed": True,
            "suppress_pricing": True,
            "skip_pricing": True,
            "disable_pricing": True,
            "pricing_enabled": False,
            "internal_job": True,
            "child_job": True,
            "is_internal_child": True,
            "child_job_of_billable_longform_parent": True,
            "bill_to_parent": True,
            "pricing": pricing,
            "pricing_context": billing_context,
            "billing_context": billing_context,
        }
    )
    return out


def _pricing_is_suppressed(payload: dict[str, Any]) -> bool:
    pricing = _as_dict(payload.get("pricing"))
    state = _clean(pricing.get("state")).lower()
    quote_id = _clean(payload.get("quote_id") or pricing.get("quote_id"))
    return (
        state == "suppressed"
        and not quote_id
        and bool(pricing.get("suppressed") or pricing.get("pricing_suppressed"))
        and _clean(pricing.get("billing_mode")).lower() == "internal"
    )


class ParentScenePricingClient:
    def __init__(self, *, base_url: str, timeout_seconds: float = 90.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=float(timeout_seconds),
            limits=httpx.Limits(max_connections=12, max_keepalive_connections=6),
        )

    def _body(self, context) -> dict[str, str]:
        return {
            "project_id": str(context.project_id),
            "workflow_id": str(context.workflow_id),
            "stage_run_id": str(context.stage_run_id),
        }

    async def _post(self, path: str, *, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(path, headers=headers, json=body)
        if response.status_code != 200:
            raise SceneFusionBridgeError(
                f"fusion_parent_pricing_{path.rsplit('/', 1)[-1]}_failed:"
                f"{response.status_code}:{response.text[:1600]}"
            )
        return dict(response.json() or {})

    async def preview(self, *, headers: dict[str, str], context) -> dict[str, Any]:
        return await self._post(
            "/api/longform/v3/scene-pricing/preview",
            headers=headers,
            body=self._body(context),
        )

    async def reserve(
        self,
        *,
        headers: dict[str, str],
        context,
        quote_id: str,
        preview_fingerprint: str,
    ) -> dict[str, Any]:
        return await self._post(
            "/api/longform/v3/scene-pricing/reserve",
            headers=headers,
            body={
                **self._body(context),
                "quote_id": quote_id,
                "preview_fingerprint": preview_fingerprint,
            },
        )

    async def commit(self, *, headers: dict[str, str], context) -> dict[str, Any]:
        return await self._post(
            "/api/longform/v3/scene-pricing/commit",
            headers=headers,
            body=self._body(context),
        )

    async def release(self, *, headers: dict[str, str], context, reason: str) -> dict[str, Any]:
        return await self._post(
            "/api/longform/v3/scene-pricing/release",
            headers=headers,
            body={**self._body(context), "reason": reason[:300]},
        )


class ParentPricedSceneFusionExecutionService(PerformantResilientSceneFusionExecutionService):
    """V3 scene execution with one logical parent price and zero-priced children.

    svc-fusion-extension owns the one parent pricing lifecycle because it already owns
    scene assembly, actual-duration probing, and the existing parent/child pricing
    pattern. svc-fusion retains provider execution ownership for each dialogue child,
    but every child is explicitly internal/bill-to-parent and must prove a suppressed
    pricing contract before any parent reservation is allowed.
    """

    child_pricing_concurrency = 8

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.parent_pricing = ParentScenePricingClient(
            base_url=self.stitch_client.base_url,
        )

    async def _fusion_post(
        self,
        path: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        pooled = getattr(self.fusion_client, "_client", None)
        if pooled is not None:
            response = await pooled.post(path, headers=headers, json=payload)
        else:
            async with httpx.AsyncClient(
                base_url=self.fusion_client.base_url,
                timeout=self.fusion_client.timeout_seconds,
            ) as client:
                response = await client.post(path, headers=headers, json=payload)
        if response.status_code not in {200, 201, 202}:
            raise SceneFusionBridgeError(
                f"fusion_internal_child_request_failed:{path}:{response.status_code}:{response.text[:1200]}"
            )
        return dict(response.json() or {})

    async def _verify_child_pricing_suppressed(
        self,
        *,
        headers: dict[str, str],
        child: dict[str, Any],
        stage_run_id: UUID,
    ) -> dict[str, Any]:
        turn_id = child["dialogue_turn_id"]
        payload = _stamp_internal_child(
            child["payload"],
            stage_run_id=stage_run_id,
            dialogue_turn_id=turn_id,
        )
        preview = await self._fusion_post(
            "/jobs/pricing/preview",
            headers=headers,
            payload=payload,
        )
        if not _pricing_is_suppressed(preview):
            raise SceneFusionBridgeError(
                f"fusion_child_pricing_not_suppressed:{turn_id}"
            )
        return {
            "dialogue_turn_id": turn_id,
            "participant_id": child["participant_id"],
            "display_name": child["display_name"],
            "sequence_no": child["sequence_no"],
            "request_nonce": _clean(_as_dict(payload.get("provider_options")).get("v3_request_nonce")),
            "pricing_suppressed": True,
            "pricing": _as_dict(preview.get("pricing")),
            "pricing_summary": _as_dict(preview.get("pricing_summary")),
            "retry_scope": child.get("retry_scope"),
        }

    async def _create_internal_child(
        self,
        *,
        headers: dict[str, str],
        child: dict[str, Any],
        stage_run_id: UUID,
    ) -> str:
        turn_id = child["dialogue_turn_id"]
        payload = _stamp_internal_child(
            child["payload"],
            stage_run_id=stage_run_id,
            dialogue_turn_id=turn_id,
        )
        created = await self._fusion_post("/jobs", headers=headers, payload=payload)
        if not _pricing_is_suppressed(created):
            raise SceneFusionBridgeError(
                f"fusion_child_generation_pricing_not_suppressed:{turn_id}"
            )
        job_id = _clean(created.get("job_id") or created.get("id"))
        if not job_id:
            raise SceneFusionBridgeError(
                f"fusion_internal_child_missing_job_id:{turn_id}"
            )
        return job_id

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
        request_nonce_by_turn = {
            turn_id: uuid4().hex for turn_id in required_turn_ids
        }
        children = await _compile_children(
            context=context,
            face_client=self.face_client,
            audio_client=self.audio_client,
            headers=headers,
            external_provider_ok=external_provider_ok,
            request_nonce_by_turn=request_nonce_by_turn,
        )
        required_children = [
            {
                **child,
                "retry_scope": "failed_child_only" if preserved else "initial_scene",
            }
            for child in children
            if child["dialogue_turn_id"] in required_turn_ids
        ]

        suppressed_children: list[dict[str, Any]] = []
        if required_children:
            semaphore = asyncio.Semaphore(
                max(1, min(self.child_pricing_concurrency, len(required_children)))
            )

            async def verify(child: dict[str, Any]) -> dict[str, Any]:
                async with semaphore:
                    return await self._verify_child_pricing_suppressed(
                        headers=headers,
                        child=child,
                        stage_run_id=stage_run_id,
                    )

            suppressed_children = await asyncio.gather(
                *(verify(child) for child in required_children)
            )
            suppressed_children.sort(key=lambda item: int(item.get("sequence_no") or 0))

        parent = await self.parent_pricing.preview(
            headers=headers,
            context=context,
        )
        parent_pricing = _as_dict(parent.get("pricing"))
        if _clean(parent_pricing.get("unit_type")).lower() != "minute":
            raise SceneFusionBridgeError("fusion_parent_pricing_unit_must_be_minute")
        if not _clean(parent_pricing.get("quote_id")):
            raise SceneFusionBridgeError("fusion_parent_pricing_quote_required")
        if not _clean(parent_pricing.get("preview_fingerprint")):
            raise SceneFusionBridgeError("fusion_parent_pricing_fingerprint_required")

        return context, {
            "parent": parent,
            "children": suppressed_children,
            "preserved_child_count": len(preserved),
            "required_child_count": len(required_turn_ids),
            "billable_parent_quote_count": 1,
            "billable_child_quote_count": 0,
            "child_pricing_suppressed": len(suppressed_children),
        }

    async def dispatch(
        self,
        pool,
        *,
        account_id: UUID,
        workflow_id: UUID,
        stage_run_id: UUID,
        headers: dict[str, str],
        parent_confirmation: dict[str, Any],
        child_confirmations: list[dict[str, Any]],
        external_provider_ok: bool,
    ):
        if not external_provider_ok:
            raise SceneFusionBridgeError("fusion_external_provider_consent_required")

        child_confirmation_by_turn = {
            _clean(item.get("dialogue_turn_id")): item
            for item in child_confirmations
            if _clean(item.get("dialogue_turn_id"))
        }

        async with pool.acquire() as conn:
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
                        _as_dict(_as_dict(latest["metadata_json"]).get("parent_pricing")),
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
            if set(child_confirmation_by_turn) != expected_turns:
                raise SceneFusionBridgeError("fusion_child_confirmation_bundle_mismatch")
            for turn_id, item in child_confirmation_by_turn.items():
                if not _clean(item.get("request_nonce")):
                    raise SceneFusionBridgeError(f"fusion_request_nonce_required:{turn_id}")

        quote_id = _clean(parent_confirmation.get("quote_id"))
        preview_fingerprint = _clean(parent_confirmation.get("preview_fingerprint"))
        if not quote_id or not preview_fingerprint:
            raise SceneFusionBridgeError("fusion_parent_pricing_confirmation_required")

        parent_reserved = await self.parent_pricing.reserve(
            headers=headers,
            context=context,
            quote_id=quote_id,
            preview_fingerprint=preview_fingerprint,
        )
        reserved_pricing = _as_dict(parent_reserved.get("pricing"))
        if _clean(reserved_pricing.get("state")).lower() != "reserved":
            raise SceneFusionBridgeError("fusion_parent_pricing_not_reserved")
        if not _clean(reserved_pricing.get("reservation_id")):
            raise SceneFusionBridgeError("fusion_parent_pricing_reservation_id_missing")

        preserved_children = [
            preserved[turn_id]
            for turn_id in sorted(
                preserved,
                key=lambda value: int(preserved[value].get("sequence_no") or 0),
            )
        ]

        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    locked_state = _clean(
                        await conn.fetchval(
                            "select state from public.v3_studio_stage_runs where stage_run_id=$1 for update",
                            stage_run_id,
                        )
                    )
                    if locked_state == "generating":
                        latest = await _latest_attempt(conn, stage_run_id=stage_run_id)
                        if latest:
                            return (
                                context,
                                UUID(str(latest["attempt_id"])),
                                int(latest["attempt_no"]),
                                str(latest["attempt_kind"]),
                                list(_as_dict(latest["metadata_json"]).get("children") or []),
                                reserved_pricing,
                            )
                    if locked_state not in {"pending", "ready", "failed", "rejected"}:
                        raise SceneFusionBridgeError(
                            f"fusion_stage_not_dispatchable:{locked_state}"
                        )

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
                          stage_run_id,attempt_no,attempt_kind,state,provider_service,provider_job_ref,
                          pricing_quote_id,preview_fingerprint,metadata_json
                        ) values($1,$2,$3,'dispatching','svc-director',$4,$5,$6,$7::jsonb)
                        returning attempt_id
                        """,
                        stage_run_id,
                        attempt_no,
                        attempt_kind,
                        f"scene-fusion:{stage_run_id}:{attempt_no}",
                        quote_id,
                        preview_fingerprint,
                        json.dumps(
                            {
                                "children": preserved_children,
                                "parent_pricing": reserved_pricing,
                                "parent_pricing_confirmation": parent_confirmation,
                                "child_confirmations": child_confirmations,
                                "preserved_child_count": len(preserved_children),
                                "retry_scope": "failed_child_only" if preserved_children else attempt_kind,
                            }
                        ),
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
                                "pricing_owner": "svc-fusion-extension",
                                "child_pricing_strategy": "internal_bill_to_parent",
                            }
                        ),
                    )
        except Exception:
            try:
                await self.parent_pricing.release(
                    headers=headers,
                    context=context,
                    reason="director_attempt_creation_failed",
                )
            except Exception:
                pass
            raise

        if not expected_turns:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    update public.v3_studio_stage_attempts
                    set state='running',metadata_json=coalesce(metadata_json,'{}'::jsonb) || $2::jsonb,updated_at=now()
                    where attempt_id=$1
                    """,
                    attempt_id,
                    json.dumps(
                        {
                            "children": preserved_children,
                            "dispatch_outcome": "stitch_only_retry",
                            "parent_pricing": reserved_pricing,
                        }
                    ),
                )
            return context, attempt_id, attempt_no, attempt_kind, preserved_children, reserved_pricing

        children = await _compile_children(
            context=context,
            face_client=self.face_client,
            audio_client=self.audio_client,
            headers=headers,
            external_provider_ok=external_provider_ok,
            request_nonce_by_turn={
                turn_id: _clean(item.get("request_nonce"))
                for turn_id, item in child_confirmation_by_turn.items()
            },
        )

        dispatched = list(preserved_children)
        try:
            for child in children:
                turn_id = child["dialogue_turn_id"]
                if turn_id not in expected_turns:
                    continue
                job_id = await self._create_internal_child(
                    headers=headers,
                    child=child,
                    stage_run_id=stage_run_id,
                )
                child_record = {k: v for k, v in child.items() if k != "payload"}
                child_record.update(
                    {
                        "fusion_job_id": job_id,
                        "status": "queued",
                        "pricing_suppressed": True,
                        "quote_id": None,
                        "preview_fingerprint": None,
                        "reused_from_prior_attempt": False,
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
                        json.dumps(
                            {
                                "children": dispatched,
                                "dispatch_outcome": "partial",
                                "parent_pricing": reserved_pricing,
                            }
                        ),
                    )
        except Exception as exc:
            release_error = None
            try:
                await self.parent_pricing.release(
                    headers=headers,
                    context=context,
                    reason="fusion_child_dispatch_failed",
                )
            except Exception as release_exc:
                release_error = str(release_exc)[:1200]
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
                    json.dumps(
                        {
                            "children": dispatched,
                            "dispatch_outcome": "failed",
                            "parent_pricing_release_error": release_error,
                        }
                    ),
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
                json.dumps(
                    {
                        "children": dispatched,
                        "dispatch_outcome": "accepted",
                        "parent_pricing": reserved_pricing,
                    }
                ),
            )
        return context, attempt_id, attempt_no, attempt_kind, dispatched, reserved_pricing

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

        try:
            result = await super().sync(
                pool,
                account_id=account_id,
                workflow_id=workflow_id,
                stage_run_id=stage_run_id,
                headers=headers,
            )
        except SceneFusionBridgeError as exc:
            try:
                released = await self.parent_pricing.release(
                    headers=headers,
                    context=context,
                    reason=str(exc)[:300] or "fusion_scene_failed",
                )
                released_pricing = _as_dict(released.get("pricing"))
                async with pool.acquire() as conn:
                    latest = await _latest_attempt(conn, stage_run_id=stage_run_id)
                    if latest:
                        await conn.execute(
                            """
                            update public.v3_studio_stage_attempts
                            set metadata_json=coalesce(metadata_json,'{}'::jsonb) || $2::jsonb,updated_at=now()
                            where attempt_id=$1
                            """,
                            latest["attempt_id"],
                            json.dumps({"parent_pricing": released_pricing}),
                        )
            except Exception as release_exc:
                async with pool.acquire() as conn:
                    latest = await _latest_attempt(conn, stage_run_id=stage_run_id)
                    if latest:
                        await conn.execute(
                            """
                            update public.v3_studio_stage_attempts
                            set metadata_json=coalesce(metadata_json,'{}'::jsonb) || $2::jsonb,updated_at=now()
                            where attempt_id=$1
                            """,
                            latest["attempt_id"],
                            json.dumps({"parent_pricing_release_error": str(release_exc)[:1200]}),
                        )
            raise

        if _clean(result.get("provider_state")).lower() != "succeeded":
            return result

        try:
            committed = await self.parent_pricing.commit(
                headers=headers,
                context=context,
            )
        except SceneFusionBridgeError as exc:
            async with pool.acquire() as conn:
                latest = await _latest_attempt(conn, stage_run_id=stage_run_id)
                if latest:
                    await conn.execute(
                        """
                        update public.v3_studio_stage_attempts
                        set metadata_json=coalesce(metadata_json,'{}'::jsonb) || $2::jsonb,updated_at=now()
                        where attempt_id=$1
                        """,
                        latest["attempt_id"],
                        json.dumps(
                            {
                                "parent_pricing_commit_pending": True,
                                "parent_pricing_commit_error": str(exc)[:1200],
                            }
                        ),
                    )
            raise SceneFusionBridgeError("fusion_parent_pricing_commit_pending") from exc

        committed_pricing = _as_dict(committed.get("pricing"))
        if _clean(committed_pricing.get("state")).lower() != "committed":
            raise SceneFusionBridgeError("fusion_parent_pricing_commit_not_canonical")

        async with pool.acquire() as conn:
            latest = await _latest_attempt(conn, stage_run_id=stage_run_id)
            if latest:
                await conn.execute(
                    """
                    update public.v3_studio_stage_attempts
                    set metadata_json=coalesce(metadata_json,'{}'::jsonb) || $2::jsonb,updated_at=now()
                    where attempt_id=$1
                    """,
                    latest["attempt_id"],
                    json.dumps(
                        {
                            "parent_pricing": committed_pricing,
                            "parent_pricing_commit_pending": False,
                            "parent_pricing_commit_error": None,
                        }
                    ),
                )
        return {**result, "parent_pricing": committed_pricing}


__all__ = [
    "ParentPricedSceneFusionExecutionService",
    "ParentScenePricingClient",
    "_pricing_is_suppressed",
    "_stamp_internal_child",
]
