"""Live orchestration for the fix_agent eval — the part no eval framework does.

This drives the real incident loop over the Boundary tunnel:
    break the app -> confirm the symptom -> run the REAL fix_agent -> verify
    /health -> restore.

It reuses boundary_common.run_remote (the same execution gate the agents use) so
the eval talks to the target exactly the way production does, and it builds the
handoff payload in the same shape sre_agent.handoff_to_fix_agent produces, so
fix_agent receives an input indistinguishable from a real diagnosis.

Everything here is deterministic plumbing; grading lives in graders.py and the
DeepEval metrics in test_fix_agent.py.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

# Import the shared guardrail/exec layer and the ground truth. The eval runs from
# the ai-agent/ dir (see conftest), so these import as top-level modules.
from boundary_common import run_remote
from incidents import INCIDENTS, RESTORE_SCRIPT, HEALTH_URL, ROOT_URL

AI_AGENT_DIR = Path(__file__).resolve().parent.parent
FIX_AGENT = AI_AGENT_DIR / "fix_agent.py"
TRANSCRIPT_DIR = Path(__file__).resolve().parent / "transcripts"

# Signatures of a Boundary/SSH transport failure (tunnel dropped) as opposed to a
# real mid-script failure on the target. When break_app sees one of these it means
# the command never reached the host, so retrying is safe and usually recovers.
_TRANSPORT_ERR_RE = re.compile(
    r"kex_exchange_identification|Connection reset by peer|"
    r"connection refused|Broken pipe|timed out|Connection closed",
    re.IGNORECASE,
)

# Lines fix_agent prints for each command it wants to run (boundary_common:202).
_WANTS_RE = re.compile(r"^>>> agent wants to run:\s*(.+)$", re.MULTILINE)
# Lines fix_agent prints for its own text (plan + summary), fix_agent.py:189.
_TEXT_RE = re.compile(r"^\[fix-agent\]\s?(.*)$", re.MULTILINE)


@dataclass
class RunResult:
    mode: str
    commands: list[str]        # the trajectory: every command fix_agent ran
    agent_text: str            # concatenated [fix-agent] output (plan + summary)
    health_output: str         # raw /health probe after the run
    recovered: bool
    transcript_path: str
    returncode: int


def _curl(url: str) -> str:
    """Probe a URL on the target over the tunnel. Read-only, auto-approves."""
    # Bare single command (no chaining) so read_only() auto-approves it.
    return run_remote(f"curl -s {url}", allow_writes=False)


def http_status(url: str) -> int | None:
    """Return the HTTP status code the target serves for `url`, or None.

    None means the probe could not be completed (tunnel error, empty output) —
    NOT that the app returned nothing. curl prints the 3-digit code (000 if it
    couldn't connect to the app); a run_remote transport failure instead yields
    an error string with no bare 3-digit code, which we report as None so the
    caller can distinguish "app answered" from "probe failed".
    """
    out = run_remote(
        f"curl -s -o /dev/null -w %{{http_code}} {url}", allow_writes=False
    )
    m = re.search(r"\b(\d{3})\b", out or "")
    return int(m.group(1)) if m else None


def break_app(mode: str, attempts: int = 3, delay: float = 2.0) -> str:
    """Run the chaos script for `mode` on the target. State-changing -> needs a plan.

    Raises AssertionError if the break did not actually execute. run_remote never
    raises: a refused/denied/tunnel-down command comes back as a plain string, and
    the chaos scripts run under `set -euo pipefail` so a mid-script failure surfaces
    as non-zero-exit noise in the output rather than an exception. If we returned
    that silently, the test would sail on to assert_symptom with no idea the break
    never applied — reporting the confusing "expected 502, got 200" instead of the
    real cause. So we inspect the output here and fail loudly at the source.

    A transient Boundary/SSH tunnel drop (e.g. "kex_exchange_identification: read:
    Connection reset by peer") means the command never reached the host, so the
    break simply didn't run — retrying usually recovers. We retry ONLY on that
    transport signature; a genuine mid-script failure (reached the target but
    `set -e` aborted) still fails loudly and immediately, preserving the
    "never grade a non-incident" guarantee. This mirrors assert_symptom's polling.
    """
    inc = INCIDENTS[mode]
    out = ""
    for i in range(attempts):
        out = run_remote(
            f"bash {inc.chaos_script}",
            plan=f"EVAL SETUP: inject the {mode} failure via {inc.chaos_script}.",
            allow_writes=True,
        )
        if "done." in out:
            break  # success — stop retrying
        if _TRANSPORT_ERR_RE.search(out) and i < attempts - 1:
            time.sleep(delay)
            continue  # tunnel hiccup, not a script failure — retry
        break  # either succeeded (handled above) or a real failure — stop and report
    # Every chaos script runs under `set -euo pipefail` and prints a final
    # `[chaos] done. Expect: ...` line only if every prior step succeeded. Requiring
    # that line is a positive success check: it catches BOTH run_remote's
    # guardrail/transport refusals (DENIED/REFUSED/tunnel-down strings, which never
    # contain it) AND a mid-script failure on the target (a bad `nginx -t`, a wrong
    # container name), where `set -e` aborts before the "done" line is reached. In
    # either case the break never applied, so we fail loudly here with the real
    # script output instead of letting assert_symptom report the misleading
    # "expected 502, got 200".
    if "done." not in out:
        raise AssertionError(
            f"[{mode}] break did not complete on the target. The chaos script "
            f"never reached its success line; refusing to grade a non-incident.\n"
            f"--- chaos output ---\n{out}"
        )
    return out


def assert_symptom(mode: str, attempts: int = 6, delay: float = 2.0) -> int:
    """Confirm the break took hold before we run the agent.

    Returns the observed status. Raises AssertionError if it does not match the
    expected failure code — so we never grade the agent against a healthy app.

    We POLL rather than probe once. The break (e.g. `nginx -s reload`) can
    briefly disrupt the target and the Boundary tunnel, so the first probe right
    after break_app() may come back as None (a transport failure, not a real
    result) even though the symptom is about to appear. Polling lets a transient
    tunnel hiccup or a slow-to-apply break resolve, while still failing loudly if
    the expected code never shows. A run of Nones means the probe never got
    through — reported distinctly from "got a code, but the wrong one".
    """
    expected = INCIDENTS[mode].http_symptom
    status = None
    for i in range(attempts):
        status = http_status(ROOT_URL)
        if status == expected:
            return status
        if i < attempts - 1:
            time.sleep(delay)

    if status is None:
        raise AssertionError(
            f"[{mode}] could not probe the app after the break ({attempts} tries "
            f"all returned no status). The Boundary tunnel likely failed, not the "
            f"chaos script — check connectivity to the target."
        )
    raise AssertionError(
        f"[{mode}] expected HTTP {expected} after break, got {status}. "
        "The chaos script may not have taken effect; refusing to grade a non-incident."
    )


def restore() -> str:
    """Run restore.sh on the target to return the app to a healthy state."""
    return run_remote(
        f"bash {RESTORE_SCRIPT}",
        plan="EVAL TEARDOWN: restore the app to healthy via restore.sh.",
        allow_writes=True,
    )


def _build_handoff(mode: str, target_name: str, target_id: str) -> dict:
    """Build the handoff payload fix_agent expects (same shape as sre_agent)."""
    inc = INCIDENTS[mode]
    return {
        "target": target_name,
        "target_id": target_id,
        "incident": inc.incident_text,
        "diagnosis": {
            "failing_layer": inc.failing_layer,
            "http_symptom": str(inc.http_symptom),
            "root_cause": inc.root_cause,
            "evidence": [],
            "recommended_commands": inc.recommended,
        },
    }


def run_fix_agent(mode: str, target_name: str, target_id: str) -> RunResult:
    """Spawn the real fix_agent unattended and capture its trajectory.

    Writes the handoff to a temp file, runs fix_agent.py with
    BOUNDARY_HANDOFF_FILE + BOUNDARY_AUTO_APPROVE=1, captures stdout, and parses
    out the commands it ran and the text it wrote. Then probes /health so the
    grader can judge recovery.
    """
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    handoff = _build_handoff(mode, target_name, target_id)

    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", prefix="eval-handoff-", delete=False
    ) as fh:
        json.dump(handoff, fh)
        handoff_path = fh.name

    env = {
        **os.environ,
        "BOUNDARY_HANDOFF_FILE": handoff_path,
        "BOUNDARY_AUTO_APPROVE": "1",   # unattended: skip the human y/N gate
    }
    try:
        # stdin is closed (DEVNULL): with auto-approve on, fix_agent never calls
        # input(), so nothing reads stdin. Capture stdout for the trajectory.
        proc = subprocess.run(
            [sys.executable, str(FIX_AGENT)],
            env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=600,
        )
    finally:
        os.unlink(handoff_path)

    stdout = proc.stdout + proc.stderr
    commands = [c.strip() for c in _WANTS_RE.findall(stdout)]
    agent_text = "\n".join(_TEXT_RE.findall(stdout)).strip()

    # Give the fix a beat to settle (e.g. app restart) before probing health.
    time.sleep(2)
    health_output = _curl(HEALTH_URL)

    from graders import recovered as _recovered  # local import avoids a cycle
    ts = time.strftime("%Y%m%d-%H%M%S")
    transcript_path = TRANSCRIPT_DIR / f"{mode}-{ts}.log"
    transcript_path.write_text(stdout)

    return RunResult(
        mode=mode,
        commands=commands,
        agent_text=agent_text,
        health_output=health_output,
        recovered=_recovered(health_output),
        transcript_path=str(transcript_path),
        returncode=proc.returncode,
    )
