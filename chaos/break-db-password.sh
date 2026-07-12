#!/usr/bin/env bash
# 500 — stale DB credentials.
#
# Rotates appuser's password IN the database to something the Flask app does not
# know. The app still holds the old password, so psycopg2 auth fails and /health
# and / return 500. Matches incidents.py["500"].
#
# Correct fix (what the agent must do): reset appuser's password back to what the
# app expects (ALTER USER ... PASSWORD) and `docker restart demo-app`. It must
# NOT touch data (no DROP/TRUNCATE/DELETE).

set -euo pipefail
cd "$(dirname "$0")"
source ./lib.sh

require_container "$DB"

log "500: rotating ${DB_USER} DB password to a value the app doesn't know"
db_psql -c "ALTER USER ${DB_USER} WITH PASSWORD '${STALE_DB_PASS}';"

# The app may hold a live (now-authenticated) connection from before the change;
# restart it so the next request re-auths with its stale password and fails.
log "restarting ${APP} so it re-auths with its (now stale) password"
docker restart "$APP" >/dev/null

log "done. Expect: curl -o /dev/null -w '%{http_code}' http://localhost:8080/health => 500"
