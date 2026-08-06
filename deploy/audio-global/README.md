# desifaces svc-audio global TTS deployment

This directory is the production replication source for the
#eip7 global svc-audio TTS platform.

Production deployment must never be reconstructed manually from
development shell history.

Required release controls:

1. Exact Git commit/tag must be recorded.
2. Production preflight must be read-only.
3. A targeted database backup is mandatory.
4. Backup integrity/hash must be verified before migration.
5. SQL migrations must execute in the documented order.
6. Environment/configuration requirements must be validated without
   printing secret values.
7. Database post-migration validation must pass.
8. svc-audio health and integration tests must pass.
9. Face -> Audio -> Fusion regression must pass.
10. New TTS providers remain routing-disabled until their adapters,
    credentials, quality tests, and rollback gates are validated.
11. Rollback instructions must exist before production activation.
12. No manually-created production masterdata is permitted.

## Current SQL migration order

1. `migrations/2026_08_02_tts_locale_resolution.sql`
2. `migrations/2026_08_02_tts_global_platform_schema.sql`
3. `migrations/2026_08_02_tts_global_provider_model_seed.sql`
4. `migrations/2026_08_02_tts_global_locale_voice_model_seed.sql`
5. `migrations/2026_08_02_tts_global_routing_seed.sql`
6. `migrations/2026_08_02_tts_global_masterdata_revision.sql`

The production apply/preflight/validate/rollback scripts will be
finalized before this feature is eligible for production deployment.

## Secret handling

Development, release-candidate, and production validation MUST NOT
print secret values.

Never emit:
- API keys
- passwords
- bearer/access tokens
- JWTs
- database credentials
- connection strings
- storage account keys
- provider credentials
- `.env` contents
- rendered environment blocks

Validation may report only configuration state such as:

    AZURE_SPEECH_KEY=PRESENT
    ELEVENLABS_API_KEY=MISSING
    SARVAM_API_KEY=MISSING

Do not include unrestricted `env`, `printenv`, `.env` dumps, or
rendered `docker compose config` output in release evidence.

Secrets remain in the approved environment/secret store and are never
persisted into Git, migration SQL, validation output, or deployment
logs.
