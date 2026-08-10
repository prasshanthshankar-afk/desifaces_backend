from __future__ import annotations

from typing import List

from app.domain.models import ScenarioPlan, StoryBeat, VideoIntent


class StoryPlannerService:
    def build_story_beats(self, intent: VideoIntent, scenario: ScenarioPlan) -> List[StoryBeat]:
        beats: List[StoryBeat] = []
        arc = scenario.suggested_arc
        duration_per = max(6, scenario.target_duration_sec // max(1, len(arc)))

        for idx, name in enumerate(arc, start=1):
            beats.append(
                StoryBeat(
                    beat_id=f"beat_{idx:02d}",
                    name=name,
                    purpose=self._purpose_for(name),
                    emotion=self._emotion_for(name),
                    duration_sec=duration_per,
                    talking_priority=self._talking_priority_for(name, scenario),
                    visual_direction=self._visual_direction_for(name, scenario),
                    message_points=self._message_points_for(name, intent),
                )
            )
        return beats

    def _purpose_for(self, name: str) -> str:
        return {
            "hook": "Capture attention immediately",
            "problem": "Frame the pain point or need",
            "personal_problem": "Make the story feel human and specific",
            "why_now": "Explain urgency and relevance",
            "solution": "Introduce the product or answer",
            "how_it_works": "Explain the mechanism simply",
            "product_reveal": "Reveal the core product promise",
            "proof": "Build credibility and evidence",
            "trust_proof": "Add signals of trust or validation",
            "future_vision": "Leave the viewer with aspiration",
            "cta": "Drive the next action",
        }.get(name, "Advance the story")

    def _emotion_for(self, name: str) -> str:
        return {
            "hook": "curious",
            "personal_problem": "empathetic",
            "why_now": "urgent",
            "product_reveal": "excited",
            "proof": "confident",
            "trust_proof": "reassuring",
            "future_vision": "inspiring",
            "cta": "decisive",
        }.get(name, "clear")

    def _talking_priority_for(self, name: str, scenario: ScenarioPlan) -> float:
        return 0.8 if name in {"hook", "personal_problem", "product_reveal", "cta"} else 0.4

    def _visual_direction_for(self, name: str, scenario: ScenarioPlan) -> str:
        return f"{scenario.scenario_type.value}::{name}"

    def _message_points_for(self, name: str, intent: VideoIntent) -> List[str]:
        if name == "cta" and intent.message.cta:
            return [intent.message.cta]
        return intent.message.must_include[:2]