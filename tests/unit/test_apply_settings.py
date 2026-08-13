"""``ApplicationKernel.apply_settings`` — the dashboard's config write path.

The safety of exposing settings to a browser rests on one property: the change
is merged over the real base config and validated with the SAME validator that
guards startup, before anything is written. An invalid overlay discovered only
at next launch, on an armed autonomous trader, is the worst outcome this
feature could produce — so these tests care far more about what is *refused*
than about what is saved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from poseidon.app import ApplicationKernel
from poseidon.core.config import AppConfig
from poseidon.core.errors import ConfigError

_BASE = {
    "mode": "research",
    "ai": {"pm_tools": {"screen_market": True}},
    "brokers": [{"name": "paper", "enabled": True, "primary": True}],
}


def _kernel(tmp_path: Path, base: dict[str, Any] | None = None) -> ApplicationKernel:
    """A kernel shell: apply_settings reads only ``config.config_path``, so
    building the whole application graph would test the fixture, not the code."""
    cfg_path = tmp_path / "poseidon.yaml"
    cfg_path.write_text(yaml.safe_dump(base if base is not None else _BASE),
                        encoding="utf-8")
    kernel = object.__new__(ApplicationKernel)
    kernel.config = AppConfig.model_validate(  # type: ignore[attr-defined]
        {**(base if base is not None else _BASE), "config_path": cfg_path})
    return kernel


def _overlay(tmp_path: Path) -> dict[str, Any]:
    path = tmp_path / "poseidon.local.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


# ------------------------------------------------------------- happy path


def test_writes_a_flag_to_the_overlay(tmp_path: Path) -> None:
    result = _kernel(tmp_path).apply_settings({"ai.pm_tools.macro_context": True})
    assert result["applied"] == ["ai.pm_tools.macro_context"]
    assert _overlay(tmp_path)["ai"]["pm_tools"]["macro_context"] is True


def test_the_base_config_file_is_never_rewritten(tmp_path: Path) -> None:
    cfg = tmp_path / "poseidon.yaml"
    before = cfg.read_text(encoding="utf-8") if cfg.exists() else None
    kernel = _kernel(tmp_path)
    before = cfg.read_text(encoding="utf-8")
    kernel.apply_settings({"ai.pm_tools.macro_context": True})
    assert cfg.read_text(encoding="utf-8") == before


def test_only_submitted_paths_are_touched(tmp_path: Path) -> None:
    # An unrelated key already in the overlay must survive untouched — the
    # writer merges, it does not replace the file.
    (tmp_path / "poseidon.local.yaml").write_text(
        yaml.safe_dump({"ai": {"model": "keep-me"}, "log_level": "DEBUG"}),
        encoding="utf-8")
    _kernel(tmp_path).apply_settings({"ai.pm_tools.macro_context": True})
    overlay = _overlay(tmp_path)
    assert overlay["ai"]["model"] == "keep-me"
    assert overlay["log_level"] == "DEBUG"
    assert overlay["ai"]["pm_tools"]["macro_context"] is True


def test_reports_that_a_restart_is_needed(tmp_path: Path) -> None:
    # The engine reads config at construction. Claiming otherwise would be a
    # lie the operator acts on.
    result = _kernel(tmp_path).apply_settings({"ai.pm_tools.macro_context": True})
    assert result["needs_restart"] is True


def test_multiple_paths_in_one_call(tmp_path: Path) -> None:
    result = _kernel(tmp_path).apply_settings({
        "ai.pm_tools.macro_context": True,
        "backtest.factor_attribution": True,
    })
    assert result["applied"] == ["ai.pm_tools.macro_context", "backtest.factor_attribution"]
    overlay = _overlay(tmp_path)
    assert overlay["ai"]["pm_tools"]["macro_context"] is True
    assert overlay["backtest"]["factor_attribution"] is True


# --------------------------------------------------- refusals (the real work)


def test_a_read_only_risk_limit_is_refused(tmp_path: Path) -> None:
    # Tier enforcement is SERVER-side. The UI disabling the control is
    # presentation; this is what actually stops a curl.
    with pytest.raises(PermissionError, match="risk.max_position_pct"):
        _kernel(tmp_path).apply_settings({"risk.max_position_pct": 0.9})
    assert not (tmp_path / "poseidon.local.yaml").exists()


def test_a_credential_path_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PermissionError):
        _kernel(tmp_path).apply_settings({"ai.api_key_credential": "stolen"})


def test_an_unregistered_path_is_refused(tmp_path: Path) -> None:
    # Fail-closed: a field nobody classified is a field nobody decided was
    # safe to change from a browser.
    with pytest.raises(PermissionError):
        _kernel(tmp_path).apply_settings({"ai.budget.max_prompt_chars": 1})


def test_an_unknown_key_is_refused(tmp_path: Path) -> None:
    # Caught by the tier gate rather than by validation — an invented path is
    # unregistered, and unregistered fails closed. Defence in depth: merged
    # validation would reject it too (extra="forbid"), but it never gets there.
    with pytest.raises(PermissionError):
        _kernel(tmp_path).apply_settings({"ai.pm_tools.no_such_flag": True})


def test_an_out_of_range_value_is_refused_before_writing(tmp_path: Path) -> None:
    # correlation_max_symbols is bounded 2..30. This is the case that proves
    # merged validation runs: the path IS writable, so only real validation
    # can catch it — and nothing may reach disk.
    with pytest.raises(ConfigError, match="invalid"):
        _kernel(tmp_path).apply_settings({"ai.pm_tools.correlation_max_symbols": 9999})
    assert not (tmp_path / "poseidon.local.yaml").exists()


def test_a_wrongly_typed_value_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        _kernel(tmp_path).apply_settings({"ai.pm_tools.macro_context": "yes-please"})


def test_a_rejected_write_leaves_an_existing_overlay_intact(tmp_path: Path) -> None:
    # The strongest form: a bad write must not corrupt a good overlay.
    (tmp_path / "poseidon.local.yaml").write_text(
        yaml.safe_dump({"ai": {"pm_tools": {"correlation": True}}}), encoding="utf-8")
    before = (tmp_path / "poseidon.local.yaml").read_text(encoding="utf-8")
    with pytest.raises(ConfigError):
        _kernel(tmp_path).apply_settings({"ai.pm_tools.correlation_max_symbols": 9999})
    assert (tmp_path / "poseidon.local.yaml").read_text(encoding="utf-8") == before


def test_an_empty_update_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        _kernel(tmp_path).apply_settings({})


def test_a_corrupt_existing_overlay_is_reported_not_silently_replaced(
        tmp_path: Path) -> None:
    (tmp_path / "poseidon.local.yaml").write_text("{[not: valid", encoding="utf-8")
    with pytest.raises(ConfigError, match="cannot parse"):
        _kernel(tmp_path).apply_settings({"ai.pm_tools.macro_context": True})


def test_validation_sees_the_base_config_not_just_the_overlay(tmp_path: Path) -> None:
    # The merge must include the base file. With a base that sets a live
    # broker, a mode change to autonomous still has to validate against the
    # WHOLE resulting config, not the overlay fragment alone.
    base = {**_BASE, "ai": {"pm_tools": {"correlation_max_symbols": 12}}}
    kernel = _kernel(tmp_path, base)
    kernel.apply_settings({"ai.pm_tools.macro_context": True})
    merged = AppConfig.model_validate(
        {**base, **{"ai": {**base["ai"], "pm_tools": {
            **base["ai"]["pm_tools"], "macro_context": True}}}})
    assert merged.ai.pm_tools.correlation_max_symbols == 12
