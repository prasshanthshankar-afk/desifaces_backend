from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import httpx
import jwt

from df_contracts.v3.director import PlannedParticipant
from desifaces_shared.v3.studio_workflow_store import CanonicalStudioWorkflowStore

from app.db import close_pools, open_business_pool
from app.participant_face import (
    FaceStudioClient,
    ParticipantFaceBinder,
    ParticipantFaceBridgeError,
    compile_participant_face_studio_input,
)

DIRECTOR_BASE = os.getenv("MPS2_DIRECTOR_BASE", "http://127.0.0.1:8011").rstrip("/")
FACE_BASE = os.getenv("MPS2_FACE_BASE", "http://svc-face:8003").rstrip("/")
PROOF_DIR = Path(os.getenv("MPS2_PROOF_DIR", "/tmp/v3_mps2_visual_proof"))


def _required(name: str) -> str:
    value = str(os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"MPS2_VISUAL_PRECHECK_FAIL={name}_missing")
    return value


def _token(user_id: UUID) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(user_id),
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=45)).timestamp()),
            "iss": _required("JWT_ISSUER"),
            "aud": _required("JWT_AUDIENCE"),
            "token_type": "access",
            "mps2_visual_proof": True,
        },
        _required("JWT_SECRET"),
        algorithm=_required("JWT_ALG"),
    )


def _write(name: str, value: Any) -> None:
    PROOF_DIR.mkdir(parents=True, exist_ok=True)
    (PROOF_DIR / name).write_text(
        json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "participant"


def _redact_url(value: str) -> str:
    parts = urlsplit(value or "")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")) if value else ""


def _sanitized_status(payload: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(payload, default=str))
    for item in list(out.get("variants") or []):
        if isinstance(item, dict) and item.get("image_url"):
            item["image_url"] = _redact_url(str(item["image_url"]))
    return out


def _approved(env_name: str, prompt: str) -> bool:
    value = str(os.getenv(env_name) or "").strip().lower()
    if value in {"1", "true", "yes", "y"}:
        return True
    if value in {"0", "false", "no", "n"}:
        return False
    try:
        return input(prompt).strip().lower() in {"y", "yes"}
    except EOFError:
        return False


async def _active_actor(pool) -> tuple[UUID, UUID]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """select bam.user_id,bam.billing_account_id
            from public.pricing_billing_account_members bam
            join public.pricing_billing_accounts ba on ba.id=bam.billing_account_id
            join core.users u on u.id=bam.user_id
            where bam.status='active' and ba.status='active'
            order by bam.is_default desc,
              case bam.role when 'owner' then 0 when 'finance_admin' then 1 else 2 end,
              bam.created_at asc limit 1"""
        )
    if not row:
        raise RuntimeError("MPS2_VISUAL_PRECHECK_FAIL=no_active_user_account_context")
    return UUID(str(row["user_id"])), UUID(str(row["billing_account_id"]))


async def _poll_director(client: httpx.AsyncClient, thread_id: str, target: set[str], timeout_s: int = 900) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    previous = None
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = await client.get(f"/api/director/runs/{thread_id}")
        if response.status_code != 200:
            raise RuntimeError(f"MPS2_VISUAL_FAIL=director_poll:{response.status_code}:{response.text[:1000]}")
        latest = response.json()
        state = str(latest.get("state") or "")
        if state != previous:
            print(f"MPS2_DIRECTOR_STATE={state}", flush=True)
            previous = state
        if state == "failed":
            raise RuntimeError(f"MPS2_VISUAL_FAIL=director_failed:{latest.get('errors')}")
        if state in target:
            return latest
        await asyncio.sleep(2)
    raise RuntimeError(f"MPS2_VISUAL_FAIL=director_timeout:last={latest}")


async def _download(url: str, path: Path) -> None:
    async with httpx.AsyncClient(timeout=90, follow_redirects=True) as client:
        response = await client.get(url)
    response.raise_for_status()
    path.write_bytes(response.content)


async def main() -> None:
    _required("OPENAI_API_KEY")
    director_model = _required("DF_DIRECTOR_LLM_MODEL")
    face_model = str(os.getenv("MPS2_FACE_MODEL_RESOLVED") or "").strip()
    if face_model != "gpt-image-2":
        raise RuntimeError(f"MPS2_VISUAL_PRECHECK_FAIL=face_model_not_gpt-image-2:{face_model}")

    if PROOF_DIR.exists():
        shutil.rmtree(PROOF_DIR)
    PROOF_DIR.mkdir(parents=True, exist_ok=True)

    pool = await open_business_pool()
    try:
        user_id, account_id = await _active_actor(pool)
        headers = {"Authorization": f"Bearer {_token(user_id)}"}
        print(f"MPS2_VISUAL_RUNTIME=PASS:director_model={director_model}:face_model={face_model}")
        print("MPS2_VISUAL_AUTH_ACCOUNT_CONTEXT=PASS")

        brief = {
            "text": (
                "Create a warm, contemporary two-person story about Ananya, a 35-year-old woman, and her father Ravi, "
                "a 65-year-old man, discussing whether to restore their ancestral house as a community arts space. "
                "Set it in Chennai, but treat Chennai only as setting: do not infer ethnicity, skin tone, religion, attire, "
                "occupation, socioeconomic status, facial anatomy, or personality from geography. Create two distinct, "
                "photorealistic recurring character identities. Both participants must appear and speak."
            ),
            "locale": "en-IN",
            "desired_scene_count": 1,
            "participant_hints": [
                {"display_name": "Ananya", "role": "daughter", "gender": "female", "age": 35},
                {"display_name": "Ravi", "role": "father", "gender": "male", "age": 65},
            ],
            "constraints": {
                "participant_count": 2,
                "scene_count": 1,
                "distinct_character_identities": True,
                "photorealistic_identity_references": True,
                "no_geographic_appearance_inference": True,
            },
        }
        _write("01_director_intent.json", brief)
        hints = {str(x["display_name"]): dict(x) for x in brief["participant_hints"]}

        async with httpx.AsyncClient(base_url=DIRECTOR_BASE, headers=headers, timeout=30) as director:
            create = await director.post("/api/director/runs", json=brief)
            if create.status_code != 202:
                raise RuntimeError(f"MPS2_VISUAL_FAIL=director_create:{create.status_code}:{create.text[:1200]}")
            thread_id = str(create.json()["thread_id"])
            print(f"MPS2_DIRECTOR_THREAD_ID={thread_id}")
            print("MPS2_INTENT_TO_DIRECTOR=PASS")

            review = await _poll_director(director, thread_id, {"awaiting_review"})
            interrupt = dict(review.get("interrupt") or {})
            plan = dict(interrupt.get("plan") or {})
            critique = dict(interrupt.get("critique") or {})
            _write("02_director_generative_plan.json", plan)
            _write("03_director_critique.json", critique)

            planned = list(plan.get("participants") or [])
            names = {str(x.get("display_name") or "") for x in planned}
            if names != {"Ananya", "Ravi"}:
                raise RuntimeError(f"MPS2_VISUAL_FAIL=director_participants:{sorted(names)}")

            print("\n================ SVC-DIRECTOR GENERATIVE AI OUTPUT ================")
            print(json.dumps({
                "intent": brief,
                "story": {"title": plan.get("title"), "summary": plan.get("summary")},
                "participants": planned,
                "critique": critique,
                "retrieved_context_refs": plan.get("retrieved_context_refs"),
            }, indent=2, ensure_ascii=False, default=str))
            print("====================================================================\n")
            print("MPS2_DIRECTOR_GENERATIVE_OUTPUT_VISIBLE=PASS")

            if not _approved("MPS2_DIRECTOR_PLAN_APPROVED", "Approve this Director plan? [y/N]: "):
                print("MPS2_VISUAL_STOP=director_plan_not_approved")
                return

            resume = await director.post(
                f"/api/director/runs/{thread_id}/resume",
                json={"approved": True, "feedback": "Approved for MPS2 visual Face proof."},
            )
            if resume.status_code != 202:
                raise RuntimeError(f"MPS2_VISUAL_FAIL=director_resume:{resume.status_code}:{resume.text[:1200]}")
            ready = await _poll_director(director, thread_id, {"ready"})
            workspace = dict(ready.get("workspace") or {})
            story_id = UUID(str(workspace["story_id"]))
            project_id = UUID(str(workspace["project_id"]))
            _write("04_story_workspace_before_faces.json", workspace)
            print("MPS2_DIRECTOR_PLAN_COMPILED=PASS")

            workflow_response = await director.post(f"/api/director/stories/{story_id}/studio-workflows")
            if workflow_response.status_code != 201:
                raise RuntimeError(f"MPS2_VISUAL_FAIL=studio_workflow:{workflow_response.status_code}:{workflow_response.text[:1200]}")
            studio = workflow_response.json()
            workflow_id = UUID(str(studio["workflow_id"]))
            face_stage_by_participant = {
                str(s["participant_id"]): UUID(str(s["stage_run_id"]))
                for s in list(studio.get("stages") or []) if s.get("stage_type") == "face"
            }
            if len(face_stage_by_participant) != 2:
                raise RuntimeError(f"MPS2_VISUAL_FAIL=face_stage_count:{len(face_stage_by_participant)}")
            print("MPS2_TWO_PARTICIPANT_FACE_STAGES=PASS")

            plan_by_name = {
                str(x["display_name"]): PlannedParticipant.model_validate(x) for x in planned
            }
            workspace_by_name = {
                str(x["display_name"]): dict(x) for x in list(workspace.get("participants") or [])
            }
            face_client = FaceStudioClient(base_url=FACE_BASE)
            requests: list[dict[str, Any]] = []

            for name in ("Ananya", "Ravi"):
                participant_id = UUID(str(workspace_by_name[name]["participant_id"]))
                stage_run_id = face_stage_by_participant[str(participant_id)]
                studio_input = compile_participant_face_studio_input(
                    participant=plan_by_name[name], participant_hint=hints[name], num_variants=1,
                )
                pricing = await face_client.preview_pricing(headers=headers, studio_input=studio_input)
                requests.append({
                    "name": name,
                    "participant_id": participant_id,
                    "stage_run_id": stage_run_id,
                    "studio_input": studio_input,
                    "pricing": pricing,
                })
                _write(f"05_{_slug(name)}_face_request.json", studio_input)
                _write(f"06_{_slug(name)}_pricing_preview.json", pricing)

            print("\n================ DIRECTOR -> FACE STUDIO REQUESTS ================")
            for item in requests:
                pricing = item["pricing"]
                print(f"\n[{item['name']}] participant_id={item['participant_id']}")
                print("Face prompt:")
                print(item["studio_input"]["user_prompt"])
                print("Pricing preview:")
                print(json.dumps({
                    "quote_id": pricing.get("quote_id"),
                    "pricing": pricing.get("pricing"),
                    "balance": pricing.get("balance"),
                    "summary": pricing.get("summary"),
                }, indent=2, ensure_ascii=False, default=str))
            print("==================================================================\n")
            print("MPS2_DIRECTOR_TO_FACE_REQUESTS_VISIBLE=PASS")

            if not _approved(
                "MPS2_FACE_GENERATION_APPROVED",
                "Approve the displayed pricing and generate exactly 2 gpt-image-2 Face candidates? [y/N]: ",
            ):
                print("MPS2_VISUAL_STOP=face_generation_not_approved")
                return

            store = CanonicalStudioWorkflowStore()
            binder = ParticipantFaceBinder(store=store)
            generated: list[dict[str, Any]] = []

            for item in requests:
                name = str(item["name"])
                participant_id = UUID(str(item["participant_id"]))
                stage_run_id = UUID(str(item["stage_run_id"]))
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        await store.mark_generating(conn, stage_run_id=stage_run_id)

                print(f"MPS2_FACE_GENERATION_START={name}")
                try:
                    result = await face_client.generate_one(
                        headers=headers,
                        studio_input=item["studio_input"],
                        pricing_preview=item["pricing"],
                        timeout_seconds=600,
                        state_callback=lambda state, n=name: print(f"MPS2_FACE_STATE={n}:{state}", flush=True),
                    )
                except Exception:
                    async with pool.acquire() as conn:
                        await conn.execute(
                            "update public.v3_studio_stage_runs set state='failed',updated_at=now() where stage_run_id=$1",
                            stage_run_id,
                        )
                    raise

                async with pool.acquire() as conn:
                    async with conn.transaction():
                        review_item_id = await binder.bind_generated_face(
                            conn,
                            account_id=account_id,
                            participant_id=participant_id,
                            stage_run_id=stage_run_id,
                            media_asset_id=result.media_asset_id,
                            face_job_id=result.job_id,
                            face_profile_id=result.face_profile_id,
                            prompt_used=result.prompt_used,
                        )

                image_path = PROOF_DIR / f"07_{_slug(name)}_face.png"
                await _download(result.image_url, image_path)
                _write(f"08_{_slug(name)}_face_status.json", _sanitized_status(result.status_payload))
                generated.append({
                    "name": name,
                    "participant_id": str(participant_id),
                    "face_stage_run_id": str(stage_run_id),
                    "review_item_id": str(review_item_id),
                    "face_job_id": result.job_id,
                    "face_profile_id": result.face_profile_id,
                    "media_asset_id": str(result.media_asset_id),
                    "image_path": str(image_path),
                    "storage_url_redacted": _redact_url(result.image_url),
                    "prompt_used": result.prompt_used,
                    "review_state": "pending",
                })
                print(
                    f"MPS2_FACE_GENERATED={name}:media_asset_id={result.media_asset_id}:"
                    f"review_item_id={review_item_id}:review=pending"
                )

            after_workspace = await director.get(f"/api/director/stories/{story_id}/workspace")
            after_workflow = await director.get(f"/api/director/studio-workflows/{workflow_id}")
            if after_workspace.status_code != 200 or after_workflow.status_code != 200:
                raise RuntimeError("MPS2_VISUAL_FAIL=post_face_projection")
            ws = after_workspace.json()
            wf = after_workflow.json()
            _write("09_story_workspace_after_faces.json", ws)
            _write("10_studio_workflow_after_faces.json", wf)

            generated_ids = {x["media_asset_id"] for x in generated}
            primaries = {
                str(x.get("primary_face_media_id"))
                for x in list(ws.get("participants") or []) if x.get("primary_face_media_id")
            }
            if primaries & generated_ids:
                raise RuntimeError("MPS2_VISUAL_FAIL=unapproved_face_promoted_to_primary")

            face_stages = [x for x in list(wf.get("stages") or []) if x.get("stage_type") == "face"]
            if len(face_stages) != 2 or any(x.get("state") != "awaiting_review" for x in face_stages):
                raise RuntimeError(f"MPS2_VISUAL_FAIL=face_stages_not_awaiting_review:{face_stages}")
            if any(
                not any(r.get("decision") == "pending" for r in list(stage.get("reviews") or []))
                for stage in face_stages
            ):
                raise RuntimeError("MPS2_VISUAL_FAIL=face_review_not_pending")

            async with pool.acquire() as conn:
                candidate_count = await conn.fetchval(
                    """select count(*) from public.v3_participant_media
                    where media_id=any($1::uuid[]) and relation='reference_face'""",
                    [UUID(x) for x in generated_ids],
                )
            if int(candidate_count or 0) != 2:
                raise RuntimeError(f"MPS2_VISUAL_FAIL=candidate_binding_count:{candidate_count}")

            manifest = {
                "proof_type": "v3_mps2_director_to_two_face_candidates",
                "director_thread_id": thread_id,
                "director_model": director_model,
                "face_model": face_model,
                "account_id": str(account_id),
                "project_id": str(project_id),
                "story_id": str(story_id),
                "studio_workflow_id": str(workflow_id),
                "faces": generated,
                "canonical_identity_state": "not_promoted_until_face_hitl_approval",
                "next_required_action": (
                    "Inspect both PNGs. Approve or revise each Face review item. "
                    "Only approved Face stages may unblock Audio."
                ),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            _write("manifest.json", manifest)

            print("\n================ MPS2 VISUAL OUTPUT ================")
            print(f"Director intent: {PROOF_DIR / '01_director_intent.json'}")
            print(f"Generative plan: {PROOF_DIR / '02_director_generative_plan.json'}")
            for face in generated:
                print(f"{face['name']} candidate: {face['image_path']}")
                print(f"  media_asset_id={face['media_asset_id']}")
                print(f"  review_item_id={face['review_item_id']}  PENDING HUMAN REVIEW")
            print(f"Manifest: {PROOF_DIR / 'manifest.json'}")
            print("====================================================\n")
            print("MPS2_INTENT_TO_GENERATIVE_PLAN=PASS")
            print("MPS2_GENERATIVE_PLAN_TO_TWO_FACE_REQUESTS=PASS")
            print("MPS2_REAL_GPT_IMAGE_2_TWO_FACES=PASS")
            print("MPS2_PARTICIPANT_FACE_CANDIDATE_BINDING=PASS")
            print("MPS2_UNAPPROVED_FACE_NOT_PRIMARY=PASS")
            print("MPS2_FACE_HITL_PENDING_REVIEW=PASS")
            print("V3_MPS2_VISUAL_FACE_PROOF=PASS")
    finally:
        await close_pools()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ParticipantFaceBridgeError as exc:
        raise SystemExit(f"MPS2_VISUAL_FACE_BRIDGE_FAIL={exc}") from exc
