#!/usr/bin/env bash
set -euo pipefail

CONTAINER="df-svc-core"

if ! /usr/bin/docker inspect "$CONTAINER" >/dev/null 2>&1; then
  echo "notification-dispatch: container $CONTAINER not found"
  exit 1
fi

RUNNING="$(
  /usr/bin/docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || true
)"
if [ "$RUNNING" != "true" ]; then
  echo "notification-dispatch: container $CONTAINER is not running"
  exit 1
fi

HEALTH="$(
  /usr/bin/docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}unknown{{end}}' "$CONTAINER" 2>/dev/null || true
)"
if [ "$HEALTH" != "healthy" ]; then
  echo "notification-dispatch: container $CONTAINER health is '$HEALTH'"
  exit 1
fi

exec /usr/bin/docker exec "$CONTAINER" python -m app.scripts.run_notification_dispatcher --limit 200
