"""Settings metadata — what the dashboard is allowed to show and change.

The dashboard could not previously reach almost any configuration. Three rounds
of features shipped off-by-default and none of them grew a control, because
every control had to be hand-written and nobody wrote one. This module exists
so that stops happening.

**Structure comes from the schema; meaning comes from the registry.** Walking
``AppConfig`` gives types, constraints, defaults and current values for free and
can never fall out of step with the code. What it cannot give is a human label:
not one field in ``AppConfig`` carries a pydantic ``description``, so a purely
generated UI would render ``min_dollar_volume`` bare. :data:`REGISTRY` supplies
label, help text, safety tier and restart semantics per dotted path.

**The anti-drift guarantee is narrow and deliberate.** Requiring hand-written
help for all 166 leaves would be busywork nobody finishes. Instead every
**boolean** leaf — the shape a feature flag always takes — MUST be registered,
and a test fails the build until it is. Add a flag, and the gate makes you give
it a label before it can ship invisible. Numeric tuning knobs may be registered
for good presentation, but fall back to read-only "advanced" if not.

**Unregistered is read-only.** The fallback is never "writable with an ugly
name" — a field nobody classified is one nobody decided was safe to change from
a browser.

Tiers, enforced server-side (the UI hiding a control is presentation, not
security):

  * :data:`TIER_BASIC` — feature flags and presentation. Free to toggle.
  * :data:`TIER_GUARDED` — real operational consequence. Needs explicit
    confirmation and lands in the audit log.
  * :data:`TIER_READ_ONLY` — visible so the operator can see the rails, never
    writable here. All of ``risk.*``: these are the outer limits on an armed
    autonomous trader, and a one-click widening control in a web page is the
    most dangerous affordance this application could offer. YAML, deliberately.

Secrets are absent by construction — the config stores credential *names*, not
values, and every ``*_credential`` path is excluded outright.
"""

from __future__ import annotations

import typing
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel

from .config import AppConfig

TIER_BASIC = "basic"
TIER_GUARDED = "guarded"
TIER_READ_ONLY = "read_only"

#: Paths never surfaced: runtime-derived internals and credential names.
EXCLUDED: frozenset[str] = frozenset({
    "config_path",   # set by the loader, not by a human
    "data_dir",      # moving the data directory from a web form is not a setting
})

#: Suffix rule: anything naming a vault entry stays out of the UI entirely.
_EXCLUDED_SUFFIX = "_credential"

#: Prefix rules applied when a path has no explicit entry. Order matters —
#: first match wins.
_PREFIX_TIERS: tuple[tuple[str, str], ...] = (
    ("risk.", TIER_READ_ONLY),
)


@dataclass(frozen=True)
class FieldMeta:
    """What a human needs to decide whether to touch a setting."""

    label: str
    help: str
    tier: str = TIER_BASIC
    #: False only where a live path genuinely exists. The engine builds its
    #: components from config at construction, so almost everything needs a
    #: restart, and saying otherwise would be a lie the operator acts on.
    restart: bool = True
    #: A capability the setting depends on. Rendered as a warning: enabling a
    #: flag whose provider is absent yields a tool that only ever reports gaps.
    requires: str = ""


def _flag(label: str, help_text: str, *, tier: str = TIER_BASIC,
          requires: str = "") -> FieldMeta:
    return FieldMeta(label=label, help=help_text, tier=tier, requires=requires)


#: Explicit metadata. EVERY boolean leaf must appear here (enforced by test).
REGISTRY: dict[str, FieldMeta] = {
    # -- trading mode -------------------------------------------------------
    "mode": _flag(
        "Trading mode",
        "research = analyse only, never order. approval = the AI proposes and "
        "you confirm. autonomous = the AI executes inside the risk limits. "
        "Switching to a live broker automatically demotes autonomous to "
        "approval.",
        tier=TIER_GUARDED),
    "log_level": _flag("Log level", "Verbosity of the engine log."),

    # -- PM research tools --------------------------------------------------
    "ai.pm_tools.screen_market": _flag(
        "Tool: screen market",
        "Lets the AI read the platform's own screener ranking instead of "
        "re-deriving candidates. Advisory: selection only, never a trade signal."),
    "ai.pm_tools.correlation": _flag(
        "Tool: correlation matrix",
        "Pairwise return correlation across a symbol set, date-aligned across "
        "mixed calendars. A concentration lens, not a signal."),
    "ai.pm_tools.correlation_max_symbols": _flag(
        "Correlation: max symbols", "Upper bound on matrix size per call."),
    "ai.pm_tools.correlation_window_days": _flag(
        "Correlation: window (days)", "Trailing window used for each pair."),
    "ai.pm_tools.correlation_min_overlap": _flag(
        "Correlation: min overlap",
        "Below this many shared days a pair reports null rather than a "
        "correlation estimated from too little history."),
    "ai.pm_tools.macro_context": _flag(
        "Tool: macro context",
        "Delayed VIX plus the Treasury yield curve and 10Y-3M term spread, as "
        "regime context. Explicitly labelled delayed, and never a price source."),
    "ai.pm_tools.web_read.enabled": _flag(
        "Tool: read web page",
        "A guarded fetch of public pages. SSRF-guarded, and page text and title "
        "are scanned for prompt injection and annotated, never rewritten. The "
        "content reaches the AI as untrusted data and can never place an order.",
        tier=TIER_GUARDED),
    "ai.pm_tools.web_read.allow_http": _flag(
        "Web read: allow plain HTTP",
        "Permits http:// as well as https://. Leave off unless you have a "
        "specific reason — it exposes fetches to network tampering.",
        tier=TIER_GUARDED),
    "ai.pm_tools.web_read.timeout_seconds": _flag(
        "Web read: timeout (s)",
        "How long a single page fetch may take before it is abandoned as a "
        "data gap."),
    "ai.pm_tools.web_read.max_bytes": _flag(
        "Web read: max bytes", "Body size cap; the stream aborts past it."),
    "ai.pm_tools.web_read.max_chars": _flag(
        "Web read: max characters", "Extracted text returned per call (paged by offset)."),
    "ai.pm_tools.web_read.max_redirects": _flag(
        "Web read: max redirects", "Each hop is re-validated against the full guard."),

    # -- fundamentals -------------------------------------------------------
    "ai.fundamentals.enabled": _flag(
        "Fundamentals, filings and insider data",
        "Filed statements and filing metadata for the AI. Filing METADATA only "
        "— never document text.",
        requires="a FUNDAMENTALS provider (sec_edgar / yahoo_fundamentals) in "
                 "Data providers"),
    "ai.fundamentals.analyst_context": _flag(
        "Fundamentals: fold into the prompt",
        "Adds a fundamentals digest to every cycle prompt, rather than leaving "
        "it only callable as a tool."),
    "ai.fundamentals.max_statement_periods": _flag(
        "Fundamentals: statement periods", "How many periods of history to carry."),
    "ai.fundamentals.max_filings": _flag("Fundamentals: max filings", "Filing rows per call."),
    "ai.fundamentals.max_insider": _flag(
        "Fundamentals: max insider rows", "Reported insider transactions per call."),
    "ai.fundamentals.max_description_chars": _flag(
        "Fundamentals: description cap", "Company description truncation."),
    "ai.fundamentals.digest_max_chars": _flag(
        "Fundamentals: digest cap", "Size of the prompt-folded digest."),

    # -- reflection / analysis ----------------------------------------------
    "ai.reflection.enabled": _flag(
        "Reflection", "The AI reviews closed positions and writes lessons."),
    "ai.reflection.inject": _flag(
        "Reflection: inject lessons", "Fold past lessons into the cycle prompt."),
    "ai.reflection.outcomes.enabled": _flag(
        "Reflection: outcome resolution",
        "Grade each decision's realized forward return against the benchmark."),
    "ai.reflection.behavior.enabled": _flag(
        "Reflection: behavioural diagnostics",
        "Sweep closed round-trips for behavioural biases. Advisory — never a veto."),
    "ai.analysis.enabled": _flag(
        "Deep analysis", "Multi-round analyst debate on candidate symbols."),
    "ai.analysis.inject": _flag(
        "Deep analysis: inject", "Fold analysis digests into the cycle prompt."),
    "ai.snapshot.identity": _flag(
        "Snapshot: identity grounding",
        "Verify each symbol resolves to the instrument the AI thinks it is."),

    # -- screeners ----------------------------------------------------------
    "screener.enabled": _flag(
        "Equity screener (S&P 500)",
        "Rank the S&P 500 each cycle and hand the AI the top candidates, "
        "instead of trading only a fixed watchlist. Market hours."),
    "crypto_screener.enabled": _flag(
        "Crypto screener",
        "Rank the Coinbase pair universe, 24/7."),

    # -- data ---------------------------------------------------------------
    "data.allow_delayed_for_research": _flag(
        "Allow delayed quotes for research",
        "Off means the AI evaluates only on real-time quotes and treats "
        "anything staler as a data gap. Order placement always keeps the strict "
        "gate regardless of this setting.",
        tier=TIER_GUARDED),

    # -- backtest -----------------------------------------------------------
    "backtest.factor_attribution": _flag(
        "Backtest: Fama-French attribution",
        "Report alpha net of market, size and value exposure, so a strategy "
        "that merely holds a size tilt is attributed to that factor instead of "
        "scoring as skill. Costs one keyless ~178 KB fetch per day, cached."),

    # -- operations ---------------------------------------------------------
    "guardian.enabled": _flag(
        "Position guardian",
        "Enforce each decision's stop-loss and take-profit between cycles.",
        tier=TIER_GUARDED),
    "guardian.interval_seconds": _flag(
        "Guardian: interval (s)", "How often open positions are checked.",
        tier=TIER_GUARDED),
    "reports.daily_summary": _flag("Daily summary report", "Scheduled end-of-day summary."),
    "updates.enabled": _flag("Auto-update: check", "Check for new releases on launch."),
    "updates.auto_apply": _flag(
        "Auto-update: install",
        "Install updates automatically, then notify you to restart."),
    "strategy_health.enabled": _flag(
        "Strategy health tracking", "Track per-strategy decay over time."),
    "strategy_health.auto_retire": _flag(
        "Strategy health: auto-retire",
        "Automatically stand down a strategy whose health has decayed.",
        tier=TIER_GUARDED),

    # -- risk (visible, never writable here) --------------------------------
    "risk.flatten_on_halt": FieldMeta(
        label="Flatten positions on halt",
        help="Whether tripping a halt also closes open positions.",
        tier=TIER_READ_ONLY),
    "risk.allow_live_crypto": FieldMeta(
        label="Allow crypto on a LIVE broker",
        help=("Off by default: crypto orders are refused on a real-money broker. "
              "The docs long described crypto as paper-only, but nothing enforced it — "
              "both live brokers advertise the crypto capability. Enable only to "
              "deliberately trade crypto with real money."),
        tier=TIER_READ_ONLY),
}


def _is_excluded(path: str) -> bool:
    leaf = path.rsplit(".", 1)[-1]
    return path in EXCLUDED or leaf.endswith(_EXCLUDED_SUFFIX)


def tier_for(path: str) -> str:
    """Safety tier for a dotted path. Unregistered paths are read-only."""
    meta = REGISTRY.get(path)
    if meta is not None:
        return meta.tier
    for prefix, tier in _PREFIX_TIERS:
        if path.startswith(prefix):
            return tier
    return TIER_READ_ONLY  # fail safe: nobody classified it, nobody may change it


def is_writable(path: str) -> bool:
    """Whether the settings endpoint may change this path.

    The single source of truth for the server-side tier check. The UI disables
    controls to match, but hiding is presentation — this is the enforcement.
    """
    if _is_excluded(path):
        return False
    return tier_for(path) in {TIER_BASIC, TIER_GUARDED}


def _dig(raw: Any, path: str) -> tuple[bool, Any]:
    """``(present, value)`` for a dotted path in a raw parsed-YAML mapping."""
    node = raw
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def provenance(path: str, base_raw: dict[str, Any],
               overlay_raw: dict[str, Any]) -> str:
    """Where a value actually came from: ``overlay`` / ``config file`` / ``default``.

    Resolved against the RAW parsed YAML, never a validated model — validation
    fills defaults in, which destroys exactly the distinction being reported.
    Without this, a setting that appears not to "stick" is unexplainable.
    """
    if _dig(overlay_raw or {}, path)[0]:
        return "overlay"
    if _dig(base_raw or {}, path)[0]:
        return "config file"
    return "default"


def _constraints(schema: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
                "minLength", "maxLength"):
        if key in schema:
            out[key] = schema[key]
    if "enum" in schema:
        out["enum"] = schema["enum"]
    return out


def _field_schema(model: type[BaseModel], name: str) -> dict[str, Any]:
    """Best-effort constraint extraction for one field.

    Two shapes need unwrapping before constraints are visible: ``Optional[...]``
    renders as ``anyOf``, and an Enum field renders as a ``$ref`` into
    ``$defs`` — so a StrEnum's choices are one indirection away and would
    otherwise be reported as an unconstrained field.
    """
    try:
        schema = model.model_json_schema()
    except Exception:  # a model that cannot render a schema still lists fields
        return {}
    defs = schema.get("$defs", {})
    entry = schema.get("properties", {}).get(name)
    if not isinstance(entry, dict):
        return {}

    def _resolve(node: dict[str, Any]) -> dict[str, Any]:
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            target = defs.get(ref.rsplit("/", 1)[-1])
            if isinstance(target, dict):
                return {**target, **{k: v for k, v in node.items() if k != "$ref"}}
        return node

    entry = _resolve(entry)
    for branch in entry.get("anyOf", []):  # Optional[...] renders as anyOf
        if isinstance(branch, dict) and branch.get("type") not in (None, "null"):
            resolved = _resolve(branch)
            return {**resolved, **{k: v for k, v in entry.items() if k != "anyOf"}}
    return entry


def _kind(annotation: Any, schema: dict[str, Any]) -> str:
    if "enum" in schema:
        return "enum"
    if annotation is bool:
        return "bool"
    if annotation is int:
        return "int"
    if annotation is float:
        return "float"
    if annotation is str:
        return "str"
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        return "enum"
    return "other"

def _default_of(field: Any) -> Any:
    if field.default_factory is not None:
        try:
            return field.default_factory()
        except Exception:
            return None
    default = field.default
    return None if repr(default) == "PydanticUndefined" else default


def _jsonable(value: Any) -> Any:
    """Coerce a config value into something JSON can carry."""
    if isinstance(value, bool | int | float | str) or value is None:
        return value
    if hasattr(value, "value"):  # StrEnum / Enum
        return value.value
    return str(value)


def walk_schema(model: type[BaseModel] = AppConfig,
                prefix: str = "") -> list[dict[str, Any]]:
    """Every scalar leaf of the config model, with type and constraints.

    Nested models recurse. Lists of models are reported as a single
    ``kind="list"`` leaf: v1 of the settings UI does not add, remove or reorder
    rows, and pretending otherwise in the schema would invite a control that
    does not exist.
    """
    out: list[dict[str, Any]] = []
    for name, field in model.model_fields.items():
        annotation = field.annotation
        path = f"{prefix}{name}"
        origin = typing.get_origin(annotation)
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            out.extend(walk_schema(annotation, f"{path}."))
            continue
        if origin is list:
            out.append({"path": path, "kind": "list", "constraints": {}, "default": None})
            continue
        schema = _field_schema(model, name)
        out.append({
            "path": path,
            "kind": _kind(annotation, schema),
            "constraints": _constraints(schema),
            "default": _jsonable(_default_of(field)),
        })
    return out


def current_value(config: AppConfig, path: str) -> Any:
    node: Any = config
    for part in path.split("."):
        node = getattr(node, part, None)
        if node is None:
            return None
    return _jsonable(node)


def _comparable(value: Any) -> Any:
    """Normalise a value for the running-vs-saved comparison.

    Decimal-typed settings are stringified for JSON transport ('10000000')
    while parsed YAML holds a number (10000000), so a raw comparison marks
    every one of them pending. Numeric-looking values are compared AS numbers;
    everything else falls through unchanged, so a genuine change is still
    caught.
    """
    if isinstance(value, bool):  # bool is an int subclass — keep it distinct
        return value
    if isinstance(value, int | float | Decimal):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except (InvalidOperation, ValueError):
            return value
    return value


def raw_value(path: str, base_raw: dict[str, Any],
              overlay_raw: dict[str, Any]) -> tuple[bool, Any]:
    """``(found, value)`` for a path in the merged raw files, overlay winning.

    This is what the NEXT start will load, as distinct from what the running
    engine currently holds.
    """
    found, value = _dig(overlay_raw or {}, path)
    if found:
        return True, value
    return _dig(base_raw or {}, path)


def describe(config: AppConfig, base_raw: dict[str, Any],
             overlay_raw: dict[str, Any]) -> list[dict[str, Any]]:
    """The full settings tree the dashboard renders.

    One entry per leaf: schema facts, the effective value, where that value came
    from, and the registry metadata when present. Excluded paths are omitted
    entirely rather than shown as blanks.
    """
    described: list[dict[str, Any]] = []
    for leaf in walk_schema():
        path = leaf["path"]
        if _is_excluded(path):
            continue
        meta = REGISTRY.get(path)
        live = None if leaf["kind"] == "list" else current_value(config, path)
        # What the files say versus what the engine is running. They differ
        # exactly when a restart-required setting has been saved but not yet
        # loaded — and a UI that rendered only the live value would snap the
        # control back, which is indistinguishable from "it did not save".
        found, pending_raw = raw_value(path, base_raw, overlay_raw)
        pending = (bool(found) and leaf["kind"] != "list"
                   and _comparable(_jsonable(pending_raw)) != _comparable(live))
        described.append({
            **leaf,
            "value": live,
            "pending_value": _jsonable(pending_raw) if pending else live,
            "pending": pending,
            "provenance": provenance(path, base_raw, overlay_raw),
            "tier": tier_for(path),
            "writable": is_writable(path) and leaf["kind"] != "list",
            "label": meta.label if meta else path.rsplit(".", 1)[-1].replace("_", " "),
            "help": meta.help if meta else "",
            "restart": meta.restart if meta else True,
            "requires": meta.requires if meta else "",
            "registered": meta is not None,
        })
    return described
