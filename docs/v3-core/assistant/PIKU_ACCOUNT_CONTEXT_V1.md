# Piku Account Context V1 — DEV contract

Status: DEV enhancement  
Date: 2026-10-01  
Production touch: NONE

## Goal

Give piku enough authenticated, request-time desifaces context to answer account-specific questions accurately without exposing unrestricted customer data to the model.

Examples:

- What plan am I on?
- How many credits do I have?
- What have I used and paid this month?
- What is in my Saved work?
- Do I have unread notifications?
- Which recent stories need review?
- What happened to my latest Face / Audio / Video generation?
- What can I create with my current credit runway?

## Data flow

Browser / native app

-> existing authenticated `POST /api/assistant/chat`

-> `svc-assistant` validates the user's JWT and resolves the user's account context

-> piku reads, concurrently and read-only, through existing authenticated service contracts:

1. `svc-dashboard /api/dashboard/home?force=true`
2. `svc-dashboard /api/dashboard/library?type=all`
3. `svc-pricing /api/payments/overview`
4. `svc-pricing /api/pricing/me/spending/summary?period=month`
5. `svc-core /api/notifications`
6. `svc-director /api/director/stories/recent`
7. user-scoped recent studio / long-form generation state
8. current Story assistant-context when a Story locator is supplied

-> a deterministic privacy projection removes unsafe fields

-> safe account context is used by deterministic account answers first, then by the LLM for broader contextual reasoning.

## Safe model context

The model may receive:

- plan label / plan code / subscription state;
- available, reserved and included credits;
- current-month credits consumed and actual money paid;
- aggregate Saved Work counts by safe artifact category;
- unread notification count, category, priority and event type;
- recent Story aliases, workflow stage and attention state;
- recent generation aliases, studio kind, status, stage, retryable flag and progress;
- pricing runway estimates;
- current Story participant aliases, scene state and dialogue metadata without dialogue text.

The model must not receive:

- user/account/project/story/workflow/job/media IDs;
- customer-authored titles or prompts;
- media URLs or signed URLs;
- email, phone, physical address or DOB;
- card/payment-method/customer/receipt identifiers;
- passwords, JWTs, provider secrets or provider request IDs;
- another user's records;
- raw notification body/title;
- raw Story title;
- raw generation prompts.

## Authorization

- Every downstream API call forwards the authenticated user's bearer token.
- Existing downstream services remain the authorization authority.
- Direct database reads in `svc-assistant` remain user-scoped.
- The current screen is a conversational hint, not a data-access boundary.
- The assistant remains advisory/read-only. It does not mutate account, billing, Story or generation state.

## Freshness

`Dashboard home` is requested with `force=true` for piku so credit/runway state is refreshed at request time.

Other account sources are read on every assistant request. No account snapshot is persisted into the LLM or knowledge base.

## Failure behavior

Each non-essential account source fails soft to an empty safe section. Authentication failures remain hard failures.

Piku must say that a value is unavailable rather than inventing it.

## Customer navigation actions

Piku may return safe application routes for:

- Saved work
- Plans & usage
- Face Studio
- Voice Studio
- Video Studio
- Multi-Person

These are navigation actions only and never imply that piku performed a billing or generation mutation.

## Certification gates

1. Python compile passes.
2. All `svc-assistant` unit tests pass.
3. Account projection tests prove restricted identifiers/content are absent.
4. Deterministic account-answer tests pass.
5. `/api/health` reports the expanded live-context sources.
6. Existing privacy tests remain green.
7. Existing credit/generation answers remain green.
8. No Face, Audio, Fusion, Director, Dashboard or Pricing service restart is required.
9. Production remains untouched.
