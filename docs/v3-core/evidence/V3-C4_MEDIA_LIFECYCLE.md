# V3-C4 Canonical Media Lifecycle

Change-ID: `V3-C4`
Status: `READY_FOR_RUNTIME_CERTIFICATION`
Owner: `#v3-core`
Date: `2026-08-18`
Backend V2 evidence anchor: `70a80cef08cebb8f02385a8e0f1adbac7c85fbb8`
C3 baseline: `61876055c5f43cdd5032e6eadd37da5ab24a9ec4`
Implementation branch: `feature/v3-c4-c6-foundation-closure-20260818`

## 1. Requirement

Establish one durable, account-owned media lifecycle for desifaces-v3 before Multi-Person, Assistant/Conversation, Story/Director, and the rich web application create new media-producing flows.

The lifecycle must preserve existing V2 media IDs and storage references, distinguish source/intermediate/preview/final/thumbnail roles, record lineage and producing jobs, support account/project ownership, and prevent temporary signed URLs from becoming canonical identity.

## 2. EIP source

EIP repository: `prasshanthshankar-afk/desifaces-eos`
EIP ref: `feature/eos-foundation`
Primary standard: `ekb/06-integration/Integration_Architecture_Standard.md`

Relevant EIP rules:
- integrations and persisted boundaries must be explicit, observable, resilient, and replaceable;
- asynchronous generation requires durable job/media identity;
- final customer media must be distinguished from internal execution artifacts;
- provider/storage details must not contaminate canonical business contracts;
- retries and idempotency must not duplicate expensive generation or durable output identity.

## 3. V2 current-state evidence

Evidence shows an existing `public.media_assets` table and current services already use UUID `media_assets.id` values as reusable media identity.

Observed patterns include:
- Face source upload/output persistence through `MediaAssetsRepo`;
- Music asset upload currently dual-writes legacy artifact identity plus optional `media_asset_id`;
- Commerce already persists directly to `media_assets` and refreshes signed URLs from stable storage references;
- Dashboard/Saved Work project final media from existing persisted media/output records;
- Azure Blob is the durable object store while signed/SAS URLs are temporary delivery mechanisms;
- legacy `public.artifacts` remains in Fusion/Music paths and cannot be treated automatically as canonical `MediaAsset` identity.

C3 froze that existing asset APIs remain compatibility façades while canonical V3 logic uses `MediaAsset`.

## 4. Evidence gaps

Before certification:
- prove every inherited V3 `media_assets` row resolves to a canonical account after the C3 account-context repair;
- prove role backfill produces only canonical roles;
- prove lineage has no orphan references;
- prove canonical create/read/lineage behavior against the V3 database;
- prove certification leaves no synthetic media rows behind.

No provider execution or blob mutation is required for C4 certification.

## 5. V3 disposition

Disposition: `PRESERVE IDENTITY + NORMALIZE LIFECYCLE`.

`public.media_assets.id` is retained as canonical `MediaAsset.media_id`. A competing V3 media table is prohibited.

Existing external Face/Music/Commerce media responses remain compatibility contracts until explicit migration decisions. Internally, new V3 capabilities use the shared canonical media boundary.

## 6. #v3-core architecture decision

1. Existing `media_assets.id` UUIDs are canonical V3 media IDs.
2. Canonical media ownership is `account_id` plus optional `owner_user_id` and `project_id`.
3. Canonical roles are `source`, `intermediate`, `preview`, `final`, `thumbnail`.
4. Lifecycle state is explicit: `active`, `archived`, `deleted`.
5. Stable `storage_ref`/`az://...` identity is canonical `storage_uri`; signed/SAS URLs are delivery views only.
6. Content hash is used for safe per-user deduplication where available.
7. Media lineage is relational (`v3_media_asset_lineage`) and may link multiple source assets to one derived asset.
8. Producing canonical generation job is recorded independently of provider execution.
9. Soft deletion/archive does not delete blobs implicitly; retention/deletion execution is a separate operational policy.
10. Legacy `artifacts` may be mapped into MediaAsset only through evidence-backed compatibility logic; an arbitrary artifact UUID is not a media ID.

## 7. Contract impact

`df_contracts.v3.domain.MediaAsset` now includes lifecycle state, stable storage identity, hash/size/dimensions/duration, thumbnail, source lineage, producing job, retention and deletion timestamps.

No V2 public route is renamed.

## 8. Database impact

Migration: `migrations/2026_08_18_v3_media_lifecycle.sql`

Additive changes to `public.media_assets`:
- account/project ownership;
- role and lifecycle state;
- thumbnail and canonical generation-job linkage;
- retention/deletion timestamps.

New:
- `public.v3_media_asset_lineage`;
- `public.v3_media_assets` canonical read model.

No blob is deleted or rewritten by the migration.

## 9. Security and privacy impact

Account ownership becomes an explicit media invariant. Shared `CanonicalMediaStore.assert_owned()` prevents cross-account media references in new V3 flows. Soft-deleted media is excluded from normal ownership resolution.

## 10. Pricing/entitlement/credit impact

None directly. Media lifecycle does not reserve or commit credits.

## 11. Provider/model impact

None. Provider execution does not own canonical media identity.

## 12. Implementation scope

- `migrations/2026_08_18_v3_media_lifecycle.sql`
- `services/shared/df_contracts/v3/domain.py`
- `services/shared/python/desifaces_shared/v3/media_store.py`
- C4/C5 runtime certification script
- focused unit tests

## 13. Compatibility / migration strategy

Existing media IDs remain unchanged. Existing service-specific upload/status/library responses continue to work. New V3 functionality consumes `CanonicalMediaStore`; existing services can migrate incrementally without changing current mobile contracts.

The V3 clone is migrated first. Production migration is applied only through the later V3 cutover process after full product certification.

## 14. Test and certification plan

Certification requires:
- focused V3 unit tests pass;
- C4 migration applies only to `desifaces_v3`;
- zero inherited media rows without account context;
- zero invalid canonical roles;
- zero orphan lineage rows;
- synthetic source/final asset create + lineage + read roundtrip succeeds;
- transaction rollback restores original row counts;
- V2 services remain healthy;
- no V3 worker/provider execution is enabled.

Status becomes `CERTIFIED` only after runtime evidence passes.
