# EIP Context — Deferred Common-Services Modernization

**Context type:** Technical debt / architecture boundary

**Priority for retrieval:** High when planning or implementing V3 architecture, Story/Multi-person UX, Face, Audio, Fusion, Viewer, Saved Work, pricing or service reuse.

## Canonical instruction

For current desifaces V3 delivery, **do not start a common-services or platform-modernization program**.

Preserve the existing single-face Face -> Audio -> Fusion experience as the UX and implementation reference. Multi-person / Story must reuse those established screens, components, Viewer, Saved Work and pricing interaction patterns, adding only the context and orchestration required for multiple participants, story progression, premium HD quality and premium pricing.

## Engineering rules EIP must apply

- Reuse before build.
- Extend existing proven flows rather than replacing them.
- Do not create generic shared services solely to eliminate duplication.
- Do not redesign the application architecture for theoretical cleanliness.
- Do not redesign the existing single-face workflow to fit Story.
- Treat seamless UX, native HD quality, pricing correctness, recovery, performance and end-to-end reliability as higher priority than abstraction.
- If an implementation proposal introduces a generalized cross-studio service or large shared abstraction, flag it against `docs/technical-debt/TD-001-deferred-common-services-modernization.md`.

## Deferred future modernization

A common-services modernization may be reconsidered later as a **separate funded program** after meaningful user scale, product usage and operational evidence exist. Expected future candidates may include shared media/viewer, saved-work/asset library, workflow/context, common pricing/entitlement facades and other cross-studio capabilities, but none are authorized by this record for current V3 work.

## EIP response behavior

When EIP generates V3 implementation plans or code changes:

1. Prefer the smallest change that reuses current contracts and components.
2. Explicitly identify any proposal that would expand into platform modernization.
3. Reject or defer that expansion unless a future approved modernization initiative supersedes this record.
4. Preserve current single-face behavior while adding Story/Multi-person context narrowly.

## Supersession

This context remains active until an explicit future ADR/technical-debt closure records that the common-services modernization program has been funded, scoped and approved.
