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
