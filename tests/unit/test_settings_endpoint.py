"""``GET/POST /api/settings`` and ``GET /api/macro`` pins.

Drives the real ``build_app`` over ``httpx.ASGITransport`` with a fake kernel,
matching test_correlation_endpoint.py. The refusals matter most: tier
enforcement has to live on the SERVER, because a UI that merely hides a control
stops nobody holding curl.
"""

from __future__ import annotations

import types
from typing import Any

import httpx
import pytest

from poseidon.api.server import build_app
from poseidon.core.config import AppConfig
from poseidon.core.errors import ConfigError
from poseidon.core.events import EventBus


class _Audit:
    def __init__(self) -> None:
        self.entries: list[tuple[str, str, dict[str, Any]]] = []

    async def append(self, actor: str, action: str, detail: dict[str, Any]) -> None:
        self.entries.append((actor, action, detail))


class _Kernel(types.SimpleNamespace):
    """Fake kernel whose apply_settings records calls and can be made to fail."""

    def apply_settings(self, updates: dict[str, Any]) -> dict[str, Any]:
        self.applied.append(dict(updates))
        if self.raises is not None:
            raise self.raises
        return {"applied": sorted(updates), "needs_restart": True}


def _kernel(*, raises: Exception | None = None,
            config_path: Any = None, base_yaml: str = "") -> _Kernel:
    """A fake kernel.

    ``config_path`` is pinned to a tmp file wherever provenance is asserted: an
    AppConfig() with config_path=None makes the endpoint fall back to the real
    ~/.config/poseidon/poseidon.yaml, which would make the test read whatever
    the developer happens to have enabled.
    """
    if config_path is not None:
        config_path.write_text(base_yaml, encoding="utf-8")
    config = AppConfig() if config_path is None else AppConfig(config_path=config_path)
    return _Kernel(bus=EventBus(), config=config, vault=None, router=None,
                   audit=_Audit(), applied=[], raises=raises)


def _client(kernel: Any, monkeypatch: pytest.MonkeyPatch) -> httpx.AsyncClient:
    monkeypatch.delenv("POSEIDON_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("POSEIDON_DASHBOARD_TOKEN_FILE", raising=False)
    app = build_app(kernel)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://127.0.0.1")


# ------------------------------------------------------------------ GET


async def test_get_returns_the_described_tree(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    kernel = _kernel(config_path=tmp_path / "poseidon.yaml", base_yaml="mode: research\n")
    async with _client(kernel, monkeypatch) as c:
        r = await c.get("/api/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    by_path = {e["path"]: e for e in body["settings"]}
    macro = by_path["ai.pm_tools.macro_context"]
    assert macro["kind"] == "bool"
    assert macro["writable"] is True
    assert macro["label"]
    assert macro["provenance"] == "default"
    assert "config_path" in body and "overlay_path" in body


async def test_get_never_exposes_credentials(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    kernel = _kernel(config_path=tmp_path / "poseidon.yaml", base_yaml="mode: research\n")
    async with _client(kernel, monkeypatch) as c:
        r = await c.get("/api/settings")
    paths = [e["path"] for e in r.json()["settings"]]
    assert not [p for p in paths if p.endswith("_credential")]
    assert "api_key" not in r.text.lower() or "api_key_credential" not in paths


async def test_get_marks_risk_limits_read_only(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    kernel = _kernel(config_path=tmp_path / "poseidon.yaml", base_yaml="mode: research\n")
    async with _client(kernel, monkeypatch) as c:
        r = await c.get("/api/settings")
    risk = [e for e in r.json()["settings"] if e["path"].startswith("risk.")]
    assert risk, "risk limits must still be VISIBLE — the operator sees the rails"
    assert all(e["writable"] is False for e in risk)
    assert all(e["tier"] == "read_only" for e in risk)


# ------------------------------------------------------------------ POST


async def test_post_applies_and_audits(monkeypatch: pytest.MonkeyPatch) -> None:
    kernel = _kernel()
    async with _client(kernel, monkeypatch) as c:
        r = await c.post("/api/settings",
                         json={"updates": {"ai.pm_tools.macro_context": True}})
    assert r.status_code == 200, r.text
    assert r.json()["needs_restart"] is True
    assert kernel.applied == [{"ai.pm_tools.macro_context": True}]
    actor, action, detail = kernel.audit.entries[-1]
    assert (actor, action) == ("human", "settings.updated")
    assert detail["paths"] == ["ai.pm_tools.macro_context"]
    assert detail["values"] == {"ai.pm_tools.macro_context": True}


async def test_post_rejects_an_empty_body(monkeypatch: pytest.MonkeyPatch) -> None:
    kernel = _kernel()
    async with _client(kernel, monkeypatch) as c:
        assert (await c.post("/api/settings", json={})).status_code == 422
        assert (await c.post("/api/settings", json={"updates": {}})).status_code == 422
        assert (await c.post("/api/settings", json={"updates": "nope"})).status_code == 422
    assert kernel.applied == []


async def test_a_refused_tier_is_403_and_is_not_audited(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # The server is the enforcement point. A caller bypassing the UI entirely
    # must still be refused — and a refusal is not a settings change, so it
    # must not appear in the audit trail as one.
    kernel = _kernel(raises=PermissionError("not writable: risk.max_position_pct"))
    async with _client(kernel, monkeypatch) as c:
        r = await c.post("/api/settings", json={"updates": {"risk.max_position_pct": 0.9}})
    assert r.status_code == 403
    assert "risk.max_position_pct" in r.json()["detail"]
    assert kernel.audit.entries == []


async def test_an_invalid_value_is_422_and_is_not_audited(
        monkeypatch: pytest.MonkeyPatch) -> None:
    kernel = _kernel(raises=ConfigError("rejected — the merged configuration is invalid"))
    async with _client(kernel, monkeypatch) as c:
        r = await c.post("/api/settings",
                         json={"updates": {"ai.pm_tools.correlation_max_symbols": 9999}})
    assert r.status_code == 422
    assert "invalid" in r.json()["detail"]
    assert kernel.audit.entries == []


# ------------------------------------------------------------------ macro


async def test_macro_endpoint_returns_the_snapshot(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from poseidon.data.macro import MacroSnapshot, VixQuote

    snapshot = MacroSnapshot(
        vix=VixQuote(level=14.55, change_percent=-5.0, last_trade_time=None),
        vix_regime="low", curve_as_of=None, yield_curve={"3M": 0.0387, "10Y": 0.0468},
        term_spread=0.0081, curve_inverted=False, gaps=[])

    async def fake(**_kwargs: Any) -> MacroSnapshot:
        return snapshot

    monkeypatch.setattr("poseidon.data.macro.fetch_macro_snapshot", fake)
    async with _client(_kernel(), monkeypatch) as c:
        r = await c.get("/api/macro")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["vix"]["level"] == 14.55
    assert body["vix"]["freshness"] == "delayed"
    assert "get_quote" in body["note"]


async def test_macro_is_not_gated_on_the_ai_tool_flag(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # ai.pm_tools.macro_context governs whether the AI may call the tool. The
    # operator reading VIX on their own dashboard is the same class of surface
    # as /api/quote, which is likewise unconditional.
    from poseidon.data.macro import MacroSnapshot

    async def fake(**_kwargs: Any) -> MacroSnapshot:
        return MacroSnapshot(vix=None, vix_regime=None, curve_as_of=None,
                             yield_curve={}, term_spread=None, curve_inverted=None,
                             gaps=["vix_unavailable: down"])

    monkeypatch.setattr("poseidon.data.macro.fetch_macro_snapshot", fake)
    kernel = _kernel()
    assert kernel.config.ai.pm_tools.macro_context is False  # the flag is OFF
    async with _client(kernel, monkeypatch) as c:
        r = await c.get("/api/macro")
    assert r.status_code == 200
    # A dead leg is a reported gap, never a zero.
    body = r.json()
    assert body["vix"] is None
    assert body["term_spread_10y_3m"] is None
    assert body["gaps"]


async def test_provenance_is_read_from_the_real_files(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    # A value set in the base file reads as "config file"; one set in the
    # dashboard overlay reads as "overlay". This is what makes "why didn't my
    # toggle stick?" answerable.
    (tmp_path / "poseidon.local.yaml").write_text(
        "ai:\n  pm_tools:\n    correlation: true\n", encoding="utf-8")
    kernel = _kernel(config_path=tmp_path / "poseidon.yaml",
                     base_yaml="ai:\n  pm_tools:\n    screen_market: true\n")
    async with _client(kernel, monkeypatch) as c:
        r = await c.get("/api/settings")
    by_path = {e["path"]: e for e in r.json()["settings"]}
    assert by_path["ai.pm_tools.screen_market"]["provenance"] == "config file"
    assert by_path["ai.pm_tools.correlation"]["provenance"] == "overlay"
    assert by_path["ai.pm_tools.macro_context"]["provenance"] == "default"
