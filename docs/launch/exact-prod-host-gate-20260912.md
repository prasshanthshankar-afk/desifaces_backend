# Exact production host gate — 2026-09-12

Production cutover must be authorized with `DESIFACES_PRODUCTION_HOSTNAME` set to the exact certified production VM hostname. Prefix matching is forbidden because DEV/non-production VM names may share the `desifaces-gpu` prefix.

Before any canonical source mutation, the launcher requires:

- `DESIFACES_PRODUCTION_CUTOVER_APPROVED=YES`;
- non-empty exact `DESIFACES_PRODUCTION_HOSTNAME`;
- `hostname -s` equals that value exactly;
- hostname is not `desifaces-dev`;
- hostname does not contain `non-prod` or `nonprod`;
- GitHub auth and immutable backend/private-Web release access;
- live DB/Redis/network and production environment presence.

This is a safety hardening only. It performs no production mutation.
