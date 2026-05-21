from __future__ import annotations

import asyncio
import json
import logging
import os
import smtplib
import socket
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any, Dict, List, Optional

from app.repos.notifications_repo import NotificationsRepo
from app.services.email_templates import render_notification_email

logger = logging.getLogger("svc_core.notification_dispatcher")

DF_NOTIFICATION_EMAIL_PROVIDER = os.getenv("DF_NOTIFICATION_EMAIL_PROVIDER", "noop").strip().lower()
DF_NOTIFICATION_PUSH_PROVIDER = os.getenv("DF_NOTIFICATION_PUSH_PROVIDER", "expo").strip().lower()

DF_EXPO_PUSH_URL = os.getenv("DF_EXPO_PUSH_URL", "https://exp.host/--/api/v2/push/send")
DF_EMAIL_FROM = os.getenv("DF_EMAIL_FROM", "noreply@desifaces.ai")

DF_RESEND_API_KEY = os.getenv("DF_RESEND_API_KEY", "")
DF_RESEND_API_URL = os.getenv("DF_RESEND_API_URL", "https://api.resend.com/emails")

DF_SMTP_HOST = os.getenv("DF_SMTP_HOST", "")
DF_SMTP_PORT = int(os.getenv("DF_SMTP_PORT", "587"))
DF_SMTP_USERNAME = os.getenv("DF_SMTP_USERNAME", "")
DF_SMTP_PASSWORD = os.getenv("DF_SMTP_PASSWORD", "")
DF_SMTP_USE_TLS = os.getenv("DF_SMTP_USE_TLS", "1").strip() not in {"0", "false", "False"}
DF_SMTP_EHLO_DOMAIN = (
    os.getenv("DF_SMTP_EHLO_DOMAIN", "").strip()
    or os.getenv("SMTP_EHLO_DOMAIN", "").strip()
    or "mail.desifaces.ai"
)
DF_SMTP_TIMEOUT_SEC = float(os.getenv("DF_SMTP_TIMEOUT_SEC", "20"))

DF_NOTIFICATION_BATCH_LIMIT = int(os.getenv("DF_NOTIFICATION_BATCH_LIMIT", "100"))
DF_NOTIFICATION_HTTP_TIMEOUT_SEC = float(os.getenv("DF_NOTIFICATION_HTTP_TIMEOUT_SEC", "20"))
DF_NOTIFICATION_STALE_PROCESSING_AFTER_MINUTES = int(
    os.getenv("DF_NOTIFICATION_STALE_PROCESSING_AFTER_MINUTES", "20")
)

# Bounded retry schedules. attempt_count is incremented when the repo claims a due row.
EMAIL_RETRY_MINUTES = [30, 60, 180]
PUSH_RETRY_MINUTES = [15, 60]


class DispatchTransientError(RuntimeError):
    pass


class DispatchPermanentError(RuntimeError):
    pass


class DispatchSkippedError(RuntimeError):
    pass


def _import_get_pool():
    try:
        from app.db import get_pool  # type: ignore
        return get_pool
    except Exception:
        from app.db.postgres import get_pool  # type: ignore
        return get_pool


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _next_retry_at(channel: str, attempt_count: int) -> Optional[datetime]:
    c = (channel or "").strip().lower()
    if c == "email":
        schedule = EMAIL_RETRY_MINUTES
    elif c == "push":
        schedule = PUSH_RETRY_MINUTES
    else:
        return None

    idx = max(0, attempt_count - 1)
    if idx >= len(schedule):
        return None

    return _utcnow() + timedelta(minutes=int(schedule[idx]))


def _truncate_error(value: str, max_len: int = 1000) -> str:
    return str(value or "")[:max_len]


def _http_post_json_sync(
    *,
    url: str,
    payload: Dict[str, Any] | List[Dict[str, Any]],
    headers: Optional[Dict[str, str]] = None,
    timeout_sec: float = DF_NOTIFICATION_HTTP_TIMEOUT_SEC,
) -> tuple[int, str]:
    raw = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=raw,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return int(resp.status), body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return int(exc.code), body
    except (urllib.error.URLError, TimeoutError, OSError, socket.error) as exc:
        raise DispatchTransientError(str(exc)) from exc


async def _http_post_json(
    *,
    url: str,
    payload: Dict[str, Any] | List[Dict[str, Any]],
    headers: Optional[Dict[str, str]] = None,
    timeout_sec: float = DF_NOTIFICATION_HTTP_TIMEOUT_SEC,
) -> tuple[int, str]:
    return await asyncio.to_thread(
        _http_post_json_sync,
        url=url,
        payload=payload,
        headers=headers,
        timeout_sec=timeout_sec,
    )


class NotificationDispatcher:
    def __init__(self, pool: Any):
        self.pool = pool
        self.repo = NotificationsRepo(pool)

    async def dispatch_once(
        self,
        *,
        channel: Optional[str] = None,
        limit: int = DF_NOTIFICATION_BATCH_LIMIT,
    ) -> Dict[str, int]:
        processed = 0
        delivered = 0
        failed = 0
        skipped = 0
        retried = 0

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                claimed = await self.repo.claim_due_deliveries(
                    channel=channel,
                    limit=max(1, min(int(limit), 1000)),
                    stale_processing_after_minutes=DF_NOTIFICATION_STALE_PROCESSING_AFTER_MINUTES,
                    conn=conn,
                )

        for delivery in claimed:
            processed += 1
            delivery_id = str(delivery["id"])
            channel_name = str(delivery.get("channel") or "")

            try:
                if channel_name == "push":
                    provider_message_id = await self._dispatch_push(delivery)
                    await self.repo.mark_delivery_delivered(
                        delivery_id=delivery_id,
                        provider_message_id=provider_message_id,
                    )
                    delivered += 1
                    continue

                if channel_name == "email":
                    provider_message_id = await self._dispatch_email(delivery)
                    await self.repo.mark_delivery_delivered(
                        delivery_id=delivery_id,
                        provider_message_id=provider_message_id,
                    )
                    delivered += 1
                    continue

                raise DispatchSkippedError(f"Unsupported channel: {channel_name}")

            except DispatchSkippedError as exc:
                logger.info(
                    "notification skipped delivery_id=%s channel=%s reason=%s",
                    delivery_id,
                    channel_name,
                    str(exc),
                )
                await self.repo.mark_delivery_skipped(
                    delivery_id=delivery_id,
                    error_code="skipped",
                    error_message=_truncate_error(str(exc)),
                    terminal_reason="skipped",
                )
                skipped += 1

            except DispatchPermanentError as exc:
                logger.warning(
                    "notification permanent failure delivery_id=%s channel=%s error=%s",
                    delivery_id,
                    channel_name,
                    str(exc),
                )
                await self.repo.mark_delivery_failed(
                    delivery_id=delivery_id,
                    error_code="permanent_failure",
                    error_message=_truncate_error(str(exc)),
                    terminal_reason="permanent_failure",
                )
                failed += 1

            except DispatchTransientError as exc:
                logger.warning(
                    "notification transient failure delivery_id=%s channel=%s error=%s",
                    delivery_id,
                    channel_name,
                    str(exc),
                )
                if await self._schedule_retry_or_fail(
                    delivery,
                    error_code="transient_failure",
                    error_message=str(exc),
                ):
                    retried += 1
                else:
                    failed += 1

            except Exception as exc:
                logger.exception(
                    "notification dispatch crashed delivery_id=%s channel=%s",
                    delivery_id,
                    channel_name,
                )
                if await self._schedule_retry_or_fail(
                    delivery,
                    error_code="dispatch_exception",
                    error_message=str(exc),
                ):
                    retried += 1
                else:
                    failed += 1

        return {
            "processed": processed,
            "delivered": delivered,
            "failed": failed,
            "skipped": skipped,
            "retried": retried,
        }

    async def _schedule_retry_or_fail(
        self,
        delivery: Dict[str, Any],
        *,
        error_code: str,
        error_message: str,
    ) -> bool:
        delivery_id = str(delivery["id"])
        channel_name = str(delivery.get("channel") or "")
        attempt_count = int(delivery.get("attempt_count") or 0)
        max_attempts = int(delivery.get("max_attempts") or 0)

        next_retry_at = _next_retry_at(channel_name, attempt_count)

        if next_retry_at is None or (max_attempts > 0 and attempt_count >= max_attempts):
            await self.repo.mark_delivery_failed(
                delivery_id=delivery_id,
                error_code=error_code,
                error_message=_truncate_error(error_message),
                terminal_reason="max_attempts_exhausted",
            )
            return False

        await self.repo.schedule_delivery_retry(
            delivery_id=delivery_id,
            next_attempt_at=next_retry_at,
            error_code=error_code,
            error_message=_truncate_error(error_message),
        )
        return True

    async def _dispatch_push(self, delivery: Dict[str, Any]) -> str:
        if DF_NOTIFICATION_PUSH_PROVIDER != "expo":
            raise DispatchPermanentError(
                f"Unsupported push provider: {DF_NOTIFICATION_PUSH_PROVIDER}"
            )

        devices = await self.repo.list_active_devices(user_id=str(delivery["user_id"]))
        if not devices:
            raise DispatchSkippedError("No active notification devices for user")

        payload_json = delivery.get("payload_json") or {}
        title = str(payload_json.get("title") or delivery.get("title") or "desifaces.ai")
        body = str(payload_json.get("body") or delivery.get("body") or "")
        action_route = payload_json.get("action_route")
        category = payload_json.get("category") or delivery.get("category") or "announcements"

        messages: List[Dict[str, Any]] = []
        for device in devices:
            messages.append(
                {
                    "to": device["expo_push_token"],
                    "title": title,
                    "body": body,
                    "sound": "default",
                    "channelId": self._expo_channel_for_category(str(category)),
                    "data": {
                        "action_route": action_route,
                        "category": category,
                        "event_type": payload_json.get("event_type") or delivery.get("event_type"),
                        "metadata": payload_json.get("metadata") or {},
                        "payload": payload_json.get("payload") or {},
                    },
                }
            )

        status_code, resp_text = await _http_post_json(
            url=DF_EXPO_PUSH_URL,
            payload=messages,
            headers=None,
            timeout_sec=DF_NOTIFICATION_HTTP_TIMEOUT_SEC,
        )

        if status_code in {408, 425, 429, 500, 502, 503, 504}:
            raise DispatchTransientError(f"push_http_{status_code}: {resp_text[:500]}")
        if status_code >= 400:
            raise DispatchPermanentError(f"push_http_{status_code}: {resp_text[:500]}")

        try:
            parsed = json.loads(resp_text or "{}")
        except Exception as exc:
            raise DispatchTransientError(f"push_response_parse_failed: {exc}") from exc

        data = parsed.get("data") or []
        invalid_tokens: List[str] = []
        provider_message_ids: List[str] = []
        ok_count = 0
        transient_errors: List[str] = []
        permanent_errors: List[str] = []

        for idx, item in enumerate(data):
            status_value = str(item.get("status") or "").lower()
            if status_value == "ok":
                ok_count += 1
                ticket = item.get("id")
                if ticket:
                    provider_message_ids.append(str(ticket))
                continue

            details = item.get("details") or {}
            item_error = str(details.get("error") or item.get("message") or "").strip()

            if item_error == "DeviceNotRegistered":
                if idx < len(messages):
                    invalid_tokens.append(str(messages[idx]["to"]))
                continue

            if item_error in {"MessageRateExceeded", "ExpoServiceUnavailable", "TooManyRequests"}:
                transient_errors.append(item_error or "transient_push_error")
            else:
                permanent_errors.append(item_error or "unknown_push_error")

        if invalid_tokens:
            await self.repo.deactivate_device_tokens(
                user_id=str(delivery["user_id"]),
                tokens=invalid_tokens,
            )

        if ok_count > 0:
            return ",".join(provider_message_ids) if provider_message_ids else "expo"

        if transient_errors:
            raise DispatchTransientError("; ".join(transient_errors)[:500])

        if permanent_errors:
            raise DispatchPermanentError("; ".join(permanent_errors)[:500])

        raise DispatchSkippedError("All push device tokens are invalid or unavailable")

    async def _dispatch_email(self, delivery: Dict[str, Any]) -> str:
        payload_json = delivery.get("payload_json") or {}
        destination = delivery.get("destination")

        if not destination:
            raise DispatchSkippedError("Email delivery missing destination address")

        rendered = render_notification_email(
            template_key=str(payload_json.get("template_key") or "system/generic_notification"),
            user_context=payload_json.get("user_context") or {},
            event=payload_json.get("event") or {},
            metadata=payload_json.get("metadata") or {},
            payload=payload_json.get("payload") or {},
        )

        return await send_transactional_email(
            to_address=str(destination),
            subject=str(rendered["subject"]),
            text_body=str(rendered["text_body"]),
            html_body=str(rendered["html_body"]),
        )

    def _expo_channel_for_category(self, category: str) -> str:
        c = (category or "").strip().lower()
        if c == "jobs":
            return "jobs"
        if c == "billing":
            return "billing"
        if c == "support":
            return "support"
        return "desifaces-default"


async def send_transactional_email(
    *,
    to_address: str,
    subject: str,
    text_body: str,
    html_body: Optional[str] = None,
) -> str:
    provider = DF_NOTIFICATION_EMAIL_PROVIDER

    if provider == "noop":
        return "noop"

    if provider == "resend":
        return await _send_email_resend(
            to_address=to_address,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
        )

    if provider == "smtp":
        return await _send_email_smtp(
            to_address=to_address,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
        )

    raise DispatchPermanentError(f"Unsupported email provider: {provider}")


async def _send_email_resend(
    *,
    to_address: str,
    subject: str,
    text_body: str,
    html_body: Optional[str],
) -> str:
    if not DF_RESEND_API_KEY:
        raise DispatchPermanentError("DF_RESEND_API_KEY is not configured")

    payload: Dict[str, Any] = {
        "from": DF_EMAIL_FROM,
        "to": [to_address],
        "subject": subject,
        "text": text_body,
    }
    if html_body:
        payload["html"] = html_body

    status_code, resp_text = await _http_post_json(
        url=DF_RESEND_API_URL,
        payload=payload,
        headers={"Authorization": f"Bearer {DF_RESEND_API_KEY}"},
        timeout_sec=DF_NOTIFICATION_HTTP_TIMEOUT_SEC,
    )

    if status_code in {408, 425, 429, 500, 502, 503, 504}:
        raise DispatchTransientError(f"resend_http_{status_code}: {resp_text[:500]}")
    if status_code >= 400:
        raise DispatchPermanentError(f"resend_http_{status_code}: {resp_text[:500]}")

    try:
        parsed = json.loads(resp_text or "{}")
    except Exception as exc:
        raise DispatchTransientError(f"resend_response_parse_failed: {exc}") from exc

    return str(parsed.get("id") or "resend")


def _classify_smtp_response_exception(exc: smtplib.SMTPResponseException) -> Exception:
    code = int(getattr(exc, "smtp_code", 0) or 0)
    if 500 <= code <= 599:
        return DispatchPermanentError(str(exc))
    return DispatchTransientError(str(exc))


async def _send_email_smtp(
    *,
    to_address: str,
    subject: str,
    text_body: str,
    html_body: Optional[str],
) -> str:
    if not DF_SMTP_HOST:
        raise DispatchPermanentError("DF_SMTP_HOST is not configured")

    msg = EmailMessage()
    msg["From"] = DF_EMAIL_FROM
    msg["To"] = to_address
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{os.urandom(12).hex()}@{DF_SMTP_EHLO_DOMAIN}>"
    msg.set_content(text_body)

    if html_body:
        msg.add_alternative(html_body, subtype="html")

    def _send_sync() -> str:
        try:
            with smtplib.SMTP(
                DF_SMTP_HOST,
                DF_SMTP_PORT,
                local_hostname=DF_SMTP_EHLO_DOMAIN,
                timeout=DF_SMTP_TIMEOUT_SEC,
            ) as smtp:
                smtp.ehlo()
                if DF_SMTP_USE_TLS:
                    smtp.starttls()
                    smtp.ehlo()
                if DF_SMTP_USERNAME:
                    smtp.login(DF_SMTP_USERNAME, DF_SMTP_PASSWORD)
                smtp.send_message(msg)
            return "smtp"
        except smtplib.SMTPAuthenticationError as exc:
            raise DispatchPermanentError(str(exc)) from exc
        except smtplib.SMTPRecipientsRefused as exc:
            raise DispatchPermanentError(str(exc)) from exc
        except smtplib.SMTPResponseException as exc:
            raise _classify_smtp_response_exception(exc) from exc
        except (
            smtplib.SMTPServerDisconnected,
            smtplib.SMTPConnectError,
            smtplib.SMTPDataError,
            smtplib.SMTPException,
            OSError,
            socket.error,
        ) as exc:
            raise DispatchTransientError(str(exc)) from exc

    return await asyncio.to_thread(_send_sync)


async def get_notification_dispatcher() -> NotificationDispatcher:
    get_pool = _import_get_pool()
    pool = await get_pool()
    return NotificationDispatcher(pool)
