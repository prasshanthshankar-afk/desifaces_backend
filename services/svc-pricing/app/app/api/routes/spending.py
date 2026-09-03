from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import AuthContext, AuthDep, PoolDep
from app.services.customer_spending_service import spending_summary, transaction_history

router = APIRouter(prefix="/api/pricing/me/spending", tags=["customer-spending"])

Period = Literal["month", "quarter", "year", "yoy"]
TransactionKind = Literal["all", "usage", "purchase", "subscription", "invoice", "refund"]


@router.get("/summary")
async def get_spending_summary(
    period: Period = Query(default="month"),
    auth: AuthContext = AuthDep,
    pool=PoolDep,
):
    """Read-only customer spending/usage summary.

    Money actually paid and credits consumed are intentionally separate measures.
    The authenticated user id is the only user selector; callers cannot request
    another customer's records.
    """
    try:
        async with pool.acquire() as conn:
            return await spending_summary(conn, user_id=auth.user_id, period=period)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/transactions")
async def get_transaction_history(
    period: Period = Query(default="year"),
    kind: TransactionKind = Query(default="all"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    auth: AuthContext = AuthDep,
    pool=PoolDep,
):
    """Return user-visible usage, purchase, subscription, invoice and refund history."""
    try:
        async with pool.acquire() as conn:
            return await transaction_history(
                conn,
                user_id=auth.user_id,
                period=period,
                kind=kind,
                limit=limit,
                offset=offset,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
