# V3 EIP Evidence Record

Change-ID: `V3-ADMIN-SUPER-ADMIN-20260830`
Status: `READY`
Owner: `V3 Admin workstream`
Date: `2026-08-30`

## 1. Requirement

Add a governed administrator-management interface to V3. Existing operational Admin users must not be able to grant or revoke administrator authority. A new `super_admin` role extends the existing Core RBAC model and exclusively governs `admin` and `super_admin` assignment.

## 2. EIP source

- EIP repository: `not separately retrieved for this bounded change`
- EIP ref/commit: `N/A`
- Retrieval objective(s):
  - establish existing V3 authentication, Core RBAC, user-role persistence, Admin routes, and audit behavior
- Retrieval query/command/reference:
  - direct inspection of the V3 branch and existing Core RBAC/auth migrations, dependencies, Admin routes, tests, and audit implementation

The implementation does not claim an EIP retrieval that was not executed. The repository EIP gate is used as the governance control for this change; the current-state source evidence below is from the inspected V3 repository.

## 3. V2 current-state evidence

No V2 behavior is being migrated into this change. The relevant current-state evidence is the existing V3 Core implementation.

### Code and service ownership

- Repository/ref: `prasshanthshankar-afk/desifaces_backend / desifaces-v3`
- Service/path/symbol: `services/svc-core/app/app/deps.py`, `services/svc-core/app/app/routes/admin.py`
- Current owner/responsibility: Core owns authentication, users, live role resolution, and privileged Admin authorization.

### API/contracts

- Endpoint/event/contract: `/api/admin/context`, `/api/admin/users`, explicit role mutation endpoints
- Handler/service: `svc-core / app.routes.admin`
- Consumers: V3 web Admin Console and its server-side route guard

### Persistence

- Schema/table/migration: `core.roles`, `core.user_roles` from `migrations/010_core_auth.sql`; `core.audit_log` from `migrations/011_core_audit_and_feature_flags.sql`
- Readers: Core authentication/Admin authorization
- Writers: explicit Core privileged-role mutations and one-time bootstrap script
- FK/index/constraint dependencies: `core.user_roles` references Core users and roles and has a composite primary key; `role_key` is unique.

### Runtime/configuration

- Environment/config keys: `DATABASE_URL`, existing JWT/auth configuration
- Queue/worker/cache/storage/provider dependencies: none introduced
- Runtime evidence identifier/path: runtime deployment certification remains pending

### Tests/operations

- Existing tests: `test/test_v3_admin_authorization.py`
- Health/monitoring/runbook dependencies: existing svc-core health/runtime; no new external dependency

## 4. Evidence gaps

- Runtime database migration has not yet been applied/certified in the live V3 environment.
- Browser positive/negative certification has not yet been executed for the new Access Control interface.
- EIP knowledge retrieval was not separately executed for this bounded RBAC extension; direct current-state code/schema inspection was used and is explicitly recorded.

## 5. V3 disposition

Disposition: `ADAPT`

Rationale:

The existing `core.roles` and `core.user_roles` model is already extensible. The safest change is to add `super_admin` as one additional role and reuse the existing authorization and audit boundaries rather than create another entitlement system.

## 6. #v3-core architecture decision

Core RBAC remains the sole authority for administrator access. `super_admin` inherits operational Admin access, while only `super_admin` may grant/revoke `admin` or `super_admin`. Authorization is resolved live from Core on privileged requests; browser/JWT role claims are not trusted as the source of truth. The first Super Admin is initialized once through a controlled bootstrap operation; subsequent role governance occurs through the protected Admin Access Control UI.

## 7. Contract impact

- Canonical contract changes: add `super_admin` role semantics; add `/api/admin/access/administrators`; add explicit `super_admin` role grant/revoke endpoints; Admin context exposes `is_super_admin`
- Versioning impact: additive V3 Admin contract
- Compatibility adapter required: no
- Client impact: V3 web Admin Console recognizes Admin and Super Admin; Access Control is displayed only for Super Admin

## 8. Database impact

- Schema change: additive data seed only; no new table/column
- Migration file: `migrations/2026_08_30_v3_admin_super_admin_role.sql`
- Data backfill/reconciliation: none
- Rollback/compensating action: revoke assigned `super_admin` roles before removing the role seed if rollback is ever required
- Confirm V3-only DB execution: migration is committed only on the V3 Admin feature branch; live execution remains a certification gate

## 9. Security and privacy impact

- Authentication: unchanged
- Authorization/account ownership: stronger separation between operational Admin and administrator governance; live DB role state remains authoritative
- Secrets: none introduced
- PII/media/privacy: no new protected user data exposed by this change
- Audit requirements: every role grant/revoke and bootstrap event writes to append-only `core.audit_log`

## 10. Pricing/entitlement/credit impact

- Pricing: none
- Entitlement: no billing/subscription entitlement change; this is Core RBAC only
- Credits/ledger/idempotency: none
- Provider billing events: none

## 11. Provider/model impact

- Provider-specific behavior inspected: not applicable
- Canonical normalization: not applicable
- Routing/failover impact: none

## 12. Implementation scope

- Files/services expected to change:
  - `services/svc-core/app/app/deps.py`
  - `services/svc-core/app/app/routes/admin.py`
  - `migrations/2026_08_30_v3_admin_super_admin_role.sql`
  - `scripts/bootstrap_super_admin.py`
  - `test/test_v3_admin_authorization.py`
  - V3 web Admin Console/server guard
- Explicitly out of scope:
  - billing/subscription entitlement redesign
  - parallel Admin-role tables
  - non-admin V3 feature development

## 13. Compatibility / migration strategy

Existing `admin` assignments continue to provide operational administration. They no longer authorize role governance. `super_admin` is additive and inherits operational Admin access, preventing a disruptive migration. The first Super Admin is created through a single controlled bootstrap after the role migration is applied. Existing user/admin behavior outside administrator governance remains unchanged.

## 14. Test and certification plan

- Unit tests: live Admin DB authorization; live Super Admin authorization; stale token rejection; inactive role-holder rejection
- Contract tests: Admin context, administrator roster, Admin and Super Admin grant/revoke route registration
- Integration tests: Core role lookup against migrated V3 DB
- Migration tests: idempotent creation of `super_admin` in existing `core.roles`
- Runtime/end-to-end certification: bootstrap first Super Admin; Super Admin sees Access Control; can grant/revoke Admin; ordinary Admin cannot call governance endpoints; normal user cannot access Admin APIs/UI
- V2 regression protection: no V2 schema/runtime modification

## 15. Final certification evidence

Complete before marking `CERTIFIED`.

- Commit/PR: backend draft PR #10; web draft PR #17
- Test result: V3 Canonical Contract Tests passed for the Super Admin backend implementation; final post-evidence gate rerun pending
- Runtime evidence: pending
- Migration/schema evidence: migration committed; live application pending
- #v3-core document updated: N/A for this bounded additive Admin role decision; this evidence record captures the decision

## 16. Freeze statement

`The V3 Admin authorization design is frozen around Core live RBAC with user, admin, and super_admin roles. Any future parallel entitlement store, browser-authoritative role decision, or expansion of administrator-governance authority beyond super_admin requires returning to #v3-core / #v3-admin architecture review.`
