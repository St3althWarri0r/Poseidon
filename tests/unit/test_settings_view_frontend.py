"""Run the Settings view-logic unit test in the gate.

There is no JS test harness in-repo, so the assertions live in the node one-off
``tests/frontend/settings_view.test.js`` (it ``require``s settings_view.js,
whose browser hookup is a no-op under node, leaving only the pure helpers).
This wrapper shells out to node so the settings UI invariants — a read-only
field never renders as an editable control, provenance is badged only when a
value was actually set, status text never claims a change took effect, guarded
settings produce a confirm naming the consequence, and a missing macro leg
renders as unavailable rather than as zero — are exercised by ``pytest``
alongside the backend tests. If node is not installed the test is skipped
rather than failing (frontend tooling is optional).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_NODE = shutil.which("node")
_TEST_JS = Path(__file__).resolve().parents[1] / "frontend" / "settings_view.test.js"


@pytest.mark.skipif(_NODE is None, reason="node not installed; frontend settings test skipped")
def test_settings_view_pure_functions() -> None:
    assert _TEST_JS.is_file(), f"missing node settings test at {_TEST_JS}"
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, trusted repo file
        [_NODE or "node", str(_TEST_JS)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"node settings test failed (exit {result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "all assertions passed" in result.stdout
