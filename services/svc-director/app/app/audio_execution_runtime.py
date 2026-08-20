"""Runtime patch for participant-level Audio Studio language/voice selection.

The canonical dialogue-turn locale remains the source language of the written
story. ``v3_participants.voice_locale`` is the user-selected target speech
locale. When the base languages differ, svc-audio receives an explicit
translation request before synthesis. This preserves canonical story text while
letting one durable character voice profile drive every dialogue turn.
"""

from __future__ import annotations

from . import audio_execution as _audio_execution

_original_compile_context_audio_input = _audio_execution.compile_context_audio_input


def _base_language(locale: str | None) -> str:
    return str(locale or "").strip().replace("_", "-").split("-", 1)[0].lower()


def compile_context_audio_input(context: _audio_execution.AudioStageContext) -> dict:
    studio_input = dict(_original_compile_context_audio_input(context))

    # load_audio_stage_context resolves dialogue-turn locale first, so before any
    # voice-locale override it is the durable language of the authored dialogue.
    source_locale = str(context.target_locale or "").strip()
    target_locale = str(context.voice_locale or source_locale).strip()

    if not source_locale:
        raise _audio_execution.ParticipantAudioBridgeError("audio_source_locale_required")
    if not target_locale:
        raise _audio_execution.ParticipantAudioBridgeError("audio_target_locale_required")

    studio_input["source_language"] = source_locale
    studio_input["target_locale"] = target_locale
    studio_input["voice_locale"] = target_locale
    studio_input["translate"] = _base_language(source_locale) != _base_language(target_locale)
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
