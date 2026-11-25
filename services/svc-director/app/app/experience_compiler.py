from __future__ import annotations

import json
from typing import Any

from df_contracts.v3.director import CreativeBrief, CreativeStoryPlan, PlannedParticipant
from df_contracts.v3.story import StoryGraph

from .compiler import CanonicalStoryCompiler
from .localization_source_contract import validate_english_source
from desifaces_shared.v3.english_source_policy import requires_translation
from desifaces_shared.v3.translation_route_contract import (
    TranslationRouteRejected,
    resolve_approved_translation_route,
)


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
        # A localization must match the requested source, target and
        # complete canonical dialogue before any Story writes occur.
        constraints = brief.constraints or {}
        source_locale = str(
            constraints.get("source_dialogue_locale")
            or brief.locale or ""
        ).strip()
        target_locale = str(
            constraints.get("requested_output_locale") or ""
        ).strip()
        if target_locale and (
            not source_locale or source_locale.casefold() == "auto"
        ):
            raise ValueError("localization_source_locale_required")

        needs_localization = requires_translation(source_locale, target_locale)
        localization = retrieved_context.get(
            "_approved_hitl_localization"
        )

        if needs_localization:
            if (
                not isinstance(localization, dict)
                or localization.get("target_locale") != target_locale
            ):
                raise ValueError(
                    "localization_approval_required_before_story_creation"
                )
            if (
                localization.get("source_locale")
                and localization["source_locale"] != source_locale
            ):
                raise ValueError("approved_hitl_source_locale_mismatch")

            approved = localization.get("turns")
            expected = [
                (
                    f"{scene.sequence}:{turn.sequence}",
                    str(turn.speaker_ref),
                    turn.text,
                )
                for scene in plan.scenes
                for turn in scene.dialogue
                if str(getattr(turn.kind, "value", turn.kind))
                == "speech"
            ]
            expected.sort(
                key=lambda row: tuple(
                    map(int, row[0].split(":"))
                )
            )
            if (
                not expected
                or not isinstance(approved, list)
                or len(approved) != len(expected)
            ):
                raise ValueError("approved_hitl_turn_count_mismatch")

            for item, (key, speaker, source_text) in zip(
                approved, expected
            ):
                if (
                    not isinstance(item, dict)
                    or item.get("turn_id") != key
                    or item.get("speaker") != speaker
                    or item.get("source_text") != source_text
                    or not isinstance(item.get("text"), str)
                    or not item["text"].strip()
                ):
                    raise ValueError("approved_hitl_precompile_mismatch")

                # Preserve the existing English-source safety check.
                # Additional source-language quality certification is
                # a separate provider/policy responsibility.
                if source_locale.split("-", 1)[0].casefold() == "en":
                    validate_english_source(source_text)

            # Defense in depth: recheck the DB direction immediately
            # before entering canonical persistence.
            async with self._pool.acquire() as conn:
                try:
                    await resolve_approved_translation_route(
                        conn,
                        source_locale=source_locale,
                        target_locale=target_locale,
                    )
                except TranslationRouteRejected as exc:
                    raise ValueError(
                        "translation_direction_not_approved_before_story_creation"
                    ) from exc
        elif localization:
            raise ValueError("unrequested_hitl_localization")

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
