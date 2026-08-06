# Global audio production impact ledger

## G1 — database/masterdata foundation

### Database migration
- `2026_08_02_tts_locale_resolution.sql`
- `2026_08_02_tts_global_platform_schema.sql`
- `2026_08_02_tts_global_provider_model_seed.sql`
- `2026_08_02_tts_global_locale_voice_model_seed.sql`
- `2026_08_02_tts_global_routing_seed.sql`
- `2026_08_02_tts_global_masterdata_revision.sql`

### Masterdata
- global languages
- canonical locales
- locale aliases
- locale context relationships
- TTS providers
- provider models
- model/language capabilities
- model/locale capabilities
- voice/locale capabilities
- voice/model capabilities
- routing policy
- routing weights
- masterdata revision triggers

### Current routing state
- Azure: enabled
- ElevenLabs: disabled
- Sarvam: disabled

## G2 — backend resolution

### Application code
- `services/svc-audio/app/app/repos/tts_catalog_repo.py`
- `services/svc-audio/app/app/services/tts_model_resolver.py`
- `services/svc-audio/app/app/repos/locale_catalog_repo.py`
- `services/svc-audio/app/app/services/locale_resolver.py`

### Tests
- `services/svc-audio/tests/test_locale_resolver.py`
- `services/svc-audio/tests/test_tts_model_resolver.py`

### Public API impact
None at G2B.

### Runtime behavior impact
None at G2B. Resolver is not yet wired into synthesis.

### Environment/config impact
None at G2B.

### Production deployment impact
Application files must deploy with the same release commit after the
database migrations have passed validation.

### Rollback
G2B/G2C are side-by-side only and no runtime route depends on them.

Production rollback MUST use an exact Git release/commit rollback or a
previous immutable deployment artifact. Do not manually delete or edit
individual production application files.

## G2C — provider-neutral voice resolution

### Application code
- `services/svc-audio/app/app/repos/tts_catalog_repo.py`
- `services/svc-audio/app/app/services/tts_voice_resolver.py`

### Tests
- `services/svc-audio/tests/test_tts_voice_resolver.py`

### Database impact
None.

### Masterdata impact
None.

### Public API impact
None.

### Runtime behavior impact
None. G2C remains side-by-side and is not yet wired to synthesis.

### Environment/config impact
None.

### Production deployment impact
Deploy only as part of the exact version-controlled application release
after the G1 SQL migration and validation gates succeed.

### Rollback
Rollback the exact application release artifact / Git release. Never
manually remove individual production files.

## G2C.1 — test isolation and source provenance

### Application code
- `services/svc-audio/app/app/repos/tts_catalog_repo.py`

The repository no longer requires `asyncpg` at module-import time merely
for a Pool type annotation. Runtime database access continues to use the
pool object injected by svc-audio.

### Database impact
None.

### Masterdata impact
None.

### Runtime behavior impact
None.

### Public API impact
None.

### Environment/config impact
None.

### Validation requirement
Resolver certification MUST verify that imported `app.*` modules resolve
from the exact release worktree/artifact under test. Tests must not
silently import application modules from another installed or historical
release directory.

### Production impact
No new production package/dependency is introduced by this change.

### Rollback
Rollback the exact immutable application release/Git release. Never
manually edit or delete individual files in production.

## G2D — immutable TTS resolution plan

### Application code
- `services/svc-audio/app/app/services/tts_resolution_planner.py`

### Validation
- `services/svc-audio/tests/test_tts_resolution_planner.py`
- `deploy/audio-global/validate_source_provenance.py`

### Resolution sequence
1. Resolve user/client locale using SQL-backed locale masterdata.
2. Resolve routing-enabled provider/model using SQL capability data.
3. Resolve eligible provider-native voice using SQL capability data.
4. Cross-check provider/model/locale consistency.
5. Return an immutable resolution plan.

### Database impact
None.

### Masterdata impact
None.

### Public API impact
None.

### Runtime synthesis impact
None. G2D remains side-by-side.

### Environment/config impact
None.

### Production validation requirement
Source-provenance validation must confirm every module under test/deploy
comes from the exact immutable release artifact being promoted.

The `app` package may be a Python namespace package; validation therefore
uses `app.__path__` plus concrete module `__file__` values rather than
requiring `app.__file__`.

### Rollback
Deploy the prior immutable application release. Never manually delete or
edit individual production files.

## G2D.1/G2E — complete unit and live database validation

### Validation assets
- `deploy/audio-global/run_unit_validation.sh`
- `deploy/audio-global/validate_live_resolution.py`

### Unit-test requirement
The complete svc-audio test discovery pattern is `test_*.py`.
A narrower resolver-only pattern is not sufficient because it can omit
the composed resolution-planner tests.

### Live integration validation
The provider-neutral resolution plan is executed from an ephemeral
svc-audio container using:
- the real svc-audio image and Python dependencies
- the real dev service environment
- the real Compose network
- a read-only mount of the exact application source under validation
- the live dev PostgreSQL TTS masterdata

### Database impact
Read only. The masterdata revision must be identical before and after
the live validation.

### Runtime impact
None. The running svc-audio API and worker are not restarted or
modified. Docker Compose creates an ephemeral validation container and
removes it after completion.

### Production requirement
The same validation assets must run against the immutable production
release candidate after database migration and before provider
activation.

### Rollback
No rollback is needed for this read-only validation. Application
rollback remains deployment of the prior immutable release artifact.

## G2E.1 — ResolvedLocale integration contract

### Finding
`LocaleResolver.resolve()` returns a `ResolvedLocale` domain object,
not a raw locale string.

The planner must use the object's canonical `.locale` field when
querying provider/model masterdata.

### Application change
- `services/svc-audio/app/app/services/tts_resolution_planner.py`

### Test change
- `services/svc-audio/tests/test_tts_resolution_planner.py`

Planner tests now model the real LocaleResolver object contract.

### Database impact
None.

### Masterdata impact
None.

### Secret/config impact
None.

### Runtime impact
None. The provider-neutral planner is still side-by-side and has not
replaced the current TTS synthesis route.

### Production validation
The complete test suite and live database resolution validation must
pass against the exact immutable release artifact.

### Rollback
Deploy the previous immutable application release artifact.
Never manually edit individual production files.

## G2G — DB-driven locale context refinement

### Application code
- `services/svc-audio/app/app/repos/locale_context_repo.py`
- `services/svc-audio/app/app/services/locale_context_resolver.py`
- `services/svc-audio/app/app/services/tts_resolution_planner.py`

### Tests
- `services/svc-audio/tests/test_locale_context_resolver.py`

### Database/schema impact
None.

### Masterdata impact
None in G2G.

G2G consumes the already-versioned `tts_locale_context_rules`
masterdata created by the G1 global platform migration.

### Resolution semantics
- Explicit regional locale is authoritative and is never rewritten by
  physical/user geography.
- Generic language locale may be refined only when every supplied
  context dimension matches SQL masterdata.
- No matching context rule preserves the generic locale so future
  multilingual models may use language-level capability.
- An unresolved top context tie fails closed.

### Production deployment
No manual production DB changes are required for G2G.

The existing G1 migration must first create and seed
`tts_locale_context_rules`; the application release then consumes those
rules.

Future provider catalog synchronization that creates new regional
locales must also maintain their context rules transactionally.

### Secret handling
No new secrets or configuration.

### Rollback
Deploy the previous immutable application release artifact.
