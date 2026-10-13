# Indian speech DEV certification — 2026-10-13

DEV candidate: `55aeee7169efb676345b2c006215669ad9e31864`.
Production remains on its existing release.

## Implemented in DEV

- Preferred speech provider is configured by language in existing routing-policy metadata; no new tables or hardcoded application language/provider mapping.
- Sarvam Bulbul v3 is preferred for eligible Indian-language defaults. Explicit voices remain eligible and unchanged.
- Browsing catalog and executable model/voice selection agree for male and female defaults.
- Story planning and critique request idiomatic speech and native Indic script before story approval.
- Translation requests preserve exact locale and grammatical gender and reject provider-reported incomplete output.
- Existing DEV Sarvam Conversational/Narration compatibility was recovered by exact file hash and preserved.
- Existing audio-worker pricing overlays were verified against main and copied unchanged into the candidate.

## Evidence

| Gate | Result | Actions run |
|---|---|---|
| Translation, style, voice, catalog, provider tests | 50 passed | 38433630816 / 38433642823 |
| DEV candidate build and rollback-only routing test | Passed | 38433638957 |
| Active catalog and execution defaults | 10 languages × 2 genders passed | 38433638957 |
| Explicit ElevenLabs Hindi voice and en-US routing | Preserved | 38433638957 |
| Audio reuse contracts and real canonical cross-project reuse | Passed | 38433638957 |
| Actual group-photo draft/approval handlers | Passed with DB rollback and network blocked | 38435005111 |
| Wallet, ledger, reservation and generation-job deltas during reuse | All zero across eight financial/job tables | 38435005111 |
| Speaker IDs and original photo provenance | Preserved | 38435005111 |
| Runtime config, network aliases and unrelated services | Preserved | 38433638957 |

### Sarvam defaults verified in DEV

| Locale | Male | Female |
|---|---|---|
| hi-IN | shubh | priya |
| ta-IN | ratan | ishita |
| te-IN | shubh | neha |
| bn-IN | rehan | roopa |
| gu-IN | ratan | priya |
| kn-IN | shubh | neha |
| ml-IN | shubh | pooja |
| mr-IN | ratan | priya |
| pa-IN | mani | roopa |
| or-IN | shubh | ritu |

## Independent affected-video audit

The supplied MP4 matched production media `77c84003-d509-471d-86e3-e3db0f41b1df` exactly by SHA-256
`f2cc918bf5e2aae39f9828c505129af92b5c7a4d4a45ab1bcd2e6a52b7a75641`.

Workflow: `f4bd196c-704a-40e0-8892-d84018b0c7e7`.
Story: `232f7216-9766-508e-be53-28411442b0e2`.
Final scene stage: `bca22e5f-9640-40c0-8b1a-65901520fb61`.

Read-only production audits: 38432892644, 38433836346, 38434793478.

- Six approved source audio turns, 7.896–15.480 seconds each.
- Original voices: Azure hi-IN-AartiNeural and hi-IN-SwaraNeural.
- Translation provider/model: OpenAI Responses / gpt-4.1-mini.
- Rendering provider: OmniHuman.
- Stitch mode: shared_dialogue.
- All six rendered audio tracks align with original speech envelopes at a consistent +300 ms lead-in (correlations 0.9736–0.9915).
- The existing adapter explicitly adds a 300 ms dialogue head pad and trims trailing silence before adding a tail handle. This explains why the +300 ms source-to-render alignment is not, by itself, evidence of mouth/audio desynchronization.
- Original audio container durations total 63.480 seconds; rendered clip containers total 63.280 seconds; final audio duration is 63.230 seconds. Silence cleanup and codec/container durations must be distinguished from missing spoken content.
- One concrete translation defect is confirmed: the answer “I have!” to a question asking whether announcements were seen became “मेरे पास है!” (“I possess it”). Group translation needs neighboring-dialogue context to resolve this ambiguity.

## Remaining quality gates

1. Validate native speech quality on actual new samples; prompt changes alone do not establish a native-speaker quality pass.
2. Add and test neighboring-dialogue context for short ambiguous responses before treating the contextual translation defect as resolved.
3. Compare mouth motion against the exact padded provider audio; waveform correlation compares audio with audio, not phonemes with mouth movement.
4. Treat pause-aware shorter clips as an experiment if that measurement shows a benefit. No 5–8-second segmentation change is included here.
5. Promote validated source through main before any production deployment. Approved/reused audio must never be silently rewritten; revisions continue to require a new priced generation.
