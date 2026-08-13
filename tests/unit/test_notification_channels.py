"""Desktop notification channel — argv construction.

``notify-send``'s usage is ``notify-send [OPTION...] <SUMMARY> [BODY]`` and
GLib's option parser *permutes*: it scans for leading-``-`` arguments anywhere
in argv, not only before the first positional. Notification bodies are
server-controlled (``_on_system_error`` forwards ``str(payload["error"])`` and
``_on_direct`` forwards a caller-supplied body verbatim), so a body that begins
with ``-`` is parsed as an option and the notification is never delivered:

    $ notify-send "AuditTest" "-x"
    Unknown option -x        # exit 1, nothing shown

The bodies most likely to lead with ``-`` are negative P&L figures, i.e. loss
and drawdown alerts — exactly the ones the operator must not miss. Passing the
``--`` end-of-options terminator is what makes body content inert.

(There is no shell here: the channel uses ``create_subprocess_exec``, so this
was never a command-injection issue — only an argument-parsing one.)
"""

from __future__ import annotations

from typing import Any

import pytest

from poseidon.core.enums import NotificationLevel
from poseidon.notifications.channels import DesktopChannel


class _FakeProc:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode


@pytest.fixture
def captured_argv(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    async def fake_exec(*args: str, **_kw: Any) -> _FakeProc:
        calls.append(list(args))
        return _FakeProc(0)

    monkeypatch.setattr("shutil.which", lambda _n: "/usr/bin/notify-send")
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    return calls


def _channel() -> DesktopChannel:
    return DesktopChannel(credential=None, options={}, min_level=NotificationLevel.INFO)


async def test_dash_leading_body_is_not_parsed_as_an_option(
    captured_argv: list[list[str]],
) -> None:
    """A body beginning with '-' must reach notify-send as a positional."""
    ok = await _channel().send(
        NotificationLevel.CRITICAL, "Daily loss limit", "-3.2% today, halting new risk"
    )
    assert ok
    argv = captured_argv[0]
    body = "-3.2% today, halting new risk"
    assert "--" in argv, f"no end-of-options terminator in {argv!r}"
    assert argv.index("--") < argv.index(body), (
        f"'--' must precede the positionals so {body!r} is not read as an option: {argv!r}"
    )


async def test_terminator_precedes_summary_and_body(captured_argv: list[list[str]]) -> None:
    await _channel().send(NotificationLevel.INFO, "Order filled", "BUY 10 AAPL @ 150")
    argv = captured_argv[0]
    assert argv[-2:] == ["Poseidon: Order filled", "BUY 10 AAPL @ 150"]
    assert argv[-3] == "--", f"terminator must sit immediately before the positionals: {argv!r}"


async def test_options_still_precede_the_terminator(captured_argv: list[list[str]]) -> None:
    await _channel().send(NotificationLevel.CRITICAL, "Circuit breaker opened", "halted")
    argv = captured_argv[0]
    assert "--urgency=critical" in argv
    assert argv.index("--urgency=critical") < argv.index("--")


async def test_nonzero_exit_is_reported_as_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dropped notification must not read as success."""

    async def fake_exec(*_a: str, **_kw: Any) -> _FakeProc:
        return _FakeProc(1)

    monkeypatch.setattr("shutil.which", lambda _n: "/usr/bin/notify-send")
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    assert await _channel().send(NotificationLevel.INFO, "t", "b") is False
