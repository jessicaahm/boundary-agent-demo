#!/usr/bin/env bash
# Shared helpers for the chaos scripts. Sourced, not run directly.
#
# The three break-*.sh scripts inject exactly ONE fault each, matching the ground
# truth the eval grades against (ai-agent/evals/incidents.py). restore.sh undoes
# all three so the demo returns to a known-good state regardless of which break
# ran. Every script is idempotent: safe to run twice.

set -euo pipefail

NGINX=demo-nginx
APP=demo-app
DB=demo-db

# App-side DB credentials. These are what the Flask app is configured with, so
# the "correct" password in the DB must match APP_DB_PASS for /health to pass.
DB_USER=appuser
APP_DB_PASS=apppass            # the password the app expects (good state)
STALE_DB_PASS=stale-was-here   # what break-db-password.sh sets it to (bad state)

# Admin psql inside demo-db. With POSTGRES_USER=appuser there is NO "postgres"
# role — appuser is itself the superuser, and local socket connections don't
# need a password (the image trusts local peer auth). So we always administer as
# appuser over the socket; this works even after its OWN password is rotated
# stale, which is exactly what break/restore need.
db_psql() { docker exec "$DB" psql -U "$DB_USER" -d storefront "$@"; }

NGINX_CONF=/etc/nginx/conf.d/default.conf

log() { printf '[chaos] %s\n' "$*"; }

require_container() {
  # Fail loudly if the stack isn't up — a break against a missing container
  # would produce a confusing symptom the eval can't interpret.
  if ! docker inspect -f '{{.State.Running}}' "$1" >/dev/null 2>&1; then
    log "container '$1' not found. Is the stack up? (docker compose up -d)"
    exit 1
  fi
}

# Rewrite the nginx upstream port WITHOUT `sed -i`.
#
# default.conf is bind-mounted from the host, so an in-place edit inside the
# container fails with "Device or resource busy" while looking like it worked —
# that is the documented 502 trap the eval forbids. We rewrite through a temp
# file and copy the bytes back, which works on a bind mount. Same technique the
# agent is expected to use for the fix.
set_upstream_port() {
  local from="$1" to="$2"
  docker exec "$NGINX" sh -c "
    sed 's/app:${from}/app:${to}/' '${NGINX_CONF}' > /tmp/default.conf.new \
      && cat /tmp/default.conf.new > '${NGINX_CONF}' \
      && rm -f /tmp/default.conf.new \
      && nginx -t \
      && nginx -s reload
  "
}
