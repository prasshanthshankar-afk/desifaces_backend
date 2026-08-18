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

import asyncpg
import httpx
import jwt

from df_contracts.v3.director import PlannedParticipant
from desifaces_shared.v3.studio_workflow_store import CanonicalStudioWorkflowStore

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


def _access_token(user_id: UUID) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=45)).timestamp()),
        "iss": _required("JWT_ISSUER"),
        "aud": _required("JWT_AUDIENCE"),
        "token_type": "access",
        "mps2_visual_proof": True,
    }
    return jwt.encode(payload, _required("JWT_SECRET"), algorithm=_required("JWT_ALG"))


async def _active_actor(conn: asyncpg.Connection) -> tuple[UUID, UUID]:
    row = await conn.fetchrow(
        """
        select bam.user_id, bam.billing_account_id
        from public.pricing_billing_account_members bam
        join public.pricing_billing_accounts ba on ba.id=bam.billing_account_id
        join core.users u on u.id=bam.user_id
        where bam.status='active' and ba.status='active'
        order by bam.is_default desc,
                 case bam.role when 'owner' then 0 when 'finance_admin' then 1 else 2 end,
                 bam.created_at asc
        limit 1
        """
    )
    if not row:
        raise RuntimeError("MPS2_VISUAL_PRECHECK_FAIL=no_active_user_account_context")
    return UUID(str(row["user_id"])), UUID(str(row["billing_account_id"]))


def _write_json(name: str, value: Any) -> None:
    PROOF_DIR.mkdir(parents=True, exist_ok=True)
    (PROOF_DIR / name).write_text(
        json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )


def _safe_name(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "participant"


def _redact_url(value: str) -> str:
    if not value:
        return ""
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _sanitize_status(payload: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(payload, default=str))
    for variant in list(out.get("variants") or []):
        if isinstance(variant, dict) and variant.get("image_url"):
            variant["image_url"] = _redact_url(str(variant["image_url"]))
    return out


def _approved_by_env_or_prompt(env_name: str, prompt: str) -> bool:
    value = str(os.getenv(env_name) or "").strip().lower()
    if value in {"1", "true", "yes", "y"}:
        return True
    if value in {"0", "false", "no", "n"}:
        return False
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


async def _poll_director(client: httpx.AsyncClient, thread_id: str, target: set[str], timeout_s: int = 900) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last_state = None
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = await client.get(f"/api/director/runs/{thread_id}")
        if response.status_code != 200:
            raise RuntimeError(f"MPS2_VISUAL_FAIL=director_poll:{response.status_code}:{response.text[:1000]}")
        last = response.json()
        state = str(last.get("state") or "")
        if state != last_state:
            print(f"MPS2_DIRECTOR_STATE={state}", flush=True)
            last_state = state
        if state == "failed":
            raise RuntimeError(f"MPS2_VISUAL_FAIL=director_failed:{last.get('errors')}")
        if state in target:
            return last
        await asyncio.sleep(2.0)
    raise RuntimeError(f"MPS2_VISUAL_FAIL=director_timeout:last={last}")


def _participant_hints_by_name(brief: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("display_name") or "").strip(): dict(item)
        for item in list(brief.get("participant_hints") or [])
        if str(item.get("display_name") or "").strip()
    }


async def _download_image(url: str, path: Path) -> None:
    async with httpx.AsyncClient(timeout=90.0, follow_redirects=True) as client:
        response = await client.get(url)
    response.raise_for_status()
    path.write_bytes(response.content)


async def main() -> None:
    database_url = _required("DATABASE_URL")
    _required("OPENAI_API_KEY")
    llm_model = _required("DF_DIRECTOR_LLM_MODEL")
    face_model = str(os.getenv("MPS2_FACE_MODEL_RESOLVED") or "gpt-image-2").strip()
    if face_model != "gpt-image-2":
        raise RuntimeError(f"MPS2_VISUAL_PRECHECK_FAIL=face_model_must_be_gpt-image-2:actual={face_model}")

    if PROOF_DIR.exists():
        shutil.rmtree(PROOF_DIR)
    PROOF_DIR.mkdir(parents=True, exist_ok=True)

    conn = await asyncpg.connect(database_url)
    try:
        user_id, account_id = await _active_actor(conn)
        headers = {"Authorization": f"Bearer {_access_token(user_id)}"}
        print(f"MPS2_VISUAL_RUNTIME=PASS:director_model={llm_model}:face_model={face_model}")
        print("MPS2_VISUAL_AUTH_ACCOUNT_CONTEXT=PASS")

        brief = {
            "text": (
                "Create a warm, contemporary two-person story about Ananya, a 35-year-old woman, and her father Ravi, "
                "a 65-year-old man, discussing whether to restore their ancestral house as a community arts space. "
                "Set the story in Chennai, but treat Chennai only as the setting: do not infer ethnicity, skin tone, religion, "
                "attire, occupation, socioeconomic status, facial anatomy, or personality from geography. Create two distinct, "
                "photorealistic character identities suitable for recurring cinematic scenes. Both participants must appear and speak."
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
        _write_json("01_director_intent.json", brief)

        async with httpx.AsyncClient(base_url=DIRECTOR_BASE, headers=headers, timeout=30.0) as director:
            create = await director.post("/api/director/runs", json=brief)
            if create.status_code != 202:
                raise RuntimeError(f"MPS2_VISUAL_FAIL=director_create:{create.status_code}:{create.text[:1200]}")
            queued = create.json()
            thread_id = str(queued["thread_id"])
            print(f"MPS2_DIRECTOR_THREAD_ID={thread_id}")
            print("MPS2_INTENT_TO_DIRECTOR=PASS")

            review = await _poll_director(director, thread_id, {"awaiting_review"})
            interrupt = dict(review.get("interrupt") or {})
            plan_raw = dict(interrupt.get("plan") or {})
            critique_raw = dict(interrupt.get("critique") or {})
            _write_json("02_director_generative_plan.json", plan_raw)
            _write_json("03_director_critique.json", critique_raw)

            participant_plan = list(plan_raw.get("participants") or [])
            names = [str(item.get("display_name") or "") for item in participant_plan]
            if names != ["Ananya", "Ravi"] and set(names) != {"Ananya", "Ravi"}:
                raise RuntimeError(f"MPS2_VISUAL_FAIL=unexpected_participants:{names}")

            print("\n================ DIRECTOR GENERATIVE AI OUTPUT ================")
            print(json.dumps({
                "title": plan_raw.get("title"),
                "summary": plan_raw.get("summary"),
                "participants": participant_plan,
                "critique": critique_raw,
            }, indent=2, ensure_ascii=False, default=str))
            print("===============================================================\n")
            print("MPS2_DIRECTOR_GENERATIVE_OUTPUT_VISIBLE=PASS")

            if not _approved_by_env_or_prompt(
                "MPS2_DIRECTOR_PLAN_APPROVED",
                "Approve this Director plan and compile it into the canonical Story? [y/N]: ",
            ):
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
            _write_json("04_story_workspace_before_faces.json", workspace)
            print("MPS2_DIRECTOR_PLAN_COMPILED=PASS")

            studio_response = await director.post(f"/api/director/stories/{story_id}/studio-workflows")
            if studio_response.status_code != 201:
                raise RuntimeError(
                    f"MPS2_VISUAL_FAIL=studio_workflow:{studio_response.status_code}:{studio_response.text[:1200]}"
                )
            studio = studio_response.json()
            workflow_id = UUID(str(studio["workflow_id"]))
            face_stage_by_participant = {
                str(stage["participant_id"]): UUID(str(stage["stage_run_id"]))
                for stage in list(studio.get("stages") or [])
                if stage.get("stage_type") == "face"
            }
            print("MPS2_TWO_PARTICIPANT_FACE_STAGES=PASS")

            hints = _participant_hints_by_name(brief)
            plan_by_name = {
                str(item.get("display_name")): PlannedParticipant.model_validate(item)
                for item in participant_plan
            }
            workspace_by_name = {
                str(item.get("display_name")): dict(item)
                for item in list(workspace.get("participants") or [])
            }

            face_client = FaceStudioClient(base_url=FACE_BASE)
            compiled: list[dict[str, Any]] = []
            for name in ("Ananya", "Ravi"):
                planned = plan_by_name[name]
                participant_view = workspace_by_name[name]
                participant_id = UUID(str(participant_view["participant_id"]))
                stage_run_id = face_stage_by_participant[str(participant_id)]
                studio_input = compile_participant_face_studio_input(
                    participant=planned,
                    participant_hint=hints.get(name),
                    language="en",
                    num_variants=1,
                )
                preview = await face_client.preview_pricing(headers=headers, studio_input=studio_input)
                compiled.append({
                    "name": name,
                    "participant_id": participant_id,
                    "stage_run_id": stage_run_id,
                    "studio_input": studio_input,
                    "pricing_preview": preview,
                })
                _write_json(f"05_{_safe_name(name)}_face_request.json", studio_input)
                _write_json(f"06_{_safe_name(name)}_pricing_preview.json", preview)

            print("\n================ FACE STUDIO REQUESTS ================")
            for item in compiled:
                preview = item["pricing_preview"]
                print(f"\n[{item['name']}] participant_id={item['participant_id']}")
                print(f"Face prompt:\n{item['studio_input']['user_prompt']}")
                print("Pricing:")
                print(json.dumps({
                    "quote_id": preview.get("quote_id"),
                    "pricing": preview.get("pricing"),
                    "balance": preview.get("balance"),
                    "summary": preview.get("summary"),
                }, indent=2, ensure_ascii=False, default=str))
            print("======================================================\n")
            print("MPS2_DIRECTOR_TO_FACE_REQUESTS_VISIBLE=PASS")

            if not _approved_by_env_or_prompt(
                "MPS2_FACE_GENERATION_APPROVED",
                "Approve the displayed pricing and generate exactly 2 Face images with gpt-image-2? [y/N]: ",
            ):
                print("MPS2_VISUAL_STOP=face_generation_not_approved")
                return

            store = CanonicalStudioWorkflowStore()
            binder = ParticipantFaceBinder(store=store)
            generated: list[dict[str, Any]] = []

            for item in compiled:
                name = str(item["name"])
                participant_id = UUID(str(item["participant_id"]))
                stage_run_id = UUID(str(item["stage_run_id"]))

                async with conn.transaction():
                    await store.mark_generating(conn, stage_run_id=stage_run_id)

                print(f"MPS2_FACE_GENERATION_START={name}")
                try:
                    result = await face_client.generate_one(
                        headers=headers,
                        studio_input=item["studio_input"],
                        pricing_preview=item["pricing_preview"],
                        timeout_seconds=600,
                        state_callback=lambda state, n=name: print(f"MPS2_FACE_STATE={n}:{state}", flush=True),
                    )
                except Exception:
                    await conn.execute(
                        "update public.v3_studio_stage_runs set state='failed',updated_at=now() where stage_run_id=$1",
                        stage_run_id,
                    )
                    raise

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

                local_image = PROOF_DIR / f"07_{_safe_name(name)}_face.png"
                await _download_image(result.image_url, local_image)
                _write_json(
                    f"08_{_safe_name(name)}_face_status.json",
                    _sanitize_status(result.status_payload),
                )
                generated.append({
                    "name": name,
                    "participant_id": str(participant_id),
                    "face_stage_run_id": str(stage_run_id),
                    "review_item_id": str(review_item_id),
                    "face_job_id": result.job_id,
                    "face_profile_id": result.face_profile_id,
                    "media_asset_id": str(result.media_asset_id),
                    "image_path": str(local_image),
                    "storage_url_redacted": _redact_url(result.image_url),
                    "prompt_used": result.prompt_used,
                    "review_state": "pending",
                })
                print(f"MPS2_FACE_GENERATED={name}:media_asset_id={result.media_asset_id}:review_item_id={review_item_id}")

            workspace_after = await director.get(f"/api/director/stories/{story_id}/workspace")
            if workspace_after.status_code != 200:
                raise RuntimeError(f"MPS2_VISUAL_FAIL=workspace_after_faces:{workspace_after.status_code}:{workspace_after.text[:1000]}")
            workspace_after_json = workspace_after.json()
            _write_json("09_story_workspace_after_faces.json", workspace_after_json)

            workflow_after = await director.get(f"/api/director/studio-workflows/{workflow_id}")
            if workflow_after.status_code != 200:
                raise RuntimeError(f"MPS2_VISUAL_FAIL=workflow_after_faces:{workflow_after.status_code}:{workflow_after.text[:1000]}")
            _write_json("10_studio_workflow_after_faces.json", workflow_after.json())

            primary_face_ids = {
                str(item.get("display_name")): str(item.get("primary_face_media_id") or "")
                for item in list(workspace_after_json.get("participants") or [])
            }
            if not primary_face_ids.get("Ananya") or not primary_face_ids.get("Ravi"):
                raise RuntimeError(f"MPS2_VISUAL_FAIL=participant_primary_faces_missing:{primary_face_ids}")

            manifest = {
                "proof_type": "v3_mps2_director_to_two_faces",
                "director_thread_id": thread_id,
                "director_model": llm_model,
                "face_model": face_model,
                "account_id": str(account_id),
                "project_id": str(project_id),
                "story_id": str(story_id),
                "studio_workflow_id": str(workflow_id),
                "intent_file": "01_director_intent.json",
                "generative_plan_file": "02_director_generative_plan.json",
                "critique_file": "03_director_critique.json",
                "faces": generated,
                "next_required_action": "Human must inspect and approve/revise each Face review item before Audio may start.",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            _write_json("manifest.json", manifest)

            print("\n================ MPS2 VISUAL PROOF OUTPUT ================")
            print(f"Director intent: {PROOF_DIR / '01_director_intent.json'}")
            print(f"Generative plan: {PROOF_DIR / '02_director_generative_plan.json'}")
            for face in generated:
                print(f"{face['name']} image: {face['image_path']}")
                print(f"  media_asset_id={face['media_asset_id']}")
                print(f"  review_item_id={face['review_item_id']} (PENDING HUMAN REVIEW)")
            print(f"Manifest: {PROOF_DIR / 'manifest.json'}")
            print("==========================================================\n")
            print("MPS2_INTENT_TO_GENERATIVE_PLAN=PASS")
            print("MPS2_GENERATIVE_PLAN_TO_TWO_FACE_REQUESTS=PASS")
            print("MPS2_REAL_GPT_IMAGE_2_TWO_FACES=PASS")
            print("MPS2_PARTICIPANT_PRIMARY_FACE_BINDING=PASS")
            print("MPS2_FACE_HITL_PENDING_REVIEW=PASS")
            print("V3_MPS2_VISUAL_FACE_PROOF=PASS")
    finally:
        await conn.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ParticipantFaceBridgeError as exc:
        raise SystemExit(f"MPS2_VISUAL_FACE_BRIDGE_FAIL={exc}") from exc
