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
