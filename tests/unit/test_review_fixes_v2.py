"""Regression tests for the second adversarial review pass and the in-app
Schwab OAuth login flow. Each test pins one confirmed fix so it cannot
silently regress.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from poseidon.brokers.plugins.schwab import DEFAULT_REDIRECT_URI, SchwabBroker
from poseidon.core.enums import AssetClass, OrderSide, OrderType
from poseidon.core.errors import BrokerError
from poseidon.core.models import Order

_SCHWAB_CREDS = {"app_key": "k", "app_secret": "s", "refresh_token": "r", "account_hash": "h"}


# ---------------------------------------------------------------- Schwab paper guard (U11)

def test_schwab_rejects_paper_mode() -> None:
    # Schwab has no paper environment; a paper request must refuse rather than
    # silently trade live.
    with pytest.raises(BrokerError, match="no paper environment"):
        SchwabBroker(credentials=_SCHWAB_CREDS, paper=True)


def test_schwab_live_construction_ok() -> None:
    broker = SchwabBroker(credentials=_SCHWAB_CREDS, paper=False)
    assert broker.name == "schwab"
    assert broker.is_paper is False


# ---------------------------------------------------------------- Schwab OAuth login flow

def test_schwab_authorize_url_targets_login() -> None:
    url = SchwabBroker.authorize_url("APPKEY123")
    assert url.startswith("https://api.schwabapi.com/v1/oauth/authorize?")
    assert "client_id=APPKEY123" in url
    assert "response_type=code" in url
    # Default callback matches the documented registered redirect URI.
    assert "127.0.0.1%3A8182" in url or DEFAULT_REDIRECT_URI in url.replace("%3A", ":")


def test_schwab_extract_code_from_redirect_url() -> None:
    pasted = "https://127.0.0.1:8182/?code=ABC.def-123&session=xyz"
    assert SchwabBroker.extract_code(pasted) == "ABC.def-123"


def test_schwab_extract_code_accepts_bare_code() -> None:
    assert SchwabBroker.extract_code("BARECODE123") == "BARECODE123"


def test_schwab_extract_code_rejects_garbage() -> None:
    with pytest.raises(BrokerError, match="no .*code"):
        SchwabBroker.extract_code("https://127.0.0.1:8182/?error=access_denied")


# ---------------------------------------------------------------- notional uses stop price (U3)

def test_estimated_notional_uses_stop_price_for_buy_stop() -> None:
    # A buy-stop above the market must be risk-checked at its trigger, not the
    # lower current price.
    order = Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.STOP,
                  quantity=Decimal("1000"), stop_price=Decimal("120"))
    assert order.estimated_notional(reference_price=Decimal("100")) == Decimal("120000")


def test_estimated_notional_prefers_highest_known_price() -> None:
    order = Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.LIMIT,
                  quantity=Decimal("10"), limit_price=Decimal("50"))
    # limit (50) beats reference (40)
    assert order.estimated_notional(reference_price=Decimal("40")) == Decimal("500")


def test_estimated_notional_none_without_any_price() -> None:
    order = Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.MARKET,
                  quantity=Decimal("10"))
    assert order.estimated_notional() is None


# ---------------------------------------------------------------- ProposedTrade quantity guard (C6)

def test_proposed_trade_rejects_nonpositive_quantity() -> None:
    from pydantic import ValidationError

    from poseidon.core.models import ProposedTrade
    for bad in (Decimal("0"), Decimal("-5")):
        with pytest.raises(ValidationError):
            ProposedTrade(symbol="AAPL", side=OrderSide.BUY, asset_class=AssetClass.EQUITY,
                          quantity=bad)


# ---------------------------------------------------------------- audit hash-encoding migration

async def _seed_legacy_chain(db, rows):
    from poseidon.security.audit import GENESIS_HASH, _record_hash_v1
    prev = GENESIS_HASH
    async with db.transaction() as conn:
        for seq, at, actor, action, payload in rows:
            h = _record_hash_v1(seq, at, actor, action, payload, prev)
            await conn.execute(
                "INSERT INTO audit (seq, at, actor, action, payload, prev_hash, hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (seq, at, actor, action, payload, prev, h),
            )
            prev = h


async def test_audit_legacy_chain_migrates(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from poseidon.security.audit import AuditLog
    from poseidon.storage.db import Database

    db = Database(tmp_path / "a.db")
    await db.open()
    await _seed_legacy_chain(db, [
        (1, "2026-01-01T00:00:00+00:00", "system", "a.one", "{}"),
        (2, "2026-01-01T00:00:01+00:00", "system", "a.two", '{"k":1}'),
        (3, "2026-01-01T00:00:02+00:00", "human", "a.three", "{}"),
    ])
    audit = AuditLog(db)
    ok, _ = await audit.verify_chain()
    assert ok is False  # current encoding rejects the legacy chain
    assert await audit.migrate_legacy_chain() is True
    ok2, bad = await audit.verify_chain()
    assert ok2 is True and bad is None
    await db.close()


async def test_audit_tampered_chain_refuses_migration(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from poseidon.security.audit import AuditLog
    from poseidon.storage.db import Database

    db = Database(tmp_path / "b.db")
    await db.open()
    await _seed_legacy_chain(db, [
        (1, "2026-01-01T00:00:00+00:00", "system", "a", "{}"),
        (2, "2026-01-01T00:00:01+00:00", "system", "b", "{}"),
    ])
    # Tamper with seq 1's payload without recomputing the hashes.
    await db.conn.execute("UPDATE audit SET payload='{\"evil\":1}' WHERE seq=1")
    await db.conn.commit()
    audit = AuditLog(db)
    assert await audit.migrate_legacy_chain() is False  # tampered -> refuse
    ok, _ = await audit.verify_chain()
    assert ok is False
    await db.close()
