from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

from app.db import get_pool
from app.domain.enums import MusicProjectMode, MusicTrackType
from app.repos.music_candidates_repo import MusicCandidatesRepo
from app.repos.provider_runs_repo import ProviderRunsRepo


def _as_dict(x: Any) -> Dict[str, Any]:
    if isinstance(x, dict):
        return x
    if isinstance(x, str) and x.strip():
        try:
            v = json.loads(x)
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}
    return {}


def _stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _idempotency_key(*, job_id: UUID, run_type: str, candidate_id: UUID, attempt: int) -> str:
    base = f"svc-music:{job_id}:{run_type}:{candidate_id}:attempt:{attempt}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


class MusicCandidatesController:
    """
    Orchestrates candidate fan-out, readiness, HITL actions, and retry loops.
    """
    def __init__(self) -> None:
        self.cands = MusicCandidatesRepo()
        self.runs = ProviderRunsRepo()

    async def _get_job_project_user(self, job_id: UUID) -> Tuple[UUID, UUID]:
        pool = await get_pool()
        row = await pool.fetchrow(
            "select id, project_id from public.music_video_jobs where id=$1",
            job_id,
        )
        if not row:
            raise ValueError("job_not_found")
        proj = await pool.fetchrow("select id, user_id, mode from public.music_projects where id=$1", row["project_id"])
        if not proj:
            raise ValueError("project_not_found")
        return UUID(str(proj["id"])), UUID(str(proj["user_id"]))

    async def _patch_job_computed(self, *, job_id: UUID, patch: Dict[str, Any]) -> None:
        pool = await get_pool()
        # We store everything inside input_json.computed.* to avoid schema churn
        await pool.execute(
            """
            update public.music_video_jobs
            set input_json = jsonb_set(
                coalesce(input_json,'{}'::jsonb),
                '{computed}',
                coalesce(input_json->'computed','{}'::jsonb) || $2::jsonb,
                true
            ),
            updated_at=now()
            where id=$1
            """,
            job_id,
            json.dumps(patch or {}),
        )

    async def start_group(
        self,
        *,
        job_id: UUID,
        candidate_type: str,   # lyrics|audio|video
        count: int,
        provider: Optional[str] = None,
        providers: Optional[List[str]] = None,
        seeds: Optional[List[int]] = None,
        hitl: bool = True,
        request_overrides: Optional[Dict[str, Any]] = None,
    ) -> UUID:
        project_id, user_id = await self._get_job_project_user(job_id)

        gid = uuid4()
        attempt = 1
        meta_patch = {
            "hitl": bool(hitl),
            "request_overrides": request_overrides or {},
        }

        # Create candidate rows
        await self.cands.create_group(
            job_id=job_id,
            project_id=project_id,
            user_id=user_id,
            candidate_type=candidate_type,
            count=count,
            attempt=attempt,
            provider=provider,
            seeds=seeds,
            meta_patch=meta_patch,
            group_id=gid,
        )

        # Enqueue provider_runs (one per candidate variant)
        items = await self.cands.list(job_id=job_id, candidate_type=candidate_type, group_id=gid, attempt=attempt)
        for it in items:
            cid = UUID(str(it["id"]))
            chosen_provider = (providers or [provider or "native"])[it["variant_index"] % max(1, len(providers or [provider or "native"]))]
            run_type = f"{candidate_type}_candidate"
            idem = _idempotency_key(job_id=job_id, run_type=run_type, candidate_id=cid, attempt=attempt)

            req = {
                "candidate_type": candidate_type,
                "group_id": str(gid),
                "variant_index": int(it["variant_index"]),
                "attempt": attempt,
                "overrides": request_overrides or {},
                "seed": it.get("seed"),
            }
            meta = {
                "svc": "svc-music",
                "run_type": run_type,
                "candidate_id": str(cid),
                "candidate_type": candidate_type,
                "group_id": str(gid),
                "variant_index": int(it["variant_index"]),
                "attempt": attempt,
                "job_id": str(job_id),
                "project_id": str(project_id),
                "user_id": str(user_id),
            }
            run_id = await self.runs.enqueue(
                job_id=job_id,  # studio_jobs.id (same uuid)
                provider=str(chosen_provider),
                idempotency_key=idem,
                request_json=req,
                meta_json=meta,
            )
            await self.cands.update_candidate(candidate_id=cid, provider=str(chosen_provider), provider_run_id=run_id, status="queued")

        # Mark required_action “building candidates” (not yet action_required)
        await self._patch_job_computed(
            job_id=job_id,
            patch={
                "candidates": {
                    candidate_type: {
                        "group_id": str(gid),
                        "attempt": attempt,
                        "hitl": bool(hitl),
                        "count": int(count),
                    }
                },
                "required_action": None,
            },
        )
        return gid

    async def refresh_required_action(self, *, job_id: UUID) -> Dict[str, Any]:
        """
        Compute whether the job is waiting on parallel work or needs user action.
        """
        pool = await get_pool()
        job = await pool.fetchrow("select input_json from public.music_video_jobs where id=$1", job_id)
        if not job:
            return {"action_required": False}

        ij = _as_dict(job["input_json"])
        computed = _as_dict(ij.get("computed"))
        cstate = _as_dict(computed.get("candidates"))

        required_action = None
        for ctype in ("lyrics", "audio", "video"):
            info = _as_dict(cstate.get(ctype))
            gid = info.get("group_id")
            attempt = info.get("attempt")
            hitl = bool(info.get("hitl", True))
            if not gid or not attempt:
                continue

            rows = await self.cands.list(job_id=job_id, candidate_type=ctype, group_id=UUID(str(gid)), attempt=int(attempt))
            if not rows:
                continue

            terminal = [r for r in rows if str(r.get("status")) in ("succeeded","failed","chosen","discarded")]
            succeeded = [r for r in rows if str(r.get("status")) == "succeeded"]
            chosen = [r for r in rows if str(r.get("status")) == "chosen"]

            # If already chosen, no action needed for this stage
            if chosen:
                continue

            # Wait until all are terminal (or at least 1 succeeded and none running/queued if you want “faster action”)
            all_terminal = len(terminal) == len(rows)

            if hitl:
                if all_terminal and len(succeeded) > 0:
                    required_action = {
                        "type": f"select_{ctype}",
                        "candidate_type": ctype,
                        "group_id": str(gid),
                        "min_select": 1,
                        "max_select": 1,
                        "message": f"Select a {ctype} option to continue.",
                    }
                    break
                # not ready yet: still generating in parallel
                required_action = None
            else:
                # Autopilot: auto-choose best score once all done
                if all_terminal and len(succeeded) > 0:
                    best = self._pick_best(succeeded)
                    await self.choose_candidate(job_id=job_id, candidate_id=UUID(str(best["id"])))
                    continue

        await self._patch_job_computed(job_id=job_id, patch={"required_action": required_action})
        return {"action_required": bool(required_action), "required_action": required_action}

    def _pick_best(self, succeeded_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        def score(r: Dict[str, Any]) -> float:
            sj = r.get("score_json") or {}
            try:
                return float(sj.get("overall", 0.0))
            except Exception:
                return 0.0
        return sorted(succeeded_rows, key=score, reverse=True)[0]

    async def choose_candidate(self, *, job_id: UUID, candidate_id: UUID) -> Dict[str, Any]:
        chosen = await self.cands.choose_candidate(job_id=job_id, candidate_id=candidate_id)
        if not chosen:
            raise ValueError("candidate_not_found")

        ctype = str(chosen.get("candidate_type") or "")
        # Persist chosen id into computed
        await self._patch_job_computed(
            job_id=job_id,
            patch={
                f"chosen_{ctype}_candidate_id": str(candidate_id),
                "required_action": None,
            },
        )
        return chosen