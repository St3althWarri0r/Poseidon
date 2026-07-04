# Plugin development

Three extension points: brokers, market-data providers, strategies. All
follow the same pattern — implement one class, register it.

## Broker plugins

Implement `poseidon.brokers.base.Broker`:

```python
from poseidon.brokers.base import Broker
from poseidon.core.enums import BrokerCapability
from poseidon.core.errors import BrokerAuthError, BrokerError
from poseidon.core.models import AccountSnapshot, Order, Position

class MyBroker(Broker):
    name = "mybroker"            # config brokers[].name
    display_name = "My Broker"

    def capabilities(self) -> frozenset[BrokerCapability]:
        return frozenset({BrokerCapability.EQUITIES, BrokerCapability.PAPER_TRADING})

    async def connect(self) -> None: ...
    async def account(self) -> AccountSnapshot: ...
    async def positions(self) -> list[Position]: ...
    async def submit_order(self, order: Order) -> Order: ...
    async def cancel_order(self, order: Order) -> Order: ...
    async def order_status(self, order: Order) -> Order: ...
    async def open_orders(self) -> list[Order]: ...
    # optional: tax_lots, dividends, recent_fills, ping
```

Contract (enforced by how the platform uses you):

- **Official APIs only.** If the broker has none, subclass
  `UnsupportedBroker` with an accurate `reason` instead.
- Pass `order.client_order_id` to the broker where supported — that is the
  duplicate-order guard.
- Raise `BrokerError` subclasses with an honest `retryable` flag; never
  return fabricated or partial state.
- `credentials` arrives as the decrypted vault JSON; `paper` selects the
  sandbox where one exists; document both in the class docstring.
- Use `self._request(...)` for HTTP: it maps 401/403 → `BrokerAuthError`,
  429/5xx → retryable `BrokerError`, timeouts included.

Register externally (separate package, no fork needed):

```toml
[project.entry-points."poseidon.brokers"]
mybroker = "my_pkg.broker:MyBroker"
```

or add it to `_load_builtin()` in `brokers/registry.py` for an in-tree
plugin. Config then simply names it: `brokers: [{name: mybroker, ...}]`.

## Market-data providers

Implement `poseidon.data.base.MarketDataProvider`; advertise only real
capabilities:

```python
class MyProvider(MarketDataProvider):
    name = "myprovider"

    def capabilities(self) -> frozenset[DataCapability]:
        return frozenset({DataCapability.QUOTES})

    async def quote(self, symbol: str) -> Quote:
        payload = await self._get_json(url, params={...})
        return Quote(symbol=symbol, last=..., as_of=<provider timestamp>, source=self.name)
```

Rules: return provider timestamps in `as_of` (receipt time only as a last
resort); raise `ProviderError`/`ProviderRateLimitError` on failure — the
router handles failover and penalties; never return defaults for missing
fields. Entry-point group: `poseidon.data_providers`; built-ins map in
`data/providers/__init__.py`.

## Strategies

Strategies are quantitative screeners: they read live data and emit
`Signal`s that inform the AI. They cannot place orders.

```python
from poseidon.strategy.base import Signal, Strategy

class MyStrategy(Strategy):
    name = "my_strategy"
    description = "One line the AI sees."

    async def scan(self, router, portfolio) -> list[Signal]:
        bars = await router.bars("AAPL", timeframe="1d", limit=60)
        ...
        return [Signal(strategy=self.name, symbol="AAPL", direction="long",
                       strength=0.8, evidence={"why": "..."})]
```

Add to `BUILTIN_STRATEGIES` in `strategy/builtin/__init__.py`, enable in
config. Handle `DataError` per symbol (skip, don't abort); the engine also
isolates and times out misbehaving strategies. Put numbers in `evidence` —
that's what the AI (and you, in the logs) will reason from. Test with the
backtester's replay shim (see `tests/unit/test_strategies.py`).

## Custom risk rules

Subclass `poseidon.risk.rules.RiskRule`, raise `RiskViolation` in
`check(ctx)`, and pass `rules=[...ALL_RULES, MyRule()]` when constructing
`RiskEngine` (kernel change) — deliberately not hot-pluggable from config;
risk changes should be code-reviewed.

## Notification channels

Subclass `poseidon.notifications.channels.Channel`, implement
`send(level, title, body) -> bool` (never raise), add to `CHANNEL_KINDS`.
