from __future__ import annotations

import json
import os
import re
from typing import Any
from uuid import UUID

from langchain_openai import OpenAIEmbeddings

from df_contracts.v3.director import CreativeBrief
from desifaces_shared.v3.creation_context import build_creation_context
from desifaces_shared.v3.story_store import CanonicalStoryStore


def _lexical_websearch_query(text: str, *, max_terms: int = 16) -> str:
    """Create a tolerant OR query for long creative briefs.

    ``plainto_tsquery`` over a full paragraph effectively requires every lexical
    term to occur in the same knowledge chunk, which is too strict for RAG. Use a
    bounded set of distinct terms and PostgreSQL's safe websearch parser instead.
    Semantic retrieval remains preferred when embeddings are configured.
    """

    seen: set[str] = set()
    terms: list[str] = []
    for raw in re.findall(r"[A-Za-z0-9_]+", text):
        token = raw.lower()
        if len(token) < 3 or token in seen:
            continue
        seen.add(token)
        terms.append(token)
        if len(terms) >= max_terms:
            break
    if not terms:
        return '"creative"'
    return " OR ".join(f'"{term}"' for term in terms)


class HybridCreativeRetriever:
    """Ground Director reasoning in canonical creation state + creative RAG.

    Structured creation state is authoritative. Semantic/lexical knowledge is
    supplemental creative grounding and must never override identity, pricing,
    entitlements, safety, media ownership or provider capability data.
    """

    def __init__(self, pool) -> None:
        self._pool = pool
        self._story_store = CanonicalStoryStore()
        self._embedding_model = str(os.getenv("DF_DIRECTOR_EMBEDDING_MODEL") or "").strip()
        self._embeddings = OpenAIEmbeddings(model=self._embedding_model) if self._embedding_model else None

    async def _semantic_chunks(self, conn, query: str, *, limit: int = 8) -> list[dict[str, Any]]:
        if not self._embeddings:
            lexical_query = _lexical_websearch_query(query)
            rows = await conn.fetch(
                """
                select c.chunk_id,s.source_type,s.source_key,s.title,c.content,c.locale,c.tags,c.metadata_json
                from public.v3_creative_knowledge_chunks c
                join public.v3_creative_knowledge_sources s on s.source_id=c.source_id
                where c.is_active=true and s.is_active=true
                  and to_tsvector('simple',coalesce(s.title,'') || ' ' || c.content)
                      @@ websearch_to_tsquery('simple',$1)
                order by ts_rank(
                  to_tsvector('simple',coalesce(s.title,'') || ' ' || c.content),
                  websearch_to_tsquery('simple',$1)
                ) desc,
                c.created_at desc
                limit $2
                """,
                lexical_query,
                limit,
            )
        else:
            vector = await self._embeddings.aembed_query(query)
            vector_literal = "[" + ",".join(str(float(x)) for x in vector) + "]"
            rows = await conn.fetch(
                """
                select c.chunk_id,s.source_type,s.source_key,s.title,c.content,c.locale,c.tags,c.metadata_json,
                       (c.embedding <=> $1::vector) as distance
                from public.v3_creative_knowledge_chunks c
                join public.v3_creative_knowledge_sources s on s.source_id=c.source_id
                where c.is_active=true and s.is_active=true
                  and c.embedding is not null
                  and c.embedding_model=$2
                order by c.embedding <=> $1::vector
                limit $3
                """,
                vector_literal,
                self._embedding_model,
                limit,
            )
        return [
            {
                "ref": f"creative_chunk:{row['chunk_id']}",
                "source_type": row["source_type"],
                "source_key": row["source_key"],
                "title": row["title"],
                "content": row["content"],
                "locale": row["locale"],
                "tags": list(row["tags"] or ()),
                "metadata": dict(row["metadata_json"] or {}),
            }
            for row in rows
        ]

    async def retrieve(self, *, brief: CreativeBrief, state: dict[str, Any]) -> dict[str, Any]:
        account_raw = state.get("account_id")
        owner_raw = state.get("owner_user_id")
        thread_id = str(state.get("thread_id") or "").strip()
        if not account_raw or not owner_raw or not thread_id:
            raise RuntimeError("director_actor_and_thread_context_required")
        account_id = UUID(str(account_raw))
        owner_user_id = UUID(str(owner_raw))

        async with self._pool.acquire() as conn:
            structured: dict[str, Any] = {
                "account_id": str(account_id),
                "owner_user_id": str(owner_user_id),
                "thread_id": thread_id,
            }
            refs: list[str] = []

            if brief.story_id:
                graph = await self._story_store.get_story_graph(
                    conn,
                    story_id=brief.story_id,
                    account_id=account_id,
                )
                context = build_creation_context(
                    graph,
                    active_scene_id=brief.focus_scene_id,
                    active_participant_id=brief.focus_participant_id,
                    allowed_assistant_actions=("explain_creation", "edit_story", "edit_dialogue"),
                )
                structured["existing_creation"] = context.model_dump(mode="json")
                refs.append(f"story:{brief.story_id}")
            elif brief.project_id:
                project = await conn.fetchrow(
                    """
                    select project_id,account_id,owner_user_id,title,description,metadata_json
                    from public.v3_projects
                    where project_id=$1 and account_id=$2 and lifecycle_state='active'
                    """,
                    brief.project_id,
                    account_id,
                )
                if project:
                    participants = await conn.fetch(
                        """
                        select participant_id,participant_kind,display_name,description,default_locale,
                               primary_face_media_id,voice_profile_ref,voice_locale,persona_json,continuity_json
                        from public.v3_participants
                        where project_id=$1 and account_id=$2 and lifecycle_state='active'
                        order by created_at,participant_id
                        """,
                        brief.project_id,
                        account_id,
                    )
                    structured["existing_project"] = {
                        "project_id": str(project["project_id"]),
                        "title": project["title"],
                        "description": project["description"],
                        "participants": [
                            {
                                "participant_id": str(p["participant_id"]),
                                "kind": p["participant_kind"],
                                "display_name": p["display_name"],
                                "description": p["description"],
                                "locale": p["voice_locale"] or p["default_locale"],
                                "primary_face_media_id": str(p["primary_face_media_id"]) if p["primary_face_media_id"] else None,
                                "voice_profile_ref": p["voice_profile_ref"],
                                "persona": dict(p["persona_json"] or {}),
                                "continuity": dict(p["continuity_json"] or {}),
                            }
                            for p in participants
                        ],
                    }
                    refs.append(f"project:{brief.project_id}")

            knowledge = await self._semantic_chunks(conn, brief.text)
            refs.extend(item["ref"] for item in knowledge)

            await conn.execute(
                """
                insert into public.v3_director_retrieval_events(
                  account_id,project_id,story_id,thread_id,query_text,source_refs,result_count,metadata_json
                ) values($1,$2,$3,$4,$5,$6::text[],$7,$8::jsonb)
                """,
                account_id,
                brief.project_id,
                brief.story_id,
                thread_id,
                brief.text,
                refs,
                len(knowledge),
                json.dumps(
                    {
                        "mode": "hybrid",
                        "embedding_model": self._embedding_model or None,
                        "focus_scene_id": str(brief.focus_scene_id) if brief.focus_scene_id else None,
                        "focus_participant_id": str(brief.focus_participant_id) if brief.focus_participant_id else None,
                    }
                ),
            )

            return {
                "refs": refs,
                "structured": structured,
                "creative_knowledge": knowledge,
                "thread_id": thread_id,
            }
