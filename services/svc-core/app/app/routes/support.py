from fastapi import APIRouter, Depends, Query

from app.deps import get_current_user_id
from app.schemas.support import (
    SupportContactRequest,
    SupportContactResponse,
    SupportReplyRequest,
    SupportRequestResponse,
)
from app.services.support_service import SupportService, get_support_service

router = APIRouter(prefix="/api/support", tags=["support"])


@router.post("/contact", response_model=SupportContactResponse)
async def contact_support(
    req: SupportContactRequest,
    user_id: str = Depends(get_current_user_id),
    svc: SupportService = Depends(get_support_service),
):
    return await svc.create_contact_request(user_id=user_id, req=req)


@router.get("/requests", response_model=list[SupportRequestResponse])
async def list_requests(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user_id: str = Depends(get_current_user_id),
    svc: SupportService = Depends(get_support_service),
):
    return await svc.list_requests(user_id=user_id, limit=limit, offset=offset)


@router.get("/requests/{request_id}", response_model=SupportRequestResponse)
async def get_request(
    request_id: str,
    user_id: str = Depends(get_current_user_id),
    svc: SupportService = Depends(get_support_service),
):
    return await svc.get_request(user_id=user_id, request_id=request_id)


@router.post("/requests/{request_id}/reply")
async def reply_request(
    request_id: str,
    req: SupportReplyRequest,
    user_id: str = Depends(get_current_user_id),
    svc: SupportService = Depends(get_support_service),
):
    await svc.reply_to_request(user_id=user_id, request_id=request_id, req=req)
    return {"ok": True}