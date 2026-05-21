from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.domain.enums import RenderRoute
from app.domain.models import ShotSpec, VideoIntent
from app.http_clients.audio_client import AudioClient
from app.http_clients.fusion_client import FusionClient


class RenderRouterService:
    def __init__(self, fusion_client: FusionClient, audio_client: AudioClient) -> None:
        self.fusion_client = fusion_client
        self.audio_client = audio_client

    async def render_shots(self, intent: VideoIntent, shots: List[ShotSpec]) -> List[Dict[str, Any]]:
        outputs: List[Dict[str, Any]] = []
        for shot in shots:
            try:
                outputs.append(await self.render_single_shot(intent, shot))
            except Exception as exc:
                outputs.append(self._failed_output(shot, exc))
        return outputs

    async def render_single_shot(self, intent: VideoIntent, shot: ShotSpec) -> Dict[str, Any]:
        if shot.render_route == RenderRoute.FUSION:
            return await self._render_fusion(intent, shot)
        if shot.render_route == RenderRoute.AUDIO_BROLL:
            return await self._render_audio_broll(intent, shot)
        if shot.render_route == RenderRoute.INTERNAL_CARD:
            return await self._render_internal_card(intent, shot)
        if shot.render_route == RenderRoute.INTERNAL_MONTAGE:
            return await self._render_internal_montage(intent, shot)

        return {
            **self._base_output(shot),
            "status": "skipped",
            "composition_required": False,
            "fallback_allowed": False,
            "reason": f"unsupported_render_route:{self._route_value(shot.render_route)}",
        }

    async def _render_fusion(self, intent: VideoIntent, shot: ShotSpec) -> Dict[str, Any]:
        result = await self.fusion_client.create_longform_shot(
            face_artifact_id=self._resolved_asset(shot, "face_artifact_id"),
            audio_artifact_id=self._resolved_asset(shot, "voice_audio_artifact_id"),
            spoken_text=self._text_or_none(getattr(shot.script, "spoken_text", None)),
            duration_sec=shot.duration_sec,
            external_provider_ok=getattr(intent.constraints, "external_provider_ok", False),
            shot_meta=shot.model_dump(mode="json"),
        )

        payload = self._base_output(shot)
        if isinstance(result, dict):
            payload.update(result)

        payload["status"] = payload.get("status") or "submitted"
        payload["composition_required"] = False
        payload["fallback_allowed"] = False
        payload["provider"] = payload.get("provider") or "fusion"

        return payload

    async def _render_audio_broll(self, intent: VideoIntent, shot: ShotSpec) -> Dict[str, Any]:
        voiceover_text = self._pick_voiceover_text(shot)
        existing_voice_ref_id = self._resolved_asset(shot, "voice_audio_artifact_id")
        existing_voice_ref_url = (
            self._resolved_asset(shot, "voice_audio_url")
            or self._resolved_asset(shot, "audio_url")
        )

        audio_result: Optional[Dict[str, Any]] = None
        if voiceover_text:
            audio_result = await self.audio_client.create_voiceover(
                text=voiceover_text,
                voice_audio_artifact_id=existing_voice_ref_id,
                meta={
                    "kind": "audio_broll",
                    "shot_id": shot.shot_id,
                    "shot_type": self._shot_type_value(shot),
                    "route": self._route_value(shot.render_route),
                    "shot_meta": shot.model_dump(mode="json"),
                },
            )

        payload = self._base_output(shot)
        payload.update(
            {
                "status": "ready_for_composition",
                "composition_required": True,
                "fallback_allowed": False,
                "composition_mode": "audio_broll",
                "provider": "internal",
                "audio": audio_result,
                "composition_payload": {
                    "mode": "audio_broll",
                    "duration_sec": shot.duration_sec,
                    "audio_artifact_id": self._pick_audio_artifact_id(audio_result) or existing_voice_ref_id,
                    "audio_url": self._pick_audio_url(audio_result) or existing_voice_ref_url,
                    "images": self._asset_list(shot, "image_urls"),
                    "videos": self._asset_list(shot, "video_urls"),
                    "screenshots": self._asset_list(shot, "screenshot_urls"),
                    "onscreen_text": self._text_or_none(getattr(shot.script, "onscreen_text", None)),
                    "spoken_text": self._text_or_none(getattr(shot.script, "spoken_text", None)),
                    "voiceover_text": voiceover_text,
                    "edit_hints": {
                        "prefer_ken_burns": True,
                        "prefer_crossfades": True,
                        "prefer_beat_sync": False,
                        "prefer_caption_overlay": bool(
                            self._text_or_none(getattr(shot.script, "onscreen_text", None))
                        ),
                    },
                },
            }
        )
        return payload

    async def _render_internal_card(self, intent: VideoIntent, shot: ShotSpec) -> Dict[str, Any]:
        onscreen_text = self._text_or_none(getattr(shot.script, "onscreen_text", None))
        spoken_text = self._text_or_none(getattr(shot.script, "spoken_text", None))
        title = onscreen_text or spoken_text or self._shot_type_value(shot)
        body = spoken_text if spoken_text and spoken_text != title else None

        payload = self._base_output(shot)
        payload.update(
            {
                "status": "ready_for_composition",
                "composition_required": True,
                "fallback_allowed": False,
                "composition_mode": "card",
                "provider": "internal",
                "composition_payload": {
                    "mode": "card",
                    "duration_sec": shot.duration_sec,
                    "card_type": self._shot_type_value(shot),
                    "title": title,
                    "body": body,
                    "logo_url": self._resolved_asset(shot, "logo_url"),
                    "background_image_url": self._resolved_asset(shot, "background_image_url"),
                    "background_video_url": self._resolved_asset(shot, "background_video_url"),
                    "style_hints": {
                        "layout": "centered_title_body",
                        "animate_in": True,
                        "animate_out": True,
                    },
                },
            }
        )
        return payload

    async def _render_internal_montage(self, intent: VideoIntent, shot: ShotSpec) -> Dict[str, Any]:
        payload = self._base_output(shot)
        payload.update(
            {
                "status": "ready_for_composition",
                "composition_required": True,
                "fallback_allowed": False,
                "composition_mode": "montage",
                "provider": "internal",
                "composition_payload": {
                    "mode": "montage",
                    "duration_sec": shot.duration_sec,
                    "images": self._asset_list(shot, "image_urls"),
                    "videos": self._asset_list(shot, "video_urls"),
                    "screenshots": self._asset_list(shot, "screenshot_urls"),
                    "onscreen_text": self._text_or_none(getattr(shot.script, "onscreen_text", None)),
                    "style_hints": {
                        "prefer_ken_burns": True,
                        "prefer_crossfades": True,
                        "prefer_speed_ramps": False,
                        "prefer_caption_overlay": bool(
                            self._text_or_none(getattr(shot.script, "onscreen_text", None))
                        ),
                    },
                },
            }
        )
        return payload

    def _base_output(self, shot: ShotSpec) -> Dict[str, Any]:
        return {
            "shot_id": shot.shot_id,
            "route": self._route_value(shot.render_route),
            "shot_type": self._shot_type_value(shot),
            "duration_sec": shot.duration_sec,
            "shot_meta": shot.model_dump(mode="json"),
        }

    def _failed_output(self, shot: ShotSpec, exc: Exception) -> Dict[str, Any]:
        return {
            **self._base_output(shot),
            "status": "failed",
            "composition_required": False,
            "fallback_allowed": False,
            "error": str(exc),
            "error_type": exc.__class__.__name__,
        }

    def _resolved_asset(self, shot: ShotSpec, key: str) -> Any:
        assets = getattr(shot, "resolved_assets", None) or {}
        return assets.get(key)

    def _asset_list(self, shot: ShotSpec, key: str) -> List[str]:
        value = self._resolved_asset(shot, key)
        if not value:
            return []
        if isinstance(value, list):
            return [str(x) for x in value if x]
        return [str(value)]

    def _pick_voiceover_text(self, shot: ShotSpec) -> Optional[str]:
        voiceover_text = self._text_or_none(getattr(shot.script, "voiceover_text", None))
        if voiceover_text:
            return voiceover_text

        spoken_text = self._text_or_none(getattr(shot.script, "spoken_text", None))
        if spoken_text:
            return spoken_text

        onscreen_text = self._text_or_none(getattr(shot.script, "onscreen_text", None))
        return onscreen_text

    def _pick_audio_artifact_id(self, audio_result: Optional[Dict[str, Any]]) -> Optional[str]:
        if not audio_result:
            return None
        for key in ("audio_artifact_id", "artifact_id", "voice_audio_artifact_id"):
            value = audio_result.get(key)
            if value:
                return str(value)
        return None

    def _pick_audio_url(self, audio_result: Optional[Dict[str, Any]]) -> Optional[str]:
        if not audio_result:
            return None
        for key in ("audio_url", "url", "voice_audio_url"):
            value = audio_result.get(key)
            if value:
                return str(value)
        return None

    def _route_value(self, value: Any) -> str:
        return getattr(value, "value", str(value))

    def _shot_type_value(self, shot: ShotSpec) -> str:
        shot_type = getattr(shot, "shot_type", None)
        return getattr(shot_type, "value", str(shot_type))

    def _text_or_none(self, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None