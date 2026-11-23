"""Language-neutral localization approval contract.

Pure validation only. Locale/provider eligibility must come from
the platform capability registry, not a hardcoded language list.
"""

import hashlib
import json


class LocalizationContractError(ValueError):
    pass


def _canonical_turns(turns):
    if not isinstance(turns, (list, tuple)):
        raise LocalizationContractError("invalid_canonical_turns")

    result = []
    seen = set()

    for item in turns:
        if not isinstance(item, dict):
            raise LocalizationContractError("invalid_source_turn")

        row = {
            "turn_id": item.get("turn_id"),
            "speaker": item.get("speaker"),
            "source_text": item.get("source_text"),
        }

        if any(not isinstance(v, str) or not v.strip()
               for v in row.values()):
            raise LocalizationContractError("invalid_source_turn")

        if row["turn_id"] in seen:
            raise LocalizationContractError("duplicate_source_turn")

        seen.add(row["turn_id"])
        result.append(row)

    return result


def source_fingerprint(source_locale, canonical_turns):
    if not isinstance(source_locale, str) or not source_locale.strip():
        raise LocalizationContractError("missing_source_locale")

    payload = {
        "source_locale": source_locale,
        "turns": _canonical_turns(canonical_turns),
    }

    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False
    ).encode("utf-8")

    return hashlib.sha256(encoded).hexdigest()


def validate_approved_localization(
    *,
    source_locale,
    target_locale,
    canonical_turns,
    decision,
    is_pair_enabled,
):
    """Validate exact HITL approval for any configured locale pair."""

    if not isinstance(source_locale, str) or not source_locale.strip():
        raise LocalizationContractError("missing_source_locale")

    if not isinstance(target_locale, str) or not target_locale.strip():
        raise LocalizationContractError("missing_target_locale")

    if source_locale.lower() == target_locale.lower():
        raise LocalizationContractError("same_source_target_locale")

    if not is_pair_enabled(source_locale, target_locale):
        raise LocalizationContractError("unsupported_locale_pair")

    if not isinstance(decision, dict) or decision.get("approved") is not True:
        raise LocalizationContractError("approval_required")

    if decision.get("target_locale") != target_locale:
        raise LocalizationContractError("approval_locale_mismatch")

    expected = _canonical_turns(canonical_turns)

    fingerprint = source_fingerprint(source_locale, expected)

    if decision.get("source_fingerprint") != fingerprint:
        raise LocalizationContractError("stale_source_version")

    translated = decision.get("turns")

    if not isinstance(translated, list) or len(translated) != len(expected):
        raise LocalizationContractError("turn_count_mismatch")

    approved = []

    for item, original in zip(translated, expected):
        if not isinstance(item, dict):
            raise LocalizationContractError("invalid_approved_turn")

        for key in ("turn_id", "speaker", "source_text"):
            if item.get(key) != original[key]:
                raise LocalizationContractError("turn_mapping_mismatch")

        text = item.get("text")

        if not isinstance(text, str) or not text.strip():
            raise LocalizationContractError("empty_approved_text")

        approved.append({
            **original,
            "text": text,
        })

    return {
        "source_locale": source_locale,
        "target_locale": target_locale,
        "source_fingerprint": fingerprint,
        "turns": approved,
    }


def _field(value, name, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def canonical_turns_from_plan(plan):
    """Derive stable speech-turn mapping from the Director's plan."""

    scenes = _field(plan, "scenes")

    if not isinstance(scenes, (list, tuple)):
        raise LocalizationContractError("invalid_story_scenes")

    entries = []

    for scene in scenes:
        scene_no = _field(scene, "sequence")

        if type(scene_no) is not int or scene_no < 0:
            raise LocalizationContractError("invalid_scene_sequence")

        dialogue = _field(scene, "dialogue")

        if not isinstance(dialogue, (list, tuple)):
            raise LocalizationContractError("invalid_scene_dialogue")

        for turn in dialogue:
            kind = _field(turn, "kind")
            kind = _field(kind, "value", kind)

            if kind != "speech":
                continue

            turn_no = _field(turn, "sequence")

            if type(turn_no) is not int or turn_no < 0:
                raise LocalizationContractError("invalid_turn_sequence")

            speaker = _field(turn, "speaker_ref")
            source = _field(turn, "text")

            entries.append((
                scene_no,
                turn_no,
                {
                    "turn_id": f"{scene_no}:{turn_no}",
                    "speaker": str(speaker) if speaker is not None else "",
                    "source_text": source,
                },
            ))

    entries.sort(key=lambda item: (item[0], item[1]))

    if not entries:
        raise LocalizationContractError("no_speech_turns")

    return _canonical_turns([item[2] for item in entries])


def validate_director_resume(
    *,
    brief,
    plan,
    resume_payload,
    is_pair_enabled,
):
    """Validate a Director HITL resume for any enabled locale pair."""

    constraints = _field(brief, "constraints", {}) or {}

    if not isinstance(constraints, dict):
        raise LocalizationContractError("invalid_brief_constraints")

    source_locale = (
        constraints.get("source_dialogue_locale")
        or _field(brief, "locale")
    )

    target_locale = constraints.get("requested_output_locale")

    if not isinstance(resume_payload, dict):
        raise LocalizationContractError("invalid_resume_payload")

    canonical = canonical_turns_from_plan(plan)

    decision = {
        "approved": resume_payload.get("approved"),
        "target_locale": resume_payload.get("target_locale"),
        "source_fingerprint": resume_payload.get("source_fingerprint"),
        "turns": resume_payload.get("localized_turns"),
    }

    return validate_approved_localization(
        source_locale=source_locale,
        target_locale=target_locale,
        canonical_turns=canonical,
        decision=decision,
        is_pair_enabled=is_pair_enabled,
    )


def build_verified_director_compiler_context(
    *,
    brief,
    plan,
    resume_payload,
    is_pair_enabled,
    retrieved_context,
):
    """Build compiler input only after validating HITL and locale eligibility."""

    snapshot = validate_director_resume(
        brief=brief,
        plan=plan,
        resume_payload=resume_payload,
        is_pair_enabled=is_pair_enabled,
    )

    context = dict(retrieved_context or {})

    existing = context.get("_approved_hitl_localization")

    if existing is not None and existing != snapshot:
        raise LocalizationContractError(
            "conflicting_localization_snapshot"
        )

    context["_approved_hitl_localization"] = snapshot

    return context
