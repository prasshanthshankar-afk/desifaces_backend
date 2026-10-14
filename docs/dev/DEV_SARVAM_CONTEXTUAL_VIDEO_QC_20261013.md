# DEV-only Sarvam + contextual dialogue + lip-sync certification

Status: **implementation workstream opened; NOT deployed, NOT certified**  
Date: 2026-10-13  
Baseline branch: main  
Isolation: DEV desifaces-dev only; PROD desifaces-gpu must remain unchanged.

## Existing capabilities — reuse, do not duplicate
- `services/svc-audio/app/app/services/sarvam_tts_adapter.py` implements Sarvam synthesis.
- `tts_provider_registry.py` registers the adapter.
- SQL masterdata contains Sarvam provider/model/voice/locale capability seeds.
- Existing `test_sarvam_tts_adapter.py` and resolver/executor suites exercise provider support.
- The historical audio deployment ledger indicates Sarvam routing disabled. Confirm **live DEV database routing** before enabling anything; do not assume Git history reflects runtime.

## DEV flags (new code must default OFF)
- `DF_DEV_SARVAM_INDIAN_SPEECH_ENABLED=false`
- `DF_DEV_CONTEXTUAL_SPEECH_REVIEW_ENABLED=false`
- `DF_DEV_VIDEO_LIPSYNC_QC_ENABLED=false`
- `DF_DEV_VIDEO_BOUNDARY_QC_ENABLED=false`

Flags must be implemented and enforced in the real routing/worker call sites; setting environmental values without guards does not confer isolation. Require DEV host/environment assertion in activation path and explicit allowlisting. Never silently fall back on a failed gated Sarvam request if doing so changes the user's selected voice or cost.

## Delivery sequence

### Gate 0 — establish baseline (read only)
1. Record deployed immutable backend SHA, active `svc-audio` and `svc-fusion-extension` image IDs and provider routing state from DEV. Do not log keys or user content.
2. Retrieve original uploaded/generated audio and video under approved access and validate the **latest affected** job: `d3f21463-5b6b-4203-a874-b9c2fefac6fe` (Bengali bn-IN, ~22 s).
3. Measure transcript completeness, initial and terminal utterance cutoff, per-turn A/V offset and transitions. A provider-complete flag or auto QC=accept is not sufficient.

### Gate 1 — contextual text preparation
1. Build a provider-neutral speech-preparation contract: speaker, language, transcript, previous and next dialogue snippets, emotion, audience, scene, and immutable source meaning.
2. For supported Indian languages, test Mayura conversational translation; use an explicitly tested fallback for unsupported locales. No one-to-one assumptions between all Sarvam speech and translation language sets.
3. Native-speech review must preserve names, brand terms, numerals, intent and speaker boundaries; allow explicit code-mixing and native-language review before synthesis.
4. Add deterministic tests: Hindi, Bengali, Tamil, Telugu, code mixed, quoted speech, proper nouns, and adjacent-turn contexts.

### Gate 2 — existing Sarvam synthesis path
1. Validate supported model IDs, locales, voice IDs, output codecs and current provider API contract against documentation and live sandbox.
2. Confirm database-driven routing capabilities, credential presence only (never print the key), usage/cost capture, audio artifact compatibility and rate limits.
3. Enable Sarvam only for allowlisted DEV test users/locales when all four feature gates and DB provider eligibility agree. Preserve Azure and existing routes.
4. Add contract tests for routing OFF, missing credential, unavailable voice/model and provider error; fail closed where necessary.

### Gate 3 — video timing and lip-sync
1. Derive segment boundaries from full dialogue and audio word/phoneme timing; do NOT blindly divide into fixed 5–8 second clips.
2. Ensure no words are omitted or duplicated at segment boundaries. Preserve speaker identity and audio continuity.
3. Validate first and last audible phonemes, per-turn mouth-to-speech offsets and post-speech visual hold using measured tolerances established on baseline samples.
4. Validate transitions and the stitched final media separately. Retain provenance from final job to each audio/video segment.
5. Make QC fail/repair actionable, with specific reasons (clipped_start, clipped_end, missing_words, lipsync_offset, abrupt_transition, speaker_mismatch). Do not treat provider's success status as the quality gate.

### Gate 4 — certification and release hold
- Existing svc-audio tests, audio provider registry/adapter/executor tests, new speech-context/QC tests, live DEV route test, plus single/multi-person regression MUST PASS.
- Compare DEV baseline vs new output via blind bilingual listening and frame review; capture cost, latency, retries and failure rates.
- Document price/credit effects and absence of DB-schema changes (unless a separate migration is explicitly approved).
- NO merge to main and NO PROD deployment until evidence is reviewed and explicitly approved.

## Evidence report
Record: source SHA; DEV runtime image SHA; flags; routing masterdata revision; exact job and segment IDs; language/model/voice; full transcript completeness; per-turn offsets; start/end clipping; transitions; final duration; provider costs; pass/fail reason; regression suite outputs.

## Safety and privacy
Do not print secrets, customer scripts, personal data or signed media URLs to public CI logs. Redact credentials. Use test users/assets. Preserve existing content safety, billing, revisions and identity-lock rules.

## Initial discovery note
Repository code already implements Sarvam TTS. This workstream is **not** a greenfield Sarvam adapter build. The missing scope is DEV-controlled activation, conversational translation, and independent audio/video quality certification. No live DEV assertions or deployments have been performed by this document.
