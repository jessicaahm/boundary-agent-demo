#!/usr/bin/env bash
# 502 — nginx upstream misconfiguration.
#
# Points the nginx upstream at app:5001 (nothing listens there) instead of the
# real Flask port app:5000. nginx then can't reach the app and returns
# 502 Bad Gateway. Matches incidents.py["502"].
#
# Correct fix (what the agent must do): restore the upstream to app:5000,
# `nginx -t`, `nginx -s reload` — NOT via `sed -i` on the bind-mounted conf.

set -euo pipefail
cd "$(dirname "$0")"
source ./lib.sh

require_container "$NGINX"

log "502: pointing nginx upstream at app:5001 (wrong port)"
set_upstream_port 5000 5001

log "done. Expect: curl -o /dev/null -w '%{http_code}' http://localhost:8080/  => 502"
