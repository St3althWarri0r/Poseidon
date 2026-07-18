"""Run the debug-console redaction unit test (spec task 8a) inside the pytest gate.

There is no JS test harness in-repo, so the actual assertions live in the node
one-off ``tests/frontend/debug_redaction.test.js`` (it ``require``s debug.js, whose
browser install() is skipped under node, leaving only the pure redaction helpers).
This wrapper shells out to node so the LOAD-BEARING "secrets never logged"
invariant is exercised by ``pytest`` alongside the backend tests. If node is not
installed the test is skipped rather than failing (frontend tooling is optional).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_NODE = shutil.which("node")
_TEST_JS = Path(__file__).resolve().parents[1] / "frontend" / "debug_redaction.test.js"


@pytest.mark.skipif(_NODE is None, reason="node not installed; frontend redaction test skipped")
def test_debug_redaction_pure_functions() -> None:
    assert _TEST_JS.is_file(), f"missing node redaction test at {_TEST_JS}"
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, trusted repo file
        [_NODE or "node", str(_TEST_JS)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"node redaction test failed (exit {result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "all assertions passed" in result.stdout
