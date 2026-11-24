#!/usr/bin/env bash
# Runs ON THE VM as user "deploy" (called by .github/workflows/deploy.yml; can also be run by hand):
#   bash /opt/sparktext/.incoming/remote-deploy.sh sha-<40-hex-commit>
# Expects the new compose.prod.yml + Caddyfile next to this script (in /opt/sparktext/.incoming).
#
#   pull ─► migrate ─► switch ─► health check ──ok──► done (prune old images)
#                                     └──fail──► switch back to the previous tag, exit 1
#
# Migrations are NOT undone on rollback. That is why schema changes must stay backward-compatible
# for one release ("expand → deploy → contract"): the previous image must still work on the new schema.
set -euo pipefail

TAG="${1:?usage: remote-deploy.sh sha-<40-hex-commit>}"
[[ "$TAG" =~ ^sha-[0-9a-f]{40}$ ]] || { echo "refusing: '$TAG' is not sha-<40 hex>" >&2; exit 2; }

APP=/opt/sparktext
IN="$APP/.incoming"
cd "$APP"
compose() { docker compose -f compose.prod.yml "$@"; }
log() { echo "[deploy $(date -u +%H:%M:%S)] $*"; }

PREV="$(sed -n 's/^IMAGE_TAG=//p' .env 2>/dev/null || true)"
log "running: ${PREV:-<none>}"
log "target:  $TAG"

# 1) Config from git. The compose file is only read by the docker CLI, so replacing it is fine.
#    The Caddyfile is a single-file bind mount: Caddy keeps seeing the OLD file if we replace it
#    (new inode). "cat >" rewrites it in place (same inode), then Caddy reloads it gracefully.
install -m 640 "$IN/compose.prod.yml" compose.prod.yml
CADDY_CHANGED=0
if ! cmp -s "$IN/Caddyfile" Caddyfile; then
  cat "$IN/Caddyfile" > Caddyfile
  CADDY_CHANGED=1
fi

# 2) Pull first. If the registry or network fails here, production has not been touched yet.
log "pulling image"
IMAGE_TAG="$TAG" compose pull --quiet

# 3) Migrate as sparktext_migrator (one-off container, see compose "migrate" service).
log "migrating"
IMAGE_TAG="$TAG" compose --profile tools run --rm migrate

# 4) Switch. .env holds the running version, so plain "docker compose ..." keeps working by hand.
echo "IMAGE_TAG=$TAG" > .env
log "starting containers"
compose up -d --remove-orphans
if [[ "$CADDY_CHANGED" == 1 ]]; then
  log "Caddyfile changed: reloading Caddy (validates first; keeps the old config if invalid)"
  compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile
fi

# 5) Health check through the REAL path: Caddy (TLS) → Django → database + Redis.
log "health check"
healthy=0
for _ in $(seq 1 30); do
  if curl -fsS --max-time 5 --resolve app.sparktext.si:443:127.0.0.1 https://app.sparktext.si/readyz >/dev/null; then
    healthy=1
    break
  fi
  sleep 3
done

if [[ "$healthy" != 1 ]]; then
  log "UNHEALTHY. Recent web logs:"
  compose logs --tail 50 web || true
  if [[ -n "$PREV" && "$PREV" != "$TAG" ]]; then
    log "rolling back to $PREV"
    echo "IMAGE_TAG=$PREV" > .env
    compose up -d --remove-orphans
  fi
  exit 1
fi

# 6) Housekeeping: delete unused images older than 10 days. Recent ones stay for quick rollbacks.
docker image prune -af --filter "until=240h" >/dev/null || true
log "OK: $TAG is live"
