# services/svc-face/app/app/api/health.py
from fastapi import APIRouter

router = APIRouter()

@router.get("")
async def health() -> dict:
    return {"status": "ok"}