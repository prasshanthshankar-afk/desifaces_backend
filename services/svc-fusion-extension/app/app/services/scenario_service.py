from __future__ import annotations

from app.domain.enums import ScenarioType
from app.domain.models import ScenarioPlan, VideoIntent


class ScenarioService:
    def choose_scenario(self, intent: VideoIntent) -> ScenarioPlan:
        scenario_type = intent.scenario_type

        if scenario_type == ScenarioType.AUTO:
            text = f"{intent.goal} {' '.join(intent.tone)}".lower()
            if "founder" in text or "vision" in text:
                scenario_type = ScenarioType.FOUNDER_STORY
            elif "explain" in text or "how it works" in text or "product" in text:
                scenario_type = ScenarioType.PRODUCT_EXPLAINER
            elif "campaign" in text or "launch" in text or "promo" in text:
                scenario_type = ScenarioType.CAMPAIGN_PROMO
            elif "testimonial" in text or "customer" in text:
                scenario_type = ScenarioType.TESTIMONIAL
            else:
                scenario_type = ScenarioType.BRAND_FILM

        return self._build_plan(intent, scenario_type)

    def _build_plan(self, intent: VideoIntent, scenario_type: ScenarioType) -> ScenarioPlan:
        if scenario_type == ScenarioType.FOUNDER_STORY:
            return ScenarioPlan(
                scenario_type=scenario_type,
                rationale="Personal narrative and mission-led arc",
                target_duration_sec=intent.duration_sec,
                talking_ratio=0.50,
                montage_ratio=0.20,
                card_ratio=0.10,
                proof_ratio=0.20,
                suggested_arc=[
                    "hook",
                    "personal_problem",
                    "why_now",
                    "product_reveal",
                    "trust_proof",
                    "future_vision",
                    "cta",
                ],
            )
        if scenario_type == ScenarioType.PRODUCT_EXPLAINER:
            return ScenarioPlan(
                scenario_type=scenario_type,
                rationale="Clear explanatory arc with proof and CTA",
                target_duration_sec=intent.duration_sec,
                talking_ratio=0.30,
                montage_ratio=0.20,
                card_ratio=0.20,
                proof_ratio=0.30,
                suggested_arc=[
                    "hook",
                    "problem",
                    "solution",
                    "how_it_works",
                    "proof",
                    "cta",
                ],
            )
        return ScenarioPlan(
            scenario_type=scenario_type,
            rationale="General cinematic brand format",
            target_duration_sec=intent.duration_sec,
            suggested_arc=["hook", "story", "proof", "cta"],
        )