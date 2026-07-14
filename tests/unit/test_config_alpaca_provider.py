from __future__ import annotations

from poseidon.core.config import AppConfig


def test_alpaca_data_provider_and_local_ai_parse() -> None:
    raw = {
        "ai": {"backend": "openai_compatible", "base_url": "http://localhost:1234/v1",
               "model": "devstral-small-2-24b-instruct-2512"},
        "data": {"providers": [
            {"name": "alpaca", "credential": "alpaca_keys", "priority": 15,
             "options": {"feed": "iex"}},
            {"name": "finnhub", "credential": "finnhub_api_key", "priority": 20}]},
    }
    cfg = AppConfig.model_validate(raw)
    names = [p.name for p in cfg.data.providers]
    assert "alpaca" in names
    assert cfg.ai.backend == "openai_compatible"
    alpaca = next(p for p in cfg.data.providers if p.name == "alpaca")
    assert alpaca.options == {"feed": "iex"} and alpaca.priority == 15
