"""Show the RAW run_remote output for the health probe, to debug 'got None'.

Run from ai-agent/ in your authenticated Boundary shell:
    python evals/probe.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("BOUNDARY_AUTO_APPROVE", "1")

from sre_agent import boundary_list_targets, ALLOWED_TARGET
from boundary_common import register_target, ensure_tunnel, run_remote

ts = boundary_list_targets() or []
m = next((t for t in ts if (t.get("name") or t["id"]) == ALLOWED_TARGET), None)
if not m:
    print("target not visible:", [(t.get("name") or t["id"]) for t in ts])
    sys.exit(1)
register_target(m["id"])
print("ensure_tunnel:", ensure_tunnel())

print("\n=== 1. plain echo + id (is sudo+tunnel alive?) ===")
print(repr(run_remote("echo HELLO; id", allow_writes=False)))

print("\n=== 2. the EXACT http_status probe, x5 (is the retry RELIABLE?) ===")
from harness import http_status, ROOT_URL
for i in range(5):
    print(f"  attempt {i}: http_status ->", http_status(ROOT_URL))

print("\n=== 3. curl /health body ===")
print(repr(run_remote("curl -s http://localhost:8080/health", allow_writes=False)))
