#!/usr/bin/env python3
"""
Shared guardrails and remote-exec plumbing for the Boundary demo agents.

Both sre_agent.py (diagnose-only) and fix_agent.py (apply-fix) import from
here so the guardrails are IDENTICAL across them: the same denylist, the same
read-only allowlist, and the same human-approval gate for state-changing
commands. All remote commands run over the SAME Boundary SSH tunnel that
sre_agent opened on 127.0.0.1:SSH_PORT.
"""

import re
import socket
import subprocess
import time

SSH_PORT = "2222"          # local Boundary listener
SSH_USER = "demo"          # unix user on the target

DENYLIST = [
    r"rm\s+-rf\s+/", r"mkfs", r"dd\s+if=", r"shutdown", r"reboot",
    r":\(\)\s*{", r"curl[^|]*\|\s*(ba)?sh",
]

# Read-only diagnostics run without human approval. A command qualifies only
# if every pipe segment matches one of these prefixes AND it contains no
# chaining/redirection (;, &&, >, $(), ...) that could smuggle in a write.
READONLY_ALLOWLIST = [
    r"curl\s+[^|]*\b(localhost|127\.0\.0\.1)",   # probing the app, not exfil
    r"docker\s+ps\b",
    r"docker\s+logs\b",
    r"docker\s+inspect\b",
    r"docker\s+exec\s+\S+\s+nginx\s+-[tT]\b",    # print/validate nginx config
    r"docker\s+exec\s+\S+\s+pg_isready\b",
    r"(ls|cat|head|tail|grep|wc|stat|file)\b",
    r"(ps|top\s+-b|uptime|df|free|env|hostname|date|id|whoami)\b",
]

UNSAFE_SHELL = re.compile(r"[;&><]|\$\(|`")      # chaining, background, redirects

# A leading `sudo` (with optional flags like -n/-E) is stripped before the
# read-only check so that e.g. `sudo docker ps` is recognized as the same
# read-only diagnostic as `docker ps` and still auto-approves. sudo alone
# doesn't make a command state-changing — what follows it does.
_SUDO_PREFIX = re.compile(r"^sudo\s+(?:-\S+\s+)*")


def blocked(cmd: str) -> bool:
    return any(re.search(p, cmd) for p in DENYLIST)


def read_only(cmd: str) -> bool:
    """True if the command is a known read-only diagnostic (safe to auto-approve)."""
    cmd = cmd.strip()
    if UNSAFE_SHELL.search(cmd):
        return False
    segments = [_SUDO_PREFIX.sub("", s.strip()) for s in cmd.split("|")]
    return all(
        any(re.match(p, seg) for p in READONLY_ALLOWLIST) for seg in segments
    )


def boundary_tunnel_up(port: str = SSH_PORT) -> bool:
    """True if a Boundary listener is accepting connections on 127.0.0.1:port."""
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=2):
            return True
    except OSError:
        return False


TUNNEL_DOWN_MSG = (
    f"Boundary session is DOWN: nothing is listening on 127.0.0.1:{SSH_PORT} "
    "and it could not be reopened."
)

AUTH_HINT = (
    "You are not authenticated to Boundary (or the token expired). Log in, "
    "then retry:\n"
    "    export BOUNDARY_ADDR=<controller-url>\n"
    "    boundary authenticate password -auth-method-id <ampw_XXXX>"
)

# Substrings Boundary prints when a command fails for lack of a valid token.
_AUTH_ERROR_MARKERS = (
    "unauthenticated", "please authenticate", "authentication",
    "no token", "token has expired", "token is expired", "invalid token",
    "permission denied", "forbidden", "unauthorized",
)


def is_auth_error(text: str) -> bool:
    """True if a boundary CLI error looks like a not-authenticated failure."""
    low = text.lower()
    return any(marker in low for marker in _AUTH_ERROR_MARKERS)


def open_tunnel(target_id: str):
    """Ensure a Boundary listener is up on 127.0.0.1:SSH_PORT for target_id.

    Reuses an existing listener if one is already up (returns None so the
    caller knows it does NOT own the tunnel). Otherwise starts
    `boundary connect` to the SAME target, waits for the listener, and returns
    the subprocess so the caller can terminate it on exit. Returns None on
    failure to open (caller should treat that as "tunnel unavailable").
    """
    if boundary_tunnel_up(SSH_PORT):
        print(f"[boundary] Reusing the existing listener on 127.0.0.1:{SSH_PORT}.")
        return None
    print(f"[boundary] Connecting to {target_id} via Boundary on 127.0.0.1:{SSH_PORT} ...")
    proc = subprocess.Popen(
        ["boundary", "connect", "-target-id", target_id, "-listen-port", SSH_PORT],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    for _ in range(20):  # up to ~10s for the session to be authorized
        if boundary_tunnel_up(SSH_PORT):
            print("[boundary] Boundary session established.")
            return proc
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    proc.terminate()
    err = proc.stderr.read().strip() if proc.stderr else ""
    if err:
        print(f"[boundary] Could not open the Boundary tunnel. {err}")
    if is_auth_error(err):
        print(f"[boundary] {AUTH_HINT}")
    return None


# The target the tunnel points at. An agent registers this once
# (register_target) but does NOT open a tunnel at startup; the tunnel is opened
# lazily by ensure_tunnel on the first command that actually needs it, and each
# agent tears down whatever it opened when it finishes. This keeps the Boundary
# session alive only for as long as an agent is actively using it.
_ACTIVE_TARGET_ID = None
_OWNED_TUNNEL = None


def register_target(target_id: str) -> None:
    """Record the Boundary target so ensure_tunnel can open/reconnect to it."""
    global _ACTIVE_TARGET_ID
    _ACTIVE_TARGET_ID = target_id


def take_owned_tunnel():
    """Return (and clear) any tunnel this process opened, for the agent to close.

    Covers both the lazy first open and any mid-run reconnect. Returns None if
    a tunnel was never opened here (e.g. the whole run needed no commands).
    """
    global _OWNED_TUNNEL
    proc, _OWNED_TUNNEL = _OWNED_TUNNEL, None
    return proc


def close_owned_tunnel() -> None:
    """Terminate the tunnel this process opened, if any. Safe to call twice."""
    proc = take_owned_tunnel()
    if proc is not None:
        proc.terminate()
        proc.wait(timeout=5)
        print("\n[boundary] Boundary session closed.")


def ensure_tunnel() -> bool:
    """Ensure the tunnel is up, opening/reconnecting to the registered target.

    Called lazily by run_remote right before a command runs — so the session
    is opened only when first needed, not at agent startup. Returns True if a
    listener is up (already, or after opening one); False if it is down and
    could not be established.
    """
    global _OWNED_TUNNEL
    if boundary_tunnel_up():
        return True
    if not _ACTIVE_TARGET_ID:
        return False
    print(f"\n[boundary] Opening Boundary session to {_ACTIVE_TARGET_ID} ...")
    proc = open_tunnel(_ACTIVE_TARGET_ID)
    if proc is not None:
        _OWNED_TUNNEL = proc  # we opened it; caller closes it on exit
    return boundary_tunnel_up()


def run_remote(cmd: str, plan: str = "", *, allow_writes: bool = True) -> str:
    """Run a command on the target over the Boundary SSH tunnel.

    Read-only diagnostics auto-approve. State-changing commands require a
    'plan' and a human 'y' at the approval prompt. When allow_writes is False
    (diagnose-only agents), any non-read-only command is refused outright.
    If the tunnel has dropped, it auto-reconnects to the registered target
    before running; only if that fails does it return a clear message.
    """
    if blocked(cmd):
        return "DENIED by agent guardrail: command matches the denylist."
    # Pre-check the tunnel before prompting/executing. If it dropped mid-run,
    # transparently reconnect to the registered target; only give up if that
    # fails, instead of failing every command with 'Connection refused'.
    if not ensure_tunnel():
        return TUNNEL_DOWN_MSG
    print(f"\n>>> agent wants to run: {cmd}")
    if read_only(cmd):
        print("    auto-approved (read-only)")
    elif not allow_writes:
        return ("REFUSED: this agent is diagnose-only and may run read-only "
                "commands only. Hand the fix off to fix_agent.")
    else:
        if not plan.strip():
            return ("REJECTED: this is a state-changing command. Resubmit it "
                    "with a 'plan' explaining what it changes, why, the "
                    "expected outcome, and how to roll back.")
        print("    plan:")
        for line in plan.strip().splitlines():
            print(f"      {line}")
        if input("    approve? [y/N] ").strip().lower() != "y":
            return "DENIED by human operator."

    def _ssh():
        return subprocess.run(
            # The target is reached through a local Boundary listener on
            # 127.0.0.1:SSH_PORT, so per-port host-key verification isn't
            # meaningful (Boundary brokers the target's identity). Skip the
            # known_hosts cache for this ephemeral port and turn off the
            # UpdateHostKeys rotation prove, which otherwise emits
            # "client_global_hostkeys_prove_confirm: ... incorrect signature"
            # to stderr because the proxied host key can't be verified locally.
            ["ssh", "-p", SSH_PORT,
             "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null",
             "-o", "UpdateHostKeys=no",
             "-o", "LogLevel=ERROR",
             f"{SSH_USER}@127.0.0.1", cmd],
            capture_output=True, text=True, timeout=30,
        )

    proc = _ssh()
    out = (proc.stdout + proc.stderr).strip() or "(no output)"
    # SSH itself can fail if the tunnel drops between the pre-check and exec.
    # Reconnect once and retry so a transient drop doesn't surface as an error.
    if proc.returncode != 0 and "connect to host" in out.lower() and ensure_tunnel():
        proc = _ssh()
        out = (proc.stdout + proc.stderr).strip() or "(no output)"
    return out[:4000]
