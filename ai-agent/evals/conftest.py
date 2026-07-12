"""pytest fixtures for the fix_agent eval.

Sets up the Boundary session once per test session and guarantees the app is
restored to health after every test — so a failed fix never leaves the demo
broken for the next run or the next person.

The eval imports agent modules (boundary_common, sre_agent, fix_agent) as
top-level modules, so this file puts the ai-agent/ dir on sys.path.
"""

import os
import sys
from pathlib import Path

import pytest

AI_AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AI_AGENT_DIR))

# Reuse the agents' own Boundary plumbing and target rules — no reinvention.
from boundary_common import (  # noqa: E402
    register_target,
    close_owned_tunnel,
    boundary_tunnel_up,
    ensure_tunnel,
)
from sre_agent import boundary_list_targets, ALLOWED_TARGET  # noqa: E402


@pytest.fixture(scope="session")
def boundary_session():
    """Authenticate/verify Boundary, resolve the target, open the tunnel once.

    Fails loudly (not skips) if Boundary isn't ready — an eval that silently
    skips would hide the fact that nothing was actually tested.
    """
    # Unattended: every state-changing command (breaks, the fix, restore)
    # auto-approves. Set here so it covers the whole session.
    os.environ["BOUNDARY_AUTO_APPROVE"] = "1"

    targets = boundary_list_targets()
    if not targets:
        pytest.fail(
            "Boundary returned no targets. Authenticate first:\n"
            "    export BOUNDARY_ADDR=<controller-url>\n"
            "    boundary authenticate password -auth-method-id <ampw_XXXX>",
            pytrace=False,
        )

    match = next(
        (t for t in targets if (t.get("name") or t["id"]) == ALLOWED_TARGET), None
    )
    if match is None:
        names = ", ".join((t.get("name") or t["id"]) for t in targets)
        pytest.fail(
            f"Target '{ALLOWED_TARGET}' not visible. Available: {names}",
            pytrace=False,
        )

    target_id = match["id"]
    target_name = match.get("name") or target_id
    register_target(target_id)

    # Open the tunnel eagerly so a connectivity failure surfaces here, once,
    # rather than inside the first test.
    if not ensure_tunnel():
        pytest.fail(
            "Could not open the Boundary tunnel to the target. Check that the "
            "target host is up and your Boundary session is valid.",
            pytrace=False,
        )

    yield {"target_id": target_id, "target_name": target_name}

    close_owned_tunnel()


@pytest.fixture
def restore_after(boundary_session):
    """Restore the app to health after each test, pass or fail."""
    yield
    from harness import restore
    restore()
