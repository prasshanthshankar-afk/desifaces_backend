# V3 production exact-host safety gate — 2026-09-12

## Change

The direct production VM cutover launcher now requires `DESIFACES_PRODUCTION_HOSTNAME` and compares it exactly with `hostname -s` before any production mutation. Prefix-only matching is removed as an authorization condition.

## Reason

DEV and non-production infrastructure can share a `desifaces-gpu` naming prefix. Exact host approval prevents accidental deployment to a similarly named non-production VM.

## Invariants

- No mutation before exact hostname, GitHub release provenance, DB/Redis/network, and production environment gates pass.
- `desifaces-dev` is explicitly forbidden.
- hostnames containing `non-prod` or `nonprod` are explicitly forbidden.
- Stripe Live remains inspection-only.
- Mobile store submission remains excluded.
- No production change is performed by this source-control safety update.
