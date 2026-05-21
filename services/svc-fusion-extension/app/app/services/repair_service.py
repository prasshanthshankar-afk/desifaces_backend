from __future__ import annotations

from typing import List

from app.domain.enums import QcDecision, RenderRoute, ShotType
from app.domain.models import QcResult, ScriptSpec, ShotSpec, VideoIntent


class RepairService:
    def apply_repairs(self, intent: VideoIntent, shots: List[ShotSpec], qc: QcResult) -> List[ShotSpec]:
        repaired = [shot.model_copy(deep=True) for shot in shots]

        if qc.decision == QcDecision.INSERT_HOOK:
            repaired.insert(
                0,
                ShotSpec(
                    shot_id="repair_hook_00",
                    beat_id="repair",
                    shot_index=0,
                    shot_type=ShotType.HOOK_OPEN,
                    render_route=RenderRoute.FUSION,
                    duration_sec=5,
                    title="repair_hook",
                    script=ScriptSpec(
                        spoken_text=intent.goal,
                        subtitle_text=intent.goal,
                        onscreen_text=[intent.goal],
                    ),
                    visual_brief="Repaired stronger opening shot",
                ),
            )
            return repaired

        if qc.decision == QcDecision.INSERT_CTA and intent.message.cta:
            repaired.append(
                ShotSpec(
                    shot_id="repair_cta_00",
                    beat_id="repair",
                    shot_index=len(repaired),
                    shot_type=ShotType.OUTRO_CTA,
                    render_route=RenderRoute.INTERNAL_CARD,
                    duration_sec=4,
                    title="repair_cta",
                    script=ScriptSpec(
                        spoken_text=intent.message.cta,
                        subtitle_text=intent.message.cta,
                        onscreen_text=[intent.message.cta],
                    ),
                    visual_brief="Explicit CTA end card",
                )
            )
            return repaired

        return repaired