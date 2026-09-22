from __future__ import annotations

from typing import Dict, Optional

from .contracts import SafetyFinding, SafetyStatus


_CATEGORY_RULES: Dict[str, Dict[str, str]] = {
    "sexual": {
        "title": "Explicit sexual or nude content is not supported",
        "reason": "The content contains nudity, pornography, explicit sexual content, or exposed intimate body parts.",
        "required_action": "Use different content that is fully clothed and non-explicit.",
    },
    "minors": {
        "title": "Unsafe sexualized content involving minors is not supported",
        "reason": "The content includes sexualized, nude, revealing, exploitative, or otherwise unsafe treatment of a minor or young person.",
        "required_action": "Use different content that does not sexualize, exploit, or place minors in unsafe contexts.",
    },
    "abuse": {
        "title": "Abuse or sexual exploitation is not supported",
        "reason": "The content includes sexual violence, exploitation, molestation, or abuse.",
        "required_action": "Use different content without abuse, exploitation, or sexual violence.",
    },
    "weapons": {
        "title": "Firearm imagery is not supported",
        "reason": "The content contains guns, firearms, rifles, pistols, or other firearm imagery.",
        "required_action": "Use different content without guns or firearm imagery.",
    },
    "violence": {
        "title": "Graphic violence is not supported",
        "reason": "The content contains blood, gore, mutilation, open wounds, or graphic bodily harm.",
        "required_action": "Use a clean, non-graphic scene without blood, gore, open wounds, or graphic injury.",
    },
    "hate": {
        "title": "Hateful or targeted abusive content is not supported",
        "reason": "The content contains hateful, demeaning, or targeted abusive material toward protected groups.",
        "required_action": "Use respectful, neutral content without slurs, hateful framing, or targeted abuse.",
    },
    "self_harm": {
        "title": "Graphic or encouraging self-harm content is not supported",
        "reason": "The content includes self-harm encouragement, instructions, or graphic self-harm material.",
        "required_action": "Use supportive, non-graphic content without self-harm instructions or graphic details.",
    },
    "terrorism": {
        "title": "Terrorist or extremist promotional content is not supported",
        "reason": "The content appears to promote, praise, recruit for, or materially support terrorist or extremist activity.",
        "required_action": "Use different content without terrorist or extremist promotion, recruitment, or support.",
    },
    "illegal_drugs": {
        "title": "Illegal drug facilitation is not supported",
        "reason": "The content includes instructions or facilitation for making, selling, trafficking, or using illegal drugs.",
        "required_action": "Remove drug-making, selling, trafficking, or usage instructions.",
    },
    "content_safety": {
        "title": "This content is not supported",
        "reason": "The content violates the desifaces content-safety policy.",
        "required_action": "Use different content that stays within supported safety requirements.",
    },
}


def category_finding(
    category: str,
    *,
    source: str,
    severity: Optional[int] = None,
    subject: Optional[str] = None,
    code: Optional[str] = None,
    reason: Optional[str] = None,
    required_action: Optional[str] = None,
    title: Optional[str] = None,
    retryable: bool = True,
) -> SafetyFinding:
    normalized = (category or "content_safety").strip().lower().replace(" ", "_")
    rule = _CATEGORY_RULES.get(normalized, _CATEGORY_RULES["content_safety"])
    metadata = {"source": source}
    if severity is not None:
        metadata["severity"] = int(severity)
    return SafetyFinding(
        code=code or f"CONTENT_SAFETY_{normalized.upper()}",
        status=SafetyStatus.FAIL,
        title=title or rule["title"],
        reason=reason or rule["reason"],
        required_action=required_action or rule["required_action"],
        category=normalized,
        subject=subject,
        retryable=retryable,
        metadata=metadata,
    )


def pass_finding(*, source: str) -> SafetyFinding:
    return SafetyFinding(
        code="CONTENT_SAFETY_PASS",
        status=SafetyStatus.PASS,
        title="Content safety checks passed",
        reason="No unsupported content was detected by the configured safety checks.",
        required_action="",
        category="content_safety",
        metadata={"source": source},
    )
