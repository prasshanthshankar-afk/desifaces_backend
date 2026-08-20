"""Runtime patch for participant-level Audio Studio language/voice selection.

The canonical dialogue-turn locale remains the source language of the written
story. ``v3_participants.voice_locale`` is the user-selected target speech
locale. When the base languages differ, svc-audio receives an explicit
translation request before synthesis. This preserves canonical story text while
letting one durable character voice profile drive every dialogue turn.
"""

from __future__ import annotations

import json
from typing import Any

from . import audio_execution as _audio_execution

_original_compile_context_audio_input = _audio_execution.compile_context_audio_input


def _base_language(locale: str | None) -> str:
    return str(locale or "").strip().replace("_", "-").split("-", 1)[0].lower()


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    try:
        return dict(value)
    except Exception:
        return {}


def compile_context_audio_input(context: _audio_execution.AudioStageContext) -> dict:
    # Multi-person Story Audio must never silently synthesize with a provider
    # default voice. The user explicitly chooses one durable language + voice
    # profile for each speaking participant before pricing/generation.
    if not str(context.voice_profile_ref or "").strip() or not str(context.voice_locale or "").strip():
        raise _audio_execution.ParticipantAudioBridgeError(
            f"audio_participant_voice_profile_required:{context.participant_id}"
        )

    studio_input = dict(_original_compile_context_audio_input(context))

    # load_audio_stage_context resolves dialogue-turn locale first, so before any
    # voice-locale override it is the durable language of the authored dialogue.
    source_locale = str(context.target_locale or "").strip()
    target_locale = str(context.voice_locale or "").strip()

    if not source_locale:
        raise _audio_execution.ParticipantAudioBridgeError("audio_source_locale_required")
    if not target_locale:
        raise _audio_execution.ParticipantAudioBridgeError("audio_target_locale_required")

    studio_input["voice_id"] = str(context.voice_profile_ref).strip()
    studio_input["source_language"] = source_locale
    studio_input["target_locale"] = target_locale
    studio_input["voice_locale"] = target_locale
    studio_input["translate"] = _base_language(source_locale) != _base_language(target_locale)

    # Explicit participant-level delivery choices override Director-authored
    # defaults for every dialogue turn. Only fields already supported by
    # svc-audio are forwarded.
    delivery = _as_dict(context.participant_metadata.get("audio_delivery"))
    for key in ("style", "style_degree", "rate", "pitch", "volume", "translation_tone"):
        value = delivery.get(key)
        if value is not None and str(value).strip() != "":
            studio_input[key] = value

    return studio_input


# ParticipantAudioExecutionService calls the module-level compile helper during
# both pricing preview and dispatch. Patch that boundary once during Director
# runtime assembly; no provider/pricing implementation is changed.
_audio_execution.compile_context_audio_input = compile_context_audio_input

ParticipantAudioBridgeError = _audio_execution.ParticipantAudioBridgeError
ParticipantAudioExecutionService = _audio_execution.ParticipantAudioExecutionService
AudioStageContext = _audio_execution.AudioStageContext

__all__ = [
    "AudioStageContext",
    "ParticipantAudioBridgeError",
    "ParticipantAudioExecutionService",
    "compile_context_audio_input",
]
