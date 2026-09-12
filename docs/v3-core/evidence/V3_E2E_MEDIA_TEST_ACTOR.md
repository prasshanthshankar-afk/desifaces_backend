# V3 E2E Media Test Actor

**Status:** FROZEN FOR CONTROLLED V3 PROVIDER TESTING  
**Scope:** Face, Audio, Fusion, Multi-Person/Story execution certification

## Canonical actor

Use the dedicated Apple-IAP test user identified by:

`test_apple_iap_test1@desifaces.ai`

for controlled V3 media-provider functional and certification runs that require real pricing/credit reservation.

## Security rule

The login password or any other credential for this actor MUST NOT be committed to Git, embedded in source code, written into evidence artifacts, printed to logs, included in LLM/RAG prompts, or stored in Studio metadata.

For internal V3 certification harnesses that already execute inside the trusted V3 runtime, resolve the actor from `core.users` by the canonical test email, resolve its active billing account from the pricing account-membership model, and use a short-lived locally signed test JWT. This avoids handling the reusable password while preserving the real account, entitlement, pricing, credit-reservation, ledger, ownership, and media lifecycle paths.

If an explicit login/authentication E2E test is required, supply the password only through an external secret mechanism or interactive runtime input. Do not persist it.

## Fail-closed requirements

A provider-execution test must fail before provider invocation if any of these are false:

1. The canonical test email resolves to exactly the intended V3 user.
2. The selected billing account is active and belongs to that user.
3. The expected entitlement/account context is active.
4. The account has enough spendable credits for the planned Face/Audio/Fusion operations.
5. Pricing preview succeeds.
6. Pricing reservation succeeds normally; no synthetic credit bypass is allowed.

## Reuse

Use environment key `DF_V3_E2E_TEST_USER_EMAIL` as the common test-harness selector. The controlled V3 media runners pin it to the canonical email above at the shell boundary so inherited container environment cannot silently switch actors.

This actor selection rule should be reused by MPS2 Face, MPS3 Audio, MPS4 Fusion, and full Story execution provider tests.
