# V3 Production Data Preservation — 2026-09-12

## Intent

Production customer/business data is authoritative and MUST remain in the existing production PostgreSQL database. DEV data MUST NOT be copied, restored, synchronized, or used as a source of truth for production.

## Production database mutation boundary

For the Sep 12 launch delta from the currently deployed production runtime (`793db700365e6d0fcbf9345b97737afac497afc0`) to backend application candidate (`18dfd6a3a4941307a466960108e4573f3b9ff555`), source history contains exactly one newly introduced SQL migration file:

- `migrations/2026_09_12_audio_tts_cost_basis.sql`

That migration is allowlisted. No migration manifest is permitted for this cutover.

## Required controls

- Existing production `desifaces-db` remains the live database container and data volume.
- Existing production `desifaces-redis` remains untouched.
- Take a full backup of the live production database before any DB mutation.
- Restore the backup only into a uniquely named temporary validation database, never over the live production database.
- Apply the allowlisted Audio COGS migration to that temporary clone first.
- Apply only the same allowlisted migration to the live database after clone certification passes.
- Do not import or restore DEV database dumps or DEV master/customer data.
- Do not run `deploy/production/migrations-v3-production-20260903.txt` for this cutover.
- Customer pricing rows must remain byte-equivalent across the Audio COGS migration; the migration may change only its effective-dated provider-cost component in `public.pricing_sku_costs`.
- Application rollout recreates only changed Audio and Director runtimes plus the Web runtime. PostgreSQL and Redis are never recreated.

## Explicitly forbidden

- DEV -> PROD database restore
- full production database replacement
- production database volume replacement
- `docker compose down -v`
- `docker volume prune`
- migration-manifest replay
- `TRUNCATE`, broad `DELETE`, or broad data synchronization
- restoring the validation database into the live production database

## Evidence

The production host audit showed an existing canonical production release, running `desifaces-db`, running `desifaces-redis`, `df-net`, production nginx bindings, and healthy public Web/Director/Assistant surfaces. The Azure VM resource is named `desifaces-gpu-non-prod` while the guest hostname is `desifaces-gpu`; production identity is therefore determined by the public endpoint/runtime proof, not by the legacy Azure resource name alone.

## Final gate

`PROD_DATA_SOURCE=EXISTING_PRODUCTION_ONLY`

`DEV_DATA_IMPORT=FORBIDDEN`

`LIVE_DB_RESTORE=FORBIDDEN`

`ALLOWLISTED_LIVE_DB_MIGRATIONS=1`
