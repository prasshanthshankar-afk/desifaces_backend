# desifaces V3 Assistant — Context-Safe Architecture

## Frozen product rule

The Assistant may reason about customer context, but it never receives unrestricted access to customer data. Authentication, authorization, context construction, privacy enforcement and tool access are deterministic application responsibilities outside the LLM.

## V1 implementation boundary

V1 is advisory/read-only. It provides context-specific answers, approved product RAG, redacted short-lived conversation memory, and deterministic PII/PCI blocking. It does not execute billable generation actions.

```text
Mobile/Web
   |
   | JWT + context locator (screen/story/scene/participant IDs)
   v
svc-assistant
   |-- JWT/account authorization
   |-- inbound restricted-intent classifier
   |-- inbound PII/PCI/secret redaction
   |-- context resolver
   |      `-- authorized svc-director assistant-context
   |-- deterministic safe-context projection
   |      `-- removes IDs, URLs, real participant names, secrets/payment fields
   |-- approved customer-safe knowledge retrieval
   |-- LLM response generation
   |-- outbound PII/PCI/secret guard (fail closed)
   `-- redacted Redis session history with TTL
```

## Data handling

- No new customer-data tables are introduced.
- Raw chat text is not written to Postgres.
- Conversation memory is stored only after redaction in the existing V3 Redis and expires by TTL.
- Redis keys are scoped by authenticated account + user + session. The identifiers are never included in the LLM prompt.
- Story context is fetched from the existing Director `assistant-context` API using the same user JWT and projected into a safe model before LLM use.
- Account/project/story/scene/participant/turn/media identifiers and signed URLs are removed from model context.
- Participant display names are replaced with deterministic aliases such as `Participant 1`.
- Payment instruments, identity PII, authentication secrets and cross-account disclosure requests are denied before retrieval or LLM execution.
- Outbound generated text is scanned again. If restricted data is detected, the response fails closed to the support route.

## Restricted request behavior

The deterministic response is:

> I can't provide or retrieve personal identity or payment-card information through chat. For identity-verified assistance, please contact support@desifaces.ai.

This response is application-owned; the model cannot override it.

## RAG boundary

Only files shipped under `services/svc-assistant/knowledge/` are eligible for customer-facing retrieval in V1. This intentionally excludes EIP engineering knowledge, source code, logs, database records and operational material.

Retrieval uses configured OpenAI embeddings when `DF_ASSISTANT_EMBEDDING_MODEL` is set. Otherwise it fails over to deterministic lexical retrieval so the Assistant remains functional without widening data access.

## API

### `POST /api/assistant/chat`

Input contains `message`, optional `session_id`, and a context locator with `surface`, `screen`, and optional Story IDs. IDs are used only by deterministic server-side context resolution and are never forwarded to the model.

### `DELETE /api/assistant/sessions/{session_id}`

Deletes the authenticated user's redacted Redis conversation state.

### `GET /api/health`

Reports LLM/RAG configuration, Redis readiness and privacy-guard mode without exposing secrets.

## Phase 2 boundary

Tool execution (retry generation, start Face/Audio/Fusion, etc.) must be added through an allowlisted registry. Any billable operation requires a deterministic pricing preview and explicit user confirmation before execution.
