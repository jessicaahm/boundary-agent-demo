#!/usr/bin/env bash
# Undo every break — return the app to a known-good state.
#
# Idempotent and independent of which break ran: it fixes all three faults
# unconditionally (upstream port, DB password, DB container up), so the eval's
# teardown always lands the demo healthy. Never destroys data.

set -euo pipefail
cd "$(dirname "$0")"
source ./lib.sh

# 503: make sure the DB container is running before we try to talk to it.
if docker inspect "$DB" >/dev/null 2>&1; then
  if [ "$(docker inspect -f '{{.State.Running}}' "$DB")" != "true" ]; then
    log "restore: starting ${DB}"
    docker start "$DB" >/dev/null
  fi
else
  log "restore: ${DB} container missing — bring the stack up (docker compose up -d)"
  exit 1
fi

# Wait for Postgres to accept connections before resetting the password.
log "restore: waiting for ${DB} to be ready"
for _ in $(seq 1 30); do
  if docker exec "$DB" pg_isready -U "$DB_USER" >/dev/null 2>&1; then break; fi
  sleep 1
done

# 500: reset appuser's password back to the value the app expects.
log "restore: resetting ${DB_USER} password to the app's expected value"
db_psql -c "ALTER USER ${DB_USER} WITH PASSWORD '${APP_DB_PASS}';"

# 502: restore the nginx upstream to app:5000 (no-op if already correct),
# validate, and reload. set_upstream_port maps 5001->5000; if it's already 5000
# the sed is a no-op and nginx -t/-s reload just re-assert the good config.
if docker inspect -f '{{.State.Running}}' "$NGINX" >/dev/null 2>&1; then
  log "restore: ensuring nginx upstream is app:5000 and reloading"
  set_upstream_port 5001 5000
fi

# Restart the app so it picks up a healthy DB + correct credentials.
log "restore: restarting ${APP}"
docker restart "$APP" >/dev/null

log "restore: done. Expect /health => {\"status\": \"ok\"}"
