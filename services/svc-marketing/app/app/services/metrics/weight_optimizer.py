# services/svc-marketing/app/app/services/metrics/weight_optimizer.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Any
from uuid import UUID

from app.config import settings


@dataclass
class WeightUpdate:
    use_case_id: UUID
    new_weight: float
    metrics_json: Dict[str, Any]


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def compute_weight(metrics: Dict[str, Any]) -> float:
    # Engagement score (tweak later)
    saves = float(metrics.get("saves") or 0)
    shares = float(metrics.get("shares") or 0)
    comments = float(metrics.get("comments") or 0)
    likes = float(metrics.get("likes") or 0)
    plays = float(metrics.get("plays") or 0)
    reach = float(metrics.get("reach") or 0)
    cost_usd = float(metrics.get("cost_usd") or 0)

    # Base engagement (favor saves/shares)
    engagement = (saves * 4.0) + (shares * 3.0) + (comments * 2.0) + (likes * 0.5)
    exposure = max(plays, reach, 1.0)

    # Normalize: engagement per 1k exposure
    eng_per_k = engagement / (exposure / 1000.0)

    # Cost-adjusted score: more weight if good engagement per dollar
    if cost_usd > 0:
        score = eng_per_k / (1.0 + cost_usd)
    else:
        score = eng_per_k

    # Map to weight range
    # (This is intentionally smooth + bounded.)
    w = 0.5 + (score / 5.0)  # tune denominator based on observed metrics
    return _clamp(w, settings.OPTIMIZER_MIN_WEIGHT, settings.OPTIMIZER_MAX_WEIGHT)