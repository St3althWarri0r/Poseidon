"""Run the AI-brain selector view-logic unit test (spec task 7) in the gate.

There is no JS test harness in-repo, so the actual assertions live in the node
one-off ``tests/frontend/model_selector.test.js`` (it ``require``s
model_selector.js, whose browser hookup is a no-op under node, leaving only the
pure helpers). This wrapper shells out to node so the selector UI invariants —
the per-backend model list keeps the live model selectable, the VRAM hint
matches the server heuristic, the precondition note/disable fires for a missing
key or an unreachable local endpoint, and a custom id overrides the select — are
exercised by ``pytest`` alongside the backend tests. If node is not installed the
test is skipped rather than failing (frontend tooling is optional).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_NODE = shutil.which("node")
_TEST_JS = Path(__file__).resolve().parents[1] / "frontend" / "model_selector.test.js"


@pytest.mark.skipif(_NODE is None, reason="node not installed; frontend selector test skipped")
def test_model_selector_pure_functions() -> None:
    assert _TEST_JS.is_file(), f"missing node selector test at {_TEST_JS}"
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, trusted repo file
        [_NODE or "node", str(_TEST_JS)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"node selector test failed (exit {result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "all assertions passed" in result.stdout
