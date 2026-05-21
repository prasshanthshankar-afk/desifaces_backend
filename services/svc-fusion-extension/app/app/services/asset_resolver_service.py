from __future__ import annotations

from typing import List

from app.domain.enums import ShotType
from app.domain.models import ShotSpec, VideoIntent


class AssetResolverService:
    def resolve_assets(self, intent: VideoIntent, shots: List[ShotSpec]) -> List[ShotSpec]:
        resolved: List[ShotSpec] = []

        for shot in shots:
            payload = shot.model_copy(deep=True)

            if shot.shot_type in {ShotType.HOOK_OPEN, ShotType.TALKING_HEAD, ShotType.OUTRO_CTA}:
                payload.resolved_assets["face_artifact_id"] = intent.assets.face_artifact_id
                payload.resolved_assets["voice_audio_artifact_id"] = intent.assets.voice_audio_artifact_id

            if shot.shot_type in {ShotType.VOICEOVER_BROLL, ShotType.MONTAGE, ShotType.PRODUCT_SHOWCASE}:
                payload.resolved_assets["image_urls"] = intent.assets.image_urls
                payload.resolved_assets["video_urls"] = intent.assets.video_urls
                payload.resolved_assets["screenshot_urls"] = intent.assets.screenshot_urls

            if shot.shot_type in {ShotType.TITLE_CARD, ShotType.OUTRO_CTA, ShotType.LOGO_STING}:
                payload.resolved_assets["logo_url"] = intent.assets.logo_url

            resolved.append(payload)

        return resolved