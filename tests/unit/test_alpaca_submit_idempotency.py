"""Alpaca's order submit must be declared non-idempotent.

`_request`'s own docstring: "``idempotent`` must be False for state-changing
calls (order submit) on brokers without a **server-enforced idempotency key**".
`client_order_id` looks like one, which is presumably why the default was left
in place — but it is not. A true idempotency key returns the ORIGINAL resource
on replay; Alpaca rejects the replay outright. Verified against the live paper
API during the 2026-08 audit:

    SUBMIT #1                  HTTP 200   (order live at the broker)
    SUBMIT #2 (duplicate coid) HTTP 422   {"code":42210000,
                                           "message":"client_order_id must be unique"}

With `idempotent=True` the chain was:

  1. timeout or 5xx on submit -> BrokerError(retryable=True, ambiguous=False)
  2. execution/manager.py retries -> resubmits the SAME client_order_id
  3. Alpaca 422s the duplicate -> retryable=False, ambiguous=False
  4. manager marks REJECTED_BROKER and calls release_validated()

...while submit #1 is LIVE at the broker. Consequences compound: no poller is
spawned, no note_order_submitted (so the in-flight reservation is released and
later orders can stack past the caps), no ORDER_FILLED (so the guardian never
arms an exit plan — the position has no stop), and _reconcile_ambiguous_orders
queries only APPROVED/ERROR, so REJECTED_BROKER is never repaired even across a
restart.

`tradier.py` and `tastytrade.py` already pass idempotent=False.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from poseidon.brokers.plugins import alpaca as alpaca_mod


def _submit_request_kwargs(module: object, func_name: str) -> dict[str, ast.expr]:
    """Keyword args of the `self._request(...)` call inside `func_name`."""
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.AsyncFunctionDef) and node.name == func_name):
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            fn = call.func
            if isinstance(fn, ast.Attribute) and fn.attr == "_request":
                first = call.args[0] if call.args else None
                if isinstance(first, ast.Constant) and first.value == "POST":
                    return {kw.arg: kw.value for kw in call.keywords if kw.arg}
    pytest.fail(f"no POST self._request(...) found in {func_name}")


def test_alpaca_submit_order_is_non_idempotent() -> None:
    kwargs = _submit_request_kwargs(alpaca_mod, "submit_order")
    node = kwargs.get("idempotent")
    assert node is not None, (
        "alpaca submit_order must pass idempotent=False explicitly — the base "
        "default is True, which lets the manager resubmit the same "
        "client_order_id, and Alpaca 422s the duplicate while order #1 is live"
    )
    assert isinstance(node, ast.Constant) and node.value is False


def test_the_plugins_that_already_got_this_right_still_do() -> None:
    """Guard against a 'consistency' refactor flipping these back."""
    plugins = Path(inspect.getfile(alpaca_mod)).parent
    for name in ("tradier", "tastytrade"):
        src = (plugins / f"{name}.py").read_text()
        assert "idempotent=False" in src, f"{name}.py lost its non-idempotent submit"
