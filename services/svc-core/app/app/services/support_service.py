from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status

from app.repos.support_repo import SupportRepo
from app.schemas.notifications import InternalNotificationEventCreate, InternalRecipient
from app.schemas.support import (
    SupportContactRequest,
    SupportContactResponse,
    SupportMessageResponse,
    SupportReplyRequest,
    SupportRequestResponse,
)
from app.services.notification_service import NotificationService

logger = logging.getLogger("svc_core.support_service")

SUPPORT_EMAIL_TO = os.getenv("DF_SUPPORT_EMAIL_TO", "support@desifaces.ai")


def _import_get_pool():
    try:
        from app.db import get_pool  # type: ignore
        return get_pool
    except Exception:
        from app.db.postgres import get_pool  # type: ignore
        return get_pool


class SupportService:
    def __init__(self, pool: Any):
        self.pool = pool
        self.repo = SupportRepo(pool)
        self.notification_service = NotificationService(pool)

    async def create_contact_request(
        self,
        *,
        user_id: str,
        req: SupportContactRequest,
    ) -> SupportContactResponse:
        user_ctx = await self.repo.get_user_context(user_id)
        effective_name = req.name.strip() or (user_ctx or {}).get("first_name") or "desifaces.ai user"
        effective_email = req.email.strip() or (user_ctx or {}).get("email")
        if not effective_email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "bad_request", "message": "email is required"},
            )

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                ticket = await self.repo.create_request(
                    user_id=user_id,
                    name=effective_name,
                    email=effective_email,
                    topic=req.topic,
                    product_area=req.product_area,
                    priority=req.priority,
                    subject=req.subject.strip(),
                    tier_code=(user_ctx or {}).get("tier_code"),
                    metadata_json={
                        "context": req.context or {},
                        "attachment_urls": req.attachment_urls or [],
                    },
                    conn=conn,
                )

                await self.repo.add_message(
                    request_id=str(ticket["id"]),
                    sender_role="user",
                    sender_user_id=user_id,
                    sender_email=effective_email,
                    body=req.message.strip(),
                    attachments_json=[{"url": x} for x in (req.attachment_urls or [])],
                    is_internal=False,
                    conn=conn,
                )

                await self.repo.update_request_state(
                    request_id=str(ticket["id"]),
                    status="waiting_on_support",
                    latest_message_at_now=True,
                    conn=conn,
                )

        # Side effects must never break primary support request creation.
        try:
            await self.notification_service.emit_internal_event(
                req=InternalNotificationEventCreate(
                    event_type="SUPPORT_REQUEST_RECEIVED",
                    category="support",
                    priority="important",
                    source_service="svc-core",
                    source_ref_type="support_request",
                    source_ref_id=str(ticket["id"]),
                    actor_user_id=user_id,
                    title="Support request received",
                    body="We received your message and routed it to the desifaces.ai support team.",
                    action_route=f"/help/contact?request_id={ticket['id']}",
                    action_label="View request",
                    image_url=None,
                    payload_json={
                        "request_id": str(ticket["id"]),
                        "topic": req.topic,
                        "product_area": req.product_area,
                        "priority": req.priority,
                        "subject": req.subject,
                    },
                    metadata_json={
                        "request_id": str(ticket["id"]),
                    },
                    dedupe_key=f"support-request-received:{ticket['id']}",
                    recipients=[
                        InternalRecipient(user_id=user_id),
                    ],
                )
            )
        except Exception:
            logger.exception(
                "support notification emit failed request_id=%s user_id=%s",
                str(ticket["id"]),
                user_id,
            )

        support_mailbox_sent = False
        requester_ack_sent = False

        try:
            support_mailbox_sent = await self._maybe_send_support_mailbox_email(
                request_id=str(ticket["id"]),
                name=effective_name,
                email=effective_email,
                topic=req.topic,
                product_area=req.product_area,
                priority=req.priority,
                subject=req.subject,
                message=req.message,
                context=req.context or {},
                attachment_urls=req.attachment_urls or [],
                tier_code=(user_ctx or {}).get("tier_code"),
            )
        except Exception:
            logger.exception(
                "support mailbox email send failed request_id=%s email=%s",
                str(ticket["id"]),
                effective_email,
            )

        try:
            requester_ack_sent = await self._maybe_send_requester_ack_email(
                request_id=str(ticket["id"]),
                requester_name=effective_name,
                requester_email=effective_email,
                subject=req.subject,
                topic=req.topic,
                product_area=req.product_area,
                priority=req.priority,
                support_mailbox_sent=support_mailbox_sent,
            )
        except Exception:
            logger.exception(
                "requester acknowledgement email send failed request_id=%s email=%s",
                str(ticket["id"]),
                effective_email,
            )

        return SupportContactResponse(
            request_id=str(ticket["id"]),
            ack_sent=bool(requester_ack_sent),
        )

    async def list_requests(
        self,
        *,
        user_id: str,
        limit: int,
        offset: int,
    ) -> List[SupportRequestResponse]:
        rows = await self.repo.list_user_requests(
            user_id=user_id,
            limit=max(1, min(limit, 100)),
            offset=max(0, offset),
        )
        return [
            SupportRequestResponse(
                id=str(row["id"]),
                topic=str(row["topic"]),
                product_area=str(row["product_area"]),
                priority=str(row["priority"]),
                subject=str(row["subject"]),
                status=str(row["status"]),
                latest_message_at=row["latest_message_at"],
                created_at=row["created_at"],
                messages=[],
            )
            for row in rows
        ]

    async def get_request(
        self,
        *,
        user_id: str,
        request_id: str,
    ) -> SupportRequestResponse:
        ticket = await self.repo.get_request_for_user(
            request_id=request_id,
            user_id=user_id,
        )
        if not ticket:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Support request not found",
            )

        messages = await self.repo.list_messages(
            request_id=request_id,
            include_internal=False,
        )

        return SupportRequestResponse(
            id=str(ticket["id"]),
            topic=str(ticket["topic"]),
            product_area=str(ticket["product_area"]),
            priority=str(ticket["priority"]),
            subject=str(ticket["subject"]),
            status=str(ticket["status"]),
            latest_message_at=ticket["latest_message_at"],
            created_at=ticket["created_at"],
            messages=[
                SupportMessageResponse(
                    id=str(msg["id"]),
                    sender_role=str(msg["sender_role"]),
                    body=str(msg["body"]),
                    attachments_json=msg.get("attachments_json") or [],
                    created_at=msg["created_at"],
                )
                for msg in messages
            ],
        )

    async def reply_to_request(
        self,
        *,
        user_id: str,
        request_id: str,
        req: SupportReplyRequest,
        sender_role: str = "user",
        sender_email: Optional[str] = None,
    ) -> None:
        ticket = await self.repo.get_request_for_user(
            request_id=request_id,
            user_id=user_id,
        )
        if not ticket:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Support request not found",
            )

        if sender_role not in {"user", "support", "system"}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid_sender_role",
            )

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await self.repo.add_message(
                    request_id=request_id,
                    sender_role=sender_role,
                    sender_user_id=user_id if sender_role == "user" else None,
                    sender_email=sender_email or ticket.get("email"),
                    body=req.body.strip(),
                    attachments_json=[{"url": x} for x in (req.attachment_urls or [])],
                    is_internal=False,
                    conn=conn,
                )

                next_status = "waiting_on_support" if sender_role == "user" else "waiting_on_customer"
                await self.repo.update_request_state(
                    request_id=request_id,
                    status=next_status,
                    latest_message_at_now=True,
                    conn=conn,
                )

        if sender_role == "support":
            owner_user_id = ticket.get("user_id")
            if owner_user_id:
                try:
                    await self.notification_service.emit_internal_event(
                        req=InternalNotificationEventCreate(
                            event_type="SUPPORT_REPLY_RECEIVED",
                            category="support",
                            priority="important",
                            source_service="svc-core",
                            source_ref_type="support_request",
                            source_ref_id=request_id,
                            actor_user_id=None,
                            title="Support replied to your request",
                            body=ticket["subject"],
                            action_route=f"/help/contact?request_id={request_id}",
                            action_label="View reply",
                            image_url=None,
                            payload_json={"request_id": request_id},
                            metadata_json={"request_id": request_id},
                            dedupe_key=None,
                            recipients=[InternalRecipient(user_id=str(owner_user_id))],
                        )
                    )
                except Exception:
                    logger.exception(
                        "support reply notification emit failed request_id=%s owner_user_id=%s",
                        request_id,
                        str(owner_user_id),
                    )

            try:
                await self._maybe_send_requester_reply_email(
                    request_id=request_id,
                    requester_email=ticket.get("email"),
                    subject=ticket.get("subject") or "Support reply",
                    reply_body=req.body.strip(),
                )
            except Exception:
                logger.exception(
                    "requester reply email send failed request_id=%s email=%s",
                    request_id,
                    ticket.get("email"),
                )

        elif sender_role == "user":
            try:
                await self._maybe_send_support_mailbox_reply_email(
                    request_id=request_id,
                    name=ticket.get("name") or "desifaces.ai user",
                    email=ticket.get("email") or "",
                    subject=ticket.get("subject") or "Support reply",
                    body=req.body.strip(),
                    attachment_urls=req.attachment_urls or [],
                )
            except Exception:
                logger.exception(
                    "support mailbox reply email send failed request_id=%s email=%s",
                    request_id,
                    ticket.get("email"),
                )

    async def _maybe_send_support_mailbox_email(
        self,
        *,
        request_id: str,
        name: str,
        email: str,
        topic: str,
        product_area: str,
        priority: str,
        subject: str,
        message: str,
        context: Dict[str, Any],
        attachment_urls: List[str],
        tier_code: Optional[str],
    ) -> bool:
        sender = await self._load_mail_sender()
        if not sender:
            return False

        rendered_subject = f"[desifaces.ai Support][{priority.upper()}][{product_area}] {subject}"
        body_lines = [
            f"Request ID: {request_id}",
            f"From: {name} <{email}>",
            f"Tier: {tier_code or 'unknown'}",
            f"Topic: {topic}",
            f"Product Area: {product_area}",
            "",
            "Message:",
            message,
            "",
            "Context:",
            str(context or {}),
            "",
            "Attachments:",
            ", ".join(attachment_urls or []),
        ]
        body_text = "\n".join(body_lines)

        await sender(
            to_address=SUPPORT_EMAIL_TO,
            subject=rendered_subject,
            text_body=body_text,
            html_body=None,
        )
        return True

    async def _maybe_send_requester_ack_email(
        self,
        *,
        request_id: str,
        requester_name: str,
        requester_email: str,
        subject: str,
        topic: str,
        product_area: str,
        priority: str,
        support_mailbox_sent: bool,
    ) -> bool:
        sender = await self._load_mail_sender()
        if not sender or not requester_email:
            return False

        mail_subject = f"We received your support request • desifaces.ai"
        body_text = "\n".join(
            [
                f"Hello {requester_name},",
                "",
                "We received your message and created a desifaces.ai support request.",
                f"Request ID: {request_id}",
                f"Topic: {topic}",
                f"Product Area: {product_area}",
                f"Priority: {priority}",
                f"Subject: {subject}",
                "",
                "Our team will review it and get back to you.",
                "",
                f"Support mailbox routing status: {'sent' if support_mailbox_sent else 'queued_or_unavailable'}",
                "",
                "Sent by desifaces.ai",
            ]
        )

        await sender(
            to_address=requester_email,
            subject=mail_subject,
            text_body=body_text,
            html_body=None,
        )
        return True

    async def _maybe_send_requester_reply_email(
        self,
        *,
        request_id: str,
        requester_email: Optional[str],
        subject: str,
        reply_body: str,
    ) -> bool:
        sender = await self._load_mail_sender()
        if not sender or not requester_email:
            return False

        mail_subject = f"Support replied • desifaces.ai"
        body_text = "\n".join(
            [
                "Hello,",
                "",
                "The desifaces.ai support team replied to your request.",
                f"Request ID: {request_id}",
                f"Subject: {subject}",
                "",
                "Reply:",
                reply_body,
                "",
                "Sent by desifaces.ai",
            ]
        )

        await sender(
            to_address=requester_email,
            subject=mail_subject,
            text_body=body_text,
            html_body=None,
        )
        return True

    async def _maybe_send_support_mailbox_reply_email(
        self,
        *,
        request_id: str,
        name: str,
        email: str,
        subject: str,
        body: str,
        attachment_urls: List[str],
    ) -> bool:
        sender = await self._load_mail_sender()
        if not sender:
            return False

        mail_subject = f"[desifaces.ai Support][REPLY] {subject}"
        body_text = "\n".join(
            [
                f"Request ID: {request_id}",
                f"From: {name} <{email}>",
                "",
                "Reply:",
                body,
                "",
                "Attachments:",
                ", ".join(attachment_urls or []),
            ]
        )

        await sender(
            to_address=SUPPORT_EMAIL_TO,
            subject=mail_subject,
            text_body=body_text,
            html_body=None,
        )
        return True

    async def _load_mail_sender(self):
        try:
            from app.services.notification_dispatcher import send_transactional_email  # type: ignore
            return send_transactional_email
        except Exception:
            logger.exception("notification mail sender unavailable")
            return None


async def get_support_service() -> SupportService:
    get_pool = _import_get_pool()
    pool = await get_pool()
    return SupportService(pool)