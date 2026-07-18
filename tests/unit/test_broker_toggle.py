"""Alpaca paper/live account toggle — env-scoped credential resolution and the
server-side live guard.

See docs/superpowers/specs/2026-07-17-broker-toggle-design.md.
"""

from __future__ import annotations

from poseidon.app import ApplicationKernel
from poseidon.core.config import AppConfig, BrokerConfig
from poseidon.security.vault import Vault


def _kernel(tmp_path, brokers: list[BrokerConfig] | None = None) -> ApplicationKernel:
    cfg = AppConfig(brokers=brokers or [])
    return ApplicationKernel(cfg, Vault(tmp_path / "v.bin"))


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
