"""No attribute may be READ in ``start()`` before it is assigned there.

This exists because of a live incident. The boot-time mode clamp
(``clamp_mode_for_broker``) was inserted right after the broker is built, and
read ``self.order_manager.mode`` — but ``self.order_manager`` is not constructed
until ~15 lines later. Every start of the engine died with:

    AttributeError: 'ApplicationKernel' object has no attribute 'order_manager'

**The entire suite passed, on both interpreters, and so did CI.** 1624 tests, and
none of them drives the real ``start()`` far enough to build a broker — so a
guaranteed, total startup failure was invisible to the gate. That is the actual
defect this file guards: not one misplaced line, but a whole class of
use-before-assignment in a long imperative bootstrap that nothing executes.

``ApplicationKernel`` declares several attributes with a bare annotation
(``self.broker: Broker``) and binds them only inside ``start()``, which is
exactly the pattern that makes this class of bug both easy to write and
invisible to mypy — an annotated-but-unassigned attribute type-checks fine at
every read.

The check is intentionally simple and syntactic: walk ``start()`` in order, and
for each ``self.X`` that ``start()`` itself assigns, assert no earlier statement
loads it.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from poseidon.app import ApplicationKernel


def _start_tree() -> ast.AsyncFunctionDef:
    src = textwrap.dedent(inspect.getsource(ApplicationKernel.start))
    node = ast.parse(src).body[0]
    assert isinstance(node, ast.AsyncFunctionDef)
    return node


def _self_attr(node: ast.AST) -> str | None:
    """'self.foo' -> 'foo', for both loads and stores."""
    if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "self"):
        return node.attr
    return None


def test_no_self_attribute_is_read_before_start_assigns_it() -> None:
    tree = _start_tree()

    # First line at which start() assigns each self.X.
    assigned_at: dict[str, int] = {}
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign | ast.AugAssign):
            targets = [node.target]
        for t in targets:
            name = _self_attr(t)
            if name is not None:
                assigned_at.setdefault(name, t.lineno)

    # Earliest line at which each is read.
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or not isinstance(node.ctx, ast.Load):
            continue
        name = _self_attr(node)
        if name is None or name not in assigned_at:
            continue
        if node.lineno < assigned_at[name]:
            violations.append(
                f"self.{name} is read on line {node.lineno} of start() but not "
                f"assigned until line {assigned_at[name]}"
            )

    assert not violations, (
        "attribute used before assignment in ApplicationKernel.start() — this "
        "crashes every engine start and no test drives start() far enough to "
        "notice:\n  " + "\n  ".join(sorted(set(violations)))
    )


def test_the_guard_can_actually_fail() -> None:
    """A guard that cannot fail is worthless — prove the analysis catches the
    real shape of the bug that motivated it."""
    src = textwrap.dedent("""
        async def start(self):
            x = self.order_manager.mode
            self.order_manager = 1
    """)
    tree = ast.parse(src).body[0]
    assert isinstance(tree, ast.AsyncFunctionDef)

    assigned = {t.attr: t.lineno for n in ast.walk(tree) if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Attribute)}
    reads = [n for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load)
             and n.attr in assigned and n.lineno < assigned[n.attr]]
    assert reads, "the ordering analysis failed to flag a known use-before-assignment"


@pytest.mark.parametrize("attr", ["order_manager", "risk", "broker", "approvals"])
def test_late_bound_attributes_are_assigned_in_start(attr: str) -> None:
    """These are declared with a bare annotation and bound only in start(), so
    mypy cannot see a premature read. Pin that they really are assigned there."""
    tree = _start_tree()
    assigned = {
        _self_attr(t)
        for n in ast.walk(tree) if isinstance(n, ast.Assign)
        for t in n.targets
    }
    assert attr in assigned, f"self.{attr} is no longer assigned in start()"
