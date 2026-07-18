"""Crypto enablement (Task 8): config + capability wiring.

The crypto data capability itself is exercised in ``test_alpaca_data_crypto``;
these tests pin the *enablement* contract documented in
``docs/api-configuration.md`` and the shipped sample config:

- the bundled ``config/poseidon.example.yaml`` is always a valid ``AppConfig``
  (guards the crypto notes/edits added to it), and
- enabling the ``alpaca`` data provider (the documented crypto source)
  advertises ``DataCapability.CRYPTO`` through the provider registry, which is
  what lets the router quote ``BASE/USD`` pairs.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from poseidon.core.config import AppConfig
from poseidon.data.base import DataCapability
from poseidon.data.providers import BUILTIN_PROVIDERS

_EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "poseidon.example.yaml"


def test_example_config_is_valid() -> None:
    raw = yaml.safe_load(_EXAMPLE.read_text())
    AppConfig.model_validate(raw)  # shipped sample must always parse


def test_enabling_alpaca_data_provider_advertises_crypto() -> None:
    # The registry entry a user enables via `data.providers: [{name: alpaca}]`
    # is the same class the router builds; enabling it advertises CRYPTO so a
    # BASE/USD quote can be routed (never to an equity-only provider).
    cls = BUILTIN_PROVIDERS["alpaca"]
    provider = cls(api_key="key_id", options={"secret_key": "shh"})
    assert DataCapability.CRYPTO in provider.capabilities()


def test_coinbase_registered_and_crypto_only() -> None:
    # Task 3 enablement: the free real-time crypto source is in the registry the
    # router builds from, and advertises CRYPTO but *no* equity/options/news
    # capability, so it can never be picked for an equity request.
    cls = BUILTIN_PROVIDERS["coinbase"]
    caps = cls(api_key="").capabilities()  # public endpoint: no key required
    assert DataCapability.CRYPTO in caps
    # crypto-only quote/bars source — no equity-flavoured capabilities that
    # would let the router pick it for a stock request.
    assert DataCapability.OPTIONS not in caps
    assert DataCapability.NEWS not in caps
    assert DataCapability.SECTOR not in caps
    assert caps == frozenset(
        {DataCapability.CRYPTO, DataCapability.QUOTES, DataCapability.BARS}
    )


def test_example_config_enables_coinbase_above_alpaca() -> None:
    # The shipped sample enables coinbase by default at a higher priority (lower
    # number) than alpaca's documented crypto priority, and needs no credential
    # (public REST endpoint), so crypto quotes work out of the box for free.
    raw = yaml.safe_load(_EXAMPLE.read_text())
    cfg = AppConfig.model_validate(raw)
    coinbase = next((p for p in cfg.data.providers if p.name == "coinbase"), None)
    assert coinbase is not None, "coinbase must be an active provider in the sample"
    assert coinbase.credential == "", "coinbase public endpoint needs no vault key"
    assert coinbase.priority == 8
    # Higher priority than the documented alpaca crypto entry (priority 15) and
    # than every other provider active in the sample.
    assert coinbase.priority < 15
    assert coinbase.priority == min(p.priority for p in cfg.data.providers)
