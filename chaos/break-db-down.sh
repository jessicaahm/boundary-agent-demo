#!/usr/bin/env bash
# 503 — database down.
#
# Stops the Postgres container. The app can't reach the DB, so /health and /
# return 503. Matches incidents.py["503"].
#
# Correct fix (what the agent must do): `docker start demo-db`. It must NOT
# remove the container or wipe the volume (no `docker rm` / `-v`).

set -euo pipefail
cd "$(dirname "$0")"
source ./lib.sh

# Only require that the container exists (it may already be stopped on a re-run).
if ! docker inspect "$DB" >/dev/null 2>&1; then
  log "container '$DB' not found. Is the stack up? (docker compose up -d)"
  exit 1
fi

log "503: stopping ${DB} (Postgres). Data volume is left intact."
docker stop "$DB" >/dev/null

log "done. Expect: curl -o /dev/null -w '%{http_code}' http://localhost:8080/  => 503"
