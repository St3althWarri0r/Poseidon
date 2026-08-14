# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Poseidon is a private, single-user, Linux-native autonomous trading platform where **Claude is the portfolio manager**. Strategy screeners produce signals → the Claude agent reasons over live market data in a tool loop → the risk engine vets every order → a broker plugin executes. It can place **real trades with real money**, so correctness, auditability, and the safety invariants below are not optional.

The default configuration starts in **research mode with the paper broker** — nothing can trade until both are deliberately changed. There are three operating modes: `research` (no orders), `approval` (human confirms each trade), `autonomous` (executes within risk limits). The operator **chat panel can never place an order** — it is discussion-only, with its own tool dispatcher.

## Commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest                                    # full suite (~a few seconds)
pytest tests/unit/test_risk.py            # one file
pytest tests/unit/test_risk.py::test_name # one test
pytest -k "guardian and not slow"         # by keyword
pytest --cov --cov-report=term-missing    # with coverage (CI uses this)

ruff check src tests                      # lint (run `ruff check --fix` before committing)
mypy src                                  # type check — strict mode
```

CI (`.github/workflows/ci.yml`) runs `ruff check src tests`, `mypy src`, and `pytest --cov` on Python 3.11 and 3.12, and builds `docker/Dockerfile`. Run all three locally before pushing — mypy is strict and the gate is real.

Runtime CLI (see `src/poseidon/cli.py`): `poseidon run` (24/7 service), `poseidon app` (desktop dashboard window), `poseidon cycle` (one review cycle then exit — useful for testing the pipeline), `poseidon doctor` (self-diagnostics), `poseidon config validate`, `poseidon vault init|set|list`, `poseidon audit verify|tail`.

## Architecture

**Start at `src/poseidon/app.py` (`ApplicationKernel`) — the composition root.** Everything is constructed and wired there in dependency order; subsystems never reach for globals, they receive dependencies and communicate over the `EventBus` (`core/events.py`, `Topics`). To understand the system, follow `ApplicationKernel.run_review_cycle()` — it touches every major subsystem in sequence:

```
strategies.scan_all → agent.run_cycle (Claude tool loop over live data)
  → persist decision (decisions + ai_usage tables) → audit.append
  → order_manager.execute_decision → RiskEngine (every rule) → Broker plugin
```

Portfolio state syncs back from the broker (`PortfolioSyncService`) and feeds the next cycle. The `PositionGuardian` enforces each entry's stop-loss/take-profit against live quotes *between* cycles.

Key subsystems (each package's module docstring states its contract):
- **`data/router.py` (`DataRouter`)** — multi-provider failover with penalty-box backoff and staleness rejection (`FreshnessPolicy`). Provider registry is `BUILTIN_PROVIDERS`; third parties register via the `poseidon.data_providers` entry point.
- **`risk/engine.py` (`RiskEngine`)** — position/exposure/leverage caps, loss limits, drawdown halt, VaR halt, concentration, circuit breaker. Every order passes every rule.
- **`ai/`** — `agent.py` (`ClaudeAgent`, review cycles via the `anthropic` SDK), `chat.py` (`ChatService`, operator chat), `tools.py` (`ToolDispatcher`, the live-data tools Claude calls), `reviewer.py` (converts pasted external algorithms). The review cycle and chat each get their **own** `ToolDispatcher` so a concurrent chat tool call can't inject data-source provenance into the audited decision record.
- **`brokers/`** — `base.Broker` interface, `registry.py` catalog, `plugins/`. `PaperBroker` *is* the simulator (also used by the integration suite). Add a broker by implementing one class (`docs/plugin-development.md`); register via the `poseidon.brokers` entry point.
- **`security/`** — `vault.py` (scrypt+Fernet encrypted credential store) and `audit.py` (hash-chained tamper-evident log, verified at startup and nightly; a broken chain refuses startup / trips the circuit breaker).
- **`storage/db.py`** — `aiosqlite` wrapper; tables include `decisions`, `orders`, `ai_usage`, `equity_marks`, `exit_plans`.
- **`scheduler/`, `health/`, `notifications/`, `analytics/`, `backtest/`, `strategy/`** — cron/interval jobs, health probes, notification channels, performance/risk metrics, the anti-lookahead backtester (`BacktestEngine` + `monte_carlo`/`walk_forward`/`stress_test`), and the strategy engine + algorithm workshop.

The codebase is fully async (`asyncio`/`aiosqlite`/`websockets`), FastAPI + uvicorn for the localhost-only dashboard (`api/`, static UI in `api/static`), and pydantic v2 for config and domain models. `docs/architecture.md` is the fuller map.

## Invariants — preserve these when changing code

1. **Live data only.** Market-data models always carry `as_of` + `source`; anything that can feed the AI or an order goes through `DataRouter` so staleness is enforced. Never source a market fact from memory or an estimate.
2. **One order path.** `OrderManager._process_order` is the *only* call path to `Broker.submit_order`. Do not add another.
3. **Money is `Decimal` end to end** — never float.
4. **Audit consequential actions.** Any new consequential action gets an `audit.append(...)` so it lands in the tamper-evident chain.
5. **Errors subclass `PoseidonError`** and set `retryable` honestly.
6. **Secrets live only in the vault.** Config (`poseidon.yaml`, the dashboard-managed `poseidon.local.yaml` overlay) stores credential *names*, never values. structlog is used everywhere; the log redactor is a backstop, not permission to log secrets.

## Config & data locations

Config: `~/.config/poseidon/poseidon.yaml` (+ `poseidon.local.yaml` overlay written by the dashboard's Account view, merged at startup). Runtime data: `~/.local/share/poseidon/` (`vault.bin`, `poseidon.db`). Vault unlock reads `POSEIDON_VAULT_PASSPHRASE` / `..._FILE` or a systemd credential (`docs/security.md`); `poseidon vault init` interactively otherwise.

## Conventions

- Python 3.11+, `from __future__ import annotations`, full type hints (mypy strict). ruff line length 100.
- **Tests use pytest-asyncio auto mode** — write plain `async def test_...`, no decorator. **No network in tests**: use `FakeProvider` from `tests/conftest.py` (configurable quotes/bars/failures/staleness) instead of mocking HTTP. Patch market state via `patch.object(MarketClock, "session", ...)` when a test needs the market "open". `tests/unit/` is isolated; `tests/integration/` wires real components over the fake provider.
- Docstrings explain *contracts and why*, not what the next line does.
- Version lives in `src/poseidon/__init__.py` + `pyproject.toml` (the PKGBUILD derives `pkgver` from `pyproject.toml`); keep them in sync.
