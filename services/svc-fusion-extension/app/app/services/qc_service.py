from __future__ import annotations

from typing import List

from app.domain.enums import QcDecision
from app.domain.models import QcIssue, QcResult, ShotSpec, TimelineManifest, VideoIntent


class QcService:
    def evaluate(self, intent: VideoIntent, timeline: TimelineManifest, shots: List[ShotSpec]) -> QcResult:
        issues: List[QcIssue] = []
        score = 1.0

        if not shots:
            return QcResult(
                decision=QcDecision.FAIL,
                score=0.0,
                issues=[QcIssue(code="no_shots", severity="high", message="No shots generated")],
            )

        if shots and shots[0].shot_type.value not in {"hook_open", "talking_head"}:
            issues.append(QcIssue(code="weak_open", severity="medium", message="Opening lacks a strong hook"))
            score -= 0.2

        shot_types = {shot.shot_type.value for shot in shots}
        if len(shot_types) < 3:
            issues.append(QcIssue(code="low_variety", severity="medium", message="Video grammar is too repetitive"))
            score -= 0.2

        if intent.message.cta and not any(shot.shot_type.value == "outro_cta" or "cta" in shot.title.lower() for shot in shots if shot.title):
            issues.append(QcIssue(code="missing_cta", severity="high", message="CTA scene missing"))
            score -= 0.25

        if score >= 0.85:
            decision = QcDecision.ACCEPT
        elif any(issue.code == "missing_cta" for issue in issues):
            decision = QcDecision.INSERT_CTA
        elif any(issue.code == "weak_open" for issue in issues):
            decision = QcDecision.INSERT_HOOK
        else:
            decision = QcDecision.REBALANCE_PACING

        return QcResult(
            decision=decision,
            score=max(0.0, score),
            issues=issues,
            recommended_repairs=[{"decision": decision.value, "issues": [x.code for x in issues]}],
        )