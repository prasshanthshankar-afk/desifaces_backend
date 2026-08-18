# desifaces-v3 EIP Engineering Gate

Status: Mandatory
Authority: #v3-core — Architecture & Integration Control
Applies to: desifaces-v3 design, implementation, migrations, integrations, deployment changes, and cross-service decisions

## 1. Policy

EIP is a required engineering control for desifaces-v3. It is not an optional documentation source and it is not a post-implementation review step.

For every material V3 change, EIP evidence must be gathered before the target design or implementation is finalized. The evidence must establish the relevant V2 current state, dependencies, contracts, schemas, runtime behavior, tests, and operational assumptions that the V3 change will preserve, adapt, migrate, or replace.

A material change must not be merged into the desifaces-v3 integration branch without an EIP evidence record.

## 2. Material changes

The EIP gate applies when a change affects one or more of the following:

- canonical domain contracts or shared libraries;
- REST, gRPC, event, webhook, or internal service contracts;
- database schemas, migrations, masterdata, ownership, or persistence behavior;
- authentication, authorization, account ownership, API keys, or service identity;
- pricing, subscriptions, entitlements, credits, ledger behavior, or payment-provider handling;
- media identity, storage, lineage, sharing, retention, or deletion;
- Face, Audio, Fusion, Story, Conversation, Director, or other generation workflows;
- provider/model selection, routing, failover, safety, or normalization;
- service boundaries, orchestration, queues, workers, Redis usage, or asynchronous job lifecycle;
- deployment topology, Docker/Compose, environment configuration, network boundaries, ports, secrets, or runtime dependencies;
- frontend/mobile behavior when it changes a backend contract or authoritative business rule;
- migration or compatibility behavior between V2 and V3.

Pure formatting, comments, spelling, and non-architectural documentation-only changes may be exempt.

## 3. Mandatory lifecycle

Every material change follows this sequence:

```text
Requirement
  -> EIP retrieval of V2 current state
  -> Evidence record
  -> Current-state dependency map
  -> V3 design decision
  -> Contract/schema/runtime impact
  -> Compatibility and migration plan
  -> Implementation
  -> Tests and certification
  -> #v3-core freeze/update
```

The design step may not precede the evidence step except for an explicitly labeled hypothesis that is not yet approved for implementation.

## 4. Evidence requirements

Each material change must add or update a record under:

`docs/v3-core/evidence/`

The record must identify:

- Change-ID;
- EIP repository/ref used;
- EIP queries or retrieval objectives;
- concrete V2 evidence: code paths, handlers, schemas/migrations, configuration, tests, runtime observations, or operational evidence;
- the design decision derived from that evidence;
- reuse / adapt / migrate / replace / not-applicable disposition;
- API impact;
- database impact;
- compatibility impact;
- security impact;
- test/certification evidence.

An evidence record may cite runtime evidence stored outside Git when secrets or large diagnostics must not be committed. In that case, record the safe evidence path/identifier and a non-sensitive summary.

## 5. Evidence quality rules

An EIP record is insufficient if it contains only generic architecture principles. It must establish the actual V2 implementation relevant to the change.

Good evidence includes exact repository paths, symbols, migration names, table names, endpoint/handler mappings, service configuration, provider adapters, test names, or runtime observations.

The following are prohibited as substitutes for evidence:

- assumptions based on memory;
- redesigning from a preferred target pattern without inspecting V2;
- copying a V2 implementation merely because it exists;
- treating frontend behavior as authoritative for backend pricing/auth/entitlement state;
- adding tables because a V3 domain object exists;
- exposing provider-specific state as the V3 canonical contract without an explicit #v3-core decision.

## 6. Decision dispositions

Each inspected V2 capability receives one explicit disposition:

- `REUSE` — preserve the implementation/contract substantially as-is;
- `ADAPT` — retain the implementation but place it behind a V3 contract or adapter;
- `MIGRATE` — evolve data or behavior to a new canonical V3 representation;
- `REPLACE` — retire the existing implementation after compatibility/cutover requirements are met;
- `NOT_APPLICABLE` — evidence inspected but not relevant to the requested change.

`REPLACE` requires the strongest evidence: current consumers, migration path, rollback/cutover plan, and equivalence tests must be documented.

## 7. Pull-request gate

Material V3 changes are expected to arrive through a branch and pull request targeting `desifaces-v3`.

The repository CI EIP gate validates that a material PR includes a changed evidence record and that mandatory evidence fields are populated. CI validates presence and completeness; architecture review validates relevance and correctness.

Direct development on `desifaces-v3` should be limited to controlled bootstrap/governance work. Feature implementation should use dedicated V3 branches and PRs so the gate can be enforced before integration.

## 8. EIP and #v3-core responsibilities

EIP answers: **What actually exists and how does it behave?**

#v3-core answers: **Given that evidence, what will V3 do and what is now frozen?**

Implementation streams answer: **How do we implement the frozen decision safely?**

No implementation stream may reinterpret shared architecture independently of #v3-core.

## 9. Database-specific gate

Before every V3 migration:

1. retrieve current V2 and current V3 schema usage through EIP/current-state evidence;
2. identify readers, writers, foreign keys, indexes, constraints, background jobs, and external dependencies;
3. choose reuse/additive/migrate/replace;
4. define forward migration and rollback/compensating strategy;
5. run only against the isolated V3 database first;
6. certify schema, data, API, and workflow behavior;
7. record evidence before integration.

No V3-specific migration may be executed against the V2 database.

## 10. V2 production-change reconciliation

V2 remains live while V3 is developed. Therefore #v3-core must periodically inspect V2 changes that occurred after the V3 baseline.

Each relevant V2 production fix receives one disposition for V3:

- port to V3;
- already superseded by V3;
- not applicable;
- defer with documented reason.

This prevents V3 from silently losing production fixes while the two lines run in parallel.

## 11. Definition of ready

A material V3 item is ready for implementation only when:

- the requirement is bounded;
- EIP retrieval is complete enough to identify current-state dependencies;
- an evidence record exists;
- the #v3-core decision is explicit;
- API/schema/compatibility/security impacts are known;
- implementation ownership and tests are identified.

## 12. Definition of done

A material V3 item is done only when:

- implementation conforms to the frozen decision;
- compatibility/migration behavior is tested;
- EIP evidence record contains final test/certification references;
- operational/runtime impact is documented;
- no V2 runtime/database was modified unintentionally;
- #v3-core documentation is updated when the decision changes the architecture baseline.
