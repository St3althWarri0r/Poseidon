"""Process identity and identity-checked termination.

A live process is identified by ``(pid, starttime)`` — starttime is field 22
of ``/proc/<pid>/stat`` (clock ticks since boot at process start). A PID alone
is recyclable; the pair is stable for the life of the boot, so a signal
decision keyed on it can never land on an innocent process that inherited the
number. Every signal below re-verifies identity immediately before sending;
the microsecond verify->signal window that remains has no atomic close on
Linux and is the accepted residual.

Group targeting: a process is signalled via ``killpg`` only when it is its own
group leader (``getpgid(pid) == pid``) — true for every legitimate engine form
(``start_new_session`` spawn, terminal job, systemd service). Non-leaders get
plain ``kill`` so a shared foreign group is never blasted.
"""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class _HasIdent(Protocol):
    @property
    def pid(self) -> int: ...
    @property
    def starttime(self) -> int: ...


@dataclass(frozen=True)
class ProcIdent:
    """One incarnation of one process."""

    pid: int
    starttime: int


def parse_stat_starttime(stat_text: str) -> int | None:
    """starttime out of a ``/proc/<pid>/stat`` line. The comm field may contain
    spaces and parentheses, so fields are counted after the LAST ``)``: the
    tail starts at field 3 (state), putting starttime (field 22) at index 19."""
    _, sep, tail = stat_text.rpartition(")")
    if not sep:
        return None
    fields = tail.split()
    if len(fields) < 20:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def _stat_state(stat_text: str) -> str | None:
    """The process state char — field 3, the first token after the last ``)``.
    Using the same LAST-``)`` split as the starttime parse means a comm holding
    parens or a stray 'Z' cannot be misread as the state."""
    _, sep, tail = stat_text.rpartition(")")
    fields = tail.split()
    return fields[0] if sep and fields else None


def proc_starttime(pid: int, proc_root: Path = Path("/proc")) -> int | None:
    """The live starttime of ``pid``, or None if it does not exist, is
    unreadable, OR is a zombie/dead process.

    A zombie ('Z', dead-but-unreaped) keeps a readable stat with its original
    starttime, so a raw read would call a corpse alive — making stop_process
    SIGKILL-escalate for the full grace on a process that is already gone.
    States 'Z'/'X'/'x' are therefore reported as gone (None).

    Bytes + lossy decode: comm may hold non-UTF-8 bytes (prctl-settable), and
    both the state and starttime tail are ASCII, so ``errors="replace"`` cannot
    perturb the check while removing the UnicodeDecodeError crash path."""
    try:
        text = (proc_root / str(pid) / "stat").read_bytes().decode(errors="replace")
    except OSError:
        return None
    if _stat_state(text) in ("Z", "X", "x"):   # zombie/dead — effectively gone
        return None
    return parse_stat_starttime(text)


def same_process(ident: _HasIdent, proc_root: Path = Path("/proc")) -> bool:
    """True while ``ident`` still names the same incarnation."""
    return proc_starttime(ident.pid, proc_root) == ident.starttime


def stop_process(
    ident: _HasIdent,
    *,
    grace: float = 10.0,
    kill_grace: float = 2.0,
    interval: float = 0.25,
    kill: Callable[[int, int], None] = os.kill,
    killpg: Callable[[int, int], None] = os.killpg,
    getpgid: Callable[[int], int] = os.getpgid,
    starttime_of: Callable[[int], int | None] = proc_starttime,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """TERM -> up to ``grace`` seconds -> KILL. True when the process is gone
    (or was never alive as this incarnation). Liveness during the grace poll is
    IDENTITY (starttime) — a recycled pid reads as dead, so the SIGKILL
    escalation cannot hit a newcomer."""

    def alive() -> bool:
        return starttime_of(ident.pid) == ident.starttime

    def send(sig: int) -> None:
        if not alive():   # re-verify immediately before signalling
            return
        try:
            pgid = getpgid(ident.pid)
        except ProcessLookupError:
            return
        try:
            if pgid == ident.pid:
                killpg(pgid, sig)
            else:
                kill(ident.pid, sig)
        except ProcessLookupError:
            pass

    if not alive():
        return True
    send(signal.SIGTERM)
    waited = 0.0
    while waited < grace:
        if not alive():
            return True
        sleep(interval)
        waited += interval
    send(signal.SIGKILL)
    waited = 0.0
    while waited < kill_grace:
        if not alive():
            return True
        sleep(interval)
        waited += interval
    return not alive()
