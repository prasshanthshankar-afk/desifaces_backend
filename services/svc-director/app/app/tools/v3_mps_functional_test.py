from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import httpx
import jwt
from langchain_openai import OpenAIEmbeddings


BASE_URL = "http://127.0.0.1:8011"


def _required(name: str) -> str:
    value = str(os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"FUNCTIONAL_PRECHECK_FAIL={name}_missing")
    return value


def _access_token(user_id: UUID) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=20)).timestamp()),
        "iss": _required("JWT_ISSUER"),
        "aud": _required("JWT_AUDIENCE"),
        "token_type": "access",
        "functional_test": True,
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
        raise RuntimeError("FUNCTIONAL_PRECHECK_FAIL=no_active_user_account_context")
    return UUID(str(row["user_id"])), UUID(str(row["billing_account_id"]))


async def _seed_rag(conn: asyncpg.Connection, marker: str) -> tuple[UUID, UUID, str]:
    source_id = uuid4()
    chunk_id = uuid4()
    source_key = f"functional:{marker}"
    content = (
        f"{marker}. Creative continuity guidance for this functional test: "
        "keep every participant agentic and contemporary; geography may inform setting, "
        "but never infer religion, socioeconomic status, attire, skin tone, facial anatomy, "
        "occupation, or personality from geography. Preserve participant identity and voice "
        "continuity across scenes."
    )
    await conn.execute(
        """
        insert into public.v3_creative_knowledge_sources(
          source_id,source_type,source_key,title,revision,metadata_json
        ) values($1,'functional_test',$2,$3,'1',$4::jsonb)
        """,
        source_id,
        source_key,
        f"MPS functional test {marker}",
        json.dumps({"functional_test": True, "marker": marker}),
    )

    embedding_model = str(os.getenv("DF_DIRECTOR_EMBEDDING_MODEL") or "").strip()
    if embedding_model:
        vector = await OpenAIEmbeddings(model=embedding_model).aembed_query(content)
        vector_literal = "[" + ",".join(str(float(x)) for x in vector) + "]"
        await conn.execute(
            """
            insert into public.v3_creative_knowledge_chunks(
              chunk_id,source_id,sequence_no,content,tags,embedding,embedding_model,metadata_json
            ) values($1,$2,0,$3,$4::text[],$5::vector,$6,$7::jsonb)
            """,
            chunk_id,
            source_id,
            content,
            ["functional-test", "continuity", "non-stereotype"],
            vector_literal,
            embedding_model,
            json.dumps({"functional_test": True}),
        )
    else:
        await conn.execute(
            """
            insert into public.v3_creative_knowledge_chunks(
              chunk_id,source_id,sequence_no,content,tags,metadata_json
            ) values($1,$2,0,$3,$4::text[],$5::jsonb)
            """,
            chunk_id,
            source_id,
            content,
            ["functional-test", "continuity", "non-stereotype"],
            json.dumps({"functional_test": True}),
        )
    return source_id, chunk_id, content


async def _cleanup(
    conn: asyncpg.Connection,
    *,
    source_id: UUID | None,
    story_id: UUID | None,
    project_id: UUID | None,
    thread_id: str | None,
) -> None:
    # Story first removes scene/dialogue/story memberships. Project then removes
    # participants. No Face/Audio/Fusion generation is created by this test.
    if story_id:
        await conn.execute("delete from public.v3_stories where story_id=$1", story_id)
    if project_id:
        await conn.execute("delete from public.v3_projects where project_id=$1", project_id)
    if source_id:
        await conn.execute("delete from public.v3_creative_knowledge_sources where source_id=$1", source_id)
    if thread_id:
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            exists = await conn.fetchval("select to_regclass($1)", f"public.{table}")
            if exists:
                await conn.execute(f"delete from public.{table} where thread_id=$1", thread_id)


async def main() -> None:
    database_url = _required("DATABASE_URL")
    _required("OPENAI_API_KEY")
    llm_model = _required("DF_DIRECTOR_LLM_MODEL")

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=180.0) as client:
        health = (await client.get("/api/health")).json()
    if not health.get("ok") or not health.get("runtime_ready") or not health.get("llm_configured"):
        raise RuntimeError(f"FUNCTIONAL_PRECHECK_FAIL=director_runtime_not_ready:{health}")
    if not health.get("review_required"):
        raise RuntimeError("FUNCTIONAL_PRECHECK_FAIL=director_review_required_must_be_true")
    print(f"FUNCTIONAL_DIRECTOR_RUNTIME=PASS:model={llm_model}:embedding={bool(health.get('embedding_configured'))}")

    conn = await asyncpg.connect(database_url)
    source_id: UUID | None = None
    story_id: UUID | None = None
    project_id: UUID | None = None
    thread_id: str | None = None
    try:
        user_id, account_id = await _active_actor(conn)
        token = _access_token(user_id)
        headers = {"Authorization": f"Bearer {token}"}
        print("FUNCTIONAL_AUTH_ACCOUNT_CONTEXT=PASS")

        marker = f"MPS-FT-{uuid4().hex[:12]}"
        source_id, chunk_id, _ = await _seed_rag(conn, marker)
        expected_ref = f"creative_chunk:{chunk_id}"
        print("FUNCTIONAL_TEMP_RAG_SEED=PASS")

        brief = {
            "text": (
                f"{marker}. Create exactly two participants named Ananya and Ravi in a warm, contemporary, "
                "non-stereotyped two-scene story set in Chennai. Ananya is Ravi's adult daughter. They discuss "
                "whether to restore their ancestral house as a community arts space. Both participants must appear "
                "in both scenes and each must speak at least once in each scene. Preserve their identity and voice "
                "continuity across scenes. Do not infer religion, attire, physical traits, wealth, occupation, or "
                "personality from geography. Return exactly two scenes."
            ),
            "locale": "en-IN",
            "desired_scene_count": 2,
            "participant_hints": [
                {"display_name": "Ananya", "role": "daughter"},
                {"display_name": "Ravi", "role": "father"},
            ],
            "constraints": {
                "participant_count": 2,
                "scene_count": 2,
                "all_participants_in_every_scene": True,
                "each_participant_speaks_in_every_scene": True,
            },
        }

        async with httpx.AsyncClient(base_url=BASE_URL, timeout=240.0, headers=headers) as client:
            create = await client.post("/api/director/runs", json=brief)
            if create.status_code != 200:
                raise RuntimeError(f"FUNCTIONAL_FAIL=create_run:{create.status_code}:{create.text[:1000]}")
            initial = create.json()
            thread_id = str(initial["thread_id"])
            if initial.get("state") != "awaiting_review" or not initial.get("interrupt"):
                raise RuntimeError(f"FUNCTIONAL_FAIL=expected_human_review:{initial}")
            print("FUNCTIONAL_LANGGRAPH_HITL_PAUSE=PASS")

            interrupt_plan = dict(initial["interrupt"].get("plan") or {})
            planned_names = {str(p.get("display_name")) for p in interrupt_plan.get("participants", [])}
            if planned_names != {"Ananya", "Ravi"}:
                raise RuntimeError(f"FUNCTIONAL_FAIL=planned_participants:{sorted(planned_names)}")
            if len(interrupt_plan.get("scenes", [])) != 2:
                raise RuntimeError(f"FUNCTIONAL_FAIL=planned_scene_count:{len(interrupt_plan.get('scenes', []))}")
            print("FUNCTIONAL_LIVE_LLM_STRUCTURED_PLAN=PASS")

            polled = await client.get(f"/api/director/runs/{thread_id}")
            if polled.status_code != 200 or polled.json().get("state") != "awaiting_review":
                raise RuntimeError(f"FUNCTIONAL_FAIL=persisted_interrupt_poll:{polled.status_code}:{polled.text[:1000]}")
            print("FUNCTIONAL_POSTGRES_CHECKPOINT_POLL=PASS")

            event = await conn.fetchrow(
                """
                select source_refs,result_count
                from public.v3_director_retrieval_events
                where thread_id=$1 and account_id=$2
                order by created_at desc limit 1
                """,
                thread_id,
                account_id,
            )
            if not event or expected_ref not in set(event["source_refs"] or ()):
                raise RuntimeError(f"FUNCTIONAL_FAIL=rag_ref_not_retrieved:expected={expected_ref}:event={event}")
            print("FUNCTIONAL_HYBRID_RAG_RETRIEVAL=PASS")

            resume = await client.post(
                f"/api/director/runs/{thread_id}/resume",
                json={"approved": True, "feedback": "Approved for functional certification."},
            )
            if resume.status_code != 200:
                raise RuntimeError(f"FUNCTIONAL_FAIL=resume:{resume.status_code}:{resume.text[:1000]}")
            ready = resume.json()
            if ready.get("state") != "ready" or not ready.get("workspace") or not ready.get("assistant_context"):
                raise RuntimeError(f"FUNCTIONAL_FAIL=director_not_ready_after_resume:{ready}")
            print("FUNCTIONAL_LANGGRAPH_HITL_RESUME=PASS")

            workspace = ready["workspace"]
            assistant = ready["assistant_context"]
            story_id = UUID(str(workspace["story_id"]))
            project_id = UUID(str(workspace["project_id"]))
            if assistant.get("story_id") != workspace.get("story_id") or assistant.get("project_id") != workspace.get("project_id"):
                raise RuntimeError("FUNCTIONAL_FAIL=canonical_ui_assistant_identity_mismatch")

            participants = workspace.get("participants", [])
            scenes = workspace.get("scenes", [])
            names = {str(p.get("display_name")) for p in participants}
            if names != {"Ananya", "Ravi"} or len(participants) != 2:
                raise RuntimeError(f"FUNCTIONAL_FAIL=workspace_participants:{participants}")
            if len(scenes) != 2:
                raise RuntimeError(f"FUNCTIONAL_FAIL=workspace_scene_count:{len(scenes)}")
            participant_ids = {str(p["participant_id"]) for p in participants}
            for scene in scenes:
                if set(str(x) for x in scene.get("participant_ids", [])) != participant_ids:
                    raise RuntimeError(f"FUNCTIONAL_FAIL=scene_participant_membership:{scene}")
                speaker_ids = {
                    str(turn["speaker_participant_id"])
                    for turn in scene.get("dialogue", [])
                    if turn.get("speaker_participant_id")
                }
                if speaker_ids != participant_ids:
                    raise RuntimeError(f"FUNCTIONAL_FAIL=scene_speaker_coverage:{scene}")
            print("FUNCTIONAL_STORY_WORKSPACE_2P_2SCENE=PASS")

            workspace_get = await client.get(
                f"/api/director/stories/{story_id}/workspace",
                params={"active_scene_id": scenes[0]["scene_id"]},
            )
            if workspace_get.status_code != 200 or workspace_get.json().get("active_scene_id") != scenes[0]["scene_id"]:
                raise RuntimeError(f"FUNCTIONAL_FAIL=workspace_api:{workspace_get.status_code}:{workspace_get.text[:1000]}")
            print("FUNCTIONAL_UI_WORKSPACE_API=PASS")

            focus_participant = participants[0]
            scoped = await client.get(
                f"/api/director/stories/{story_id}/assistant-context",
                params={
                    "scene_id": scenes[0]["scene_id"],
                    "participant_id": focus_participant["participant_id"],
                },
            )
            if scoped.status_code != 200:
                raise RuntimeError(f"FUNCTIONAL_FAIL=assistant_context_api:{scoped.status_code}:{scoped.text[:1000]}")
            context = scoped.json()
            if context.get("context_scope") != "scene_participant":
                raise RuntimeError(f"FUNCTIONAL_FAIL=assistant_context_scope:{context.get('context_scope')}")
            if context.get("active_scene_id") != scenes[0]["scene_id"]:
                raise RuntimeError("FUNCTIONAL_FAIL=assistant_context_scene_focus")
            if context.get("active_participant_id") != focus_participant["participant_id"]:
                raise RuntimeError("FUNCTIONAL_FAIL=assistant_context_participant_focus")
            if len(context.get("scene_context", [])) != 1 or len(context.get("participant_context", [])) != 2:
                # Scene+participant scope retains the complete scene cast for coherent dialogue grounding,
                # while active_participant_id marks the conversational focus.
                raise RuntimeError(f"FUNCTIONAL_FAIL=assistant_context_shape:{context}")
            if {item.get("scene_id") for item in context.get("dialogue_context", [])} != {scenes[0]["scene_id"]}:
                raise RuntimeError("FUNCTIONAL_FAIL=assistant_context_dialogue_not_scene_scoped")
            print("FUNCTIONAL_ASSISTANT_CONTEXT_SCOPING=PASS")

            story_count = await conn.fetchval(
                "select count(*) from public.v3_stories where story_id=$1 and account_id=$2",
                story_id,
                account_id,
            )
            if int(story_count or 0) != 1:
                raise RuntimeError("FUNCTIONAL_FAIL=canonical_story_not_persisted")
            print("FUNCTIONAL_CANONICAL_STORY_PERSISTENCE=PASS")

        print("V3_MPS_CREATIVE_DIRECTOR_FUNCTIONAL_TEST=PASS")
    finally:
        try:
            await _cleanup(
                conn,
                source_id=source_id,
                story_id=story_id,
                project_id=project_id,
                thread_id=thread_id,
            )
            print("FUNCTIONAL_TEST_CLEANUP=PASS")
        finally:
            await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
