from __future__ import annotations

from html import escape
from typing import Any, Dict, Optional


def _tier_intro(first_name: str, tier_code: Optional[str]) -> str:
    tier = (tier_code or "").strip().lower()
    if tier == "enterprise":
        return f"Hello {first_name}, your desifaces.ai enterprise account has an update."
    if tier in {"business", "pro"}:
        return f"Hello {first_name}, your desifaces.ai account has an update."
    return f"Hello {first_name}, here is your desifaces.ai update."


def render_notification_email(
    *,
    template_key: str,
    user_context: Dict[str, Any],
    event: Dict[str, Any],
    metadata: Dict[str, Any],
    payload: Dict[str, Any],
) -> Dict[str, str]:
    first_name = (
        (user_context or {}).get("first_name")
        or ((user_context or {}).get("email") or "there").split("@")[0]
    )
    tier_code = (user_context or {}).get("tier_code")
    title = str((event or {}).get("title") or "desifaces.ai update")
    body = str((event or {}).get("body") or "")
    action_label = (event or {}).get("action_label") or "Open desifaces.ai"
    action_route = (event or {}).get("action_route") or ""
    intro = _tier_intro(first_name, tier_code)

    subject = title

    if template_key == "billing/payment_success":
        subject = "Payment received • desifaces.ai"
    elif template_key == "billing/payment_failed":
        subject = "Payment failed • desifaces.ai"
    elif template_key == "billing/subscription_upgraded":
        subject = "Plan upgraded • desifaces.ai"
    elif template_key == "billing/subscription_downgraded":
        subject = "Plan changed • desifaces.ai"
    elif template_key == "jobs/face_ready":
        subject = "Your Face output is ready • desifaces.ai"
    elif template_key == "jobs/audio_ready":
        subject = "Your Audio output is ready • desifaces.ai"
    elif template_key == "jobs/fusion_ready":
        subject = "Your Fusion video is ready • desifaces.ai"
    elif template_key == "jobs/job_failed":
        subject = "A desifaces.ai job needs attention"
    elif template_key == "support/contact_ack":
        subject = "We received your support request • desifaces.ai"
    elif template_key == "support/reply_received":
        subject = "Support replied • desifaces.ai"

    text_lines = [
        intro,
        "",
        title,
        body,
    ]
    if action_route:
        text_lines.extend(["", f"{action_label}: {action_route}"])

    text_body = "\n".join(text_lines).strip()

    html_body = f"""
    <html>
      <body style="margin:0;padding:0;background:#080808;color:#FFF7E8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
        <div style="max-width:640px;margin:0 auto;padding:32px 20px;">
          <div style="padding:24px;border-radius:20px;border:1px solid #2A221A;background:#13100D;">
            <div style="font-size:14px;color:#D9C6A7;margin-bottom:12px;">{escape(intro)}</div>
            <div style="font-size:24px;font-weight:800;line-height:1.3;margin-bottom:10px;">{escape(title)}</div>
            <div style="font-size:15px;line-height:1.7;color:#E9DCC2;">{escape(body)}</div>
            {"<div style='margin-top:20px;'><a href='" + escape(action_route) + "' style='display:inline-block;padding:12px 16px;border-radius:12px;background:#E89838;color:#080808;text-decoration:none;font-weight:800;'>" + escape(action_label) + "</a></div>" if action_route else ""}
            <div style="margin-top:28px;font-size:12px;color:#A78F6B;">Sent by desifaces.ai</div>
          </div>
        </div>
      </body>
    </html>
    """.strip()

    return {
        "subject": subject,
        "text_body": text_body,
        "html_body": html_body,
    }