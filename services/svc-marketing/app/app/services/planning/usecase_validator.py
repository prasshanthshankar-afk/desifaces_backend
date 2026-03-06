from __future__ import annotations

from dataclasses import dataclass

from app.domain.models import UseCaseSpec


@dataclass
class SpecificityResult:
    ok: bool
    score: int
    reason: str


def specificity_gate(spec: UseCaseSpec) -> SpecificityResult:
    score = 0
    if spec.persona:
        score += 1
    if spec.industry and len(spec.industry.strip()) >= 2:
        score += 1
    if spec.hook_text and len(spec.hook_text.strip()) >= 6:
        score += 1
    if spec.voiceover_script and len(spec.voiceover_script.strip()) >= 40:
        score += 1

    # anchor is mandatory for non-generic: season OR offer OR product_anchor
    if (spec.season_event and spec.season_event.strip()) or (spec.offer and spec.offer.strip()) or (spec.product_anchor and spec.product_anchor.strip()):
        score += 2

    if spec.onscreen_lines and any(len(x.strip()) >= 8 for x in spec.onscreen_lines):
        score += 1

    ok = score >= 6
    reason = "ok" if ok else "UseCaseSpec too generic (missing anchor/overlays/script)."
    return SpecificityResult(ok=ok, score=score, reason=reason)