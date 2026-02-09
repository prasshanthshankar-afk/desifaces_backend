from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel


router = APIRouter(prefix="/music/catalog", tags=["music_catalog"])


class MusicCatalogResponse(BaseModel):
    band_packs: list[dict]
    scene_packs: list[dict]
    camera_edits: list[dict]
    duet_layouts: list[dict]
    modes: list[dict]


@router.get("", response_model=MusicCatalogResponse)
async def get_music_catalog():
    """
    MVP catalog for Music Studio UI.

    You can later move these into DB tables and add /admin sync endpoints,
    similar to svc-audio catalog.
    """
    return MusicCatalogResponse(
        modes=[
            {"id": "single", "label": "Single"},
            {"id": "duet", "label": "Duet"},
        ],
        duet_layouts=[
            {"id": "split", "label": "Split Screen"},
            {"id": "stack", "label": "Stacked"},
            {"id": "cut", "label": "Cut / Alternate"},
        ],
        camera_edits=[
            {"id": "beat_cut", "label": "Beat Cut"},
            {"id": "chorus_pushin", "label": "Chorus Push-In"},
            {"id": "cinematic_slowmo", "label": "Cinematic Slow-mo"},
        ],
        band_packs=[
            {"id": "bollywood_pop", "label": "Bollywood Pop"},
            {"id": "hollywood_stage", "label": "Hollywood Stage"},
            {"id": "indie_band", "label": "Indie Band"},
            {"id": "acoustic", "label": "Acoustic"},
            {"id": "edm_club", "label": "EDM Club"},
        ],
        scene_packs=[
            {"id": "stage_arena", "label": "Arena Stage"},
            {"id": "studio_live", "label": "Live Studio"},
            {"id": "rooftop_night", "label": "Rooftop Night"},
            {"id": "street_neon", "label": "Neon Street"},
        ],
    )