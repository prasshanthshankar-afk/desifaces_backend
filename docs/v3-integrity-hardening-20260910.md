# V3 integrity hardening — 2026-09-10

This hardening cycle is a non-production gate triggered by the Multi-Person dev integrity audit.

## Root-cause classes

1. Mutable dev checkout drift must never be a deployment source of truth.
2. Director API/worker lifecycle must be restart-safe and image-aligned.
3. Director worker must survive transient DNS/DB interruptions and requeue transient run failures within the technical attempt budget.
4. Audio canonical-output/read-url routes must be certified from the running API, including router registration files.
5. Certification must never print fully-resolved Compose environment values.
6. Local virtual environments must not be tracked or included in secret scans.

## Release rule

All repair/deployment actions must execute from an immutable Git worktree pinned to a commit SHA. The mutable `$HOME/workspace/desifaces-v3` checkout is permitted only as a source of the untracked `infra/.env` file and for Git object transport. No production promotion is permitted until the read-only integrity audit and the end-to-end Multi-Person acceptance gates pass in dev.
