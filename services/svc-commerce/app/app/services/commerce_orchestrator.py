from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from app.domain.models import CommerceConfirmIn, CommerceConfirmOut, CommerceQuoteIn, CommerceQuoteOut
from app.repos.commerce_campaigns_repo import CommerceCampaignsRepo
from app.repos.commerce_quotes_repo import CommerceQuotesRepo
from app.repos.studio_jobs_repo import StudioJobsRepo
from app.services.pricing_client import PricingClient


class CommerceOrchestrator:
    def __init__(self) -> None:
        self.pricing = PricingClient()
        self.quotes_repo = CommerceQuotesRepo()
        self.campaigns_repo = CommerceCampaignsRepo()
        self.studio_jobs_repo = StudioJobsRepo()

    async def create_quote(self, *, user_id: UUID, req: CommerceQuoteIn) -> CommerceQuoteOut:
        quote = await self.pricing.quote(user_id=user_id, req=req)

        await self.quotes_repo.create(
            user_id=user_id,
            request_json=req.model_dump(mode="json"),
            response_json=quote.model_dump(mode="json"),
            total_credits=quote.total_credits,
            total_usd=float(quote.totals.get("usd", 0.0)),
            total_inr=float(quote.totals.get("inr", 0.0)),
            expires_at=quote.expires_at,
            quote_id=quote.quote_id,
        )

        return quote

    async def confirm_and_start(self, *, user_id: UUID, req: CommerceConfirmIn) -> CommerceConfirmOut:
        row = await self.quotes_repo.get_for_user(quote_id=req.quote_id, user_id=user_id)
        if not row:
            raise ValueError("quote_not_found")

        expires_at = row["expires_at"]
        if isinstance(expires_at, datetime):
            now = datetime.now(timezone.utc)
            if expires_at <= now:
                await self.quotes_repo.mark_expired(quote_id=req.quote_id)
                raise ValueError("quote_expired")

        if row["status"] != "quoted":
            # Idempotent-ish behavior: if already confirmed, still allow user to query campaign separately later
            raise ValueError(f"quote_not_quotable:{row['status']}")

        # Placeholder: reserve credits here later via svc-pricing/ledger
        await self.quotes_repo.mark_confirmed(quote_id=req.quote_id)

        # Build campaign from the original quote request
        request_json = row["request_json"] or {}
        mode = request_json.get("mode", "platform_models")
        product_type = request_json.get("product_type", "mixed")

        campaign_id = await self.campaigns_repo.create(
            user_id=user_id,
            mode=mode,
            product_type=product_type,
            quote_id=req.quote_id,
            input_json=request_json,
            status="queued",
        )

        # Create a studio_job that the commerce worker will consume
        payload_json = {
            "commerce_campaign_id": str(campaign_id),
            "quote_id": str(req.quote_id),
            "request": request_json,
            "idempotency_key": req.idempotency_key,
        }
        studio_job_id = await self.studio_jobs_repo.create_commerce_job(
            user_id=user_id,
            campaign_id=campaign_id,
            quote_id=req.quote_id,
            payload_json=payload_json,
        )

        return CommerceConfirmOut(
            campaign_id=campaign_id,
            studio_job_id=studio_job_id,
            status="queued",
        )