#!/usr/bin/env bash
# Authenticate to Boundary and point Claude Code at the local MCP server.
#
#   source mcp/login.sh      <- SOURCE it, don't execute it
#
# The token reaches the server as per-request BOUNDARY_TOKEN / BOUNDARY_ADDR
# headers written into .mcp.json, so start.sh no longer has to be started from
# this same shell. Still SOURCE this: the exports (and the stale-token unset
# below) have to land in your interactive shell, not in a subshell that dies on
# the last line -- the python agents and the boundary CLI read them from there.
#
# .mcp.json therefore holds a live token. It is gitignored (`.*`) and written
# 0600; it is not a file to commit or paste.
#
# No `set -e` on purpose: sourcing would leak it into your interactive shell.

_bd_login() {
  local repo_root mcp_json mcp_url tok cur

  repo_root="$(git rev-parse --show-toplevel 2>/dev/null)"
  [ -n "$repo_root" ] || { echo "[mcp] run this from inside the repo" >&2; return 1; }
  mcp_json="$repo_root/.mcp.json"
  mcp_url="${BOUNDARY_MCP_URL:-http://127.0.0.1:8080}"

  [ -n "${BOUNDARY_ADDR:-}" ] ||
    { echo "[mcp] set BOUNDARY_ADDR to the controller URL first" >&2; return 1; }
  [ -n "${BOUNDARY_PASSWORD:-}" ] ||
    { echo "[mcp] set BOUNDARY_PASSWORD first (see .env)" >&2; return 1; }

  # A stale token in the environment overrides the keyring, so a fresh login
  # would otherwise look like it did nothing.
  unset BOUNDARY_TOKEN

  boundary authenticate password \
    -auth-method-id ampw_VwPciyixsN \
    -login-name admin \
    -password env://BOUNDARY_PASSWORD || return 1

  tok="$(boundary config get-token)" || return 1
  export BOUNDARY_ADDR
  export BOUNDARY_TOKEN="$tok"

  # Merge the boundary entry into .mcp.json, keeping any other servers.
  # The headers are what actually authenticate the MCP host to the server: in
  # local mode it honours both, and only falls back to its own process
  # environment when they are absent. umask 077 so the token never exists in a
  # world-readable file, not even for the moment before mv.
  cur="$(jq -c . "$mcp_json" 2>/dev/null)" || cur='{}'
  ( umask 077
    jq -n --arg url "$mcp_url" --arg tok "$tok" --arg addr "$BOUNDARY_ADDR" \
      --argjson cur "$cur" \
      '$cur | .mcpServers = ((.mcpServers // {}) + {
         boundary: {
           type: "http",
           url: $url,
           headers: {
             BOUNDARY_TOKEN: $tok,
             BOUNDARY_ADDR: $addr
           }
         }
       })' > "$mcp_json.tmp" ) || return 1
  mv "$mcp_json.tmp" "$mcp_json" || return 1

  echo "[mcp] .mcp.json -> boundary at $mcp_url (token in headers, mode 0600)"
  echo "[mcp] token expires: $(boundary auth-tokens read \
    -id "$(printf '%s' "$tok" | cut -d_ -f1,2)" -format json 2>/dev/null |
    jq -r '.item.expiration_time // "unknown"')"
  echo "[mcp] next:  mcp/start.sh   (then reload the VS Code window)"
}

_bd_login
