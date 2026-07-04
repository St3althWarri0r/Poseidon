"""Broker plugin registry.

Built-in plugins are registered here; third-party plugins are discovered
through the ``poseidon.brokers`` entry-point group, so adding another
brokerage is: implement Broker in a separate package, declare the entry
point, install it. No changes to Poseidon itself.
"""

from __future__ import annotations

from importlib.metadata import entry_points

import structlog

from ..core.errors import ConfigError
from .base import Broker

log = structlog.get_logger(__name__)

_registry: dict[str, type[Broker]] | None = None


def _load_builtin() -> dict[str, type[Broker]]:
    # Imported lazily to keep startup fast and avoid import cycles.
    from .plugins.alpaca import AlpacaBroker
    from .plugins.etrade import ETradeBroker
    from .plugins.fidelity import FidelityBroker
    from .plugins.ibkr import IBKRBroker
    from .plugins.m1finance import M1FinanceBroker
    from .plugins.paper import PaperBroker
    from .plugins.public_com import PublicBroker
    from .plugins.robinhood import RobinhoodBroker
    from .plugins.schwab import SchwabBroker
    from .plugins.tastytrade import TastytradeBroker
    from .plugins.tradier import TradierBroker
    from .plugins.vanguard import VanguardBroker
    from .plugins.webull import WebullBroker

    plugins: list[type[Broker]] = [
        PaperBroker, AlpacaBroker, TradierBroker, TastytradeBroker, SchwabBroker,
        IBKRBroker, ETradeBroker, PublicBroker, WebullBroker,
        FidelityBroker, M1FinanceBroker, RobinhoodBroker, VanguardBroker,
    ]
    return {p.name: p for p in plugins}


def broker_registry() -> dict[str, type[Broker]]:
    global _registry
    if _registry is None:
        _registry = _load_builtin()
        for ep in entry_points(group="poseidon.brokers"):
            try:
                cls = ep.load()
            except Exception:
                log.exception("failed to load broker plugin", entry_point=ep.name)
                continue
            if not (isinstance(cls, type) and issubclass(cls, Broker)):
                log.error("entry point is not a Broker subclass", entry_point=ep.name)
                continue
            if cls.name in _registry:
                log.warning("plugin overrides built-in broker", name=cls.name)
            _registry[cls.name] = cls
            log.info("loaded external broker plugin", name=cls.name)
    return _registry


def create_broker(name: str, *, credentials: dict[str, str], paper: bool,
                  options: dict[str, object] | None = None) -> Broker:
    registry = broker_registry()
    try:
        cls = registry[name]
    except KeyError:
        raise ConfigError(
            f"unknown broker '{name}'. Available: {', '.join(sorted(registry))}"
        ) from None
    return cls(credentials=credentials, paper=paper, options=dict(options or {}))


# ---------------------------------------------------------------------------
# Connection catalog: what the dashboard's Account view needs to render a
# credential form per broker. Field names match exactly what each plugin's
# __init__/connect reads from the vault JSON (see docs/broker-setup.md).
# ---------------------------------------------------------------------------

def _field(key: str, label: str, *, secret: bool = False, optional: bool = False,
           placeholder: str = "", help_text: str = "") -> dict[str, object]:
    return {"key": key, "label": label, "secret": secret, "optional": optional,
            "placeholder": placeholder, "help": help_text}


# name -> UI metadata for connectable (non-stub) plugins.
_CONNECT_META: dict[str, dict[str, object]] = {
    "paper": {
        "credential": "",
        "fields": [],
        "paper_choice": "always",  # simulation only
        "notes": "Built-in simulator. Fills are priced from live quotes; no real money moves.",
    },
    "alpaca": {
        "credential": "alpaca_keys",
        "fields": [
            _field("key_id", "API key ID", placeholder="AK..."),
            _field("secret_key", "API secret", secret=True),
        ],
        "paper_choice": "toggle",
        "notes": "Free API keys from app.alpaca.markets. Paper environment supported.",
    },
    "tradier": {
        "credential": "tradier_creds",
        "fields": [
            _field("access_token", "Access token", secret=True),
            _field("account_id", "Account ID", placeholder="VA000000"),
        ],
        "paper_choice": "toggle",
        "notes": "Paper mode targets the free developer sandbox.",
    },
    "tastytrade": {
        "credential": "tasty_creds",
        "fields": [
            _field("username", "Username"),
            _field("password", "Password", secret=True,
                   help_text="After the first login a remember_token can replace it."),
            _field("account_number", "Account number", optional=True, placeholder="5WX00000"),
        ],
        "paper_choice": "toggle",
        "notes": "Paper mode targets the certification environment.",
    },
    "schwab": {
        "credential": "schwab_creds",
        "fields": [
            _field("app_key", "App key"),
            _field("app_secret", "App secret", secret=True),
            _field("refresh_token", "Refresh token", secret=True,
                   help_text="From the one-time OAuth consent — see docs/broker-setup.md (Schwab)."),
            _field("account_hash", "Account hash"),
        ],
        "paper_choice": "live_only",
        "notes": "Schwab has no paper environment; the 7-day refresh token needs periodic "
                 "re-consent (docs/broker-setup.md walks through the OAuth flow).",
    },
    "ibkr": {
        "credential": "ibkr_creds",
        "fields": [
            _field("account_id", "Account ID", optional=True, placeholder="U1234567",
                   help_text="Blank = first account on the gateway session."),
        ],
        "paper_choice": "toggle",
        "notes": "Requires IBKR's Client Portal Gateway running locally (you log in there); "
                 "paper vs live follows the gateway login.",
    },
    "public": {
        "credential": "public_api_secret",
        "fields": [
            _field("secret", "API secret", secret=True,
                   help_text="Public app: Settings → Security → API."),
            _field("account_id", "Account ID", optional=True,
                   help_text="Blank = first account on the key."),
        ],
        "paper_choice": "live_only",
        "notes": "LIVE trading only — Public has no paper environment. The same secret also "
                 "unlocks the free public_data real-time market data provider.",
    },
}


def broker_catalog() -> list[dict[str, object]]:
    """Everything the Account view needs, without instantiating any plugin
    (constructing a real plugin with missing credentials raises)."""
    from .base import UnsupportedBroker

    catalog: list[dict[str, object]] = []
    for name, cls in broker_registry().items():
        stub = issubclass(cls, UnsupportedBroker)
        meta = _CONNECT_META.get(name)
        entry: dict[str, object] = {
            "name": name,
            "display_name": cls.display_name or name,
            "connectable": (not stub) and meta is not None,
            "stub_reason": getattr(cls, "reason", "") if stub else "",
        }
        if meta is not None and not stub:
            entry.update({
                "credential": meta["credential"],
                "fields": meta["fields"],
                "paper_choice": meta["paper_choice"],
                "notes": meta["notes"],
            })
        elif not stub:
            # Connectable class without UI metadata (e.g. an external
            # entry-point plugin): offer config-file setup guidance only.
            entry["notes"] = "Configure via poseidon.yaml + vault (no dashboard form metadata)."
        catalog.append(entry)
    # Stable, human-sensible order: paper first, then connectable A→Z, stubs last.
    catalog.sort(key=lambda e: (e["name"] != "paper", not e["connectable"], str(e["display_name"]).lower()))
    return catalog
