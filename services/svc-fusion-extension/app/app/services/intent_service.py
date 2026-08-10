from __future__ import annotations

from typing import Any, Dict

from app.domain.enums import LongformMode, ScenarioType
from app.domain.models import IntentAssets, IntentConstraints, IntentMessage, VideoIntent
from app.domain.validators import validate_video_intent


class IntentService:
    def normalize_request(self, payload: Dict[str, Any]) -> VideoIntent:
        intent_block = payload.get("intent") or {}
        message_block = payload.get("message") or {}
        assets_block = payload.get("assets") or {}
        constraints_block = payload.get("constraints") or {}

        intent = VideoIntent(
            mode=payload.get("mode", LongformMode.DIRECTED),
            goal=intent_block.get("goal") or payload.get("goal") or "",
            audience=intent_block.get("audience"),
            tone=intent_block.get("tone") or [],
            style=intent_block.get("style") or [],
            scenario_type=intent_block.get("scenario_type", ScenarioType.AUTO),
            duration_sec=int(intent_block.get("duration_sec") or payload.get("duration_sec") or 90),
            message=IntentMessage(**message_block),
            assets=IntentAssets(**assets_block),
            constraints=IntentConstraints(**constraints_block),
            meta={"raw_payload": payload},
        )
        return validate_video_intent(intent)