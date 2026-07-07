#!/usr/bin/env python3
"""
SRE DIAGNOSIS agent for the Boundary demo (the read-only half of a two-agent
workflow).

This agent diagnoses the incident using read-only commands only, then hands
its diagnosis to fix_agent.py (spawned as a subprocess) which presents a plan
and applies the fix. Each agent owns its OWN Boundary session: this agent
opens a tunnel lazily on its first diagnostic command and CLOSES it before the
handoff; fix_agent then opens its own session (only if a fix is needed) and
closes it when done.

The agent's ONLY path to the target is an SSH tunnel opened by:
    boundary connect ssh -target-id ttcp_XXXX -listen-port 2222 -- -N
(or `boundary connect` for a raw TCP listener). The agent itself never
sees an SSH key or password — Boundary brokers/injects them.

Guardrails baked in (shared with fix_agent via boundary_common.py):
  - this agent runs read-only diagnostics only; writes are refused here
  - fix_agent requires human approval before every state-changing command
  - denylist for destructive commands
  - full audit log printed to stdout (pairs with Boundary session recording)

Usage:
    pip install anthropic
    export ANTHROPIC_API_KEY=...
    export BOUNDARY_ADDR=...      # and `boundary authenticate` beforehand
    python sre_agent.py
    # the agent checks you are logged in to Boundary (exits if not), lists
    # your targets, opens `boundary connect` to the one you pick, then asks
    # what you want to achieve
"""

import json
import os
import re
import subprocess
import sys
import tempfile

import anthropic

from boundary_common import (
    AUTH_HINT,
    close_owned_tunnel,
    register_target,
    run_remote,
)

MODEL = "claude-haiku-4-5"
MAX_TURNS = 20
ALLOWED_TARGET = "it-ssh-cert-target"   # the only target this agent may troubleshoot

SYSTEM = """You are an SRE DIAGNOSIS agent. You can run shell commands on ONE
remote host through the run_remote tool, but you may ONLY run read-only
diagnostics — you never change state yourself. Your access is a
Boundary-managed SSH session scoped to this single target.

Application context (a 3-tier demo app, run via
Docker Compose on the target host):

    browser --> nginx (:8080) --> Flask app (:5000) --> Postgres (:5432)

- Containers: demo-nginx (host port 8080), demo-app (Flask), demo-db
  (Postgres 16). Ports 5000/5432 exist only inside the compose network.
- Endpoints: GET / returns products from the DB, GET /health reports
  whether the app can reach the DB, GET /nginx-health proves nginx is up.
- Known failure modes and their fixes:
    502 => nginx upstream misconfig; correct upstream to app:5000,
           then nginx -s reload
    500 => stale DB credentials; look for psycopg2 errors in demo-app
           logs, reset the password, restart the app
    503 => database down; docker start demo-db
- Useful checks: docker logs demo-nginx/demo-app --tail 50,
  docker exec demo-nginx nginx -T, docker exec demo-db pg_isready -U appuser

Task: DIAGNOSE the web application failure using read-only commands only.
You do NOT apply fixes — a separate fix agent does that. When you have
identified the root cause, STOP calling tools and end your final message
with a ```json code block containing exactly these keys:
    "failing_layer":        "nginx" | "app" | "db"
    "http_symptom":         "502" | "500" | "503"
    "root_cause":           one or two sentences
    "evidence":             list of {"command", "excerpt"} — the 2-3
                            command outputs that prove the root cause,
                            quoting the exact relevant lines
    "recommended_commands": the exact commands the fix agent should run,
                            in order
That JSON block is parsed and handed off to the fix agent.

Health check: if the user only asks whether the web application is healthy
or down (a status check, not a fix request), run exactly these three
commands and nothing else:
    curl http://localhost:8080/nginx-health   # nginx alive
    curl http://localhost:8080/health         # {"status": "ok"}
    curl http://localhost:8080/               # JSON products
Then report the status of each layer (nginx, app-to-DB, end-to-end) and
stop. Do not investigate further.

Rules: explain WHY before each command, prefer read-only commands first
(curl, docker ps, docker logs, nginx -T), never touch data, and never run
state-changing commands — if you try, the tool refuses. Once you know the
root cause, stop and write the diagnosis + recommended fix."""

TOOLS_SCHEMA = [{
    "name": "run_remote",
    "description": (
        "Run a READ-ONLY diagnostic command on the target host via the "
        "Boundary SSH tunnel and return stdout/stderr. This agent diagnoses "
        "only; state-changing commands are refused. Recommend the fix in your "
        "final message instead — the fix agent applies it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
        },
        "required": ["command"],
    },
}]


def boundary_list_targets():
    """Return the Boundary targets visible to the user, or None if not logged in."""
    try:
        proc = subprocess.run(
            ["boundary", "targets", "list", "-recursive", "-format", "json"],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        print("[agent] The 'boundary' CLI is not installed.")
        print("        macOS: brew install hashicorp/tap/boundary")
        sys.exit(1)
    if proc.returncode != 0:
        if proc.stderr.strip():
            print(f"        boundary said: {proc.stderr.strip().splitlines()[0]}")
        return None
    return json.loads(proc.stdout).get("items", [])


def choose_target(targets):
    """Show the available targets and let the user pick one."""
    print("\n[agent] Boundary targets available to you:")
    for i, t in enumerate(targets, 1):
        name = t.get("name") or "(unnamed)"
        print(f"    {i}. {name}  [{t['id']}, type={t.get('type', '?')}]")
    while True:
        pick = input("\n[agent] Which target host do you want to access? [number] ").strip()
        if pick.isdigit() and 1 <= int(pick) <= len(targets):
            return targets[int(pick) - 1]
        print("        Please enter a number from the list.")


def extract_diagnosis(text: str):
    """Parse the structured JSON diagnosis from the agent's final message.

    Accepts a ```json fenced block or a bare JSON object. Returns the dict,
    or None if the message contains neither (e.g. a plain health-check
    report) — the caller falls back to handing off the raw notes.
    """
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = match.group(1) if match else None
    if candidate is None:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            candidate = text[start:end + 1]
    if candidate is None:
        return None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def handoff_to_fix_agent(target_name: str, target_id: str, incident: str,
                         diagnosis: dict) -> None:
    """Spawn fix_agent.py as a subprocess, handing over the diagnosis as JSON.

    The payload is printed first so the operator can audit exactly what
    crosses the read-only -> write boundary. The Boundary tunnel is owned by
    this agent and stays open — fix_agent reuses the same listener on
    127.0.0.1:2222. The target_id is included so fix_agent can reconnect to
    the SAME target if the tunnel has dropped by the time it runs.
    """
    context = json.dumps({
        "target": target_name,
        "target_id": target_id,
        "incident": incident,
        "diagnosis": diagnosis,
    }, indent=2)
    fix_agent = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fix_agent.py")
    print("\n[agent] Handing off to the fix agent (Boundary session stays open).")
    print(f"[agent] Handoff payload:\n{context}")

    # Pass the payload via a temp file (path in an env var), NOT via stdin.
    # fix_agent needs its stdin attached to the terminal so its human 'y/N'
    # approval prompt works; feeding stdin here would make input() hit EOF.
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", prefix="boundary-handoff-", delete=False
    ) as fh:
        fh.write(context)
        handoff_path = fh.name
    try:
        env = {**os.environ, "BOUNDARY_HANDOFF_FILE": handoff_path}
        subprocess.run([sys.executable, fix_agent], env=env)
    finally:
        os.unlink(handoff_path)


def main() -> None:
    targets = boundary_list_targets()
    if targets is None:
        print(f"\n[agent] {AUTH_HINT}")
        sys.exit(1)
    if not targets:
        print("[agent] No Boundary targets are visible to your account.")
        sys.exit(1)

    target = choose_target(targets)
    target_name = target.get("name") or target["id"]
    if target_name != ALLOWED_TARGET:
        print(f"\n[agent] Troubleshooting not available for '{target_name}'.")
        print(f"        This agent only supports the '{ALLOWED_TARGET}' target.")
        sys.exit(1)

    target_id = target["id"]
    # Register the target but do NOT open a tunnel yet. ensure_tunnel opens one
    # lazily on the first diagnostic command, and we close it after diagnosis —
    # each agent owns its own session for only as long as it needs it.
    register_target(target_id)

    incident = input("\n[agent] What do you want to achieve? ").strip()
    if not incident:
        incident = "The site on :8080 is down."

    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": f"Target host: {target_name}\n{incident}"}]

    notes = []  # every text block the model writes, in order
    try:
        for _ in range(MAX_TURNS):
            resp = client.messages.create(
                model=MODEL, max_tokens=1500, system=SYSTEM,
                tools=TOOLS_SCHEMA, messages=messages,
            )
            for block in resp.content:
                if block.type == "text" and block.text.strip():
                    print(f"\n[agent] {block.text}")
                    notes.append(block.text.strip())

            if resp.stop_reason != "tool_use":
                break  # diagnosis complete

            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    # Diagnose-only: run_remote refuses any non-read-only command.
                    output = run_remote(block.input["command"],
                                        block.input.get("plan", ""),
                                        allow_writes=False)
                    print(f"    output: {output[:500]}")
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output,
                    })
            messages.append({"role": "user", "content": results})

        # Diagnosis done: close THIS agent's Boundary session before handing
        # off. fix_agent will open its own session (only if a fix is needed).
        close_owned_tunnel()

        # Hand the diagnosis off to the fix agent. Prefer the structured JSON
        # from the final message; if the model didn't produce one, fall back to
        # the full running notes so the handoff never loses the investigation.
        if notes:
            diagnosis = extract_diagnosis(notes[-1]) or {"root_cause": "\n\n".join(notes)}
            handoff_to_fix_agent(target_name, target_id, incident, diagnosis)
    finally:
        # Safety net: if we bailed out before the explicit close above (an
        # error mid-diagnosis), still tear down any tunnel this agent opened.
        close_owned_tunnel()


if __name__ == "__main__":
    main()
