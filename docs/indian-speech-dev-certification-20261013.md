# Indian speech DEV certification — 2026-10-13

Current DEV audio API/worker candidate: `84cddb707e68dc5c4514227b276809dae9b6b47a`.
Director context rollout: `b518c19558a09436d9f7cb37854f06f0f3c7301b` (Director source is unchanged in the audio-review candidate).
Production remains on its existing release.

## Implemented in DEV

- Preferred speech provider is configured by language in existing routing-policy metadata; no new tables or hardcoded application language/provider mapping.
- Sarvam Bulbul v3 is preferred for eligible Indian-language defaults. Explicit voices remain eligible and unchanged.
- Browsing catalog and executable model/voice selection agree for male and female defaults.
- Story planning and critique request idiomatic speech and native Indic script before story approval.
- Translation requests preserve exact locale and grammatical gender and reject provider-reported incomplete output.
- Up to three neighboring turns from the validated workflow and scene reach translation through the existing context field. The current spoken line, participant ID, voice selection and pricing units are preserved.
- New translated Sarvam generations receive a separate native-language review before synthesis. An incomplete review fails rather than synthesizing the unreviewed draft. Already approved/reused audio is never rewritten.
- Execution explicitly imports an image-owned translation helper. Read-only inspection found the older shared helper was still imported after the first candidate rollout; the packaging correction was verified in both live audio containers.
- Existing DEV Sarvam Conversational/Narration compatibility was recovered by exact file hash and preserved.
- Existing audio-worker pricing overlays were verified against main and copied unchanged into the candidate.

## Evidence

| Gate | Result | Actions run |
|---|---|---|
| Translation, context, native review, style, voice, catalog and provider tests | 61 passed | 38447606535 |
| Context and image-owned-helper DEV activation | Passed | 38445927775 |
| Independent native-review DEV activation, audio services only | Passed, policy/config/unrelated services unchanged | 38447772380 |
| Real workflow context and preview/generation payload parity | Passed on three existing stages | 38447932543 |
| Reviewed Sarvam speech and transcription | Hindi male/female, Tamil female, Bengali male passed | 38447932543 |
| Observed Tamil malformed ending repaired by actual reviewer | Passed | 38447932543 |
| Post-review reuse billing and original lineage | Zero reservation/ledger/job/external-call deltas; transaction rolled back | 38447932543 |
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

## Actual native-language QA

Contextual Hindi “I have!” became “हाँ, मैंने देखी हैं!”, rather than the possessive “मेरे पास है!”.
Actual Sarvam outputs used priya (Hindi female), shubh (Hindi male), ishita (Tamil female) and rehan (Bengali male).
The first Tamil QA draft contained the malformed ending “பார்த்தேன்ம்”, which STT normalized; intelligibility alone did not establish text quality.
A separate reviewer was added and the actual model repaired that frozen negative sample to “பார்த்தேன்”.
Final reviewed Tamil and Bengali speech transcribed verbatim; Hindi differences were punctuation and accepted spelling variants.
QA samples were 3.840–7.765 seconds. Catalog access was read-only; no customer jobs, assets, reservations or billing writes were made by synthesis QA.
Automated transcription and linguistic checks do not certify subjective pronunciation, accent or prosody across all ten languages.

## Quality conclusions and production boundary

1. Broader native-speaker listening remains useful for pronunciation and prosody; automated real samples and linguistic review passed for the tested Hindi/Tamil/Bengali cases.
2. Contextual translation is active and passed real-workflow, actual-model and regression checks.
3. The six-clip visual assessment completed against the exact prepared provider audio. Numerical results below show a small residual estimate; a perfect lip-sync pass is not claimed.
4. These clips show no clear accumulating drift and do not justify blanket 5–8-second segmentation. A timing correction or shorter-clip comparison would require a separate validated experiment. No segmentation or global timing shift is included here.
5. Promote validated source through main before any production deployment. Approved/reused audio must never be silently rewritten; revisions continue to require a new priced generation.

Visual assessment run: https://github.com/prasshanthshankar-afk/desifaces_web/actions/runs/38445849863
The isolated CPU assessment completed successfully using official SyncNet source pinned at `907c0b579c2e2d83f0eae1b2ac9e720cde4e5623`.
Weights SHA-256: SyncNet `961e8696f888fce4f3f3a6c3d5b3267cf5b343100b238e79b2659bff2c605442`; S3FD `d54a87c2b7543b64729c9a25eafd188da15fd3f6e02f0ecec76ae1b30d86c491`.
The reference offset changed by exactly five frames when audio was delayed 200 ms, confirming the measurement's relative response and sign.
This validates the offset test, not an absolute zero-offset ground truth.

## Exact prepared audio and visual measurements

| Turn | Active speaker | Prepared audio → render audio lag | Speaking-face offset estimate | First/last estimate | SyncNet confidence |
|---|---|---|---|---|---|
| 1 | Speaker 1, left | 0 ms | −40 ms | −40 / −80 ms | 8.230 |
| 2 | Speaker 2, right | 0 ms | −80 ms | −40 / −80 ms | 7.139 |
| 3 | Speaker 1, left | 0 ms | −40 ms | 0 / −40 ms | 8.199 |
| 4 | Speaker 2, right | 0 ms | −80 ms | −80 / −80 ms | 7.974 |
| 5 | Speaker 1, left | 0 ms | −40 ms | 0 / −80 ms | 5.852 |
| 6 | Speaker 2, right | 0 ms | −40 ms | −40 / −80 ms | 7.537 |

Audio-envelope lag was measured at 10 ms resolution; correlations rounded to 1.0000 in every clip.
Both faces were tracked separately: active faces gave strong matching scores, while listener scores were 0.168–0.721 for whole clips.
Visual estimates use 25 fps / 40 ms steps. Negative values correspond to later audio under the synthetic-delay control; they are model estimates, not perceptual certification.
The 300 ms source lead-in exists in the actual prepared Fal audio, and the rendered audio matches that prepared timeline. It is not an additional remux delay.
First/last windows show no clear accumulating long-video drift. A small 40–80 ms residual estimate remains, so perfect synchronization is not claimed.
Production services, media and approved audio were read only; all computation ran on the isolated CI runner.
No production rollout, audio rewrite, segmentation change or global timing shift was performed.
