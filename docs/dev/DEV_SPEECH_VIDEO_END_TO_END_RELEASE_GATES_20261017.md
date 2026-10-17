# DEV speech/video end-to-end release gates — 2026-10-17
Status: REVIEW_REQUIRED, not certified. Covers Issue #78 and draft PR #77. Production untouched until separate explicit approval.

## Frozen contracts
- No changes to pricing, payment, checkout, subscriptions, currency routing, credits, wallet, reservations, ledger or refunds. Read-only regression validation only.
- Preserve uncommitted DEV runtime files, particularly services/svc-pricing/app/app/api/deps.py. No overwrite, reset, or staging.
- Do not merge to main or deploy production. Preserve existing audio, video, and Face workflows for users outside approved DEV test cohort.

## Backend implementation gates
1. Prove live DEV image SHAs, real DB identity, flags, prior image rollback handles.
2. Thread validated authenticated user ID through API -> queued job -> audio worker -> translation selection. Enable provider-first only if DEV host, explicit test-user allowlist, default-off flag, and verified capability all agree.
3. Provider-neutral translation selector must use DB-backed directional capabilities distinct from TTS. Sarvam Mayura first for verified source/target direction; Azure Translator for eligible language direction; GPT only an explicit bounded fallback. No silent selected-voice changes.
4. Enforce independent bilingual natural-language QA: neighboring context, semantic faithfulness, speaker/gender, proper nouns, accents/colloquialisms, no omitted or invented content. Fail closed on missing review.
5. Do not regenerate approved/reused audio or modify TTS reservation/idempotency or credit contracts.

## Database and global catalog
- Complete 139 user-selectable locale audit: translation direction, provider model, male/female TTS eligible voice, runtime credential status, real API sample, quality result.
- Prefer existing metadata tables. If new schema truly needed, design additive reviewed migration with OFF defaults; keep all financial tables untouched.
- Canonical Odia or-IN and provider od-IN must be resolved as catalog data rather than a source-level hardcoded platform route.

## Frontend
- No pricing/payment screens or contract changes.
- No new UI until backend has stable authorized QC result contract. If warranted, display explicit DEV test-user failure reasons, never an unearned PASS.
- Preserve Face, audio, single/multi-person, Saved Work and identity workflow parity.

## Strict new-video verification
- Newly generate one 6-turn two-speaker video in DEV and preserve immutable job/audio/clip/stitch/final-MP4 lineage. Previous six clips are only baselines.
- Calibrated audiovisual evaluator, independent speaking-face tracking, beginning/end phonemes, signed offset, confidence, per-turn median and p95, drift, seam checks and human normal-speed review.
- Provisional thresholds: median absolute <=40 ms, p95 <=80 ms for confident events, and zero perceptible lag. Inconclusive never PASS.
- Fail on unnatural language, missing or duplicate words, clipped speech, wrong speaker, audible or visual seam discontinuities. Repair only failed turn/boundary, no blind global delay.
- Record price preview and unchanged ledger/reservation/regression evidence without changing any financial implementation.

## Certification and clean promotion
1. Isolated build pinned to immutable commit; automated backend tests and frontend build/regression.
2. DEV-only allowlisted activation with recorded image/DB rollback references; no automatic global switch.
3. Evidence: source SHA, Actions run IDs, deployed image hashes, flags, language sample judgments, TTS audio, six-turn final video, human review, pricing/payment regression.
4. Document PASS/FAIL per gate. Remain draft/unmerged while incomplete. After full PASS, separate human approval required before any production merge/deployment.
Release state: REVIEW_REQUIRED / PROD NO-GO.
