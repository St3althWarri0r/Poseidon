"""Bundled screener universe loader — pure, and severed from ``research/``.

The live market screener needs its OWN copy of the S&P 500 constituent list so
it never reads into the offline ``research/`` lab (safety invariant 4: research
stays severed in both import directions). This loader reads the packaged
``data/universe/<name>.txt`` via :mod:`importlib.resources`, so it works whether
the install is an unpacked source tree or a zipped wheel, and returns an
uppercased, order-stable, de-duplicated ticker list.

Imports only the stdlib and :mod:`poseidon.core.errors` — never ``research``
(enforced by a test). The sibling ``data/universe/`` directory holds the data
file; this module is the loader and the two coexist without an import clash
because ``universe/`` carries no ``__init__.py`` (it is resource data, not a
package), so ``poseidon.data.universe`` resolves to this module.
"""
from __future__ import annotations

from importlib.resources import files

from ..core.errors import ConfigError


def load_universe(name: str) -> list[str]:
    """Return the bundled screener universe ``name`` as an uppercased, de-duped,
    order-stable ticker list.

    Parses a one-ticker-per-line file: whitespace is stripped, blank lines and
    ``#`` comment lines (e.g. the header) are skipped, tickers are upcased, and
    duplicates collapse while preserving first-seen order.

    Raises :class:`~poseidon.core.errors.ConfigError` for an unknown (missing)
    or empty universe.
    """
    resource = files("poseidon.data") / "universe" / f"{name}.txt"
    try:
        text = resource.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as exc:
        raise ConfigError(f"unknown screener universe {name!r}: {exc}") from exc
    seen: dict[str, None] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        seen.setdefault(stripped.upper())
    if not seen:
        raise ConfigError(f"screener universe {name!r} is empty")
    return list(seen)
