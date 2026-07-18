"""Alpaca paper/live account toggle — env-scoped credential resolution and the
server-side live guard.

See docs/superpowers/specs/2026-07-17-broker-toggle-design.md.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from poseidon.app import ApplicationKernel
from poseidon.brokers.base import Broker
from poseidon.core.config import AppConfig, BrokerConfig
from poseidon.core.enums import BrokerCapability
from poseidon.core.models import AccountSnapshot, Order, Position
from poseidon.security.vault import Vault


def _kernel(tmp_path, brokers: list[BrokerConfig] | None = None) -> ApplicationKernel:
    cfg = AppConfig(brokers=brokers or [])
    return ApplicationKernel(cfg, Vault(tmp_path / "v.bin"))


class _RecordingBroker(Broker):
    """A fake Alpaca broker that records the credentials it was constructed with
    so a test can prove which env-scoped vault entry ``_build_broker`` reused.
    Its account_id echoes the credential ``key_id`` — a credential-less connect
    that fetches an account thus reveals WHICH saved key was loaded."""

    name = "alpaca"
    display_name = "Alpaca"

    def capabilities(self) -> frozenset[BrokerCapability]:
        return frozenset({BrokerCapability.EQUITIES})

    async def connect(self) -> None:
        self._connected = True

    async def account(self) -> AccountSnapshot:
        return AccountSnapshot(
            broker=self.name, account_id=self._credentials.get("key_id", ""),
            equity=Decimal("1000"), cash=Decimal("1000"),
            buying_power=Decimal("1000"), as_of=datetime.now(UTC),
        )

    async def positions(self) -> list[Position]:
        return []

    async def open_orders(self) -> list[Order]:
        return []

    async def submit_order(self, order: Order) -> Order:
        return order

    async def cancel_order(self, order: Order) -> Order:
        return order

    async def order_status(self, order: Order) -> Order:
        return order


def _fake_create_broker(name, *, credentials, paper, options):
    return _RecordingBroker(credentials=credentials, paper=paper, options=options)


def _seed_alpaca_keys(kernel: ApplicationKernel) -> None:
    kernel.vault.create("test-passphrase")
    kernel.vault.set("alpaca_paper_keys",
                     json.dumps({"key_id": "PKPAPER", "secret_key": "s-paper"}))
    kernel.vault.set("alpaca_live_keys",
                     json.dumps({"key_id": "AKLIVE", "secret_key": "s-live"}))


# ------------------------------------------------- env-scoped credential pick


def test_env_credential_resolves_per_env_with_no_config_entry(tmp_path) -> None:
    """With nothing in config, alpaca resolves the catalog's per-env name."""
    kernel = _kernel(tmp_path)
    paper = kernel._broker_config_for("alpaca", paper=True)
    live = kernel._broker_config_for("alpaca", paper=False)
    assert paper.credential == "alpaca_paper_keys"
    assert paper.paper is True
    assert live.credential == "alpaca_live_keys"
    assert live.paper is False


def test_env_credential_ignores_opposite_env_config_entry(tmp_path) -> None:
    """Config holds only the PAPER alpaca entry; asking for LIVE must still
    resolve the live vault name — never hand a live switch the paper credential
    just because that is the one matching-by-name entry."""
    kernel = _kernel(tmp_path, [
        BrokerConfig(name="alpaca", primary=True, credential="alpaca_paper_keys",
                     paper=True),
    ])
    live = kernel._broker_config_for("alpaca", paper=False)
    assert live.credential == "alpaca_live_keys"
    assert live.paper is False
    # The paper request still inherits the matching-env config entry.
    paper = kernel._broker_config_for("alpaca", paper=True)
    assert paper.credential == "alpaca_paper_keys"


def test_env_credential_honours_matching_env_custom_credential(tmp_path) -> None:
    """A matching-env config entry with a custom credential name wins over the
    catalog default (operator-configured vault entry is respected)."""
    kernel = _kernel(tmp_path, [
        BrokerConfig(name="alpaca", primary=True, credential="my_live_alpaca",
                     paper=False),
    ])
    live = kernel._broker_config_for("alpaca", paper=False)
    assert live.credential == "my_live_alpaca"
    # Opposite env falls back to the catalog per-env default.
    paper = kernel._broker_config_for("alpaca", paper=True)
    assert paper.credential == "alpaca_paper_keys"


def test_single_credential_brokers_unchanged(tmp_path) -> None:
    """Brokers without per-env catalog names keep their single credential for
    both envs (env-scoping is Alpaca-only for now)."""
    kernel = _kernel(tmp_path)
    for paper in (True, False):
        cfg = kernel._broker_config_for("tradier", paper=paper)
        assert cfg.credential == "tradier_creds"


def test_options_inherited_by_name_across_envs(tmp_path) -> None:
    """Options-by-name inheritance is unchanged: an option saved on the (paper)
    config entry is inherited even when resolving the live env, and form options
    layer on top."""
    kernel = _kernel(tmp_path, [
        BrokerConfig(name="ibkr", primary=True, credential="ibkr_creds",
                     paper=True, options={"gateway_url": "http://localhost:5000"}),
    ])
    live = kernel._broker_config_for("ibkr", paper=False,
                                     options={"account_id": "U999"})
    assert live.options["gateway_url"] == "http://localhost:5000"
    assert live.options["account_id"] == "U999"


# ------------------------------------- connect with saved credentials (reuse)
#
# The toggle flips accounts with a credential-LESS connect: the endpoint calls
# ``switch_broker(name, paper=..., credentials=None)`` and the vault supplies the
# key — no re-entry. ``switch_broker`` funnels the credential pick through
# ``_broker_config_for`` (env-scoped, task 3) then ``_build_broker(cfg,
# credentials_override=None)``, which reads ``vault.get_json(cfg.credential)``.
# ``broker_connection_test`` (Test button) shares that exact seam, so both must
# reuse the RIGHT env-scoped saved key when handed credentials=None.


async def test_build_broker_reuses_saved_paper_credential(tmp_path, monkeypatch) -> None:
    """credentials=None + paper=True loads ``alpaca_paper_keys`` from the vault
    (the switch_broker build seam), never the live key."""
    kernel = _kernel(tmp_path)
    _seed_alpaca_keys(kernel)
    monkeypatch.setattr("poseidon.app.create_broker", _fake_create_broker)
    cfg = kernel._broker_config_for("alpaca", paper=True)
    broker = await kernel._build_broker(cfg)  # credentials_override defaults None
    assert broker._credentials == {"key_id": "PKPAPER", "secret_key": "s-paper"}
    assert broker.is_paper is True


async def test_build_broker_reuses_saved_live_credential(tmp_path, monkeypatch) -> None:
    """credentials=None + paper=False loads ``alpaca_live_keys`` — even with the
    paper entry also present, the env-scoped name resolves the live key."""
    kernel = _kernel(tmp_path)
    _seed_alpaca_keys(kernel)
    monkeypatch.setattr("poseidon.app.create_broker", _fake_create_broker)
    cfg = kernel._broker_config_for("alpaca", paper=False)
    broker = await kernel._build_broker(cfg)
    assert broker._credentials == {"key_id": "AKLIVE", "secret_key": "s-live"}
    assert broker.is_paper is False


@pytest.mark.parametrize(
    ("paper", "expect_account_id"),
    [(True, "PKPAPER"), (False, "AKLIVE")],
)
async def test_connection_test_reuses_saved_env_credential(
        tmp_path, monkeypatch, paper, expect_account_id) -> None:
    """The public credential-less connect path (broker_connection_test, shared
    with switch_broker) proves the account with the correct env-scoped saved key
    — no credentials passed, no re-entry. The fetched account_id echoes the key
    that was loaded, so it pins WHICH vault entry was reused."""
    kernel = _kernel(tmp_path)
    _seed_alpaca_keys(kernel)
    monkeypatch.setattr("poseidon.app.create_broker", _fake_create_broker)
    result = await kernel.broker_connection_test("alpaca", paper=paper, credentials=None)
    assert result["account_id"] == expect_account_id
    assert result["paper"] is paper
