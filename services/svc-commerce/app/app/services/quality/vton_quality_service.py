# services/svc-commerce/app/app/services/quality/vton_quality_service.py
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------


def _as_dict(x: Any) -> Dict[str, Any]:
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return {}
        try:
            v = json.loads(s)
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}
    try:
        return dict(x)
    except Exception:
        return {}



def _as_list(x: Any) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, tuple):
        return list(x)
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return []
        try:
            v = json.loads(s)
            return v if isinstance(v, list) else [x]
        except Exception:
            return [x]
    return [x]



def _norm_text(x: Any) -> str:
    return str(x or "").strip().lower().replace("-", "_")



def _clamp01(x: float) -> float:
    if x < 0:
        return 0.0
    if x > 1:
        return 1.0
    return float(x)


# -----------------------------------------------------------------------------
# public types
# -----------------------------------------------------------------------------


@dataclass(slots=True)
class VTONCandidate:
    output_url: str
    width: Optional[int] = None
    height: Optional[int] = None
    content_type: Optional[str] = None
    provider_name: Optional[str] = None
    provider_output_id: Optional[str] = None
    metrics_json: Dict[str, Any] = field(default_factory=dict)
    debug_json: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ScoredVTONCandidate:
    candidate: VTONCandidate
    score: float
    accepted: bool
    rank: int
    reasons: List[str]
    qc_json: Dict[str, Any]


@dataclass(slots=True)
class VTONQualitySummary:
    garment_kind: str
    accepted: bool
    best_score: float
    accepted_count: int
    rejected_count: int
    best_output_url: Optional[str]
    reason_codes: List[str]
    summary_json: Dict[str, Any]


class VTONQualityRejected(RuntimeError):
    pass


# -----------------------------------------------------------------------------
# service
# -----------------------------------------------------------------------------


class VTONQualityService:
    """
    Scores provider candidates, enforces category compliance, and returns only the
    single best shippable result for the public API.

    This service intentionally does not inspect pixels directly. It operates on:
      - provider QC metrics
      - pipeline metadata
      - garment-target rules
      - category-specific acceptance thresholds

    It is designed to sit after provider execution and before output persistence.
    """

    CATEGORY_MINIMUMS: Dict[str, Dict[str, float]] = {
        "saree_set": {"category_compliance": 0.96, "framing": 0.92, "garment_fidelity": 0.92, "human_realism": 0.88},
        "salwar_suit": {"category_compliance": 0.90, "framing": 0.90, "garment_fidelity": 0.88, "human_realism": 0.88},
        "lehenga_set": {"category_compliance": 0.92, "framing": 0.92, "garment_fidelity": 0.90, "human_realism": 0.88},
        "kurti_leggings_set": {"category_compliance": 0.88, "framing": 0.88, "garment_fidelity": 0.86, "human_realism": 0.88},
        "kurta_pyjama": {"category_compliance": 0.90, "framing": 0.84, "garment_fidelity": 0.88, "human_realism": 0.88},
        "dhoti_kurta": {"category_compliance": 0.94, "framing": 0.92, "garment_fidelity": 0.90, "human_realism": 0.88},
        "sherwani": {"category_compliance": 0.91, "framing": 0.84, "garment_fidelity": 0.88, "human_realism": 0.88},
        "nehru_jacket_set": {"category_compliance": 0.88, "framing": 0.84, "garment_fidelity": 0.84, "human_realism": 0.88},
        "kurta_only": {"category_compliance": 0.86, "framing": 0.82, "garment_fidelity": 0.84, "human_realism": 0.88},
    }

    def __init__(self, default_acceptance_score: float = 0.87) -> None:
        self._default_acceptance_score = float(default_acceptance_score)

    def select_best_candidate(
        self,
        *,
        garment_kind: str,
        candidates: Sequence[VTONCandidate],
        lower_body_visibility_required: bool = False,
        strict_category_routing: bool = True,
    ) -> tuple[ScoredVTONCandidate, VTONQualitySummary, List[ScoredVTONCandidate]]:
        garment_kind_norm = _norm_text(garment_kind)
        if not candidates:
            raise VTONQualityRejected(f"No candidates available for garment_kind={garment_kind_norm}")

        scored: List[ScoredVTONCandidate] = []
        for candidate in candidates:
            scored.append(
                self._score_candidate(
                    garment_kind=garment_kind_norm,
                    candidate=candidate,
                    lower_body_visibility_required=lower_body_visibility_required,
                    strict_category_routing=strict_category_routing,
                )
            )

        ranked_raw = sorted(scored, key=lambda row: row.score, reverse=True)
        ranked: List[ScoredVTONCandidate] = []
        for idx, row in enumerate(ranked_raw, start=1):
            ranked.append(
                ScoredVTONCandidate(
                    candidate=row.candidate,
                    score=row.score,
                    accepted=row.accepted,
                    rank=idx,
                    reasons=list(row.reasons),
                    qc_json=dict(row.qc_json),
                )
            )

        accepted = [row for row in ranked if row.accepted]
        if not accepted:
            reason_codes = sorted({reason for row in ranked for reason in row.reasons})
            raise VTONQualityRejected(
                f"QC_REJECTED garment_kind={garment_kind_norm} reasons={','.join(reason_codes) or 'unknown'}"
            )

        best = accepted[0]
        summary = VTONQualitySummary(
            garment_kind=garment_kind_norm,
            accepted=True,
            best_score=best.score,
            accepted_count=len(accepted),
            rejected_count=len(ranked) - len(accepted),
            best_output_url=best.candidate.output_url,
            reason_codes=sorted({reason for row in ranked for reason in row.reasons}),
            summary_json={
                "garment_kind": garment_kind_norm,
                "best_output_url": best.candidate.output_url,
                "best_score": best.score,
                "accepted_count": len(accepted),
                "rejected_count": len(ranked) - len(accepted),
                "ranked": [
                    {
                        "rank": row.rank,
                        "score": row.score,
                        "accepted": row.accepted,
                        "output_url": row.candidate.output_url,
                        "reasons": row.reasons,
                        "qc_json": row.qc_json,
                    }
                    for row in ranked
                ],
            },
        )
        return best, summary, ranked

    def _score_candidate(
        self,
        *,
        garment_kind: str,
        candidate: VTONCandidate,
        lower_body_visibility_required: bool,
        strict_category_routing: bool,
    ) -> ScoredVTONCandidate:
        metrics = _as_dict(candidate.metrics_json)

        garment_fidelity = _clamp01(float(metrics.get("garment_fidelity") or metrics.get("garment_similarity") or 0.0))
        human_realism = _clamp01(float(metrics.get("human_realism") or metrics.get("photorealism") or 0.0))
        category_compliance = _clamp01(float(metrics.get("category_compliance") or 0.0))
        framing = _clamp01(float(metrics.get("framing") or metrics.get("body_visibility") or 0.0))
        face_quality = _clamp01(float(metrics.get("face_quality") or human_realism or 0.0))
        background_cleanliness = _clamp01(float(metrics.get("background_cleanliness") or 0.8))
        lower_body_visibility = _clamp01(float(metrics.get("lower_body_visibility") or framing or 0.0))
        artifact_penalty = _clamp01(float(metrics.get("artifact_penalty") or 0.0))
        explicit_failures = [_norm_text(v) for v in _as_list(metrics.get("explicit_failures"))]

        thresholds = self.CATEGORY_MINIMUMS.get(
            garment_kind,
            {"category_compliance": 0.86, "framing": 0.82, "garment_fidelity": 0.84, "human_realism": 0.86},
        )
        min_category_compliance = float(thresholds["category_compliance"])
        min_framing = float(thresholds["framing"])
        min_garment_fidelity = float(thresholds["garment_fidelity"])
        min_human_realism = float(thresholds["human_realism"])

        score = (
            0.33 * garment_fidelity
            + 0.24 * category_compliance
            + 0.18 * human_realism
            + 0.10 * framing
            + 0.07 * face_quality
            + 0.05 * background_cleanliness
            + 0.03 * lower_body_visibility
            - 0.15 * artifact_penalty
        )
        score = _clamp01(score)

        reasons: List[str] = []
        accepted = True
        if explicit_failures:
            accepted = False
            reasons.extend(sorted(set(explicit_failures)))
        if garment_fidelity < min_garment_fidelity:
            accepted = False
            reasons.append("GARMENT_FIDELITY_LOW")
        if human_realism < min_human_realism:
            accepted = False
            reasons.append("HUMAN_REALISM_LOW")
        if framing < min_framing:
            accepted = False
            reasons.append("FRAMING_LOW")
        if category_compliance < min_category_compliance:
            accepted = False
            reasons.append("CATEGORY_COMPLIANCE_LOW")
        if lower_body_visibility_required and lower_body_visibility < 0.85:
            accepted = False
            reasons.append("LOWER_BODY_VISIBILITY_LOW")
        if strict_category_routing and metrics.get("category_collapse_detected") is True:
            accepted = False
            reasons.append("CATEGORY_COLLAPSE_DETECTED")
        if metrics.get("gender_mismatch_detected") is True:
            accepted = False
            reasons.append("GENDER_MISMATCH_DETECTED")
        if metrics.get("broken_hands_or_limbs_detected") is True:
            accepted = False
            reasons.append("ANATOMY_BROKEN")
        if metrics.get("face_broken_detected") is True:
            accepted = False
            reasons.append("FACE_BROKEN")
        if score < self._default_acceptance_score:
            accepted = False
            reasons.append("QC_SCORE_LOW")

        qc_json = {
            "garment_fidelity": garment_fidelity,
            "human_realism": human_realism,
            "category_compliance": category_compliance,
            "framing": framing,
            "face_quality": face_quality,
            "background_cleanliness": background_cleanliness,
            "lower_body_visibility": lower_body_visibility,
            "artifact_penalty": artifact_penalty,
            "strict_category_routing": strict_category_routing,
            "thresholds": thresholds,
        }
        return ScoredVTONCandidate(
            candidate=candidate,
            score=score,
            accepted=accepted,
            rank=9999,
            reasons=sorted(set(reasons)),
            qc_json=qc_json,
        )
