"""DEV-only provider translation. Does not change synthesis, wallets or billing."""
from __future__ import annotations
import json
import os
import re
import urllib.request
import uuid
import httpx


class DevTranslationRejected(RuntimeError):
    pass


def selected_for_dev(user_id: str | None) -> bool:
    if os.getenv("DF_DEV_PROVIDER_TRANSLATION_ENABLED", "").lower() != "true":
        return False
    if os.getenv("DF_RUNTIME_ENV", "").lower() not in {"dev", "development"}:
        raise DevTranslationRejected("dev_environment_required")
    # Environment flags and Docker hostnames are not trusted host identity.
    # Azure Instance Metadata Service binds the decision to the actual VM.
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(
            "http://169.254.169.254/metadata/instance/compute?api-version=2021-02-01",
            headers={"Metadata": "true"},
        )
        with opener.open(request, timeout=2) as response:
            vm = json.load(response)
        trusted = (
            vm.get("name") == "desifaces-dev"
            and str(vm.get("resourceGroupName", "")).lower() == "rg-desifaces-dev"
            and str(vm.get("subscriptionId", "")).lower()
            == "e70463a7-4016-4cf5-9291-b640b0f0f5cb"
        )
    except Exception as exc:
        raise DevTranslationRejected("dev_host_attestation_required") from exc
    if not trusted:
        raise DevTranslationRejected("dev_host_attestation_required")
    try:
        identity = str(uuid.UUID(str(user_id)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise DevTranslationRejected("invalid_authenticated_user_id") from exc
    allowed = {
        v.strip().lower() for v in os.getenv("DF_DEV_TRANSLATION_USER_IDS", "").split(",")
        if v.strip()
    }
    return identity.lower() in allowed


async def _detect_auto_source_language(text: str) -> str:
    """Resolve UI 'auto' to an ISO 639-1 language via independent detection.

    Never infer geographic dialect, approve a provider, or default to English.
    Routing still requires a unique enabled source locale and approved DB edge.
    """
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise DevTranslationRejected("auto_source_detection_credential_missing")
    if not text.strip() or len(text) > 1000:
        raise DevTranslationRejected("invalid_text_length")
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await client.post(
                os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/responses",
                headers={"Authorization": "Bearer " + key},
                json={
                    "model": os.getenv("DF_DEV_TRANSLATION_QA_MODEL", "gpt-4.1-mini"),
                    "instructions": (
                        "Identify the language of the CURRENT spoken utterance only, "
                        "not the requested output language or surrounding dialogue. "
                        "Return JSON ONLY with language_code, a lowercase ISO 639-1 "
                        "two-letter code, or null if uncertain, mixed or ambiguous. "
                        "Never assume English from context or invent a regional locale."
                    ),
                    "input": json.dumps({"utterance": text}, ensure_ascii=False),
                    "max_output_tokens": 100,
                },
            )
            response.raise_for_status()
            body = response.json()
            output = body.get("output_text") or "\\n".join(
                c.get("text", "") for o in body.get("output", []) if isinstance(o, dict)
                for c in o.get("content", []) if isinstance(c, dict)
            )
            detected = json.loads(output).get("language_code")
        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
            raise DevTranslationRejected("auto_source_detection_failed") from exc
    if not isinstance(detected, str) or not re.fullmatch(r"[a-z]{2}", detected):
        raise DevTranslationRejected("auto_source_language_ambiguous")
    return detected


async def translate_approved(
    pool, *, text: str, source_locale: str, target_locale: str,
    speaker_gender: str, original_context: dict,
) -> tuple[str, str, str]:
    """Approved directional DB row required. No automatic provider fallback."""
    if str(source_locale or "").strip().lower() == "auto":
        source_locale = await _detect_auto_source_language(text)
    # Existing-schema translation contract: stored only in target locale
    # meta_json.translation_routing_v1. No new table and no implicit fallback.
    records = await pool.fetch(
        """SELECT locale, language_code, meta_json
           FROM public.tts_locales
           WHERE is_enabled AND (
             locale=$2 OR locale=$1 OR
             (strpos($1,'-')=0 AND lower(language_code)=lower($1))
           )""",
        source_locale, target_locale,
    )
    source_locales = {
        r["locale"] for r in records
        if r["locale"] == source_locale or (
            "-" not in source_locale and
            str(r["language_code"] or "").lower() == source_locale.lower()
        )
    }
    targets = [r for r in records if r["locale"] == target_locale]
    if len(targets) != 1 or not source_locales:
        raise DevTranslationRejected("no_approved_translation_direction")
    metadata = targets[0]["meta_json"] or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError as exc:
            raise DevTranslationRejected("invalid_translation_metadata") from exc
    routing = metadata.get("translation_routing_v1") if isinstance(metadata, dict) else None
    if not isinstance(routing, dict) or routing.get("enabled") is not True:
        raise DevTranslationRejected("no_approved_translation_direction")
    entries = routing.get("capabilities")
    if not isinstance(entries, list):
        raise DevTranslationRejected("invalid_translation_metadata")
    candidates = []
    for c in entries:
        if not isinstance(c, dict) or c.get("approved") is not True or c.get("enabled") is not True:
            continue
        if c.get("source_locale") not in source_locales or c.get("target_locale") != target_locale:
            continue
        if not all(isinstance(c.get(k), str) and c[k].strip() for k in (
            "provider", "model", "provider_source_code", "provider_target_code"
        )):
            continue
        candidates.append(c)
    if not candidates:
        raise DevTranslationRejected("no_approved_translation_direction")
    exact = [c for c in candidates if c["source_locale"] == source_locale]
    pool_candidates = exact or candidates
    if len({c["source_locale"] for c in pool_candidates}) != 1:
        raise DevTranslationRejected("ambiguous_source_locale")
    if len(pool_candidates) != 1:
        raise DevTranslationRejected("ambiguous_translation_route")
    c = pool_candidates[0]
    row = dict(provider_code=c["provider"], model_code=c["model"],
               provider_source_code=c["provider_source_code"],
               provider_target_code=c["provider_target_code"])
    provider, model = row["provider_code"], row["model_code"]
    if not text.strip() or len(text) > 1000:
        raise DevTranslationRejected("invalid_text_length")
    # Preserve early fail-closed credential validation before attempting imports
    # or external calls. Provider adapters also validate credentials.
    if provider == "sarvam" and not os.getenv("SARVAM_API_KEY", "").strip():
        raise DevTranslationRejected("sarvam_credential_missing")
    if provider in {"azure", "azure_translator"} and not (
        os.getenv("AZURE_TRANSLATOR_KEY", "").strip()
        and os.getenv("AZURE_TRANSLATOR_REGION", "").strip()
    ):
        raise DevTranslationRejected("azure_translator_credential_missing")
    from app.services.dev_translation_provider_adapters import (
        translate_via_configured_provider,
    )
    full_script = str(original_context.get("full_audio_script") or "")[:16000]
    # The contextual pipeline is required; legacy direct translation must not
    # bypass its independent QA when provider-first is enabled.
    from app.services.dev_contextual_translation_gate import (
        contextual_translation_with_review, Review, TranslationReviewRequired,
    )
    async with httpx.AsyncClient(timeout=30) as client:
        async def responses_json(instructions, value, tokens=600):
            review_key = os.getenv("OPENAI_API_KEY", "").strip()
            if not review_key:
                raise DevTranslationRejected("independent_qa_credential_missing")
            request = {
                "model": os.getenv("DF_DEV_TRANSLATION_QA_MODEL", "gpt-4.1-mini"),
                "instructions": instructions,
                "input": json.dumps(value, ensure_ascii=False),
                "max_output_tokens": max(600, tokens),
                "text": {"format": {"type": "json_object"}},
            }
            # One bounded retry for malformed/incomplete JSON or transient
            # upstream failures. Never synthesize approval or skip review.
            for attempt in range(2):
                try:
                    response = await client.post(
                        os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/responses",
                        headers={"Authorization": "Bearer " + review_key},
                        json=request,
                    )
                    response.raise_for_status()
                    body = response.json()
                    if body.get("status") == "incomplete":
                        raise DevTranslationRejected("incomplete_qa_response")
                    output = body.get("output_text") or "\n".join(
                        item.get("text", "")
                        for entry in body.get("output", []) if isinstance(entry, dict)
                        for item in entry.get("content", []) if isinstance(item, dict)
                        and item.get("type") == "output_text"
                    )
                    if not isinstance(output, str) or not output.strip():
                        raise DevTranslationRejected("empty_qa_response")
                    parsed = json.loads(output)
                    if not isinstance(parsed, dict):
                        raise DevTranslationRejected("invalid_qa_response_shape")
                    return parsed
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code not in {408, 429, 500, 502, 503, 504} or attempt:
                        raise
                except (httpx.TimeoutException, httpx.TransportError,
                        json.JSONDecodeError, DevTranslationRejected):
                    if attempt:
                        raise
            raise DevTranslationRejected("independent_qa_unavailable")

        async def clarify(original, previous, next_turn):
            answer = await responses_json(
                "You are preparing ONE speaker's LINE OF DIRECT DIALOGUE for translation. "
                "Use the full script ONLY to disambiguate meaning, relationship, emotion, "
                "pronouns and register. Output a SPEAKABLE English rewrite of CURRENT TURN ONLY. "
                "Preserve first/second person, speaker perspective, and all factual claims. "
                "NEVER describe the speaker, summarize the plot, quote other turns, "
                "add labels such as 'Speaker 1', or refer to the dialogue in third person. "
                "For example, 'I'm excited about the venture' can be clarified as "
                "'I'm excited about our new business venture', NEVER 'Speaker 1 is excited'. "
                "Keep the length comparable to the current line. Maintain exact brand spelling "
                "desifaces and do not add new claims. Return JSON ONLY with key meaning.",
                {"current": original, "previous": previous, "next": next_turn,
                 "full_audio_script": full_script, "speaker_relationship": original_context.get("speaker_relationship", ""),
                 "emotion": original_context.get("emotion", ""), "target_locale": target_locale,
                 "speaker_gender": speaker_gender, "scene": original_context.get("scene_context", "")},
            )
            meaning = answer.get("meaning") if isinstance(answer, dict) else None
            if not isinstance(meaning, str) or not meaning.strip():
                raise DevTranslationRejected("invalid_meaning_clarification")
            # Defense in depth: a line must not become an English plot synopsis.
            summary_prefix = r"^\\s*(?:speaker\\s*\\d+|the speaker|speaker one|speaker two|character\\s*\\d+|the character)\\b"
            if re.search(summary_prefix, meaning, flags=re.IGNORECASE):
                raise DevTranslationRejected("narrative_instead_of_dialogue")
            if len(meaning) > max(250, len(original) * 2):
                raise DevTranslationRejected("meaning_expansion_not_dialogue")
            return meaning

        async def provider_translate(meaning):
            return await translate_via_configured_provider(
                client, route=row, text=meaning, speaker_gender=speaker_gender,
            )

        async def independently_review(original, candidate, previous, next_turn, meaning):
            # Regional register is a separate veto: a generic bilingual PASS
            # cannot certify Egyptian colloquial Arabic or idiomatic Odia.
            regional_requirements = {
                "hi-IN": "Evaluate natural everyday SPOKEN Hindi for an Indian audience. "
                         "Reject transliterated English sentences such as 'next Friday open', "
                         "'that's exciting', 'good shape', 'first fifty customers', "
                         "'neighborhood share', or 'absolutely' when clear idiomatic Hindi "
                         "is available. Familiar established loanwords such as cafe, "
                         "coffee, menu, and offer are acceptable where natural. Preserve "
                         "who does what, all numbers, deadlines, offers, and chronology; "
                         "in particular distinguish 'I will start telling people today' "
                         "from 'I will start next Friday'. Reject even fluent Hindi "
                         "if timing or speaker intent changes.",
                "or-IN": "Evaluate natural, current spoken Odia. Reject mechanical English "
                         "calques and excessive untranslated English when natural Odia wording "
                         "is available; allow familiar local loanwords.",
                "ar-EG": "Evaluate Egyptian COLLOQUIAL Arabic as spoken in Egypt, not formal "
                         "Modern Standard Arabic. Reject formal newsreader constructions.",
                "es-MX": "Evaluate conversational Mexican Spanish (es-MX), not an unnatural "
                         "literal translation or Spain-only regional vocabulary.",
            }
            requirement = regional_requirements.get(target_locale,
                "Evaluate the locale's authentic conversational register.")
            regional = await responses_json(
                "Act as a strict independent regional DIALOGUE reviewer. "
                "Do not rewrite, translate or summarize. The candidate must sound like "
                "one person's real spoken line in the requested regional variety. "
                "Return JSON ONLY with approved boolean and specific reason; if unsure reject. "
                + requirement,
                {"original": original, "candidate": candidate, "target_locale": target_locale,
                 "context": full_script, "speaker_gender": speaker_gender},
            )
            if not isinstance(regional, dict) or regional.get("approved") is not True:
                return Review(False, "regional_spoken_language_rejected: " +
                              str(regional.get("reason", "invalid_region_review")
                                  if isinstance(regional, dict) else "invalid_region_review")[:250])

            answer = await responses_json(
                "You are a STRICT independent bilingual reviewer of ONE character's "
                "SPEAKABLE dialogue. Compare the ORIGINAL English current utterance with "
                "the translated candidate using full-script context ONLY to understand it. "
                "REJECT if candidate narrates or summarizes what speakers said, refers to "
                "'Speaker 1', 'Speaker 2', 'the speaker', or other characters rather than "
                "speaking their current line in the original first/second-person perspective. "
                "REJECT if candidate combines other dialogue turns, includes story exposition, "
                "omits original details, invents new facts, changes desifaces brand spelling, "
                "uses stiff literal calques, or is unnaturally verbose. "
                 "For ar-EG require natural Egyptian conversational register; for es-MX "
                "require natural Mexican conversational register. For or-IN require natural "
                "conversational Odia, not an English-heavy scripted summary. "
                "If ambiguous or uncertain, reject. Return JSON ONLY with approved boolean "
                "and concrete reason; do not silently repair or generate replacement text.",
                {"original": original, "candidate": candidate, "meaning": meaning,
                 "full_audio_script": full_script, "speaker_relationship": original_context.get("speaker_relationship", ""),
                 "emotion": original_context.get("emotion", ""),
                 "previous": previous, "next": next_turn, "gender": speaker_gender,
                 "target_locale": target_locale},
            )
            # Structural checks cannot replace linguistic review but must prevent
            # obvious scene recaps and corruption even if reviewer approves them.
            if len(candidate) > max(450, len(original) * 3):
                return Review(False, "translation_expands_into_scene_summary")
            if "desifaces" in original and "desifaces" not in candidate:
                return Review(False, "brand_name_not_preserved")
            if not isinstance(answer, dict) or answer.get("approved") is not True:
                return Review(False, str(answer.get("reason", "rejected")) if isinstance(answer, dict) else "invalid_review")
            return Review(True, str(answer.get("reason", "approved")))

        async def repair_target_speech(original, candidate, meaning, reason):
            answer = await responses_json(
                "Rewrite ONLY the provided target-language candidate as one natural "
                "spoken utterance in the specified locale. Fix regional register "
                "without changing the speaker, original English meaning, facts, "
                "brand spelling desifaces, or adding narration. Preserve first-person "
                "voice and the approximate duration. This is a proposed correction, "
                "NOT an approval: independent semantic and regional QA will rerun. "
                "Return JSON only with key 'text'.",
                {"original": original, "meaning": meaning, "candidate": candidate,
                 "target_locale": target_locale, "regional_rejection": reason,
                 "full_audio_script": full_script, "speaker_gender": speaker_gender},
                tokens=500,
            )
            revised = answer.get("text") if isinstance(answer, dict) else None
            if not isinstance(revised, str) or not revised.strip():
                raise DevTranslationRejected("regional_repair_invalid_response")
            if len(revised) > max(450, len(original) * 3):
                raise DevTranslationRejected("regional_repair_expands_dialogue")
            return revised

        try:
            approved = await contextual_translation_with_review(
                original=text,
                previous_turn=str(original_context.get("previous_turn") or ""),
                next_turn=str(original_context.get("next_turn") or ""),
                clarify_meaning=clarify,
                provider_translate=provider_translate,
                independent_review=independently_review,
                max_repairs=2,
                repair_target_speech=repair_target_speech,
            )
        except (TranslationReviewRequired, httpx.HTTPError, ValueError, TypeError) as exc:
            raise DevTranslationRejected("TRANSLATION_REVIEW_REQUIRED") from exc
        return approved.text, provider, model
