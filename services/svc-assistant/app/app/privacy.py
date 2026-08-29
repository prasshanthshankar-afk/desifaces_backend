from __future__ import annotations

from dataclasses import dataclass
import re

SUPPORT_EMAIL = "support@desifaces.ai"
RESTRICTED_RESPONSE = (
    "I can't provide or retrieve personal identity or payment-card information through chat. "
    f"For identity-verified assistance, please contact {SUPPORT_EMAIL}."
)

_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}(?!\d)")
_SSN_RE = re.compile(r"(?<!\d)\d{3}-?\d{2}-?\d{4}(?!\d)")
_CARD_CANDIDATE_RE = re.compile(r"(?<!\d)(?:\d[ -]*?){13,19}(?!\d)")
_SECRET_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|Bearer\s+[A-Za-z0-9._~+/-]{16,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b",
    re.IGNORECASE,
)
_CVV_RE = re.compile(r"\b(?:cvv|cvc|security\s*code)\s*(?:is|=|:)?\s*\d{3,4}\b", re.IGNORECASE)

_DISCLOSURE_VERBS = re.compile(
    r"\b(?:show|tell|give|retrieve|lookup|look up|find|display|reveal|print|list|read|repeat|send|what is|what's|whats|which|provide|share|confirm|identify)\b",
    re.IGNORECASE,
)
_POSSESSIVE_DISCLOSURE = re.compile(
    r"\b(?:my|mine|on my account|on file|associated with (?:my|this) account|for (?:my|this) account)\b",
    re.IGNORECASE,
)

_PCI_TERMS = re.compile(
    r"\b(?:credit\s*card|debit\s*card|payment\s*card|card(?:\s*(?:number|details|digits|expiry|expiration))?|cvv|cvc|security\s*code|payment\s*instrument|payment\s*method|billing\s*details|last\s*four(?:\s*digits)?|ending\s*in)\b",
    re.IGNORECASE,
)
_PII_TERMS = re.compile(
    r"\b(?:email(?:\s*address)?|phone(?:\s*number)?|mobile(?:\s*number)?|home\s*address|mailing\s*address|street\s*address|physical\s*address|social\s*security(?:\s*number)?|ssn|government\s*id|passport(?:\s*number)?|driver'?s\s*license(?:\s*number)?|date\s*of\s*birth|birth\s*date|dob)\b",
    re.IGNORECASE,
)
_AUTH_TERMS = re.compile(
    r"\b(?:password|passcode|otp|one[- ]time\s*password|access\s*token|refresh\s*token|jwt|api\s*key|secret\s*key|authentication\s*token)\b",
    re.IGNORECASE,
)
_OTHER_USER = re.compile(
    r"\b(?:another|other|someone\s+else'?s|different)\s+(?:user|customer|account|person)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RedactionResult:
    text: str
    categories: tuple[str, ...]

    @property
    def redacted(self) -> bool:
        return bool(self.categories)


@dataclass(frozen=True)
class PolicyDecision:
    restricted: bool
    category: str | None = None


def _luhn(number: str) -> bool:
    digits = [int(x) for x in number if x.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    checksum = 0
    parity = len(digits) % 2
    for i, digit in enumerate(digits):
        value = digit
        if i % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        checksum += value
    return checksum % 10 == 0


def redact_sensitive_text(value: str) -> RedactionResult:
    text = value
    categories: list[str] = []

    def replace_email(match: re.Match[str]) -> str:
        raw = match.group(0)
        if raw.lower() == SUPPORT_EMAIL:
            return raw
        categories.append("pii")
        return "[REDACTED_EMAIL]"

    text = _EMAIL_RE.sub(replace_email, text)

    for pattern, replacement, category in (
        (_PHONE_RE, "[REDACTED_PHONE]", "pii"),
        (_SSN_RE, "[REDACTED_ID]", "pii"),
        (_SECRET_RE, "[REDACTED_SECRET]", "auth_secret"),
        (_CVV_RE, "[REDACTED_CARD_SECURITY_CODE]", "pci"),
    ):
        if pattern.search(text):
            categories.append(category)
            text = pattern.sub(replacement, text)

    def replace_card(match: re.Match[str]) -> str:
        raw = match.group(0)
        if _luhn(raw):
            categories.append("pci")
            return "[REDACTED_PAYMENT_CARD]"
        return raw

    text = _CARD_CANDIDATE_RE.sub(replace_card, text)
    return RedactionResult(text=text, categories=tuple(sorted(set(categories))))


def _disclosure_intent(message: str) -> bool:
    return bool(_DISCLOSURE_VERBS.search(message) or _POSSESSIVE_DISCLOSURE.search(message))


def classify_restricted_request(message: str) -> PolicyDecision:
    normalized = message.strip()
    disclosure = _disclosure_intent(normalized)
    if _OTHER_USER.search(normalized) and disclosure:
        return PolicyDecision(True, "cross_account")
    if _PCI_TERMS.search(normalized) and disclosure:
        return PolicyDecision(True, "pci")
    if _AUTH_TERMS.search(normalized) and disclosure:
        return PolicyDecision(True, "auth_secret")
    if _PII_TERMS.search(normalized) and disclosure:
        return PolicyDecision(True, "pii")
    return PolicyDecision(False, None)


def guard_output(answer: str) -> tuple[str, bool]:
    result = redact_sensitive_text(answer)
    if not result.redacted:
        return answer, False
    return RESTRICTED_RESPONSE, True
