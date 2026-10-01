# piku account-context plane — DEV enhancement 2026-10-01

## Goal

Give the customer-facing piku assistant authenticated, current, account-specific desifaces context so answers can be grounded in the user's actual application state instead of generic product knowledge.

This enhancement is read-only. It does not grant piku authority to mutate a customer account, submit generations, change billing, expose payment credentials, or access another user's records.

## Context sources

piku reuses existing authoritative service APIs and user-scoped runtime data:

| Context | Source | Scope |
| --- | --- | --- |
| Plan, available/reserved credits, runway | `svc-dashboard /api/dashboard/home` | authenticated user |
| Saved Work / media taxonomy | `svc-dashboard /api/dashboard/library` | authenticated user |
| Month credit usage and money paid | `svc-pricing /api/pricing/me/spending/summary` | authenticated user |
| Recent account usage/payment events | `svc-pricing /api/pricing/me/spending/transactions` | authenticated user |
| Recent Face/Audio/Fusion jobs | business DB read-only projection | authenticated user |
| Recent longform/video jobs | business DB read-only projection | authenticated user |
| Current multi-person story stage | Director assistant-context endpoint | authenticated story |

The browser does not send balances, spending values, generation state, or Saved Work details to piku. It sends only the screen/story locator. `svc-assistant` resolves current context server-side using the caller's bearer token and authenticated user identity.

## Privacy projection

The model receives a minimized projection, not raw service responses.

Removed before model access:
- account/user/project/story/scene/participant/media/job/transaction IDs where not needed for dialogue;
- signed URLs, storage paths and media references;
- email, phone and physical address;
- payment-card/payment-method/customer identifiers;
- tokens, secrets, receipts and provider request IDs;
- customer-authored media titles/scripts from account-wide history.

Allowed account facts include:
- current plan label;
- available/reserved credits and runway estimates;
- current-period consumed/refunded/purchased credits;
- current-period money paid and non-sensitive category breakdown;
- recent transaction type/category/credits/money/status without IDs;
- Saved Work counts and safe media taxonomy;
- generation type/status/stage/progress/failure/retry/final-output availability;
- multi-person participant aliases and workflow state.

## Deterministic answers

High-value operational questions bypass the generative path when structured data is available:

- "How many credits do I have?"
- "What did I use this month?"
- "Where did my credits go?"
- "How much money did I pay this month?"
- "What did I create recently?"
- "How many saved items do I have?"
- "What is the status of my latest video/audio/face generation?"

This prevents the model from estimating values already known by the platform.

## API

### POST /api/assistant/chat

Existing user-facing chat endpoint. It now receives the expanded context automatically.

### GET /api/assistant/context

Authenticated read-only diagnostic endpoint returning the exact privacy-projected context supplied to piku.

Query parameters:
- `surface=web|mobile`
- `screen=<current screen>`

The endpoint is intended for DEV certification and client diagnostics. It does not expose the raw upstream payloads.

## Safe actions

piku remains advisory/read-only. Suggested actions may include safe navigation:
- Open Saved work → `/app/library`
- Open Plans & usage → `/app/billing`

No account mutation occurs merely because an action is shown.

## Freshness behavior

Context is resolved on every chat request. No financial/account context is stored in the assistant conversation session. Redis stores only minimized conversational text history.

Dashboard and Pricing remain the business sources of truth; piku does not maintain a parallel balance or spending ledger.

## Certification gates

1. All existing svc-assistant tests pass.
2. New account-context tests prove spending and Saved Work projection.
3. Tests prove signed URLs, media IDs, transaction IDs, payment data and internal SKU details do not enter model context.
4. Assistant health reports the expanded context capability.
5. `GET /api/assistant/context` requires authentication.
6. Cross-user selectors do not exist in the public assistant contract.
7. Production remains untouched until DEV runtime certification.

## Non-goals

- No generation execution from piku in this change.
- No billing mutations or purchases from piku.
- No provider administration.
- No raw logs/database access from the LLM.
- No long-term persistence of account financial context in assistant sessions.
