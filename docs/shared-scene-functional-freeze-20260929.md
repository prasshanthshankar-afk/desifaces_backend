# Shared-scene functional freeze — 2026-09-29

Status: **FUNCTIONALLY FROZEN for launch certification**

This document records the launch baseline for the desifaces shared-scene / group-photo conversation workflow. Changes after this point must not alter the functional contracts below unless a verified regression or launch-blocking defect requires it.

## Frozen end-to-end behavior

1. Creative Director plans speaker-attributed dialogue and presents it for human review before media generation.
2. People are approved with durable speaker profiles and an immutable approved speaker snapshot.
3. Group-photo source is explicitly chosen:
   - desifaces-generated photo for exactly two approved speakers.
   - uploaded group photo for two or more approved speakers.
4. Group-photo mapping persists every participant target before approval.
5. Audio generation uses the approved Director dialogue, selected locale/translation settings, and compatible speaker voices.
6. Shared-scene Fusion uses the approved group photo, mapped active-speaker coordinates, approved dialogue audio, and the explicit saved motion mode.
7. Child video jobs are internally bill-to-parent; the scene owns the customer pricing lifecycle.
8. Child dispatch is parallel and completed children are preserved across failed-child retry.
9. Final scene assembly uses approved child outputs and produces the durable final media asset.
10. Canonical shared-scene workflow state remains the authority for phase, next_action and allowed_actions.
11. Runtime container/network naming remains version-neutral.
12. Production remains untouched until DEV certification and explicit production promotion.

## Frozen provider / performance behavior

- Core Fusion worker concurrency remains independently configurable.
- Sync3 provider concurrency is runtime-configurable and must reflect the current provider entitlement.
- Provider 429 concurrency handling, idempotent retry, clean-download resolution, and preserved-child retry semantics remain unchanged.
- Pricing, reservation, commit/release semantics and customer credit behavior remain unchanged.

## Allowed launch-phase changes

The following are explicitly allowed without reopening functional design:

- performance configuration and capacity tuning;
- read-only performance analysis and observability;
- truthful progress/status presentation;
- UI layout and visual hierarchy;
- UX language and progressive disclosure;
- next-step navigation/guidance;
- responsive web presentation;
- native mobile presentation aligned to the same canonical workflow state;
- accessibility improvements;
- tests and documentation that protect the frozen contracts.

## Not allowed without an explicit functional-change decision

- changing workflow phase semantics;
- changing pricing/billing ownership or credit behavior;
- changing speaker/profile lineage or approved snapshot semantics;
- changing group-photo approval/mapping authority;
- changing Audio script lineage after Director approval;
- changing provider routing semantics;
- changing retry scope from failed-child-only;
- introducing new production DB schema for this launch path;
- weakening safety/content validation;
- production mutation during DEV UX refinement.

## Current launch UX principle

Each phase should expose:

1. where the user is;
2. one obvious current action;
3. truthful progress;
4. advanced/technical details only through progressive disclosure.

Completed stages should collapse by default. The final media should become the primary experience once the workflow is complete.
