# TD-001 — Deferred desifaces Common-Services Modernization

**Status:** Deferred / intentionally not scheduled for V3

**Applies to:** desifaces V3 and future platform evolution

## Decision

Do **not** introduce a platform-modernization or common-services program as part of the current V3 delivery.

The existing single-face Face -> Audio -> Fusion product experience remains the reference implementation. Multi-person / Story must reuse and extend the existing screens, Viewer, Saved Work, pricing patterns, media handling and proven backend capabilities rather than replacing them with generalized cross-product services or a new platform architecture.

## Current V3 guardrails

1. **Reuse before build** — existing single-face components and flows are the reference.
2. **Extend, do not replace** — Story-specific context is added around proven experiences.
3. **No premature abstraction** — do not create generic shared services merely because Face, Audio and Fusion have overlapping behavior.
4. **Product quality first** — investment goes to seamless UX, native HD quality, premium pricing correctness, recovery, performance and end-to-end reliability.

## Explicitly out of scope now

The following are technical-debt candidates for a later modernization program and must not be pulled into V3 implementation merely for architectural cleanliness:

- generalized common media service / Viewer service;
- generalized Saved Work / asset-library service;
- generalized pricing/entitlement facade beyond current proven contracts;
- generalized workflow/context service spanning Face, Audio, Fusion and Story;
- generalized cross-studio UI/service abstractions that require broad rewrites;
- decomposition or consolidation of current services solely to remove duplication;
- redesign of the existing single-face workflow to accommodate Story;
- migration to a new common-services platform without a separate funded modernization plan.

## Why this debt is accepted

The current priority is product-market execution. Premature modernization would increase delivery scope, regression risk and time-to-market while producing little near-term user value. V3 should prove user demand, quality, pricing, retention and operational behavior before desifaces invests in a broad platform refactor.

## Revisit triggers

Reconsider this debt only as a **separate modernization project** when one or more of the following are true:

- meaningful registered-user scale and sustained product usage;
- external funding or a dedicated modernization budget/team;
- repeated cross-service duplication is measurably slowing feature delivery;
- operational incidents show that shared ownership/contracts would materially improve reliability;
- performance/cost data justify centralizing a capability;
- product contracts have stabilized enough to extract common services without destabilizing active workflows.

## Required planning before implementation

Before any future common-services extraction begins, require:

- explicit architecture review and ADRs;
- inventory of existing Face, Audio, Fusion, Story, Viewer, Saved Work and pricing contracts;
- compatibility/migration strategy;
- measurable business and engineering justification;
- regression plan for existing single-face and multi-person workflows;
- staged rollout with rollback capability;
- dedicated delivery capacity separate from feature work.

## V3 enforcement note

Any V3 change proposing a new generalized platform/common service must reference this technical-debt record and explain why the change cannot be implemented by reusing/extending the existing proven flow. In the absence of an approved modernization project, the default decision is **do not introduce the abstraction**.
