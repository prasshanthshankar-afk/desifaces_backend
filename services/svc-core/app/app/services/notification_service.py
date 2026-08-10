from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import HTTPException, status

from app.repos.notifications_repo import NotificationsRepo
from app.schemas.notifications import (
    InternalNotificationEventCreate,
    NotificationAction,
    NotificationItemResponse,
    NotificationListResponse,
    NotificationPreferenceItem,
    NotificationPreferencesResponse,
    NotificationPreferencesUpdateRequest,
    RegisterDeviceRequest,
)


def _import_get_pool():
    try:
        from app.db import get_pool  # type: ignore
        return get_pool
    except Exception:
        from app.db.postgres import get_pool  # type: ignore
        return get_pool


class NotificationService:
    VALID_CATEGORIES = {"jobs", "billing", "account", "support", "announcements"}

    def __init__(self, pool: Any):
        self.pool = pool
        self.repo = NotificationsRepo(pool)

    async def list_notifications(
        self,
        *,
        user_id: str,
        category: Optional[str],
        limit: int,
        offset: int,
    ) -> NotificationListResponse:
        if category and category not in self.VALID_CATEGORIES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unsupported_notification_category:{category}",
            )

        await self.repo.ensure_default_preferences(user_id=user_id)

        items = await self.repo.list_user_items(
            user_id=user_id,
            category=category,
            limit=max(1, min(limit, 100)),
            offset=max(0, offset),
        )
        unread_count = await self.repo.get_unread_count(user_id=user_id)

        return NotificationListResponse(
            items=[
                NotificationItemResponse(
                    id=str(item["id"]),
                    title=str(item["title"]),
                    body=str(item["body"]),
                    category=str(item["category"]),
                    priority=str(item["priority"]),
                    event_type=str(item["event_type"]),
                    created_at=item["created_at"],
                    is_read=bool(item["is_read"]),
                    image_url=item.get("image_url"),
                    action=NotificationAction(
                        label=item.get("action_label"),
                        route=item.get("action_route"),
                    )
                    if item.get("action_label") or item.get("action_route")
                    else None,
                    metadata=item.get("metadata_json") or {},
                )
                for item in items
            ],
            unread_count=int(unread_count),
        )

    async def get_unread_count(self, user_id: str) -> int:
        return await self.repo.get_unread_count(user_id=user_id)

    async def mark_read(self, *, user_id: str, item_id: str) -> None:
        await self.repo.mark_read(user_id=user_id, item_id=item_id)

    async def mark_all_read(self, *, user_id: str) -> None:
        await self.repo.mark_all_read(user_id=user_id)

    async def get_preferences(self, *, user_id: str) -> NotificationPreferencesResponse:
        await self.repo.ensure_default_preferences(user_id=user_id)
        rows = await self.repo.get_preferences(user_id=user_id)
        return NotificationPreferencesResponse(
            items=[
                NotificationPreferenceItem(
                    category=str(row["category"]),
                    in_app_enabled=bool(row["in_app_enabled"]),
                    push_enabled=bool(row["push_enabled"]),
                    email_enabled=bool(row["email_enabled"]),
                )
                for row in rows
            ]
        )

    async def update_preferences(
        self,
        *,
        user_id: str,
        req: NotificationPreferencesUpdateRequest,
    ) -> NotificationPreferencesResponse:
        bad = [x.category for x in req.items if x.category not in self.VALID_CATEGORIES]
        if bad:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "bad_request", "message": f"Invalid categories: {bad}"},
            )

        await self.repo.upsert_preferences(
            user_id=user_id,
            items=[
                {
                    "category": item.category,
                    "in_app_enabled": item.in_app_enabled,
                    "push_enabled": item.push_enabled,
                    "email_enabled": item.email_enabled,
                }
                for item in req.items
            ],
        )
        return await self.get_preferences(user_id=user_id)

    async def register_device(self, *, user_id: str, req: RegisterDeviceRequest) -> None:
        await self.repo.register_device(
            user_id=user_id,
            platform=req.platform,
            expo_push_token=req.expo_push_token,
            device_name=req.device_name,
            app_version=req.app_version,
        )


    async def emit_internal_event_best_effort(
        self,
        *,
        req: InternalNotificationEventCreate,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        try:
            return await self.emit_internal_event(req=req)
        except Exception:
            import logging
            logging.getLogger("svc_core.notifications").exception(
                "notification_emit_best_effort_failed", extra=(context or {})
            )
            return {"event_id": "", "deduped": False}

    async def emit_internal_event(
        self,
        *,
        req: InternalNotificationEventCreate,
    ) -> Dict[str, Any]:
        if req.category not in self.VALID_CATEGORIES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unsupported_notification_category:{req.category}",
            )

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                if req.dedupe_key:
                    existing = await self.repo.get_event_by_dedupe_key(
                        req.dedupe_key,
                        conn=conn,
                    )
                    if existing:
                        return {
                            "event_id": str(existing["id"]),
                            "deduped": True,
                        }

                event = await self.repo.create_event(
                    event_type=req.event_type,
                    category=req.category,
                    priority=req.priority,
                    source_service=req.source_service,
                    source_ref_type=req.source_ref_type,
                    source_ref_id=req.source_ref_id,
                    actor_user_id=req.actor_user_id,
                    title=req.title,
                    body=req.body,
                    action_route=req.action_route,
                    action_label=req.action_label,
                    image_url=req.image_url,
                    payload_json=req.payload_json,
                    metadata_json=req.metadata_json,
                    dedupe_key=req.dedupe_key,
                    conn=conn,
                )

                preference_cache: Dict[str, Dict[str, Dict[str, bool]]] = {}
                user_context_cache: Dict[str, Dict[str, Any]] = {}

                for recipient in req.recipients:
                    rid = str(recipient.user_id)

                    if rid not in preference_cache:
                        await self.repo.ensure_default_preferences(user_id=rid, conn=conn)
                        rows = await self.repo.get_preferences(user_id=rid, conn=conn)
                        preference_cache[rid] = {
                            str(row["category"]): {
                                "in_app_enabled": bool(row["in_app_enabled"]),
                                "push_enabled": bool(row["push_enabled"]),
                                "email_enabled": bool(row["email_enabled"]),
                            }
                            for row in rows
                        }

                    if rid not in user_context_cache:
                        user_context_cache[rid] = await self.repo.get_user_context(rid, conn=conn) or {}

                    pref = preference_cache[rid].get(req.category, {})
                    user_ctx = user_context_cache[rid]

                    allow_in_app = bool(recipient.channels.in_app) and bool(pref.get("in_app_enabled", True))
                    allow_push = bool(recipient.channels.push) and bool(pref.get("push_enabled", True))
                    allow_email = bool(recipient.channels.email) and bool(pref.get("email_enabled", True))

                    if allow_in_app:
                        await self.repo.create_user_item(
                            event_id=str(event["id"]),
                            user_id=rid,
                            category=req.category,
                            priority=req.priority,
                            event_type=req.event_type,
                            title=req.title,
                            body=req.body,
                            action_route=req.action_route,
                            action_label=req.action_label,
                            image_url=req.image_url,
                            metadata_json=req.metadata_json,
                            conn=conn,
                        )

                    if allow_push:
                        await self.repo.create_delivery(
                            event_id=str(event["id"]),
                            user_id=rid,
                            channel="push",
                            destination=None,
                            provider="expo",
                            payload_json={
                                "event_type": req.event_type,
                                "category": req.category,
                                "priority": req.priority,
                                "title": req.title,
                                "body": req.body,
                                "action_route": req.action_route,
                                "action_label": req.action_label,
                                "image_url": req.image_url,
                                "metadata": req.metadata_json,
                                "payload": req.payload_json,
                            },
                            conn=conn,
                        )

                    if allow_email and user_ctx.get("email"):
                        await self.repo.create_delivery(
                            event_id=str(event["id"]),
                            user_id=rid,
                            channel="email",
                            destination=str(user_ctx["email"]),
                            provider="transactional_email",
                            payload_json={
                                "template_key": self._email_template_key_for_event(req.event_type),
                                "user_context": {
                                    "user_id": rid,
                                    "email": user_ctx.get("email"),
                                    "first_name": user_ctx.get("first_name"),
                                    "tier_code": user_ctx.get("tier_code"),
                                    "plan_code": user_ctx.get("plan_code"),
                                },
                                "event": {
                                    "event_type": req.event_type,
                                    "category": req.category,
                                    "priority": req.priority,
                                    "title": req.title,
                                    "body": req.body,
                                    "action_route": req.action_route,
                                    "action_label": req.action_label,
                                    "image_url": req.image_url,
                                },
                                "metadata": req.metadata_json,
                                "payload": req.payload_json,
                            },
                            conn=conn,
                        )

        return {
            "event_id": str(event["id"]),
            "deduped": False,
        }

    def _email_template_key_for_event(self, event_type: str) -> str:
        mapping = {
            "PAYMENT_SUCCESS": "billing/payment_success",
            "PAYMENT_FAILED": "billing/payment_failed",
            "SUBSCRIPTION_UPGRADED": "billing/subscription_upgraded",
            "SUBSCRIPTION_DOWNGRADED": "billing/subscription_downgraded",
            "FACE_READY": "jobs/face_ready",
            "AUDIO_READY": "jobs/audio_ready",
            "FUSION_READY": "jobs/fusion_ready",
            "ARTIFACT_JOB_FAILED": "jobs/job_failed",
            "TIER_CHANGED": "account/tier_changed",
            "CREDITS_LOW": "account/credits_low",
            "SUPPORT_REQUEST_RECEIVED": "support/contact_ack",
            "SUPPORT_REPLY_RECEIVED": "support/reply_received",
        }
        return mapping.get(event_type, "system/generic_notification")


async def get_notification_service() -> NotificationService:
    get_pool = _import_get_pool()
    pool = await get_pool()
    return NotificationService(pool)