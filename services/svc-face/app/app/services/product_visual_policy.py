from __future__ import annotations

import base64
import json
import os
from typing import Any

import requests

from desifaces_shared.safety import (
    SafetyDecision,
    category_finding,
    decision_from_findings,
    pass_finding,
)


class ProductVisualPolicyUnavailable(RuntimeError):
    pass


_SCHEMA = {
    "type": "object",
    "properties": {
        "firearm_or_weapon": {"type": "boolean"},
        "terrorist_or_extremist_promotion": {"type": "boolean"},
        "nudity_or_explicit_sexual": {"type": "boolean"},
        "sexual_content_involving_minor": {"type": "boolean"},
        "graphic_blood_or_gore": {"type": "boolean"},
        "graphic_violence_or_torture": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": [
        "firearm_or_weapon",
        "terrorist_or_extremist_promotion",
        "nudity_or_explicit_sexual",
        "sexual_content_involving_minor",
        "graphic_blood_or_gore",
        "graphic_violence_or_torture",
        "reason",
    ],
    "additionalProperties": False,
}


def _enabled() -> bool:
    return str(os.getenv("DF_PRODUCT_VISUAL_POLICY_ENABLED", "true")).strip().lower() not in {
        "0", "false", "no", "off",
    }


def _extract_output_text(payload: dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"output_text", "text"} and str(part.get("text") or "").strip():
                return str(part.get("text")).strip()
    return ""


def evaluate_product_visual_policy(
    image_bytes: bytes,
    *,
    content_type: str = "image/jpeg",
) -> SafetyDecision:
    """Product-level visual policy classifier for categories not fully covered by
    generic image moderation alone.

    This classifier answers only a bounded safety schema. It must not infer a
    person's identity, ethnicity, religion, health, or other unrelated traits.
    """
    if not _enabled():
        return decision_from_findings([pass_finding(source="product_visual_policy_disabled")])

    api_key = str(os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise ProductVisualPolicyUnavailable("OPENAI_API_KEY is not configured")

    base_url = str(os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    model = str(os.getenv("OPENAI_SAFETY_VISION_MODEL") or "gpt-5.6-luna").strip()
    encoded = base64.b64encode(image_bytes).decode("ascii")
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif image_bytes.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        mime = content_type if str(content_type or "").startswith("image/") else "image/jpeg"
    data_url = f"data:{mime};base64,{encoded}"

    body = {
        "model": model,
        "instructions": (
            "You are a narrow product-safety image classifier for desifaces. "
            "Classify only the requested visual policy categories. "
            "Do not identify people and do not infer ethnicity, religion, health, politics, "
            "sexual orientation, occupation, or socioeconomic status. "
            "Mark firearm_or_weapon true for visible guns, firearms, ammunition, or weapon-focused armed poses. "
            "Mark terrorist_or_extremist_promotion true only for promotional, recruitment, praise, support, "
            "or clearly propagandistic extremist/terrorist imagery or symbols; neutral documentary context alone is false. "
            "Mark nudity_or_explicit_sexual for nudity, pornography, exposed intimate parts, or explicit sexual activity. "
            "Mark sexual_content_involving_minor when a person who appears under 18 is depicted in a sexualized, nude, "
            "or exploitative context. Mark graphic_blood_or_gore for visible blood, gore, mutilation, or open graphic wounds. "
            "Mark graphic_violence_or_torture for graphic bodily harm, torture, or severe abuse. "
            "Return the schema only."
        ),
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Classify this image for the desifaces product safety gate."},
                    {"type": "input_image", "image_url": data_url, "detail": "low"},
                ],
            }
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "desifaces_product_visual_policy",
                "strict": True,
                "schema": _SCHEMA,
            }
        },
        "store": False,
    }

    try:
        response = requests.post(
            f"{base_url}/responses",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=float(os.getenv("OPENAI_SAFETY_VISION_TIMEOUT_SEC", "35")),
        )
    except Exception as exc:
        raise ProductVisualPolicyUnavailable("visual policy request failed") from exc

    if response.status_code >= 400:
        raise ProductVisualPolicyUnavailable(
            f"visual policy provider returned HTTP {response.status_code}"
        )

    try:
        payload = response.json()
        text = _extract_output_text(payload)
        result = json.loads(text)
    except Exception as exc:
        raise ProductVisualPolicyUnavailable("visual policy response was not valid structured JSON") from exc

    findings = []
    mapping = (
        ("firearm_or_weapon", "weapons"),
        ("terrorist_or_extremist_promotion", "terrorism"),
        ("nudity_or_explicit_sexual", "sexual"),
        ("sexual_content_involving_minor", "minors"),
        ("graphic_blood_or_gore", "violence"),
        ("graphic_violence_or_torture", "violence"),
    )
    for key, category in mapping:
        if bool(result.get(key)):
            findings.append(
                category_finding(
                    category,
                    source="product_visual_policy",
                    code=f"CONTENT_SAFETY_VISUAL_{key.upper()}",
                )
            )

    if findings:
        return decision_from_findings(findings)
    return decision_from_findings([pass_finding(source="product_visual_policy")])
