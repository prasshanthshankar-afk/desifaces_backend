# services/svc-marketing/app/app/services/orchestration/run_context.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict
from uuid import UUID

from app.services.orchestration.downstream_clients import DownstreamContext


def _norm_bearer_token(t: str | None) -> str:
    s = str(t or "").strip()
    if not s:
        return ""
    if s.lower().startswith("bearer "):
        s = s.split(" ", 1)[1].strip()
    return s


@dataclass(frozen=True)
class RunContext:
    run_id: UUID
    run_as_user_id: UUID
    bearer_token: str
    cost_bucket: str
    cost_category: str

    def to_downstream(self) -> DownstreamContext:
        return DownstreamContext(
            run_id=self.run_id,
            run_as_user_id=self.run_as_user_id,
            bearer_token=_norm_bearer_token(self.bearer_token),
            cost_bucket=self.cost_bucket,
            cost_category=self.cost_category,
        )

    def headers(self) -> Dict[str, str]:
        h: Dict[str, str] = {
            "X-DF-RUN-ID": str(self.run_id),
            "X-DF-RUN-AS-USER-ID": str(self.run_as_user_id),
            "X-DF-COST-BUCKET": self.cost_bucket,
            "X-DF-COST-CATEGORY": self.cost_category,
            "X-User-Id": str(self.run_as_user_id),
            "X-User-ID": str(self.run_as_user_id),
        }
        tok = _norm_bearer_token(self.bearer_token)
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        return h