# Production VM identification — read-only gate

Before invoking the production cutover launcher, identify the exact target VM from Azure inventory and verify its guest hostname with a read-only command. Do not infer production from a shared naming prefix.

Safe guest preflight:

```bash
hostname -s
printf 'canonical_env='; test -f /home/azureuser/workspace/desifaces/infra/.env && echo YES || echo NO
printf 'legacy_env='; test -f /home/azureuser/workspace/desifaces-v2/infra/.env && echo YES || echo NO
printf 'db='; docker inspect desifaces-db >/dev/null 2>&1 && echo PRESENT || echo ABSENT
printf 'redis='; docker inspect desifaces-redis >/dev/null 2>&1 && echo PRESENT || echo ABSENT
printf 'network='; docker network inspect df-net >/dev/null 2>&1 && echo PRESENT || echo ABSENT
```

This step is read-only and performs no production mutation.
