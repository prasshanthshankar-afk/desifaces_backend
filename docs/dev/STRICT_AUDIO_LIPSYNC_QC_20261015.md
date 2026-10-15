# Strict audio-to-lip synchronization certification — DEV only

Status: **RELEASE BLOCKER; unimplemented and not certified**. User approval: 2026-10-15.

Affected artifact: `from-venture-idea-to-global-story-scene.mp4`, approx. 65.22 seconds, 30 fps, two visible speakers. Measured container track start difference (~21 ms) is *not* a measurement of visual phoneme sync. Prior six-clip estimates of 40–80 ms do not prove the latest artifact passes.

## Required evidence and lineage
- Resolve original audio artifact(s), generated per-speaker visual clips, stitch manifest, provider requests, frame rate, presentation timestamps, audio delay/trim/fade operations, and final MP4 from the exact DEV video job ID.
- Record timeline in a common clock. Do not compare absolute timestamps from different clocks until normalized.
- Preserve approved audio bytes as authoritative. Do not regenerate narration to fit poor animation.
- Differentiate source clip AV offset, render offset, global mux offset, offset varying over each speech turn, and transition truncation.

## Alignment measurement
For each speaker turn:
1. Independently derive audio speech/phoneme onset and offset estimates, including pauses, from *approved* audio; compute word/phoneme alignment with confidence.
2. Detect speaking person's mouth landmarks or mouth aperture/viseme activations in frames. Use face tracking by mapped speaker ID; do not attribute listeners' expressions as speech.
3. Compare matching phonetic events (e.g., bilabial closures) to expected corresponding visual events with sign, per-turn median/p95 absolute offset, and drift across the turn. Report ambiguity and confidence; do not assert automatic perfect accuracy.
4. Inspect turn beginnings/ends and stitches by human frame+audio review, including at 0.5× speed and normal speed, for clipped or duplicate phonemes.
5. Include separate exported intermediate and final clip metrics. Never certify on container start PTS alone.

## Provisional acceptance policy (calibrate empirically)
- Target sustained median absolute offset <= 40 ms for confident events, target p95 <= 80 ms, **and no perceptually apparent lag** on blind human review.
- No detectable progressive drift; no incorrect speaker mouth activation; no clipped words or discontinuity at seams.
- 30 fps frame granularity = 33.33 ms. Any automated estimator must report confidence and video sampling limitations.
- If metrics cannot be measured confidently, mark `INCONCLUSIVE`, never `PASS`.
- Platform marks video `QC_FAIL` or `QC_REVIEW_REQUIRED` if any gate fails; *provider succeeded* must not override it.

## Repair hierarchy
1. If **global mux-only** offset proven: adjust timestamp alignment at final composition; verify all turns remain aligned.
2. If **per-clip constant** offset proven: adjust only affected clip with explicit boundary/hold handling. No blind hard-coded +/-100/200 ms.
3. If **nonlinear mouth drift** or wrong visemes: regenerate affected speaker video using the same original audio, tighter bounded shots, or evaluate a validated alternate lip-sync provider. Never time-warp authoritative audio solely to match incorrect lips.
4. If **stitch** truncation: correct clip trim/padding and revalidate full output.
5. Respect idempotency and pricing: platform-caused repairs must not duplicate user charges or ledger entries.

## DEV certification matrix
- Single-speaker and shared-photo two-speaker, English, Hindi and at least one regional-language sample.
- Short and long utterances; rapid alternating turns; pauses; consonant closures; first and last phonemes; final sentence termination.
- Verify generated and reused audio, existing-photo and generated-photo flows, identity and speaker mapping.
- Check no regressions to existing audio, Face, Fusion, billing/credits, output categorization and saved work.
- All quality evidence tied to job ID and immutable media artifacts; report measured offsets and human observations.
- Do not merge to main, activate in PROD, or label release certified until all gates pass.

## Follow-up prerequisite
Obtain exact DEV job ID for the attached video to retrieve intermediate clips and original audio. The file alone allows media-level inspection but not verified generation lineage. 
