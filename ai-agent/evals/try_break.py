"""Validate a single incident's break through the EVAL's own code path.

Runs exactly what test_fix_agent does up to (but not including) the agent:
    open tunnel -> break_app(mode) -> assert_symptom(mode) -> restore()

so you can confirm the chaos script fires and produces the right HTTP symptom
over the Boundary tunnel, without spending an agent run. Usage from ai-agent/:

    python evals/try_break.py 502     # or 500 / 503

Exits 0 if the symptom was observed and restore brought it back healthy.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # ai-agent/
sys.path.insert(0, str(Path(__file__).resolve().parent))          # evals/

os.environ.setdefault("BOUNDARY_AUTO_APPROVE", "1")

from sre_agent import boundary_list_targets, ALLOWED_TARGET
from boundary_common import register_target, ensure_tunnel
from harness import break_app, assert_symptom, restore, http_status
from incidents import INCIDENTS, ROOT_URL, HEALTH_URL


def main(mode: str) -> int:
    if mode not in INCIDENTS:
        print(f"unknown mode {mode!r}; choose one of {list(INCIDENTS)}")
        return 2

    targets = boundary_list_targets() or []
    m = next((t for t in targets if (t.get("name") or t["id"]) == ALLOWED_TARGET), None)
    if not m:
        names = ", ".join((t.get("name") or t["id"]) for t in targets) or "(none)"
        print(f"target {ALLOWED_TARGET!r} not visible. Available: {names}")
        return 1
    register_target(m["id"])
    if not ensure_tunnel():
        print("could not open the Boundary tunnel")
        return 1

    inc = INCIDENTS[mode]
    print(f"\n[try] mode={mode}  chaos_script={inc.chaos_script}")
    print(f"[try] health BEFORE break: {http_status(ROOT_URL)}")

    print("[try] running break_app() ...")
    print(break_app(mode))

    try:
        status = assert_symptom(mode)
        print(f"[try] OK — assert_symptom passed, observed HTTP {status}")
        ok = True
    except AssertionError as exc:
        print(f"[try] FAIL — {exc}")
        ok = False

    print("[try] running restore() ...")
    restore()
    print(f"[try] health AFTER restore: {http_status(HEALTH_URL)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "502"))
