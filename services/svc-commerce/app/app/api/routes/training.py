from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.training.training_dataset_service import get_db_pool
from app.services.training.training_orchestrator import TrainingOrchestrator


router = APIRouter(prefix="/training", tags=["commerce-training"])

_SUPPORTED_FAMILIES = {
    "salwar_suit",
    "lehenga_set",
    "kurta_pyjama",
    "sherwani",
}


# -----------------------------------------------------------------------------
# Request / Response models
# -----------------------------------------------------------------------------


class TrainingStartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: UUID
    family: str = Field(..., description="One of salwar_suit, lehenga_set, kurta_pyjama, sherwani")
    split: str = Field(default="train")
    limit: Optional[int] = Field(default=None, ge=1)
    base_model: str = Field(default="fal-ai/flux-2")
    steps: Optional[int] = Field(default=None, ge=1)
    learning_rate: Optional[float] = Field(default=None, gt=0.0)
    default_caption: Optional[str] = None
    force_new_run: bool = False
    mirror_to_azure: bool = True

    # Optional convenience mode:
    # wait=False => submit and return immediately
    # wait=True  => block and poll until terminal state
    wait: bool = False
    poll_secs: Optional[float] = Field(default=None, gt=0.0)
    poll_timeout_s: Optional[int] = Field(default=None, ge=1)

    @field_validator("family")
    @classmethod
    def _validate_family(cls, v: str) -> str:
        fam = str(v or "").strip().lower()
        if fam not in _SUPPORTED_FAMILIES:
            raise ValueError(f"unsupported family={v!r}; expected one of {sorted(_SUPPORTED_FAMILIES)}")
        return fam

    @field_validator("split")
    @classmethod
    def _validate_split(cls, v: str) -> str:
        s = str(v or "").strip().lower()
        if not s:
            return "train"
        if s in {"train", "val", "test"}:
            return s
        raise ValueError("split must be one of: train, val, test")


class ResumeRunsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=10, ge=1, le=100)
    mirror_to_azure: bool = True


class TrainingResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str


# -----------------------------------------------------------------------------
# Internal helpers
# -----------------------------------------------------------------------------


async def _resolve_db_pool(request: Request) -> asyncpg.Pool:
    """
    Best-effort pool resolution so this route works with current app wiring
    without forcing an immediate startup refactor.
    """
    for attr in ("db_pool", "pool", "commerce_db_pool", "training_db_pool"):
        pool = getattr(request.app.state, attr, None)
        if pool is not None:
            return pool

    # Fallback: lazily create and cache a dedicated pool for training routes.
    pool = await get_db_pool()
    request.app.state.training_db_pool = pool
    return pool


async def _get_orchestrator(request: Request) -> TrainingOrchestrator:
    pool = await _resolve_db_pool(request)

    orch = getattr(request.app.state, "training_orchestrator", None)
    if orch is not None and getattr(orch, "pool", None) is pool:
        return orch

    orch = TrainingOrchestrator(pool=pool)
    request.app.state.training_orchestrator = orch
    return orch


def _http_500(detail: str) -> HTTPException:
    return HTTPException(status_code=500, detail=detail)


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------


@router.post("/start", response_model=TrainingResponse, response_model_exclude_none=True)
async def start_training(req: TrainingStartIn, request: Request) -> Dict[str, Any]:
    """
    Start a new training run for an Indian non-saree family.

    - wait=false: returns immediately after submit
    - wait=true : blocks until terminal state
    """
    orch = await _get_orchestrator(request)

    try:
        if req.wait:
            out = await orch.run_training(
                dataset_id=req.dataset_id,
                family=req.family,
                split=req.split,
                limit=req.limit,
                base_model=req.base_model,
                steps=req.steps,
                learning_rate=req.learning_rate,
                default_caption=req.default_caption,
                force_new_run=req.force_new_run,
                mirror_to_azure=req.mirror_to_azure,
                poll_secs=req.poll_secs,
                poll_timeout_s=req.poll_timeout_s,
            )
            return {
                "status": str(out.get("status") or "unknown"),
                **out,
            }

        out = await orch.start_training(
            dataset_id=req.dataset_id,
            family=req.family,
            split=req.split,
            limit=req.limit,
            base_model=req.base_model,
            steps=req.steps,
            learning_rate=req.learning_rate,
            default_caption=req.default_caption,
            force_new_run=req.force_new_run,
            mirror_to_azure=req.mirror_to_azure,
        )
        return {
            "status": str(out.get("status") or "running"),
            **out,
        }

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        msg = str(e)
        if any(
            s in msg
            for s in (
                "training_dataset_not_found",
                "training_dataset_not_frozen",
                "training_dataset_too_small",
                "training_dataset_val_too_small",
                "unsupported_training_family",
                "no_training_examples_for_export",
                "all_examples_rejected",
            )
        ):
            raise HTTPException(status_code=400, detail=msg) from e
        raise _http_500(msg) from e
    except Exception as e:
        raise _http_500(f"{type(e).__name__}: {e}") from e


@router.get("/{checkpoint_id}/status", response_model=TrainingResponse, response_model_exclude_none=True)
async def get_training_status(
    checkpoint_id: UUID,
    request: Request,
    refresh: bool = Query(default=True, description="If true, poll provider before returning status"),
    mirror_to_azure: bool = Query(default=True),
) -> Dict[str, Any]:
    """
    Get training status for a checkpoint id.

    - refresh=true  => actively poll provider if run is incomplete
    - refresh=false => return current DB state only
    """
    orch = await _get_orchestrator(request)

    try:
        if refresh:
            out = await orch.poll_training(
                checkpoint_id=checkpoint_id,
                mirror_to_azure=mirror_to_azure,
            )
            return {
                "status": str(out.get("status") or "unknown"),
                **out,
            }

        row = await orch._get_checkpoint_row(str(checkpoint_id))  # best-effort lightweight read
        if not row:
            raise HTTPException(status_code=404, detail=f"checkpoint_not_found checkpoint_id={checkpoint_id}")

        return {
            "status": str(row.get("status") or "unknown"),
            "checkpoint_id": str(row["id"]),
            "model_family": row.get("model_family"),
            "base_model": row.get("base_model"),
            "notes": row.get("notes"),
            "config_json": row.get("config_json") or {},
            "hyperparams_json": row.get("hyperparams_json") or {},
            "metrics_json": row.get("metrics_json") or {},
            "artifacts_json": row.get("artifacts_json") or {},
            "created_at": row.get("created_at"),
        }

    except HTTPException:
        raise
    except RuntimeError as e:
        msg = str(e)
        if "checkpoint_not_found" in msg:
            raise HTTPException(status_code=404, detail=msg) from e
        raise _http_500(msg) from e
    except Exception as e:
        raise _http_500(f"{type(e).__name__}: {e}") from e


@router.post("/resume", response_model=List[TrainingResponse], response_model_exclude_none=True)
async def resume_training_runs(req: ResumeRunsIn, request: Request) -> List[Dict[str, Any]]:
    """
    Resume / poll incomplete training runs.
    Useful after worker or service restart.
    """
    orch = await _get_orchestrator(request)

    try:
        out = await orch.resume_incomplete_runs(
            limit=req.limit,
            mirror_to_azure=req.mirror_to_azure,
        )
        normalized: List[Dict[str, Any]] = []
        for item in out:
            d = dict(item or {})
            d["status"] = str(d.get("status") or "unknown")
            normalized.append(d)
        return normalized
    except Exception as e:
        raise _http_500(f"{type(e).__name__}: {e}") from e