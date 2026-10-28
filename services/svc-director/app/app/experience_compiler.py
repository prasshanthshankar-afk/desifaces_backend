from __future__ import annotations

import json
from typing import Any

from df_contracts.v3.director import CreativeBrief, CreativeStoryPlan, PlannedParticipant
from df_contracts.v3.story import StoryGraph

from .compiler import CanonicalStoryCompiler
from .localization_source_contract import validate_english_source


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _normalize_face_gender(value: Any) -> str | None:
    raw = _clean(value).casefold().replace("_", "-")
    if raw in {"male", "man", "boy", "masculine"}:
        return "male"
    if raw in {"female", "woman", "girl", "feminine"}:
        return "female"
    return None


def _first(source: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = _clean(source.get(key))
        if value:
            return value
    return ""


def _explicit_constraints_from_plan(item: PlannedParticipant) -> dict[str, Any]:
    """Recover explicit user/context facts carried by the validated Creative Plan.

    The Creative Director system prompt is the policy boundary: age/gender presentation
    may be present in the plan only when explicitly supplied by the user/context and
    must never be inferred from name, geography, locale or relationship role.

    This compiler layer prevents those explicit facts from being dropped merely
    because the client did not also construct participant_hints.
    """
    persona = dict(item.persona or {})
    visual = dict(item.visual_direction or {})

    gender = _normalize_face_gender(
        _first(persona, "gender", "gender_presentation", "sex")
        or _first(visual, "gender", "gender_presentation")
    )
    age = _first(persona, "age", "age_range", "age_presentation") or _first(
        visual, "age", "age_range", "age_presentation"
    )

    out: dict[str, Any] = {}
    if gender:
        out["gender"] = gender
    if age:
        out["age"] = age
    return out


class UserOwnedStoryCompiler(CanonicalStoryCompiler):
    """Canonical compiler plus user-owned intent preservation.

    The base compiler remains authoritative for IDs, tenancy and relational Story
    state. This additive layer backfills explicit Face constraints already carried by
    the validated Creative Plan so ordinary users are not forced to enter the same
    fact twice through a technical participant_hints structure.
    """

    async def compile(
        self,
        *,
        brief: CreativeBrief,
        plan: CreativeStoryPlan,
        retrieved_context: dict[str, Any],
    ) -> StoryGraph:
        # Canonical source language is English for this DEV en-US -> hi-IN path.
        # Never treat a Hindi Director draft as the English original.
        requested_locale = str(brief.constraints.get("requested_output_locale") or "")
        if requested_locale == "hi-IN":
            if str(brief.locale or "").lower() not in {"en-us", "en"}:
                raise ValueError("canonical_source_must_be_english")
            for scene in plan.scenes:
                for turn in scene.dialogue:
                    if str(getattr(turn.kind, "value", turn.kind)) != "speech":
                        continue
                    devanagari = sum("\\u0900" <= ch <= "\\u097f" for ch in turn.text)
                    if devanagari >= 3:
                        raise ValueError("director_source_contains_hindi_not_english")
            approved_snapshot = retrieved_context.get("_approved_hitl_localization")
            if not isinstance(approved_snapshot, dict) or approved_snapshot.get("target_locale") != "hi-IN":
                raise ValueError("hindi_hitl_approval_required_before_story_creation")
        # Validate the COMPLETE HITL snapshot before any canonical story writes.
        localization = retrieved_context.get("_approved_hitl_localization")
        if localization:
            if localization.get("target_locale") != "hi-IN":
                raise ValueError("unsupported_hitl_localization_locale")
            approved = localization.get("turns")
            expected = [
                (f"{scene.sequence}:{turn.sequence}", str(turn.speaker_ref), turn.text)
                for scene in plan.scenes for turn in scene.dialogue
                if str(getattr(turn.kind, "value", turn.kind)) == "speech"
            ]
            expected.sort(key=lambda entry: tuple(map(int, entry[0].split(":"))))
            if not isinstance(approved, list) or len(approved) != len(expected):
                raise ValueError("approved_hitl_turn_count_mismatch")
            for item, (key, speaker, source_text) in zip(approved, expected):
                validate_english_source(source_text)
                if (not isinstance(item, dict)
                    or item.get("turn_id") != key
                    or item.get("speaker") != speaker
                    or item.get("source_text") != source_text
                    or not isinstance(item.get("text"), str)
                    or not item["text"].strip()):
                    raise ValueError("approved_hitl_precompile_mismatch")
        graph = await super().compile(
            brief=brief,
            plan=plan,
            retrieved_context=retrieved_context,
        )

        planned_by_name = {item.display_name: item for item in plan.participants}
        changed = False

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                for participant in graph.participants:
                    planned = planned_by_name.get(participant.display_name)
                    if planned is None:
                        continue
                    recovered = _explicit_constraints_from_plan(planned)
                    if not recovered:
                        continue

                    row = await conn.fetchrow(
                        """
                        select metadata_json,persona_json
                        from public.v3_participants
                        where participant_id=$1 and account_id=$2 and project_id=$3
                        for update
                        """,
                        participant.participant_id,
                        graph.project.account_id,
                        graph.project.project_id,
                    )
                    if not row:
                        continue

                    metadata = dict(row["metadata_json"] or {})
                    explicit = dict(metadata.get("explicit_face_constraints") or {})
                    persona = dict(row["persona_json"] or {})
                    participant_changed = False

                    if recovered.get("gender") and not _clean(explicit.get("gender")):
                        explicit["gender"] = recovered["gender"]
                        if not _clean(persona.get("gender_presentation")):
                            persona["gender_presentation"] = recovered["gender"]
                        participant_changed = True
                    if recovered.get("age") and not (
                        _clean(explicit.get("age"))
                        or _clean(explicit.get("age_range"))
                        or _clean(explicit.get("age_presentation"))
                    ):
                        explicit["age"] = recovered["age"]
                        if not _clean(persona.get("age")):
                            persona["age"] = recovered["age"]
                        participant_changed = True

                    if participant_changed:
                        metadata["explicit_face_constraints"] = explicit
                        provenance = dict(metadata.get("production_provenance") or {})
                        provenance["explicit_face_constraints"] = "creative_plan_user_context"
                        metadata["production_provenance"] = provenance
                        await conn.execute(
                            """
                            update public.v3_participants
                            set metadata_json=$2::jsonb,persona_json=$3::jsonb,updated_at=now()
                            where participant_id=$1
                            """,
                            participant.participant_id,
                            json.dumps(metadata, ensure_ascii=False),
                            json.dumps(persona, ensure_ascii=False),
                        )
                        changed = True

        if not changed:
            return graph

        async with self._pool.acquire() as conn:
            return await self._store.get_story_graph(
                conn,
                story_id=graph.story.story_id,
                account_id=graph.project.account_id,
            )


__all__ = ["UserOwnedStoryCompiler"]
