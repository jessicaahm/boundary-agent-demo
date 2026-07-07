#!/usr/bin/env python3
"""
Fix agent for the Boundary demo.

sre_agent.py diagnoses the incident (read-only) and then spawns this agent as
a subprocess, handing over the diagnosis as JSON in a temp file whose path is
passed in the BOUNDARY_HANDOFF_FILE env var (stdin is deliberately left
attached to the terminal so the human 'y/N' approval prompt works):

    {"target": "...", "target_id": "ttcp_...", "incident": "...",
     "diagnosis": {"failing_layer": "...", "http_symptom": "...",
                   "root_cause": "...",
                   "evidence": [{"command": "...", "excerpt": "..."}],
                   "recommended_commands": ["..."]}}

If the diagnosis agent failed to produce structured output, "diagnosis" is
a dict with only "root_cause" holding its raw notes.

This agent does NOT re-diagnose. It reads the handed-over context, presents a
short PLAN and the concrete STEPS it intends to run, and then applies the fix
over the Boundary SSH tunnel on 127.0.0.1:2222. Every state-changing command
still requires human 'y' approval (shared run_remote).

This agent owns its OWN Boundary session, separate from sre_agent's. It opens
a tunnel to the handed-over target LAZILY — on the first command it actually
runs (i.e. only when a fix is needed) — and CLOSES it when done. If the model
concludes no fix is required and runs nothing, no session is ever opened.
"""

import json
import os
import sys

import anthropic

from boundary_common import (
    TUNNEL_DOWN_MSG,
    close_owned_tunnel,
    register_target,
    run_remote,
)

MODEL = "claude-sonnet-4-6"
MAX_TURNS = 20

SYSTEM = """You are the FIX agent for an SRE workflow. A separate diagnosis
agent has already investigated the incident (read-only) and handed you its
findings. Do NOT start over or re-run the whole investigation — trust the
diagnosis, confirm only what you must, and apply the smallest safe fix.

Application context (3-tier demo app via Docker Compose on the target):

    browser --> nginx (:8080) --> Flask app (:5000) --> Postgres (:5432)

- Containers: demo-nginx (host port 8080), demo-app (Flask), demo-db
  (Postgres 16). Ports 5000/5432 exist only inside the compose network.
- Known failure modes and their fixes:
    502 => nginx upstream misconfig; correct upstream to app:5000,
           validate with nginx -t, then nginx -s reload
    500 => stale DB credentials; reset the password, restart demo-app
    503 => database down; docker start demo-db

Editing nginx config — IMPORTANT: /etc/nginx/conf.d/default.conf in
demo-nginx is a BIND-MOUNTED single file. Do NOT use `sed -i` on it:
in-place edit renames a temp file over the original, which fails on a bind
mount with "Device or resource busy", and the `&&`-chained reload then never
runs, so the 502 persists while looking like it succeeded. Instead edit via a
temp file and write back with `cat >` (truncates the existing inode, no
rename), then validate before reloading, e.g.:
    docker exec demo-nginx sh -c "sed 's/app:5001/app:5000/' \
      /etc/nginx/conf.d/default.conf > /tmp/d.conf && \
      cat /tmp/d.conf > /etc/nginx/conf.d/default.conf && \
      nginx -t && nginx -s reload"

Read-only diagnostics auto-approve ONLY when written as a bare single
command with no shell chaining or redirection. Do NOT wrap read-only checks
in `&&`, `||`, `;`, `2>&1`, `>`, `$(...)`, or backticks — any of these force
an unnecessary human approval prompt. Run each read-only check on its own,
e.g. `docker ps -a` (not `echo hi && docker ps -a` or `docker ps -a 2>&1 ||
true`). Save chaining for the actual state-changing fix, which is gated by
approval anyway.

Workflow you MUST follow:
1. First, in a plain text message, present a PLAN: the root cause from the
   diagnosis, the exact fix, the ordered STEPS (the commands you will run),
   the expected outcome, and how to roll back.
2. Then call run_remote for each step. Every state-changing command MUST
   include the 'plan' field (the operator approves it at the prompt).
3. After applying the fix, verify recovery with:
       curl http://localhost:8080/health    # expect {"status": "ok"}
   and, if relevant, curl http://localhost:8080/ for the end-to-end path.
4. Stop once /health returns ok. Then summarize root cause and prevention.

Never touch data. Prefer one precise change over several."""

TOOLS_SCHEMA = [{
    "name": "run_remote",
    "description": (
        "Run a shell command on the target host via the Boundary SSH tunnel "
        "and return stdout/stderr. Read-only diagnostics run immediately. "
        "State-changing commands MUST include a 'plan' — the human operator "
        "reads it before approving, and the command is rejected without one."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "plan": {
                "type": "string",
                "description": (
                    "Required for state-changing commands. Explain: what this "
                    "changes, why it fixes the diagnosed problem, the expected "
                    "outcome, and how to roll back if it goes wrong."
                ),
            },
        },
        "required": ["command"],
    },
}]


def build_kickoff(ctx: dict) -> str:
    """Turn the handed-over context into the first user message."""
    diag = ctx.get("diagnosis") or {}
    if isinstance(diag, str):  # legacy handoff: a plain transcript string
        diag = {"root_cause": diag}
    lines = [
        f"Target host: {ctx.get('target', '(unknown)')}",
        f"Operator's incident: {ctx.get('incident', '(none)')}",
        "",
        "Diagnosis handed over from the SRE agent:",
        f"- Failing layer: {diag.get('failing_layer', '(not given)')}",
        f"- HTTP symptom: {diag.get('http_symptom', '(not given)')}",
        f"- Root cause: {diag.get('root_cause', '(not given)')}",
    ]
    if diag.get("evidence"):
        lines.append("- Evidence:")
        for item in diag["evidence"]:
            lines.append(f"    $ {item.get('command', '?')}")
            for excerpt_line in str(item.get("excerpt", "")).splitlines():
                lines.append(f"      {excerpt_line}")
    if diag.get("recommended_commands"):
        lines.append("- Recommended fix commands, in order:")
        for cmd in diag["recommended_commands"]:
            lines.append(f"    {cmd}")
    lines += ["", "Present your fix plan and steps, then apply the smallest safe fix."]
    return "\n".join(lines)


def main() -> None:
    # The handoff payload arrives via a file whose path sre_agent passes in
    # BOUNDARY_HANDOFF_FILE — NOT via stdin, which stays attached to the
    # terminal so the human 'y/N' approval prompt in run_remote works.
    handoff_path = os.environ.get("BOUNDARY_HANDOFF_FILE")
    if not handoff_path:
        print("[fix-agent] No BOUNDARY_HANDOFF_FILE set; run me via sre_agent.py.")
        sys.exit(1)
    try:
        with open(handoff_path, encoding="utf-8") as fh:
            ctx = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[fix-agent] Could not read handoff context: {exc}")
        sys.exit(1)

    print("\n[fix-agent] Received handoff from the SRE agent.")

    # Register the target but do NOT open a tunnel yet. ensure_tunnel (called by
    # run_remote) opens this agent's OWN Boundary session lazily on the first
    # command it actually runs — so if the model decides no fix is needed, no
    # session is ever opened. Whatever gets opened is closed in the finally.
    target_id = ctx.get("target_id")
    if not target_id:
        print(f"\n[fix-agent] {TUNNEL_DOWN_MSG} No target_id in the handoff to "
              "open a session with.")
        sys.exit(1)
    register_target(target_id)

    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": build_kickoff(ctx)}]

    try:
        for _ in range(MAX_TURNS):
            resp = client.messages.create(
                model=MODEL, max_tokens=1500, system=SYSTEM,
                tools=TOOLS_SCHEMA, messages=messages,
            )
            for block in resp.content:
                if block.type == "text" and block.text.strip():
                    print(f"\n[fix-agent] {block.text}")

            if resp.stop_reason != "tool_use":
                break  # agent is done

            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    output = run_remote(block.input["command"],
                                        block.input.get("plan", ""),
                                        allow_writes=True)
                    print(f"    output: {output[:500]}")
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output,
                    })
            messages.append({"role": "user", "content": results})
    finally:
        # Close the tunnel this agent opened (lazily, on its first command).
        # If no command ever ran, nothing was opened and this is a no-op.
        close_owned_tunnel()


if __name__ == "__main__":
    main()
