# desifaces V3 production readiness manifest — 2026-09-12

This manifest is the source-controlled handoff from certified DEV behavior to production. Production changes must use these gates and must not reconstruct ad-hoc runtime state.

## Certified DEV application state

- Director recent-Story route source: `16d89f0914df20660e9cf9e1a298251f222de425`
- Web Piku action/markdown source: `242e9fd0c88c4900d43925e87ffdb1507803e73d`
- Web Multi-Person durable Face artifact hydration source: `1ee7875476d68f780820e5b1d3786ac51242f5cf`
- Exact Story pricing reconciliation: 9 committed reservations, 168 credits, 168 ledger consumption, 0 refunds, 0 duplicate idempotency groups.

## Economics defect correction

Branch/PR: `fix/v3-audio-cogs-production-readiness-20260912` / PR #18.

- `AUDIO_TTS_1K_CHARS` remains customer-priced at 3 credits per 1K characters.
- Internal Azure standard-neural TTS baseline COGS is effective-dated at USD 0.016 / 1K billable characters.
- Customer pricing, entitlements and billing behavior must hash-identically before and after the COGS migration.
- Story economics must report `COST_INCOMPLETE` / unavailable margin when any billable leaf SKU has no effective cost row.
- Production applies the exact migration blob through `scripts/apply-v3-audio-cogs-production.sh` only after DEV certification passes.

## Scheduler carry-forward

The existing DEV recurring jobs are systemd timers, not crontab entries:

- `desifaces-notification-dispatch.timer`
- `desifaces-safe-docker-cleanup.timer`

Do not recreate these from memory. `scripts/capture-v3-production-systemd-bundle-dev.sh` exports the exact installed scripts, service units and timer units from DEV, scans for embedded credentials, creates `SHA256SUMS`, and stores them under `ops/production/systemd/` on PR #18.

Production installation uses `scripts/install-v3-production-systemd-bundle.sh`, which:

- requires explicit production confirmation and exact target hostname;
- refuses to run on DEV;
- verifies the SHA256 manifest;
- refuses DEV-specific bindings or embedded credentials;
- backs up any existing production units/scripts;
- enables/starts only the two timers;
- never manually runs the Docker-cleanup job during installation.

## Stripe Live

Stripe Live remains disabled during preparation. `scripts/certify-v3-stripe-live-readiness.sh` checks key mode and catalog coverage without printing secrets.

Cutover order:

1. Provision live `sk_live_...`, `pk_live_...`, and the production webhook `whsec_...` through protected production secret storage; never commit them.
2. Provision/verify production Stripe Product/Price IDs for every active web plan and credit pack.
3. Run Stripe readiness in `cutover` mode.
4. Perform one bounded real-money purchase/subscription smoke test and verify webhook fulfillment exactly once.
5. Refund/cancel the smoke transaction and verify the corresponding ledger/account state.
6. Only then open live web billing broadly.

## Web release

The current DEV Web hotfix branch diverges from Web `main`; do not deploy production by blindly replacing `main` with the hotfix branch. Integrate the certified component deltas (Piku + Multi-Person artifact hydration) onto the current production release baseline, rebuild once, and certify authenticated Story/Piku/browser behavior before cutover.

## Mobile parity

Repository: `prasshanthshankar-afk/desifaces_frontend`
Branch/PR: `release/v3-production-web-mobile-parity-20260912` / PR #11 targeting canonical `desifaces-v3`.

Required parity contract:

- Multi-Person Creative Director and recent Story continuation.
- Story stage progression Face -> Audio -> Fusion -> Story Final.
- Durable Face artifact rehydration from `media_id` and canonical stage-aware status.
- Piku context, privacy policy, lightweight bold rendering and `Continue story` navigation action.
- Saved Work, spending/transactions, plans/credits and account state from shared backend APIs.
- Native billing rails remain platform-native; product entitlement/credit state remains shared and authoritative.
- No mobile-local pricing formulas, Stripe price IDs or entitlement mutation logic.

PR #11 remains draft until TypeScript/source/build gates and physical-device acceptance pass.

## Production order

1. DEV Audio COGS correction + complete economics certification.
2. Export/certify exact DEV systemd scheduler bundle.
3. Complete Web artifact/Piku browser acceptance.
4. Complete mobile parity build/device acceptance.
5. Integrate backend/Web/mobile release branches onto canonical production baselines.
6. Apply production internal COGS migration.
7. Install production systemd timers.
8. Deploy backend/Web application release.
9. Run production health/authenticated Story/Piku/saved-work smoke tests.
10. Provision and certify Stripe Live, then bounded real-money smoke/refund.
11. Release native mobile builds after shared backend production acceptance.

No production step is considered complete unless its corresponding fail-closed gate reports PASS.
