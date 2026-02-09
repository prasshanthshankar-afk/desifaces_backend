from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status

from app.deps import get_current_claims  # ✅ existing svc-core auth dependency
from app.services.masterdata_service import MasterdataService, get_masterdata_service

router = APIRouter(prefix="/core/api/masterdata", tags=["masterdata"])


@router.get("/version")
async def masterdata_version(
    domain: str = "face",
    _claims: dict = Depends(get_current_claims),  # ✅ enforce auth
    svc: MasterdataService = Depends(get_masterdata_service),
):
    try:
        return await svc.get_version(domain)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown masterdata domain: {domain}")


@router.get("/face")
async def masterdata_face(
    response: Response,
    lang: str = "en",
    if_none_match: str | None = Header(default=None),
    _claims: dict = Depends(get_current_claims),  # ✅ enforce auth
    svc: MasterdataService = Depends(get_masterdata_service),
):
    data = await svc.get_face(lang=lang)
    rev = data["revision"]

    etag = f"\"face:{lang}:{rev}\""
    response.headers["ETag"] = etag
    response.headers["Vary"] = "If-None-Match, Accept-Encoding"
    response.headers["Cache-Control"] = "private, max-age=0, must-revalidate"

    if if_none_match == etag:
        response.status_code = status.HTTP_304_NOT_MODIFIED
        return None

    return data


@router.get("/tts")
async def masterdata_tts(
    response: Response,
    if_none_match: str | None = Header(default=None),
    _claims: dict = Depends(get_current_claims),  # ✅ enforce auth
    svc: MasterdataService = Depends(get_masterdata_service),
):
    data = await svc.get_tts()
    rev = data["revision"]

    etag = f"\"tts:{rev}\""
    response.headers["ETag"] = etag
    response.headers["Vary"] = "If-None-Match, Accept-Encoding"
    response.headers["Cache-Control"] = "private, max-age=0, must-revalidate"

    if if_none_match == etag:
        response.status_code = status.HTTP_304_NOT_MODIFIED
        return None

    return data