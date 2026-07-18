"""Configuration model and loader.

Configuration lives in a single YAML file (default:
``~/.config/poseidon/poseidon.yaml``); every field is validated by pydantic
at startup and the process refuses to boot on invalid config rather than
running with surprises. Secrets are *never* stored here — the config holds
credential *names* which resolve through the encrypted vault
(:mod:`poseidon.security.vault`).

Environment variables prefixed with ``POSEIDON_`` override file values using
``__`` as the nesting delimiter, e.g. ``POSEIDON_AI__MODEL=claude-opus-4-8``.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .enums import TradingMode
from .errors import ConfigError


def default_config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "poseidon"


def default_data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "poseidon"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReflectionConfig(StrictModel):
    """Post-trade reflection → lesson-memory loop (advisory).

    Defaults give the closed loop: write a lesson on each close and re-inject
    relevant lessons into future cycles. ``inject: false`` makes it a reviewed
    ledger (written but not fed to the model); ``enabled: false`` turns it off.
    Lessons are advisory context only — they never gate or bypass the risk
    engine, and they are kept out of the tamper-evident audit chain.
    """

    enabled: bool = True
    inject: bool = True
    max_injected: int = Field(default=8, ge=0)
    per_symbol: int = Field(default=2, ge=0)
    global_n: int = Field(default=3, ge=0)
    lookback_days: int = Field(default=120, ge=1)


class StrategyHealthConfig(StrictModel):
    """Strategy-decay watchdog (advisory). Flags a strategy whose realized edge has
    decayed to <= 0; opt-in auto_retire deactivates a decayed CUSTOM strategy. It can
    only reduce trading — it never touches the risk engine or the order path."""

    enabled: bool = True
    auto_retire: bool = False
    window_trades: int = Field(default=20, ge=1)
    min_trades: int = Field(default=8, ge=1)
    baseline_min_trades: int = Field(default=20, ge=1)
    decay_t: float = Field(default=2.0, gt=0)
    decay_streak: int = Field(default=2, ge=1)
    retire_streak: int = Field(default=4, ge=1)
    recover_streak: int = Field(default=2, ge=1)


class AnalysisConfig(StrictModel):
    """Advisory analyst-firm → debate packet (upstream of the PM; never gates risk).

    OFF by default: it is call-heavy and only worth enabling deliberately. When
    enabled, a scheduled sweep precomputes one packet per active-watchlist symbol
    on the utility model; inject re-feeds the freshest packet into review cycles.
    Advisory only — the packet never reaches the risk engine or the order path.
    """

    enabled: bool = False
    inject: bool = True
    debate_rounds: int = Field(default=2, ge=1, le=4)
    risk_rounds: int = Field(default=1, ge=1, le=3)
    refresh_hours: int = Field(default=24, ge=1)
    max_injected: int = Field(default=3, ge=0)
    max_render_chars: int = Field(default=1200, ge=200)
    max_symbols_per_sweep: int = Field(default=8, ge=1)


class AIConfig(StrictModel):
    model: str = "claude-opus-4-8"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    max_tokens: int = Field(default=16000, ge=1024, le=128000)
    api_key_credential: str = "anthropic_api_key"  # vault entry name
    # Which LLM backend runs the portfolio manager. "anthropic" is the API
    # (default, unchanged); "openai_compatible" targets a local/self-hosted
    # OpenAI-style endpoint (e.g. LM Studio) via base_url — free, no API credit.
    backend: Literal["anthropic", "openai_compatible"] = "anthropic"
    base_url: str | None = None  # required when backend == "openai_compatible"
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)  # openai_compatible path only
    max_tool_iterations: int = Field(default=24, ge=1, le=100)
    review_interval_seconds: int = Field(default=300, ge=30)
    # Metering (USD per million tokens; defaults match claude-opus-4-8).
    input_price_per_mtok: float = Field(default=5.0, ge=0)
    output_price_per_mtok: float = Field(default=25.0, ge=0)
    # Hard monthly spend ceiling; review cycles pause when the estimate hits
    # it (0 disables the ceiling).
    monthly_budget_usd: float = Field(default=0.0, ge=0)
    # Post-trade reflection → lesson-memory loop (advisory; see ReflectionConfig).
    reflection: ReflectionConfig = Field(default_factory=ReflectionConfig)
    # Advisory analyst-firm → debate packet (advisory; see AnalysisConfig).
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    # Optional cheap/fast "utility" model for auxiliary roles (operator chat +
    # reflection). Same backend + endpoint as the primary, model swapped. None =
    # no tiering (all roles use the primary). The trading decision always uses
    # the primary model.
    utility_model: str | None = None

    @model_validator(mode="after")
    def _check_backend(self) -> AIConfig:
        if self.backend == "openai_compatible" and not self.base_url:
            raise ValueError("ai.base_url is required when ai.backend is 'openai_compatible'")
        if self.backend == "anthropic" and not self.api_key_credential:
            raise ValueError("ai.api_key_credential is required when ai.backend is 'anthropic'")
        return self


class ProviderConfig(StrictModel):
    name: str  # e.g. "polygon", "finnhub", "twelvedata", "alphavantage", "alpaca", "tradier"
    enabled: bool = True
    credential: str = ""  # vault entry name holding the API key/token
    priority: int = Field(default=100, ge=0)  # lower = preferred
    options: dict[str, Any] = Field(default_factory=dict)


class BrokerConfig(StrictModel):
    name: str  # plugin name, e.g. "alpaca", "tradier", "paper"
    enabled: bool = True
    primary: bool = False  # orders route to the primary broker
    credential: str = ""  # vault entry name (JSON blob of the plugin's fields)
    paper: bool = True  # sandbox/paper endpoints where the broker offers them
    options: dict[str, Any] = Field(default_factory=dict)


class DataConfig(StrictModel):
    providers: list[ProviderConfig] = Field(default_factory=list)
    real_time_max_age_seconds: float = Field(default=5.0, gt=0)
    delayed_max_age_seconds: float = Field(default=900.0, gt=0)
    allow_delayed_for_research: bool = True  # delayed data OK for research, never for orders
    request_timeout_seconds: float = Field(default=10.0, gt=0)


class RiskConfig(StrictModel):
    max_position_pct: float = Field(default=0.10, gt=0, le=1)  # of equity per position
    max_portfolio_exposure_pct: float = Field(default=1.0, gt=0, le=2)
    max_daily_loss_pct: float = Field(default=0.03, gt=0, le=1)
    max_weekly_loss_pct: float = Field(default=0.07, gt=0, le=1)
    max_drawdown_pct: float = Field(default=0.15, gt=0, le=1)
    max_leverage: float = Field(default=1.0, ge=1.0)
    max_options_exposure_pct: float = Field(default=0.20, ge=0, le=1)
    max_sector_concentration_pct: float = Field(default=0.30, gt=0, le=1)
    max_order_notional: Decimal = Field(default=Decimal("25000"))
    min_order_notional: Decimal = Field(default=Decimal("1"))
    max_spread_pct: float = Field(default=0.02, gt=0)  # liquidity/spread filter
    min_avg_volume: int = Field(default=100_000, ge=0)
    max_orders_per_day: int = Field(default=40, ge=1)
    trade_cooldown_seconds: int = Field(default=300, ge=0)  # per-symbol cooldown
    news_blackout_minutes_before_econ: int = Field(default=10, ge=0)
    volatility_halt_daily_move_pct: float = Field(default=0.08, gt=0)  # index circuit proxy
    circuit_breaker_error_threshold: int = Field(default=5, ge=1)
    circuit_breaker_window_seconds: int = Field(default=300, ge=10)
    circuit_breaker_cooldown_seconds: int = Field(default=1800, ge=60)
    slippage_limit_pct: float = Field(default=0.01, gt=0)  # market-order protection band
    # Portfolio VaR halt: block NEW risk when the book's 1-day historical
    # VaR(95) exceeds this fraction of equity. 0 disables the rule. When
    # enabled, fresh risk metrics are REQUIRED before opening new risk.
    max_portfolio_var_pct: float = Field(default=0.0, ge=0, le=1)
    benchmark_symbol: str = "SPY"  # beta/correlation benchmark for risk metrics
    # Vol-targeted sizing: per-position daily risk budget as a fraction of
    # equity (0.005 = a position sized so one typical day moves it by
    # ~0.5% of account equity). Advisory input to the AI's sizing tool.
    position_risk_budget_pct: float = Field(default=0.005, gt=0, le=0.1)


class GuardianConfig(StrictModel):
    """Position guardian: enforces each decision's stop-loss / take-profit
    between review cycles (see docs/risk-controls.md#position-guardian)."""

    enabled: bool = True
    interval_seconds: int = Field(default=60, ge=5)


class ReportsConfig(StrictModel):
    daily_summary: bool = True
    daily_summary_cron: str = "15 16 * * 1-5"  # 16:15 ET weekdays


class StrategyConfig(StrictModel):
    name: str
    enabled: bool = True
    symbols: list[str] = Field(default_factory=list)
    options: dict[str, Any] = Field(default_factory=dict)


class ScheduleConfig(StrictModel):
    name: str
    job: str  # registered job name
    every_seconds: int | None = Field(default=None, ge=1)
    cron: str | None = None  # standard 5-field cron, evaluated in America/New_York
    only_market_hours: bool = False
    enabled: bool = True

    @model_validator(mode="after")
    def _one_trigger(self) -> ScheduleConfig:
        if bool(self.every_seconds) == bool(self.cron):
            raise ValueError(f"schedule '{self.name}': set exactly one of every_seconds or cron")
        return self


class NotificationChannelConfig(StrictModel):
    kind: Literal["desktop", "email", "discord", "telegram", "webhook"]
    enabled: bool = True
    credential: str = ""  # vault entry for tokens/SMTP password
    min_level: Literal["info", "warning", "critical"] = "info"
    options: dict[str, Any] = Field(default_factory=dict)


class DashboardConfig(StrictModel):
    host: str = "127.0.0.1"  # local-only by default
    port: int = Field(default=8321, ge=1, le=65535)
    # Vault entry holding a bearer token. Optional on loopback; REQUIRED when
    # host is anything else (validated at startup).
    auth_token_credential: str = ""


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class UpdateConfig(StrictModel):
    enabled: bool = True
    check_interval_hours: int = Field(default=24, ge=1)
    channel: Literal["git"] = "git"  # self-update via the installed git checkout
    auto_apply: bool = True  # self-update on launch by default; restart-gated + --ff-only safe


class WatchlistConfig(StrictModel):
    name: str = "default"
    symbols: list[str] = Field(default_factory=list)

    @field_validator("symbols")
    @classmethod
    def _upper(cls, v: list[str]) -> list[str]:
        return [s.strip().upper() for s in v if s.strip()]


class ResearchConfig(StrictModel):
    """Offline factor research (`poseidon research factors`): point-in-time IC/IR
    ranking of the factor library over historical bars. Read-only — this path
    never touches the risk engine, the order path, or live capital."""

    horizon: int = Field(default=5, ge=1)  # bars ahead for the primary forward return
    rebalance_every: int = Field(default=5, ge=1)  # trading days between IC samples
    horizons: list[int] = Field(default_factory=lambda: [1, 5, 10, 20])  # IC-decay profile
    min_cross: int = Field(default=5, ge=1)  # minimum cross-sectional names per sample date
    lookback_days: int = Field(default=400, ge=1)  # default history window when unset by --days


class AppConfig(StrictModel):
    mode: TradingMode = TradingMode.RESEARCH  # safest default
    data_dir: Path = Field(default_factory=default_data_dir)
    # Set by load_config() to the file it loaded — lets runtime features
    # (the dashboard's Account view) locate the sibling poseidon.local.yaml
    # overlay. Not meant to be set in YAML; harmless if it is.
    config_path: Path | None = None
    log_level: str = "INFO"
    ai: AIConfig = Field(default_factory=AIConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    brokers: list[BrokerConfig] = Field(default_factory=list)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    guardian: GuardianConfig = Field(default_factory=GuardianConfig)
    reports: ReportsConfig = Field(default_factory=ReportsConfig)
    strategies: list[StrategyConfig] = Field(default_factory=list)
    schedules: list[ScheduleConfig] = Field(default_factory=list)
    notifications: list[NotificationChannelConfig] = Field(default_factory=list)
    watchlists: list[WatchlistConfig] = Field(default_factory=list)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    updates: UpdateConfig = Field(default_factory=UpdateConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    strategy_health: StrategyHealthConfig = Field(default_factory=StrategyHealthConfig)

    @model_validator(mode="after")
    def _validate_brokers(self) -> AppConfig:
        enabled = [b for b in self.brokers if b.enabled]
        primaries = [b for b in enabled if b.primary]
        if len(primaries) > 1:
            raise ValueError("only one broker may be marked primary")
        if self.mode is not TradingMode.RESEARCH and not primaries:
            raise ValueError("a primary broker is required outside research mode")
        names = [b.name for b in enabled]
        if len(names) != len(set(names)):
            raise ValueError("duplicate enabled broker names")
        return self

    @model_validator(mode="after")
    def _validate_dashboard_exposure(self) -> AppConfig:
        if (self.dashboard.host not in _LOOPBACK_HOSTS
                and not self.dashboard.auth_token_credential
                and not dashboard_token_from_env()):
            raise ValueError(
                "dashboard.host is not loopback — set dashboard.auth_token_credential "
                "(a vault entry) or provide POSEIDON_DASHBOARD_TOKEN[_FILE] before "
                "exposing the dashboard"
            )
        return self

    def primary_broker(self) -> BrokerConfig | None:
        for b in self.brokers:
            if b.enabled and b.primary:
                return b
        return None

    def all_watchlist_symbols(self) -> list[str]:
        seen: dict[str, None] = {}
        for wl in self.watchlists:
            for s in wl.symbols:
                seen.setdefault(s)
        return list(seen)


# Reserved POSEIDON_* env vars consumed directly by other modules (the vault
# reads these for its passphrase; the dashboard reads a bearer token) — they
# are NOT config fields, so folding them into the AppConfig override dict would
# trip extra="forbid" and abort every command. Excluded from the override scan.
_RESERVED_ENV = {
    "POSEIDON_VAULT_PASSPHRASE", "POSEIDON_VAULT_PASSPHRASE_FILE",
    "POSEIDON_DASHBOARD_TOKEN", "POSEIDON_DASHBOARD_TOKEN_FILE",
}


def dashboard_token_from_env() -> str | None:
    """Bearer token supplied directly via env or a secret file, as an
    alternative to a vault entry (auth_token_credential). This lets a
    container/secret deployment satisfy the exposed-dashboard auth requirement
    without pre-seeding the vault (a chicken-and-egg at first boot). Mirrors the
    vault-passphrase env convention."""
    direct = os.environ.get("POSEIDON_DASHBOARD_TOKEN")
    if direct and direct.strip():
        return direct.strip()
    path = os.environ.get("POSEIDON_DASHBOARD_TOKEN_FILE")
    if path:
        try:
            token = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return token or None
    return None


def _deep_env_overrides() -> dict[str, Any]:
    """Build a nested override dict from POSEIDON_* environment variables."""
    result: dict[str, Any] = {}
    for key, value in os.environ.items():
        if not key.startswith("POSEIDON_") or key in _RESERVED_ENV:
            continue
        path = key[len("POSEIDON_"):].lower().split("__")
        node = result
        for i, part in enumerate(path[:-1]):
            nxt = node.setdefault(part, {})
            if not isinstance(nxt, dict):
                raise ConfigError(
                    f"conflicting environment overrides: {key} nests under "
                    f"POSEIDON_{'__'.join(path[: i + 1]).upper()}, which is also set"
                )
            node = nxt
        if isinstance(node.get(path[-1]), dict):
            raise ConfigError(
                f"conflicting environment overrides: {key} would overwrite "
                f"nested {key}__* variables"
            )
        node[path[-1]] = value
    return result


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


def _merge_named_list(base: list[Any], overlay: list[Any]) -> list[Any]:
    """Merge two lists of {name: ...} entries. An overlay entry deep-merges
    over the same-name base entry — overlay keys win, base-only keys (e.g. a
    broker's ``options``) survive; unknown overlay names are appended. Base
    rows pass through verbatim (including malformed or duplicate ones) so
    pydantic still reports the same errors with or without an overlay."""
    overlay_by_name: dict[str, dict[str, Any]] = {}
    for entry in overlay:
        if isinstance(entry, dict) and "name" in entry:
            overlay_by_name[str(entry["name"])] = dict(entry)
    merged: list[Any] = []
    for entry in base:
        if isinstance(entry, dict) and "name" in entry:
            name = str(entry["name"])
            if name in overlay_by_name:
                merged.append(_deep_merge(entry, overlay_by_name.pop(name)))
                continue
        merged.append(entry)
    merged.extend(overlay_by_name.values())
    return merged


def apply_local_overlay(raw: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge the dashboard-managed poseidon.local.yaml over the main config.

    Semantics differ from _deep_merge for the two named lists the dashboard
    manages: ``brokers`` and ``data.providers`` merge per-entry by name
    instead of being replaced wholesale, and if the overlay marks a broker
    primary every base broker loses its primary flag first (there can be
    only one, and the overlay's choice wins).
    """
    merged = dict(raw)
    overlay = dict(overlay)
    overlay_brokers = overlay.pop("brokers", None)
    overlay_data = dict(overlay.pop("data", {}) or {})
    overlay_providers = overlay_data.pop("providers", None)

    merged = _deep_merge(merged, overlay)
    if overlay_data:
        merged["data"] = _deep_merge(dict(merged.get("data", {}) or {}), overlay_data)
    if overlay_providers is not None:
        data_section = dict(merged.get("data", {}) or {})
        data_section["providers"] = _merge_named_list(
            list(data_section.get("providers", []) or []), list(overlay_providers)
        )
        merged["data"] = data_section
    if overlay_brokers is not None:
        base_brokers = list(merged.get("brokers", []) or [])
        if any(isinstance(b, dict) and b.get("primary") for b in overlay_brokers):
            base_brokers = [
                {**b, "primary": False} if isinstance(b, dict) else b for b in base_brokers
            ]
        merged["brokers"] = _merge_named_list(base_brokers, list(overlay_brokers))
    return merged


def local_overlay_path(config_path: Path) -> Path:
    return config_path.with_name("poseidon.local.yaml")


def load_config(path: Path | None = None) -> AppConfig:
    path = path or default_config_dir() / "poseidon.yaml"
    raw: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError(f"cannot parse {path}: {exc}") from exc
        if loaded is not None and not isinstance(loaded, dict):
            raise ConfigError(f"{path} must contain a YAML mapping")
        raw = loaded or {}
    # Dashboard-managed overlay (broker connected from the Account view).
    overlay_file = local_overlay_path(path)
    if overlay_file.exists():
        try:
            overlay = yaml.safe_load(overlay_file.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError(f"cannot parse {overlay_file}: {exc}") from exc
        if overlay is not None and not isinstance(overlay, dict):
            raise ConfigError(f"{overlay_file} must contain a YAML mapping")
        if overlay:
            try:
                raw = apply_local_overlay(raw, overlay)
            except (TypeError, ValueError, AttributeError) as exc:
                raise ConfigError(
                    f"invalid overlay structure in {overlay_file}: {exc} — fix or delete the file"
                ) from exc
    raw = _deep_merge(raw, _deep_env_overrides())
    try:
        config = AppConfig.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError formats nicely via str()
        raise ConfigError(f"invalid configuration ({path}):\n{exc}") from exc
    config.config_path = path
    return config
