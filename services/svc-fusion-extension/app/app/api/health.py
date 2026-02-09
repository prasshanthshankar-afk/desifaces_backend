from fastapi import APIRouter

router = APIRouter(prefix="/api/health", tags=["health"])

@router.get("")
@router.get("/")
async def health():
    return {"status": "ok", "service": "svc-fusion-extension"}

@router.get("/ready")
async def ready():
    return {"ready": True}