from __future__ import annotations

from typing import List

from app.domain.enums import RenderRoute, ShotType
from app.domain.models import ScenarioPlan, ScriptSpec, ShotSpec, StoryBeat, VideoIntent


class ShotPlannerService:
    def build_shots(
        self,
        intent: VideoIntent,
        scenario: ScenarioPlan,
        beats: List[StoryBeat],
    ) -> List[ShotSpec]:
        shots: List[ShotSpec] = []
        shot_index = 0

        for beat in beats:
            beat_shots = self._shots_for_beat(intent, scenario, beat, shot_index)
            shots.extend(beat_shots)
            shot_index += len(beat_shots)

        return shots

    def _shots_for_beat(
        self,
        intent: VideoIntent,
        scenario: ScenarioPlan,
        beat: StoryBeat,
        shot_index_start: int,
    ) -> List[ShotSpec]:
        specs: List[ShotSpec] = []

        if beat.name == "hook":
            specs.append(
                self._make_shot(
                    beat=beat,
                    shot_index=shot_index_start,
                    shot_type=ShotType.HOOK_OPEN,
                    render_route=RenderRoute.FUSION,
                    duration_sec=min(8, beat.duration_sec),
                    spoken_text=self._hook_line(intent),
                    visual_brief="Strong opening line with high-confidence framing",
                )
            )
            specs.append(
                self._make_shot(
                    beat=beat,
                    shot_index=shot_index_start + 1,
                    shot_type=ShotType.TITLE_CARD,
                    render_route=RenderRoute.INTERNAL_CARD,
                    duration_sec=3,
                    onscreen_text=[intent.goal],
                    visual_brief="Minimal premium title card",
                )
            )
            return specs

        if beat.name in {"product_reveal", "solution", "cta"}:
            specs.append(
                self._make_shot(
                    beat=beat,
                    shot_index=shot_index_start,
                    shot_type=ShotType.TALKING_HEAD if beat.name != "solution" else ShotType.PRODUCT_SHOWCASE,
                    render_route=RenderRoute.FUSION if beat.name != "solution" else RenderRoute.INTERNAL_MONTAGE,
                    duration_sec=max(6, beat.duration_sec // 2),
                    spoken_text=self._spoken_text_for(beat, intent),
                    onscreen_text=beat.message_points,
                    visual_brief=f"{beat.name} with presenter-led clarity",
                )
            )
            if beat.name != "cta":
                specs.append(
                    self._make_shot(
                        beat=beat,
                        shot_index=shot_index_start + 1,
                        shot_type=ShotType.VOICEOVER_BROLL,
                        render_route=RenderRoute.AUDIO_BROLL,
                        duration_sec=max(4, beat.duration_sec // 2),
                        spoken_text=self._voiceover_for(beat, intent),
                        visual_brief=f"{beat.name} support visuals / proof montage",
                    )
                )
            return specs

        specs.append(
            self._make_shot(
                beat=beat,
                shot_index=shot_index_start,
                shot_type=ShotType.VOICEOVER_BROLL,
                render_route=RenderRoute.AUDIO_BROLL,
                duration_sec=max(5, beat.duration_sec),
                spoken_text=self._voiceover_for(beat, intent),
                onscreen_text=beat.message_points,
                visual_brief=f"{beat.name} cinematic support sequence",
            )
        )
        return specs

    def _make_shot(
        self,
        *,
        beat: StoryBeat,
        shot_index: int,
        shot_type: ShotType,
        render_route: RenderRoute,
        duration_sec: int,
        spoken_text: str | None = None,
        onscreen_text: List[str] | None = None,
        visual_brief: str | None = None,
    ) -> ShotSpec:
        return ShotSpec(
            shot_id=f"{beat.beat_id}_shot_{shot_index:02d}",
            beat_id=beat.beat_id,
            shot_index=shot_index,
            shot_type=shot_type,
            render_route=render_route,
            duration_sec=duration_sec,
            title=beat.name,
            script=ScriptSpec(
                spoken_text=spoken_text,
                voiceover_text=spoken_text,
                subtitle_text=spoken_text,
                onscreen_text=onscreen_text or [],
            ),
            visual_brief=visual_brief,
        )

    def _hook_line(self, intent: VideoIntent) -> str:
        return intent.message.must_include[0] if intent.message.must_include else intent.goal

    def _spoken_text_for(self, beat: StoryBeat, intent: VideoIntent) -> str:
        if beat.name == "cta" and intent.message.cta:
            return intent.message.cta
        return " ".join(beat.message_points) or beat.purpose

    def _voiceover_for(self, beat: StoryBeat, intent: VideoIntent) -> str:
        return self._spoken_text_for(beat, intent)