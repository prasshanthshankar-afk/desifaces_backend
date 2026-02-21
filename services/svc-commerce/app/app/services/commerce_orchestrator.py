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

        # If idempotency_key is provided, reuse existing campaign for this quote+key
        if req.idempotency_key:
            existing = await self.campaigns_repo.find_by_idempotency(
                user_id=user_id,
                quote_id=req.quote_id,
                idempotency_key=req.idempotency_key,
            )
            if existing:
                campaign_id = existing["id"]

                # Prefer reuse existing job if repo supports it
                job = None
                if hasattr(self.studio_jobs_repo, "get_latest_commerce_job_for_campaign"):
                    job = await self.studio_jobs_repo.get_latest_commerce_job_for_campaign(
                        user_id=user_id, campaign_id=campaign_id
                    )
                if job:
                    return CommerceConfirmOut(
                        campaign_id=campaign_id,
                        studio_job_id=job["id"],
                        status=str(job["status"]),
                    )

                payload_json = {
                    "commerce_campaign_id": str(campaign_id),
                    "quote_id": str(req.quote_id),
                    "request": existing.get("input_json") or {},
                    "idempotency_key": req.idempotency_key,
                    "stage": "queued",
                    "computed": {},
                    "error": None,
                }
                studio_job_id = await self.studio_jobs_repo.create_commerce_job(
                    user_id=user_id,
                    campaign_id=campaign_id,
                    quote_id=req.quote_id,
                    payload_json=payload_json,
                    meta_json={
                        "quote_id": str(req.quote_id),
                        "commerce_campaign_id": str(campaign_id),
                        "idempotency_key": req.idempotency_key,
                        "request_type": "commerce_confirm",
                    },
                )
                return CommerceConfirmOut(campaign_id=campaign_id, studio_job_id=studio_job_id, status="queued")

        # If already confirmed previously, return latest campaign/job (idempotent confirm)
        if row["status"] != "quoted":
            existing_campaign = await self.campaigns_repo.get_by_quote_id(user_id=user_id, quote_id=req.quote_id)
            if existing_campaign:
                campaign_id = existing_campaign["id"]
                job = None
                if hasattr(self.studio_jobs_repo, "get_latest_commerce_job_for_campaign"):
                    job = await self.studio_jobs_repo.get_latest_commerce_job_for_campaign(
                        user_id=user_id, campaign_id=campaign_id
                    )
                if job:
                    return CommerceConfirmOut(
                        campaign_id=campaign_id,
                        studio_job_id=job["id"],
                        status=str(job["status"]),
                    )
            raise ValueError(f"quote_not_quotable:{row['status']}")

        # reserve credits later via svc-pricing/ledger
        await self.quotes_repo.mark_confirmed(quote_id=req.quote_id)

        request_json = row["request_json"] or {}
        mode = request_json.get("mode", "platform_models")
        product_type = request_json.get("product_type", "mixed")

        campaign_id = await self.campaigns_repo.create(
            user_id=user_id,
            mode=mode,
            product_type=product_type,
            quote_id=req.quote_id,
            input_json=request_json,
            meta_json={"idempotency_key": req.idempotency_key} if req.idempotency_key else {},
            status="queued",
        )

        payload_json = {
            "commerce_campaign_id": str(campaign_id),
            "quote_id": str(req.quote_id),
            "request": request_json,
            "idempotency_key": req.idempotency_key,
            "stage": "queued",
            "computed": {},
            "error": None,
        }
        studio_job_id = await self.studio_jobs_repo.create_commerce_job(
            user_id=user_id,
            campaign_id=campaign_id,
            quote_id=req.quote_id,
            payload_json=payload_json,
            meta_json={
                "quote_id": str(req.quote_id),
                "commerce_campaign_id": str(campaign_id),
                "idempotency_key": req.idempotency_key,
                "request_type": "commerce_confirm",
            },
        )

        return CommerceConfirmOut(
            campaign_id=campaign_id,
            studio_job_id=studio_job_id,
            status="queued",
        )