# services/svc-commerce/app/app/api/routes/commerce_business_api.py
from __future__ import annotations

import time
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field, HttpUrl

from app.services.catalog.platform_model_selector import (
    PlatformModelSelectionInput,
    PlatformModelSelector,
)
from app.services.ops.operational_controls import (
    AuditLogService,
    IdempotencyService,
    TenantAuthService,
    TenantRateLimitService,
    WebhookDeliveryService,
    WebhookEvent,
    make_request_fingerprint,
)


router = APIRouter(prefix="/api/commerce/v1", tags=["commerce-business-api"])


# -----------------------------------------------------------------------------
# public models
# -----------------------------------------------------------------------------


class AssetUploadOut(BaseModel):
    asset_id: str
    kind: str
    garment_kind: Optional[str] = None
    status: Literal["uploaded"]
    content_type: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    url: Optional[str] = None
    created_at: str


class GarmentAssetIn(BaseModel):
    asset_id: Optional[str] = None
    url: Optional[HttpUrl] = None
    kind: str


class TargetModelIn(BaseModel):
    gender: Optional[Literal["male", "female", "unisex"]] = None
    region: Optional[str] = None
    age_band: Optional[str] = None
    body_type: Optional[str] = None
    preferred_model_id: Optional[str] = None
    style_tags: List[str] = Field(default_factory=list)


class OutputPreferencesIn(BaseModel):
    count: int = 1
    aspect_ratio: str = "3:4"
    background: Optional[str] = "studio_clean"
    quality_tier: str = "production"
    return_only_best: bool = True


class RoutingHintsIn(BaseModel):
    allow_fallback: bool = True
    strict_category_routing: bool = True


class BusinessJobCreateIn(BaseModel):
    client_job_id: Optional[str] = None
    mode: Literal["platform_model_tryon", "customer_tryon", "hybrid_tryon"] = "platform_model_tryon"
    garment_assets: List[GarmentAssetIn]
    target_model: TargetModelIn = Field(default_factory=TargetModelIn)
    output_preferences: OutputPreferencesIn = Field(default_factory=OutputPreferencesIn)
    routing_hints: RoutingHintsIn = Field(default_factory=RoutingHintsIn)
    webhook_url: Optional[HttpUrl] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class BusinessJobCreateOut(BaseModel):
    job_id: str
    client_job_id: Optional[str] = None
    status: Literal["queued"]
    stage: Literal["received"]
    created_at: str
    estimated_output_count: int
    webhook_registered: bool


class BusinessJobStatusOut(BaseModel):
    job_id: str
    client_job_id: Optional[str] = None
    tenant_id: str
    status: str
    stage: str
    created_at: str
    updated_at: str
    inputs_summary: Dict[str, Any] = Field(default_factory=dict)
    routing_summary: Dict[str, Any] = Field(default_factory=dict)
    artifact_status: Dict[str, Any] = Field(default_factory=dict)
    qc_summary: Optional[Dict[str, Any]] = None
    outputs: List[Dict[str, Any]] = Field(default_factory=list)
    errors: List[Dict[str, Any]] = Field(default_factory=list)


# -----------------------------------------------------------------------------
# dependency placeholders
# Replace these with concrete repo/service wiring from your existing DI pattern.
# -----------------------------------------------------------------------------


def get_tenant_auth_service() -> TenantAuthService:
    raise NotImplementedError("Wire TenantAuthService with commerce_api_credentials repo")


def get_rate_limit_service() -> TenantRateLimitService:
    raise NotImplementedError("Wire TenantRateLimitService with commerce_tenant_rate_limits repo")


def get_idempotency_service() -> IdempotencyService:
    raise NotImplementedError("Wire IdempotencyService with commerce_business_jobs repo")


def get_audit_log_service() -> AuditLogService:
    raise NotImplementedError("Wire AuditLogService with commerce_request_audit_logs repo")


def get_webhook_delivery_service() -> WebhookDeliveryService:
    raise NotImplementedError("Wire WebhookDeliveryService with commerce_webhooks repo")


def get_platform_model_selector() -> PlatformModelSelector:
    raise NotImplementedError("Wire PlatformModelSelector with platform_models and garment_target_rules repos")


def get_business_jobs_repo() -> Any:
    raise NotImplementedError("Wire commerce_business_jobs repo")


def get_assets_service() -> Any:
    raise NotImplementedError("Wire asset upload service")


# -----------------------------------------------------------------------------
# route helpers
# -----------------------------------------------------------------------------


async def _authenticate(
    authorization: Optional[str],
    auth_service: TenantAuthService,
) -> Any:
    if not authorization:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    try:
        return await auth_service.authenticate_bearer(authorization)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc


# -----------------------------------------------------------------------------
# routes
# -----------------------------------------------------------------------------


@router.post("/assets/upload", response_model=AssetUploadOut)
async def upload_asset(
    request: Request,
    file: UploadFile = File(...),
    kind: str = Form(...),
    garment_kind: Optional[str] = Form(None),
    client_asset_id: Optional[str] = Form(None),
    metadata_json: Optional[str] = Form(None),
    authorization: Optional[str] = Header(default=None),
    auth_service: TenantAuthService = Depends(get_tenant_auth_service),
    rate_limits: TenantRateLimitService = Depends(get_rate_limit_service),
    audit_logs: AuditLogService = Depends(get_audit_log_service),
    assets_service: Any = Depends(get_assets_service),
) -> AssetUploadOut:
    t0 = time.time()
    request_id = str(uuid4())
    auth = await _authenticate(authorization, auth_service)

    decision = await rate_limits.check_and_increment(
        tenant_id=auth.tenant_id,
        route_pattern="/api/commerce/v1/assets/upload",
    )
    if not decision.allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=decision.deny_reason)

    # assets_service is expected to validate mime/resolution/NSFW and persist asset metadata.
    result = await assets_service.upload_business_asset(
        tenant_id=auth.tenant_id,
        upload=file,
        kind=kind,
        garment_kind=garment_kind,
        client_asset_id=client_asset_id,
        metadata_json=metadata_json,
    )

    await audit_logs.log(
        request_id=request_id,
        route_pattern="/api/commerce/v1/assets/upload",
        method="POST",
        http_status=200,
        tenant_id=auth.tenant_id,
        credential_id=auth.credential_id,
        client_job_id=None,
        business_job_id=None,
        remote_addr=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        payload_size_bytes=request.headers.get("content-length"),
        duration_ms=int((time.time() - t0) * 1000),
        provider_name=None,
        provider_request_id=None,
        audit_json={"kind": kind, "garment_kind": garment_kind},
    )
    return AssetUploadOut(**result)


@router.post("/jobs", response_model=BusinessJobCreateOut)
async def create_business_job(
    request: Request,
    payload: BusinessJobCreateIn,
    authorization: Optional[str] = Header(default=None),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    auth_service: TenantAuthService = Depends(get_tenant_auth_service),
    rate_limits: TenantRateLimitService = Depends(get_rate_limit_service),
    idempotency: IdempotencyService = Depends(get_idempotency_service),
    audit_logs: AuditLogService = Depends(get_audit_log_service),
    webhook_delivery: WebhookDeliveryService = Depends(get_webhook_delivery_service),
    platform_model_selector: PlatformModelSelector = Depends(get_platform_model_selector),
    jobs_repo: Any = Depends(get_business_jobs_repo),
) -> BusinessJobCreateOut:
    t0 = time.time()
    request_id = str(uuid4())
    auth = await _authenticate(authorization, auth_service)

    decision = await rate_limits.check_and_increment(
        tenant_id=auth.tenant_id,
        route_pattern="/api/commerce/v1/jobs",
    )
    if not decision.allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=decision.deny_reason)

    if not payload.garment_assets:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="garment_assets is required")

    request_payload = payload.model_dump(mode="json")
    request_fingerprint = make_request_fingerprint(request_payload)
    idem = await idempotency.evaluate(
        tenant_id=auth.tenant_id,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
    )
    if idem.conflict:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=idem.conflict_reason)
    if idem.is_replay and idem.existing_job_id:
        row = await jobs_repo.get_by_id(idem.existing_job_id)
        return BusinessJobCreateOut(
            job_id=str(row["id"]),
            client_job_id=row.get("client_job_id"),
            status="queued",
            stage="received",
            created_at=str(row["created_at"]),
            estimated_output_count=int(payload.output_preferences.count or 1),
            webhook_registered=bool(payload.webhook_url),
        )

    garment_kind = str(payload.garment_assets[0].kind)
    selection = await platform_model_selector.select(
        PlatformModelSelectionInput(
            tenant_id=auth.tenant_id,
            garment_kind=garment_kind,
            requested_gender=payload.target_model.gender,
            requested_region=payload.target_model.region,
            requested_age_band=payload.target_model.age_band,
            requested_body_type=payload.target_model.body_type,
            requested_style_tags=payload.target_model.style_tags,
            preferred_model_id=payload.target_model.preferred_model_id,
            seed=payload.client_job_id or request_id,
            metadata=payload.metadata,
        )
    )

    created = await jobs_repo.create_business_job(
        {
            "tenant_id": auth.tenant_id,
            "client_job_id": payload.client_job_id,
            "mode": payload.mode,
            "status": "queued",
            "stage": "received",
            "idempotency_key": idempotency_key,
            "request_json": {**request_payload, "request_fingerprint": request_fingerprint},
            "resolved_json": {
                "platform_model_selection": {
                    "platform_model_id": selection.platform_model_id,
                    "model_code": selection.model_code,
                    "chosen_asset_url": selection.chosen_asset_url,
                    "chosen_asset_role": selection.chosen_asset_role,
                    "selection_score": selection.selection_score,
                    "explain_json": selection.explain_json,
                }
            },
            "artifact_status_json": {
                "input_validation": "succeeded",
                "platform_model_selection": "succeeded",
                "vton_generation": "pending",
                "qc_reranking": "pending",
                "final_output_upload": "pending",
            },
            "webhook_url": str(payload.webhook_url) if payload.webhook_url else None,
        }
    )

    await webhook_delivery.enqueue_job_event(
        WebhookEvent(
            tenant_id=auth.tenant_id,
            job_id=str(created["id"]),
            event_type="job.created",
            payload={
                "event": "job.created",
                "job_id": str(created["id"]),
                "client_job_id": payload.client_job_id,
                "status": "queued",
                "stage": "received",
                "timestamp": str(created["created_at"]),
                "metadata": payload.metadata,
            },
        )
    )

    await audit_logs.log(
        request_id=request_id,
        route_pattern="/api/commerce/v1/jobs",
        method="POST",
        http_status=200,
        tenant_id=auth.tenant_id,
        credential_id=auth.credential_id,
        client_job_id=payload.client_job_id,
        business_job_id=str(created["id"]),
        remote_addr=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        payload_size_bytes=request.headers.get("content-length"),
        duration_ms=int((time.time() - t0) * 1000),
        provider_name=None,
        provider_request_id=None,
        audit_json={"garment_kind": garment_kind, "mode": payload.mode},
    )

    return BusinessJobCreateOut(
        job_id=str(created["id"]),
        client_job_id=payload.client_job_id,
        status="queued",
        stage="received",
        created_at=str(created["created_at"]),
        estimated_output_count=int(payload.output_preferences.count or 1),
        webhook_registered=bool(payload.webhook_url),
    )


@router.get("/jobs/{job_id}", response_model=BusinessJobStatusOut)
async def get_business_job(
    request: Request,
    job_id: str,
    authorization: Optional[str] = Header(default=None),
    auth_service: TenantAuthService = Depends(get_tenant_auth_service),
    rate_limits: TenantRateLimitService = Depends(get_rate_limit_service),
    audit_logs: AuditLogService = Depends(get_audit_log_service),
    jobs_repo: Any = Depends(get_business_jobs_repo),
) -> BusinessJobStatusOut:
    t0 = time.time()
    request_id = str(uuid4())
    auth = await _authenticate(authorization, auth_service)
    decision = await rate_limits.check_and_increment(
        tenant_id=auth.tenant_id,
        route_pattern="/api/commerce/v1/jobs/{job_id}",
    )
    if not decision.allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=decision.deny_reason)

    row = await jobs_repo.get_by_id(job_id)
    if not row or str(row.get("tenant_id")) != auth.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")

    outputs = await jobs_repo.list_outputs(job_id)
    resolved_json = row.get("resolved_json") or {}
    await audit_logs.log(
        request_id=request_id,
        route_pattern="/api/commerce/v1/jobs/{job_id}",
        method="GET",
        http_status=200,
        tenant_id=auth.tenant_id,
        credential_id=auth.credential_id,
        client_job_id=row.get("client_job_id"),
        business_job_id=job_id,
        remote_addr=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        payload_size_bytes=0,
        duration_ms=int((time.time() - t0) * 1000),
        provider_name=(resolved_json or {}).get("provider_name"),
        provider_request_id=(resolved_json or {}).get("provider_request_id"),
        audit_json={"status": row.get("status"), "stage": row.get("stage")},
    )
    return BusinessJobStatusOut(
        job_id=str(row["id"]),
        client_job_id=row.get("client_job_id"),
        tenant_id=str(row["tenant_id"]),
        status=str(row["status"]),
        stage=str(row["stage"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        inputs_summary={
            "garment_kinds": [g.get("kind") for g in (row.get("request_json") or {}).get("garment_assets", [])],
            "target_gender": ((row.get("request_json") or {}).get("target_model") or {}).get("gender"),
            "mode": row.get("mode"),
        },
        routing_summary={
            "resolved_provider": (resolved_json or {}).get("provider_name"),
            "resolved_category": (row.get("request_json") or {}).get("garment_assets", [{}])[0].get("kind"),
            "resolved_platform_model_id": ((resolved_json or {}).get("platform_model_selection") or {}).get("platform_model_id"),
        },
        artifact_status=row.get("artifact_status_json") or {},
        qc_summary=row.get("qc_json") or None,
        outputs=outputs or [],
        errors=[row.get("error_json")] if row.get("error_json") else [],
    )
