"""The UI verifier's stub kernel must stay call-compatible with the real one.

``tools/ui_verify.py`` drives the REAL ``build_app`` and the REAL static assets
against a stub ``FakeKernel``. That makes the stub part of the dashboard's
contract surface: when a route learns to pass a new argument and the stub does
not learn to accept it, every request through that route becomes a 500 and the
UI gate reports a *product* failure that is really a harness failure.

That is not hypothetical — it happened. ``/api/mode`` began passing the
autonomy consent bound (``expires_at``) in dcd48f4 (2026-07-17); the stub kept
the old one-argument signature, so every dashboard mode change under
``ui_verify.py`` raised ``TypeError`` -> HTTP 500 and the gate leg sat red for a
month, including the operator's path *out* of autonomous mode.

Signature drift is the bug class, so this pins the signatures rather than any
one call. The stub is read with ``ast`` rather than imported: importing
``tools/ui_verify.py`` executes module-level harness setup, which a unit test
must not do.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from poseidon.app import ApplicationKernel

_UI_VERIFY = Path(__file__).resolve().parents[2] / "tools" / "ui_verify.py"

# Stub methods that stand in for a real kernel method. Extend when the stub
# grows another; a method here that drifts fails the test below.
PINNED_METHODS = ["set_mode", "run_review_cycle"]


def _stub_params(method: str) -> tuple[set[str], bool]:
    """(accepted parameter names, accepts_**kwargs) for FakeKernel.<method>."""
    tree = ast.parse(_UI_VERIFY.read_text(encoding="utf-8"), str(_UI_VERIFY))
    for node in tree.body:
        if not (isinstance(node, ast.ClassDef) and node.name == "FakeKernel"):
            continue
        for item in node.body:
            if isinstance(item, ast.AsyncFunctionDef | ast.FunctionDef) and item.name == method:
                a = item.args
                names = {p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs)}
                return names, a.kwarg is not None
        pytest.fail(f"FakeKernel has no method {method!r} in {_UI_VERIFY}")
    pytest.fail(f"no FakeKernel class in {_UI_VERIFY}")


@pytest.mark.parametrize("method", PINNED_METHODS)
def test_stub_accepts_every_argument_the_real_kernel_accepts(method: str) -> None:
    real = inspect.signature(getattr(ApplicationKernel, method))
    accepted, takes_kwargs = _stub_params(method)
    if takes_kwargs:
        return  # **kwargs absorbs anything; no drift possible

    missing = [
        name for name, p in real.parameters.items()
        if name not in accepted
        and name != "self"
        and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
    ]
    assert not missing, (
        f"FakeKernel.{method} does not accept {missing}, which "
        f"ApplicationKernel.{method}{real} declares. A route passing that argument "
        f"raises TypeError and surfaces as an HTTP 500 in tools/ui_verify.py."
    )


def test_stub_set_mode_accepts_the_consent_bound() -> None:
    """The concrete regression: api/server.py always calls
    ``kernel.set_mode(mode, expires_at=...)``."""
    accepted, takes_kwargs = _stub_params("set_mode")
    assert takes_kwargs or "expires_at" in accepted, (
        "FakeKernel.set_mode must accept expires_at — /api/mode always passes it"
    )
