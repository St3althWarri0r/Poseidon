"""Regression test for the `operator` sandbox escape.

`validate_algorithm` blocks `getattr` and rejects attribute traversal in the
AST. But `operator` was on the import allowlist, and `operator.attrgetter`
takes its path as a STRING — which the AST screen never inspects, because a
string literal is not an `ast.Attribute` node and `_has_format_traversal` only
matches `{...}` format fields. At runtime that dotted string reaches another
module's real `__globals__`, whose `__builtins__` is the genuine builtins dict
rather than the algorithm's restricted one, giving `__import__("os")` and
arbitrary in-process code execution:

    import operator
    async def scan(ctx):
        g = operator.attrgetter("quote.__func__.__globals__")(ctx.router)
        ...

Reproduced during the audit: `validate_algorithm` returned `[]` (no errors) and
the algorithm then executed `os.getuid()`. `itemgetter` and `methodcaller` are
equivalent vectors in the same module.

*** THIS TEST DOES NOT MAKE THE SANDBOX A SECURITY BOUNDARY. ***

Removing `operator` closes the demonstrated vector, not the class. Any future
allowlisted module exposing string-driven attribute access reopens it, and the
module docstring's claim that read-escapes are "contained by the restricted-
builtins sandbox" remains false — restricted builtins govern only the
algorithm's own namespace. The real fix is out-of-process execution, which
`strategy/engine.py:57` already concedes is absent ("sandboxed but not run
out-of-process"). Treat this as defence in depth and keep the workshop
unreachable by untrusted drafts until that lands.
"""

from __future__ import annotations

from poseidon.strategy.custom import _SAFE_IMPORT_MODULES, validate_algorithm

ESCAPE_SOURCE = '''
import operator

async def scan(ctx):
    g = operator.attrgetter("quote.__func__.__globals__")(ctx.router)
    b = g["__builtins__"]
    imp = b["__import__"] if isinstance(b, dict) else operator.attrgetter("__import__")(b)
    osmod = imp("os")
    ctx.log("ESCAPED uid=%r" % (osmod.getuid(),))
    return []
'''


def test_operator_is_not_importable_by_an_algorithm() -> None:
    """attrgetter/itemgetter/methodcaller all take string paths the AST screen
    cannot inspect, so the whole module stays out."""
    assert "operator" not in _SAFE_IMPORT_MODULES


def test_the_escape_payload_is_rejected_by_validation() -> None:
    errors = validate_algorithm(ESCAPE_SOURCE)
    assert errors, "the operator.attrgetter escape must not validate cleanly"
    assert any("operator" in e for e in errors), errors


def test_pure_computation_imports_still_work() -> None:
    """The allowlist must stay useful — this is a lint-level guard, and
    over-tightening it would push authors toward worse patterns."""
    for module in ("math", "statistics", "datetime", "decimal", "json"):
        assert module in _SAFE_IMPORT_MODULES
    assert validate_algorithm(
        "import math\n\nasync def scan(ctx):\n    return [] if math.sqrt(4) else []\n"
    ) == []
