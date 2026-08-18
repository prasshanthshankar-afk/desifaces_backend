# V3 EIP Evidence Record — Parallel Runtime Isolation

Change-ID: `V3-C2C`
Status: `IMPLEMENTATION_IN_PROGRESS`
Owner: `#v3-core`
Date: `2026-08-17`

## 1. Requirement

Run desifaces-v2 and desifaces-v3 in parallel on the same Azure development VM without V3 colliding with, mutating, or accidentally depending on V2 runtime state. V3 is the next-generation development line for the same product and will eventually replace V2 after certification and migration; it is not a permanent second product.

The bounded V3-C2C change covers Docker/Compose project identity, networks, PostgreSQL connection, Redis, container names, host ports, service-to-service URLs, worker/runtime dependencies, environment injection, secrets handling, and storage/media isolation. No feature/API/domain redesign is included in this increment.

## 2. EIP source

- EIP repository: `prasshanthshankar-afk/desifaces-eos`
- EIP ref/commit: `feature/eos-foundation` / `d52dd8772fb8c4882a9d882ebcea61b72b9a08d2`
- Retrieval objectives:
  - establish desifaces integration/runtime isolation principles;
  - establish requirements for service contracts, async jobs, provider normalization, resilience, telemetry, and secret handling;
  - ensure AI-assisted V3 changes retrieve current standards and source evidence before patching.
- Retrieval references:
  - `ekb/06-integration/Integration_Architecture_Standard.md`
  - `ekb/07-security/Security_Architecture_Standard.md`

EIP findings relevant to V3-C2C:

- integrations must be explicit, observable, resilient, and replaceable;
- backend services own canonical rules and API contracts must be stable;
- AI-assisted code generation must retrieve standards and relevant source before proposing patches;
- async generation uses job-based processing and requires stable lifecycle semantics;
- provider-specific state must be normalized behind backend records;
- retry/idempotency behavior must prevent duplicate expensive operations;
- secrets must never enter source, docs, RAG, or logs and runtime secrets belong in environment-specific secret stores.

## 3. V2 current-state evidence

### Code and service ownership

- Repository/ref: `prasshanthshankar-afk/desifaces_backend` V2 baseline `70a80cef08cebb8f02385a8e0f1adbac7c85fbb8`; V3 was branched from that exact baseline.
- Primary runtime definition inspected: `docker-compose.yml` inherited into `desifaces-v3`.
- The Compose file currently declares `name: desifaces`.
- Common Python services attach to network `df-net` and depend on `desifaces-db` plus `desifaces-redis`.
- Explicit `container_name` values use V2 identities such as `desifaces-db`, `desifaces-redis`, `df-svc-core`, `df-svc-face`, `df-svc-face-worker`, `df-svc-audio`, `df-svc-audio-worker`, `df-svc-fusion`, `df-svc-fusion-worker`, `df-svc-pricing`, `df-svc-commerce`, `df-svc-dashboard`, `df-svc-fusion-extension`, `df-svc-music`, and `df-svc-marketing` plus corresponding workers/schedulers.
- The bottom-level network is explicitly named `df-net`, making the current file unsafe to start unchanged beside V2.

### API/contracts

V3-C2C does not change external API contracts. It must preserve internal service DNS contracts inside the isolated V3 network where possible.

Observed internal URLs/defaults include:

- `http://svc-pricing:8009`
- `http://svc-core:8000`
- `http://svc-face:8003`
- `http://svc-audio:8004`
- `http://svc-fusion:8002`
- `http://svc-commerce:8008`
- `http://svc-music:8000`

These indicate that service-to-service coupling is primarily through Compose service names, not current V2 container names. Therefore V3 can preserve service names inside a V3-only network while changing project/container/host identities.

### Persistence

V3-C2B has already produced and certified a physically isolated V3 PostgreSQL clone.

V2:

- container: `desifaces-db`
- database: `desifaces_dev`
- volume: `desifaces-dev_df_pgdata`
- host port: `127.0.0.1:15432`

V3:

- container: `desifaces-v3-db`
- database: `desifaces_v3`
- volume: `desifaces-v3_df_pgdata`
- host port: `127.0.0.1:25432`

Certification evidence directory:

`/home/azureuser/backups/desifaces-v3-bootstrap/20260818T003138Z`

Certified results:

- snapshot checksum PASS;
- restore PASS;
- source tables 161;
- target tables 161;
- exact row-count parity PASS;
- normalized schema parity PASS;
- column structure parity PASS;
- index parity PASS;
- constraint parity PASS;
- V2 and V3 database volumes are different;
- both PostgreSQL containers are simultaneously healthy.

### Runtime/configuration

Source evidence from inherited `docker-compose.yml`:

- common `env_file`: `./infra/.env`;
- application services receive `DATABASE_URL` and `REDIS_URL` through environment variables;
- PostgreSQL uses persistent volume `df_pgdata`;
- Redis uses persistent volume `df_redisdata` and AOF (`--appendonly yes`);
- additional writable/local runtime volumes exist for saree/template caches, Hugging Face cache, marketing output, and other workers;
- Audio mounts `./services/shared` read-only;
- Pricing mounts `/opt/desifaces/secrets/google_play_service_account.json` read-only;
- Face/Audio/Fusion/Commerce/Music/Marketing use Azure storage configuration and named output containers;
- workers share DB/Redis/storage/provider configuration with their associated API services;
- host API ports in V2 source include 8000 core, 8002 fusion, 8003 face, 8004 audio, 8005 dashboard, 8006 fusion extension, 8007 music-to-container-8000, 8008 commerce, 8009 pricing, 8010 marketing;
- V2 PostgreSQL host port is 15432.

Observed VM runtime evidence from the V3-C2A discovery confirms live V2 containers including `desifaces-db`, `desifaces-redis`, APIs and workers for Core, Face, Audio, Fusion, Fusion Extension, Dashboard, Pricing, Commerce, Music, Marketing, and related workers/schedulers.

Runtime evidence path:

`/home/azureuser/backups/eip/v3-core-persistence-inventory-20260817T213855Z`

### Tests/operations

- API services use HTTP `/api/health` checks through the common Compose healthcheck where applicable.
- Database has `pg_isready` health checking.
- Several background workers intentionally disable HTTP health checks or use process-level checks.
- V2 is currently operating and must remain untouched while V3 runtime topology is created.

## 4. Evidence gaps

The following runtime facts must be established from the live VM before the V3 Compose design is finalized:

- exact current V2 Compose project labels and active network membership for every container;
- exact current Redis container volume and whether Redis data is semantically safe to clone or should start empty for V3;
- non-secret resolved `DATABASE_URL` and `REDIS_URL` host/database/index shape used by live V2 containers;
- Redis database numbers / key prefixes used by each service and worker;
- all live host port bindings, including services that source Compose exposes broadly but runtime binds to loopback;
- all writable bind mounts and named volumes actually attached to live V2 services;
- whether external schedulers/systemd/cron invoke one-shot Compose services or scripts by V2 container/project name;
- Azure storage account/container names used by V2 and whether V3 writes must use separate V3 containers/prefixes;
- any external reverse-proxy/nginx/Cloudflare routes currently pointing at V2 host ports;
- runtime environment-variable names relevant to service URLs, queues, Redis, storage, schedulers, and providers, without exposing values/secrets.

No runtime design decision may silently infer these gaps.

## 5. V3 disposition

Disposition: `ADAPT`

Rationale:

The working V2 service topology and internal service-name contracts should be retained initially because they represent proven runtime behavior. The deployment wrapper must be adapted so V3 owns a separate Compose project, network, containers, ports, PostgreSQL, Redis, writable volumes, and environment configuration. Shared provider credentials may only be reused where explicitly safe; writable product state must not be shared by accident.

## 6. #v3-core architecture decision

Preliminary decision pending closure of section 4 evidence gaps:

1. V2 continues running unchanged.
2. V3 runs as a side-by-side environment on the same development VM.
3. V3 must have an explicit Compose project identity independent of V2.
4. V3 must use a V3-only bridge network.
5. V3 must connect only to `desifaces_v3` / `desifaces-v3-db`, never `desifaces_dev` / `desifaces-db`.
6. V3 must have an independent Redis runtime and persistence unless EIP evidence proves a specific shared read-only/cache case is safe; default is isolation.
7. Internal Compose service names should remain stable (`svc-core`, `svc-pricing`, etc.) inside the V3 network so service-to-service URLs need minimal application changes.
8. V3 host ports must not collide with V2 and should bind to loopback in development unless externally required.
9. V3 writable Docker volumes must use V3-specific identities.
10. V3 secrets/configuration must be stored outside source and never copied into EIP evidence.
11. Azure/media output writes require explicit isolation before generation services are enabled; the default is separate V3 containers or an evidence-backed V3 prefix strategy.
12. V3 must not start from the inherited Compose file until these isolation changes are implemented and validated.

This decision becomes frozen only after the live runtime probe closes the remaining gaps.

## 7. Contract impact

- Canonical contract changes: none.
- Versioning impact: none.
- Compatibility adapter required: no application-level adapter for this increment.
- Client impact: none until a V3 endpoint is intentionally exposed.

## 8. Database impact

- Schema change: none for V3-C2C.
- Migration file: N/A.
- Data backfill/reconciliation: already completed as V3-C2B initial clone.
- Rollback/compensating action: stop/remove only V3 runtime containers/network/volumes created by V3-C2C; never remove V2 resources.
- Confirm V3-only DB execution: V3 database is `desifaces_v3` in `desifaces-v3-db` on isolated volume `desifaces-v3_df_pgdata`.

## 9. Security and privacy impact

- Authentication: preserve current V2-derived behavior initially; V3 runtime must use explicit V3 environment configuration.
- Authorization/account ownership: no semantic change in V3-C2C.
- Secrets: must remain outside Git/EIP; source-embedded fallback secrets must not be introduced into V3 runtime controls.
- PII/media/privacy: V3 generated media must not silently write into V2 writable media namespaces.
- Audit requirements: record environment identity, service startup/certification, and isolation evidence without secret values.

## 10. Pricing/entitlement/credit impact

- Pricing: no rule change.
- Entitlement: no rule change.
- Credits/ledger/idempotency: V3 runtime must operate only against the V3-cloned database; V2 ledger must remain untouched.
- Provider billing events: external webhook/provider routing into V3 is out of scope until explicit endpoint isolation and replay/idempotency strategy are defined.

## 11. Provider/model impact

- Provider-specific behavior inspected: Compose passes provider credentials/configuration into Face, Audio, Fusion, Commerce, Music, Marketing, and pricing/payment adapters.
- Canonical normalization: unchanged in V3-C2C.
- Routing/failover impact: none intended; provider execution must not be enabled against shared writable product state before storage and callback isolation are certified.

## 12. Implementation scope

Expected V3-only changes after evidence closure:

- `docker-compose.yml` or a V3-specific Compose layer/override;
- V3 environment template/configuration controls that contain names/placeholders only, never secrets;
- runtime guard scripts/tests preventing V3 from resolving V2 DB/Redis/network/container identities;
- documentation and certification scripts under `docs/v3-core` / `infra/scripts` as appropriate.

Explicitly out of scope:

- V2 source/runtime changes;
- canonical API redesign;
- database schema migrations;
- Face/Audio/Fusion functional redesign;
- pricing rule changes;
- production cutover.

## 13. Compatibility / migration strategy

V2 remains the active environment. V3 starts from the V2-derived code and cloned database but runs on isolated infrastructure. Internal service DNS contracts are preserved where possible. No V2 consumer is redirected to V3 during V3-C2C. V3 endpoints remain development-only until later certification. Final product migration/cutover is governed separately by #v3-core.

## 14. Test and certification plan

- Static Compose validation: V3 config parses with required environment file and no V2 container/network/volume identities.
- Collision test: all V2 containers remain running and healthy while V3 starts.
- Database guard: V3 service `DATABASE_URL` resolves to V3 DB only; fail startup if V2 database/container/port is detected.
- Redis guard: V3 services resolve V3 Redis only.
- Network guard: no V3 application container joins V2 `df-net`.
- Volume guard: no writable V3 volume equals a V2 writable volume.
- Port guard: V2 and V3 host bindings are distinct.
- Service-DNS test: V3 internal calls resolve V3 `svc-*` services within the V3 network.
- Health test: each API and required worker reaches its expected healthy/running state.
- Storage guard: V3 writes use explicitly isolated V3 storage namespace before generation jobs are allowed.
- V2 regression protection: V2 health endpoints and container states remain unchanged throughout certification.

## 15. Final certification evidence

Pending.

- Commit/PR: pending implementation branch/PR.
- Test result: pending.
- Runtime evidence: current V2 discovery at `/home/azureuser/backups/eip/v3-core-persistence-inventory-20260817T213855Z`; V3-C2C runtime probe pending.
- Migration/schema evidence: V3-C2B certified at `/home/azureuser/backups/desifaces-v3-bootstrap/20260818T003138Z`.
- #v3-core document updated: pending final freeze.

## 16. Freeze statement

Current freeze: V3-C2C may not start the inherited V2 Compose definition unchanged. V2 remains running and untouched. V3 must use its already isolated PostgreSQL clone and must establish separate project/network/Redis/ports/writable volumes/configuration before the V3 application stack is started. The exact runtime mappings remain DRAFT until the live VM probe closes the evidence gaps listed in section 4.


## V3-C2C Implementation 2 — Infrastructure Certification

Infrastructure adoption has been certified on the Azure development VM.

Certified topology:

- V2 Compose project remains `desifaces-dev`.
- V2 network remains `df-net`.
- V3 Compose project is `desifaces-v3`.
- V3 network is `df-v3-net`.
- V3 PostgreSQL container is `desifaces-v3-db`.
- V3 PostgreSQL database is `desifaces_v3`.
- V3 PostgreSQL user is `desifaces_v3_admin`.
- V3 PostgreSQL host binding is `127.0.0.1:25432`.
- V3 PostgreSQL persistent volume is `desifaces-v3_df_pgdata`.
- V3 Redis container is `desifaces-v3-redis`.
- V3 Redis persistent volume is `desifaces-v3_df_redisdata`.
- V3 Redis begins with an empty logical DB 0 and AOF persistence enabled.
- V3 infrastructure resolves internal aliases `desifaces-db` and
  `desifaces-redis` only inside the isolated V3 network.
- V3 execution workers remain profile-gated and disabled by default.
- No V3 application API container was started during infrastructure adoption.

Certification results:

- `V3_INFRA_PRECONDITIONS=PASS`
- `V3_DB_STORAGE_GUARD=PASS`
- `V3_COMPOSE_DB_HEALTH=PASS`
- `V3_DB_COMPOSE_OWNERSHIP=PASS`
- `V3_DB_NETWORK_ISOLATION=PASS`
- `V3_DB_DATA_PARITY_AFTER_ADOPTION=PASS`
- `V3_REDIS_PING=PASS`
- `V3_REDIS_EMPTY=PASS`
- `V3_REDIS_AOF=PASS`
- `V3_REDIS_VOLUME=PASS`
- `V3_REDIS_NETWORK_ISOLATION=PASS`
- `V3_INTERNAL_DNS=PASS`
- `V2_V3_VOLUME_ISOLATION=PASS`
- `POSTGRES_PORT_ISOLATION=PASS`
- `V3_APPLICATION_CONTAINERS=NONE`
- `V2_HEALTH_UNCHANGED=PASS`

Runtime evidence:

`/home/azureuser/backups/eip/v3-c2c-infra-adoption-20260818T012304Z`

The PostgreSQL table-count signature before and after Compose adoption was
identical:

`068c10c9c9117841e783a29607adda3e8e07ef484b0a119f3859dbcf638b7e94`

This certifies infrastructure isolation only. V3 API startup and execution
workers remain separately gated.

