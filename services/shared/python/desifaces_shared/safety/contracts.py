from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional


class SafetyStatus(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True)
class SafetyFinding:
    code: str
    status: SafetyStatus
    title: str
    reason: str
    required_action: str = ""
    category: str = ""
    subject: Optional[str] = None
    retryable: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "status": self.status.value,
            "title": self.title,
            "reason": self.reason,
            "required_action": self.required_action,
            "category": self.category or None,
            "subject": self.subject,
            "retryable": self.retryable,
            "metadata": dict(self.metadata or {}),
        }


@dataclass(frozen=True)
class SafetyDecision:
    status: SafetyStatus
    findings: List[SafetyFinding] = field(default_factory=list)
    summary: str = ""
    component: str = "desifaces-content-safety"
    contract_version: int = 1

    @property
    def allow(self) -> bool:
        return self.status != SafetyStatus.FAIL

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allow": self.allow,
            "status": self.status.value,
            "summary": self.summary,
            "component": self.component,
            "contract_version": self.contract_version,
            "findings": [finding.to_dict() for finding in self.findings],
        }

    def legacy_tuple(self) -> tuple[bool, str]:
        if self.allow:
            return True, ""
        first = next((f for f in self.findings if f.status == SafetyStatus.FAIL), None)
        if first is None:
            return False, self.summary or "Content safety check failed."
        detail = first.reason
        if first.required_action:
            detail = f"{detail} {first.required_action}".strip()
        return False, detail


def decision_from_findings(
    findings: Iterable[SafetyFinding],
    *,
    pass_summary: str = "Content safety checks passed.",
    warn_summary: str = "Content safety checks passed with warnings.",
    fail_summary: str = "Content cannot be used until the safety issues are resolved.",
) -> SafetyDecision:
    items = list(findings)
    if any(item.status == SafetyStatus.FAIL for item in items):
        return SafetyDecision(status=SafetyStatus.FAIL, findings=items, summary=fail_summary)
    if any(item.status == SafetyStatus.WARN for item in items):
        return SafetyDecision(status=SafetyStatus.WARN, findings=items, summary=warn_summary)
    return SafetyDecision(status=SafetyStatus.PASS, findings=items, summary=pass_summary)
