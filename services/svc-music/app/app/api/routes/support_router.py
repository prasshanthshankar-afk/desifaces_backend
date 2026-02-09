from __future__ import annotations

from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.deps import require_admin, require_user
from app.db import get_pool
from app.services.support_audit import SupportAuditService

router = APIRouter(prefix="/music/support", tags=["support"])

SupportKind = Literal["snapshot", "action", "user_message", "assistant_message", "system"]


class SupportSessionUpsertIn(BaseModel):
    project_id: UUID
    job_id: Optional[UUID] = None
    surface: str = Field(default="music_studio", min_length=1)


class SupportSessionOut(BaseModel):
    session_id: UUID
    user_id: UUID
    project_id: UUID
    job_id: Optional[UUID]
    surface: str
    status: str


class SupportEventIn(BaseModel):
    session_id: UUID
    kind: SupportKind
    payload: dict[str, Any] = Field(default_factory=dict)
    request_id: Optional[str] = None


class SupportEventOut(BaseModel):
    event_id: UUID
    created_at: str
    event_hash_hex: str
    prev_hash_hex: Optional[str]


class AdminQueryIn(BaseModel):
    project_id: Optional[UUID] = None
    job_id: Optional[UUID] = None
    user_id: Optional[UUID] = None
    surface: Optional[str] = None
    limit: int = 200


class AdminEventIn(BaseModel):
    """
    Admin/support staff event.

    Client sends:
      - session_id
      - kind/payload (+ optional request_id)

    Server derives impersonated_user_id from support_sessions.user_id
    (source of truth) to satisfy legacy support_events.user_id NOT NULL.
    """
    session_id: UUID
    kind: SupportKind = "action"
    payload: dict[str, Any] = Field(default_factory=dict)
    request_id: Optional[str] = None


def _req_ip(req: Request) -> Optional[str]:
    return req.client.host if req.client else None


@router.post("/sessions/upsert", response_model=SupportSessionOut)
async def upsert_session(body: SupportSessionUpsertIn, user=Depends(require_user)):
    pool = await get_pool()
    svc = SupportAuditService(pool)

    session = await svc.upsert_session(
        user_id=user.id,
        project_id=body.project_id,
        job_id=body.job_id,
        surface=body.surface,
    )

    return SupportSessionOut(
        session_id=session["id"],
        user_id=session["user_id"],
        project_id=session["project_id"],
        job_id=session["job_id"],
        surface=session["surface"],
        status=session["status"],
    )


@router.post("/events", response_model=SupportEventOut)
async def append_event(body: SupportEventIn, req: Request, user=Depends(require_user)):
    pool = await get_pool()
    svc = SupportAuditService(pool)

    ip = _req_ip(req)
    ua = req.headers.get("user-agent")
    rid = body.request_id or req.headers.get("x-request-id")

    ok = await svc.session_belongs_to_user(session_id=body.session_id, user_id=user.id)
    if not ok:
        raise HTTPException(status_code=403, detail="session_forbidden")

    row = await svc.append_user_event(
        session_id=body.session_id,
        actor_user_id=user.id,
        kind=body.kind,
        payload=body.payload,
        request_id=rid,
        ip=ip,
        user_agent=ua,
    )

    return SupportEventOut(
        event_id=row["id"],
        created_at=row["created_at"].isoformat(),
        event_hash_hex=row["event_hash_hex"],
        prev_hash_hex=row["prev_hash_hex"],
    )


# -------------------------
# Admin APIs (svc-music only)
# -------------------------
@router.post("/admin/events", response_model=SupportEventOut)
async def admin_append_event(body: AdminEventIn, req: Request, admin=Depends(require_admin)):
    """
    Append an admin/support event to the session stream.

    Invariants:
      - support_events.user_id is NOT NULL (legacy)
      - admin events must be tied to a user context
      - we derive impersonated_user_id from support_sessions.user_id
    """
    pool = await get_pool()
    svc = SupportAuditService(pool)

    ip = _req_ip(req)
    ua = req.headers.get("user-agent")
    rid = body.request_id or req.headers.get("x-request-id")

    # Source-of-truth user context
    row_sess = await pool.fetchrow(
        """
        SELECT user_id
        FROM public.support_sessions
        WHERE id=$1
        """,
        body.session_id,
    )
    if not row_sess:
        raise HTTPException(status_code=404, detail="session_not_found")

    impersonated_user_id: Optional[UUID] = row_sess["user_id"]
    if impersonated_user_id is None:
        # Should never happen (support_sessions.user_id should be NOT NULL)
        raise HTTPException(status_code=500, detail="session_missing_user_id")

    # Optional: detect whether there were prior events before inserting.
    prior_count = await pool.fetchval(
        "SELECT count(*) FROM public.support_events WHERE session_id=$1",
        body.session_id,
    )

    row = await svc.append_admin_event(
        session_id=body.session_id,
        actor_admin_id=admin.id,
        impersonated_user_id=impersonated_user_id,
        kind=body.kind,
        payload=body.payload,
        request_id=rid,
        ip=ip,
        user_agent=ua,
    )

    # Optional sanity guard: if there were prior events, prev_hash should not be null.
    if prior_count and row.get("prev_hash_hex") is None:
        raise HTTPException(status_code=500, detail="hash_chain_prev_missing")

    return SupportEventOut(
        event_id=row["id"],
        created_at=row["created_at"].isoformat(),
        event_hash_hex=row["event_hash_hex"],
        prev_hash_hex=row["prev_hash_hex"],
    )


@router.post("/admin/events/query")
async def admin_query_events(body: AdminQueryIn, admin=Depends(require_admin)):
    pool = await get_pool()

    where = []
    args = []
    n = 1

    if body.project_id:
        where.append(f"project_id = ${n}")
        args.append(body.project_id)
        n += 1
    if body.job_id:
        where.append(f"job_id = ${n}")
        args.append(body.job_id)
        n += 1
    if body.user_id:
        where.append(f"session_user_id = ${n}")
        args.append(body.user_id)
        n += 1
    if body.surface:
        where.append(f"surface = ${n}")
        args.append(body.surface)
        n += 1

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    limit = max(1, min(body.limit, 1000))

    rows = await pool.fetch(
        f"""
        SELECT *
        FROM public.v_support_events_admin
        {where_sql}
        ORDER BY created_at DESC
        LIMIT {limit}
        """,
        *args,
    )

    return {"items": [dict(r) for r in rows]}


@router.get("/admin/sessions/{session_id}/verify-chain")
async def admin_verify_chain(session_id: UUID, admin=Depends(require_admin)):
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM public.verify_support_event_chain($1)", session_id)
    if not row:
        raise HTTPException(status_code=404, detail="not_found")
    return dict(row)