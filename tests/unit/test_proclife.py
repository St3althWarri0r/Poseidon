"""Process identity (pid, starttime) and the identity-checked stop sequence.

starttime (field 22 of /proc/<pid>/stat) pins a PID to one incarnation: a
recycled PID reads as a DIFFERENT process, so signals can never land on an
innocent. Signals are re-verified immediately before sending; the remaining
microsecond verify->signal window has no atomic close on Linux (documented
residual).
"""
from __future__ import annotations

import signal

from poseidon.proclife import (
    ProcIdent,
    parse_stat_starttime,
    proc_starttime,
    same_process,
    stop_process,
)

# --- starttime parsing (comm may contain spaces AND parens) ---

def test_parse_stat_starttime_plain() -> None:
    line = "1234 (python3) S 1 1234 1234 0 -1 4194560 " + "0 " * 12 + "777 " + "0 " * 20
    assert parse_stat_starttime(line) == 777


def test_parse_stat_starttime_hostile_comm() -> None:
    # comm "(evil ) 2 (x" — everything after the LAST ')' is the field tail.
    tail = ("S 1 2 3 0 -1 4194560 0 0 0 0 0 0 0 0 20 0 1 0 424242 " + "0 " * 20).strip()
    line = f"999 ((evil ) 2 (x) {tail}"
    assert parse_stat_starttime(line) == 424242


def test_parse_stat_starttime_garbage() -> None:
    assert parse_stat_starttime("") is None
    assert parse_stat_starttime("12 (x) S 1") is None            # too few fields
    assert parse_stat_starttime("12 (x) " + "a " * 25) is None   # non-numeric


def test_proc_starttime_reads_proc_tree(tmp_path) -> None:
    stat = tmp_path / "42" / "stat"
    stat.parent.mkdir()
    stat.write_text("42 (poseidon run) S 1 42 42 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 31337 0 0")
    assert proc_starttime(42, proc_root=tmp_path) == 31337
    assert proc_starttime(43, proc_root=tmp_path) is None        # no such pid


def test_proc_starttime_tolerates_non_utf8_comm(tmp_path) -> None:
    # comm is prctl-settable to arbitrary bytes; the identity read must not
    # crash on invalid UTF-8 (the starttime tail is ASCII regardless).
    stat = tmp_path / "9" / "stat"
    stat.parent.mkdir()
    stat.write_bytes(b"9 (\xffevil\xff) S 1 9 9 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 555 0 0")
    assert proc_starttime(9, proc_root=tmp_path) == 555


def test_same_process_matches_only_same_incarnation(tmp_path) -> None:
    stat = tmp_path / "7" / "stat"
    stat.parent.mkdir()
    stat.write_text("7 (x) S 1 7 7 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 100 0 0")
    assert same_process(ProcIdent(7, 100), proc_root=tmp_path) is True
    assert same_process(ProcIdent(7, 999), proc_root=tmp_path) is False   # recycled pid
    assert same_process(ProcIdent(8, 100), proc_root=tmp_path) is False   # dead pid


# --- stop_process sequencing (all seams injected; no real signals) ---

class _Seams:
    """Scriptable process world: starttime lookups + recorded signals."""

    def __init__(
        self,
        timeline: list[int | None],
        pgid: int | None = None,
        raise_on_signal: bool = False,
    ) -> None:
        self.timeline = list(timeline)   # successive starttime_of() answers
        self.signals: list[tuple[str, int, int]] = []
        self._pgid = pgid
        self._raise_on_signal = raise_on_signal   # signal call itself raises
        self.slept: list[float] = []

    def starttime_of(self, pid: int) -> int | None:
        return self.timeline.pop(0) if len(self.timeline) > 1 else self.timeline[0]

    def getpgid(self, pid: int) -> int:
        if self._pgid is None:
            raise ProcessLookupError(pid)
        return self._pgid

    def kill(self, pid: int, sig: int) -> None:
        self.signals.append(("kill", pid, sig))
        if self._raise_on_signal:
            raise ProcessLookupError(pid)

    def killpg(self, pgid: int, sig: int) -> None:
        self.signals.append(("killpg", pgid, sig))
        if self._raise_on_signal:
            raise ProcessLookupError(pgid)

    def sleep(self, s: float) -> None:
        self.slept.append(s)

    def run(self, ident: ProcIdent, **kw) -> bool:
        return stop_process(
            ident, kill=self.kill, killpg=self.killpg, getpgid=self.getpgid,
            starttime_of=self.starttime_of, sleep=self.sleep, **kw)


def test_stop_term_then_exit_no_sigkill() -> None:
    # alive at verify, alive at TERM, gone on first poll
    w = _Seams(timeline=[100, 100, None], pgid=50)
    assert w.run(ProcIdent(50, 100)) is True
    assert w.signals == [("killpg", 50, signal.SIGTERM)]


def test_stop_escalates_to_sigkill_when_it_refuses_to_die() -> None:
    w = _Seams(timeline=[100], pgid=50)          # alive forever (same incarnation)
    assert w.run(ProcIdent(50, 100), grace=0.5, kill_grace=0.25, interval=0.25) is False
    assert ("killpg", signal.SIGTERM) in [(k, c) for k, _, c in w.signals]
    assert ("killpg", signal.SIGKILL) in [(k, c) for k, _, c in w.signals]


def test_stop_already_dead_is_success_and_silent() -> None:
    w = _Seams(timeline=[None], pgid=50)
    assert w.run(ProcIdent(50, 100)) is True
    assert w.signals == []


def test_stop_recycled_pid_mid_grace_reads_as_dead_no_sigkill() -> None:
    # TERM sent, then the pid comes back with a NEW starttime => treated dead.
    w = _Seams(timeline=[100, 100, 999], pgid=50)
    assert w.run(ProcIdent(50, 100), grace=1.0, interval=0.25) is True
    assert [c for _, _, c in w.signals] == [signal.SIGTERM]


def test_stop_group_leader_gets_killpg_nonleader_gets_kill() -> None:
    leader = _Seams(timeline=[100, 100, None], pgid=50)
    leader.run(ProcIdent(50, 100))
    assert leader.signals[0][0] == "killpg"

    nonleader = _Seams(timeline=[100, 100, None], pgid=999)   # pgid != pid
    nonleader.run(ProcIdent(50, 100))
    assert nonleader.signals[0][0] == "kill"                  # never blast a shared group


def test_stop_getpgid_process_lookup_error_means_dead() -> None:
    # Alive at the initial check AND at send()'s re-verify, so execution
    # genuinely reaches getpgid — which raises (pgid=None) => treated dead.
    w = _Seams(timeline=[100, 100, None], pgid=None)          # getpgid raises
    assert w.run(ProcIdent(50, 100)) is True
    assert w.signals == []


def test_stop_signal_raising_process_lookup_error_is_tolerated() -> None:
    # Process dies inside the verify->signal window: the killpg call itself
    # raises. stop_process must neither crash nor escalate to SIGKILL.
    w = _Seams(timeline=[100, 100, 100, None], pgid=50, raise_on_signal=True)
    assert w.run(ProcIdent(50, 100)) is True
    assert w.signals == [("killpg", 50, signal.SIGTERM)]      # one attempt, no KILL
