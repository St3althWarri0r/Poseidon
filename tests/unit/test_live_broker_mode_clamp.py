"""Autonomous must never survive a restart onto a LIVE broker.

The in-session demotion (`app.py`, broker switch) is correct but **in-memory
only**: `kernel.set_mode` mutates the order manager and the kv consent latch and
never writes config. The *persisted* mode lives in `poseidon.local.yaml`, which
the dashboard Settings view can write and which `mode=cfg.mode` reads at
construction. Nothing re-checked paper-vs-live at boot.

So: set Trading mode -> autonomous in Settings while on paper; later connect a
LIVE account (correctly demoted to APPROVAL in memory); restart -> the engine
boots **AUTONOMOUS against the LIVE account**, and the control's own help text
told the operator the demotion protected them.

This is the boot-time clamp. It is deliberately independent of how the mode got
there, so it also catches a hand-edited config.
"""

from __future__ import annotations

from poseidon.app import clamp_mode_for_broker
from poseidon.core.enums import TradingMode


def test_autonomous_on_a_live_broker_is_demoted() -> None:
    assert clamp_mode_for_broker(TradingMode.AUTONOMOUS, is_paper=False) is TradingMode.APPROVAL


def test_autonomous_on_paper_is_untouched() -> None:
    assert clamp_mode_for_broker(TradingMode.AUTONOMOUS, is_paper=True) is TradingMode.AUTONOMOUS


def test_demotion_only_never_promotes() -> None:
    """RESEARCH must never be raised to APPROVAL by a clamp."""
    for paper in (True, False):
        assert clamp_mode_for_broker(TradingMode.RESEARCH, is_paper=paper) is TradingMode.RESEARCH
        assert clamp_mode_for_broker(TradingMode.APPROVAL, is_paper=paper) is TradingMode.APPROVAL
