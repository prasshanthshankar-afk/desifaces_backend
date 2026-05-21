from fastapi import APIRouter, Depends

from app.schemas.help import HelpArticleResponse, HelpCategoryResponse
from app.services.help_service import HelpService, get_help_service

router = APIRouter(prefix="/api/help", tags=["help"])


@router.get("/categories", response_model=list[HelpCategoryResponse])
async def list_categories(
    svc: HelpService = Depends(get_help_service),
):
    return await svc.list_categories()


@router.get("/faq", response_model=list[HelpArticleResponse])
async def list_faq(
    svc: HelpService = Depends(get_help_service),
):
    return await svc.list_faq()


@router.get("/articles/{slug}", response_model=HelpArticleResponse)
async def get_article(
    slug: str,
    svc: HelpService = Depends(get_help_service),
):
    return await svc.get_article(slug=slug)