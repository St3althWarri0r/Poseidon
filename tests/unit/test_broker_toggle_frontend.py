"""Run the broker-toggle view-logic unit test (spec task 7) inside the pytest gate.

There is no JS test harness in-repo, so the actual assertions live in the node
one-off ``tests/frontend/broker_toggle.test.js`` (it ``require``s broker_toggle.js,
whose browser hookup is skipped under node, leaving only the pure helpers). This
wrapper shells out to node so the toggle UI invariants — badge reflects paper vs
LIVE, the dropdown lists only SAVED environments and never offers an unsaved live
account — are exercised by ``pytest`` alongside the backend tests. If node is not
installed the test is skipped rather than failing (frontend tooling is optional).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_NODE = shutil.which("node")
_TEST_JS = Path(__file__).resolve().parents[1] / "frontend" / "broker_toggle.test.js"


@pytest.mark.skipif(_NODE is None, reason="node not installed; frontend toggle test skipped")
def test_broker_toggle_pure_functions() -> None:
    assert _TEST_JS.is_file(), f"missing node toggle test at {_TEST_JS}"
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, trusted repo file
        [_NODE or "node", str(_TEST_JS)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"node toggle test failed (exit {result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "all assertions passed" in result.stdout
