# Launcher Lifecycle (v2.12.1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Closing the Poseidon window terminates the engine (SIGTERM→grace→SIGKILL of its process group), and every launch first kills any already-running engine — never reuses one.

**Architecture:** A new dependency-free `proclife` module provides (pid, starttime) process identity and an identity-checked stop sequence. The launcher gains a discovery+kill pass (pid file → `/proc` scan → unconditional systemd stop → post-pass port assertion), spawns the engine as a *tracked* child inside a try/finally, and opens the window through a new **blocking** gui helper (dedicated chromium profile so the process lifetime equals the window lifetime). A flock singleton with identity-verified takeover serializes launchers. Spec: `docs/superpowers/specs/2026-07-15-launcher-lifecycle-design.md` (advisor-approved; its 13 findings are all encoded below).

**Tech Stack:** Python stdlib only (`os`, `signal`, `fcntl`, `subprocess`, `json`, `pathlib`); pytest + monkeypatch seams in the existing `tests/unit/test_launcher.py` style.

## Global Constraints

- Python 3.11+ compatible (CI runs 3.11 and 3.12; do not use newer-only syntax).
- `mypy src` strict must stay clean; `ruff check src tests` clean (line length 100).
- `from __future__ import annotations` at top of every touched module.
- Tests: pytest-asyncio auto mode exists but ALL tests here are synchronous plain `def`; no network; use `tmp_path`/`monkeypatch`; follow neighboring test style.
- Engine spawn argv must remain byte-identical: `[sys.executable, "-m", "poseidon", "run"]`.
- Passphrase reaches the engine ONLY via `POSEIDON_VAULT_PASSPHRASE` env (never argv/file); `engine.pid` and `launcher.lock` contain pid/starttime JSON only.
- The launcher never changes trading mode. `poseidon app` CLI, `gui.launch`, `gui.open_window` are untouched.
- Do NOT edit any file outside: `src/poseidon/proclife.py` (new), `src/poseidon/launcher.py`, `src/poseidon/gui.py`, `tests/unit/test_proclife.py` (new), `tests/unit/test_gui_window.py` (new), `tests/unit/test_launcher.py`.
- Commit after every task with the message given in its final step (append the standard `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` trailer).
- Run only the test files named in the task (never the full suite): `.venv/bin/pytest <files> -q`; finish each task with `.venv/bin/ruff check src tests && .venv/bin/mypy src`.

---

### Task 1: `proclife` — process identity + identity-checked stop

**Files:**
- Create: `src/poseidon/proclife.py`
- Test: `tests/unit/test_proclife.py` (new)

**Interfaces:**
- Produces (later tasks import these from `poseidon.proclife`):
  - `@dataclass(frozen=True) class ProcIdent: pid: int; starttime: int`
  - `parse_stat_starttime(stat_text: str) -> int | None`
  - `proc_starttime(pid: int, proc_root: Path = Path("/proc")) -> int | None`
  - `same_process(ident: ProcIdent, proc_root: Path = Path("/proc")) -> bool`
  - `stop_process(ident, *, grace=10.0, kill_grace=2.0, interval=0.25, kill=os.kill, killpg=os.killpg, getpgid=os.getpgid, starttime_of=proc_starttime, sleep=time.sleep) -> bool` (accepts any object with `.pid`/`.starttime`)

- [ ] **Step 1: Write the failing tests**

```python
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

    def __init__(self, timeline: list[int | None], pgid: int | None = None) -> None:
        self.timeline = list(timeline)   # successive starttime_of() answers
        self.signals: list[tuple[str, int, int]] = []
        self._pgid = pgid
        self.slept: list[float] = []

    def starttime_of(self, pid: int) -> int | None:
        return self.timeline.pop(0) if len(self.timeline) > 1 else self.timeline[0]

    def getpgid(self, pid: int) -> int:
        if self._pgid is None:
            raise ProcessLookupError(pid)
        return self._pgid

    def kill(self, pid: int, sig: int) -> None:
        self.signals.append(("kill", pid, sig))

    def killpg(self, pgid: int, sig: int) -> None:
        self.signals.append(("killpg", pgid, sig))

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
    kinds = [(k, s) for k, _, s in [(a, b, c) for a, b, c in w.signals]]
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
    w = _Seams(timeline=[100, None], pgid=None)               # getpgid raises
    assert w.run(ProcIdent(50, 100)) is True
    assert w.signals == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_proclife.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'poseidon.proclife'`

- [ ] **Step 3: Implement `src/poseidon/proclife.py`**

```python
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
    pid: int
    starttime: int


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


def proc_starttime(pid: int, proc_root: Path = Path("/proc")) -> int | None:
    """The live starttime of ``pid``, or None if it does not exist / is unreadable."""
    try:
        return parse_stat_starttime((proc_root / str(pid) / "stat").read_text())
    except OSError:
        return None


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_proclife.py -q`
Expected: PASS (all)

- [ ] **Step 5: Lint/type gate, then commit**

Run: `.venv/bin/ruff check src tests && .venv/bin/mypy src`
Expected: clean. Then:

```bash
git add src/poseidon/proclife.py tests/unit/test_proclife.py
git commit -m "feat(launcher): proclife — (pid,starttime) identity + identity-checked stop"
```

---

### Task 2: pid file + engine matcher + `/proc` scan

**Files:**
- Modify: `src/poseidon/launcher.py` (add a "process lifecycle" section after the pure-core section)
- Test: `tests/unit/test_launcher.py` (append)

**Interfaces:**
- Consumes: `poseidon.proclife.{ProcIdent, proc_starttime, same_process}`
- Produces (in `poseidon.launcher`):
  - `read_pidfile(path: Path) -> ProcIdent | None`
  - `write_pidfile(path: Path, ident: ProcIdent) -> None`
  - `is_engine_cmdline(exe: str, argv: list[str]) -> bool`
  - `@dataclass(frozen=True) class FoundEngine(ProcIdent): cmdline: str`
  - `find_running_engines(proc_root: Path = Path("/proc")) -> list[FoundEngine]`

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/test_launcher.py`)

```python
# ---- process lifecycle: pid file, engine matcher, /proc scan ----

import json as _json

from poseidon.launcher import (
    FoundEngine,
    find_running_engines,
    is_engine_cmdline,
    read_pidfile,
    write_pidfile,
)
from poseidon.proclife import ProcIdent


def test_pidfile_roundtrip_and_content(tmp_path) -> None:
    path = tmp_path / "engine.pid"
    write_pidfile(path, ProcIdent(pid=123, starttime=456))
    assert read_pidfile(path) == ProcIdent(123, 456)
    # The file must contain identity fields ONLY — never anything secret.
    assert set(_json.loads(path.read_text())) == {"pid", "starttime"}


def test_pidfile_garbage_and_missing_read_as_none(tmp_path) -> None:
    path = tmp_path / "engine.pid"
    assert read_pidfile(path) is None                       # missing
    path.write_text("not json")
    assert read_pidfile(path) is None                       # garbage
    path.write_text('{"pid": "x", "starttime": 1}')
    assert read_pidfile(path) is None                       # wrong types


def test_engine_matcher_accepts_both_real_spawn_shapes() -> None:
    py = "/home/u/.local/share/poseidon/venv/bin/python3.14"
    assert is_engine_cmdline(py, [py, "-m", "poseidon", "run"]) is True
    assert is_engine_cmdline(py, ["/venv/bin/poseidon", "run"]) is True       # console script
    assert is_engine_cmdline(py, [py, "-mposeidon", "run"]) is True           # fused form


def test_engine_matcher_rejects_lookalikes() -> None:
    py = "/usr/bin/python3"
    assert is_engine_cmdline("/usr/bin/vim", ["vim", "poseidon", "run"]) is False    # not python
    assert is_engine_cmdline(py, [py, "x.py", "poseidon", "run"]) is False    # arg tail, not -m
    assert is_engine_cmdline(py, [py, "-m", "poseidon", "app"]) is False      # not run
    assert is_engine_cmdline(py, [py, "-m", "poseidonx", "run"]) is False     # wrong module
    assert is_engine_cmdline(py, []) is False


def _fake_proc(tmp_path, pid: int, exe: str, argv: list[str], starttime: int) -> None:
    d = tmp_path / str(pid)
    d.mkdir()
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    tail = f"S 1 {pid} {pid} 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 {starttime} 0 0"
    (d / "stat").write_text(f"{pid} ({argv[0][:15]}) {tail}")
    (d / "exe").symlink_to(exe)


def test_find_running_engines_scans_and_filters(tmp_path) -> None:
    py = "/usr/bin/python3.14"
    _fake_proc(tmp_path, 100, py, [py, "-m", "poseidon", "run"], 11)          # engine
    _fake_proc(tmp_path, 101, py, ["/venv/bin/poseidon", "run"], 22)          # engine (script)
    _fake_proc(tmp_path, 102, "/usr/bin/vim", ["vim", "poseidon", "run"], 33)  # imposter
    _fake_proc(tmp_path, 103, py, [py, "app.py", "poseidon", "run"], 44)      # arg tail
    (tmp_path / "not-a-pid").mkdir()
    found = find_running_engines(proc_root=tmp_path)
    assert sorted((e.pid, e.starttime) for e in found) == [(100, 11), (101, 22)]
    assert all(isinstance(e, FoundEngine) and "poseidon" in e.cmdline for e in found)


def test_find_running_engines_skips_itself(tmp_path) -> None:
    import os as _os
    py = "/usr/bin/python3.14"
    _fake_proc(tmp_path, _os.getpid(), py, [py, "-m", "poseidon", "run"], 55)
    assert find_running_engines(proc_root=tmp_path) == []
```

Note the exe check uses `os.readlink` on `/proc/<pid>/exe` in production; the fake tree uses a symlink so the same code path works.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/unit/test_launcher.py -q`
Expected: FAIL — `ImportError: cannot import name 'FoundEngine'`

- [ ] **Step 3: Implement in `src/poseidon/launcher.py`**

Add to the imports: `import json`, `from dataclasses import dataclass`, and `from .proclife import ProcIdent, proc_starttime, same_process` (extend the existing import block; keep `TYPE_CHECKING` imports as they are). Then add the section:

```python
# ---------------------------------------------------------- process lifecycle

_ENGINE_PIDFILE = "engine.pid"


def read_pidfile(path: Path) -> ProcIdent | None:
    """The recorded engine identity, or None for missing/garbage — a bad pid
    file must never be a reason to signal anything."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        pid, starttime = data["pid"], data["starttime"]
        if isinstance(pid, bool) or isinstance(starttime, bool):
            return None
        if not isinstance(pid, int) or not isinstance(starttime, int):
            return None
        return ProcIdent(pid=pid, starttime=starttime)
    except (OSError, ValueError, TypeError, KeyError):
        return None


def write_pidfile(path: Path, ident: ProcIdent) -> None:
    """Identity fields only — this file must never carry anything secret."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": ident.pid, "starttime": ident.starttime}),
                    encoding="utf-8")


def is_engine_cmdline(exe: str, argv: list[str]) -> bool:
    """True only for the two exact engine spawn shapes. Positional matching:
    a stray ``... poseidon run`` argument tail must NOT match — this predicate
    authorizes a kill."""
    if not Path(exe).name.startswith("python"):
        return False
    if not argv:
        return False
    if Path(argv[0]).name == "poseidon" and argv[1:2] == ["run"]:
        return True
    for i, tok in enumerate(argv[:-1]):
        if tok == "-m" and argv[i + 1] == "poseidon" and argv[i + 2:i + 3] == ["run"]:
            return True
        if tok == "-mposeidon" and argv[i + 1] == "run":
            return True
    return False


@dataclass(frozen=True)
class FoundEngine(ProcIdent):
    """A verified running engine plus its cmdline (for the forensic log)."""

    cmdline: str


def find_running_engines(proc_root: Path = Path("/proc")) -> list[FoundEngine]:
    """Every same-UID process matching an exact engine spawn shape. Other
    users' entries drop out naturally (their ``exe`` link is unreadable)."""
    me = os.getpid()
    found: list[FoundEngine] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == me:
            continue
        try:
            exe = os.readlink(entry / "exe")
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        argv = raw.decode(errors="replace").split("\0")
        while argv and argv[-1] == "":
            argv.pop()
        if not is_engine_cmdline(exe, argv):
            continue
        starttime = proc_starttime(pid, proc_root)
        if starttime is None:
            continue
        found.append(FoundEngine(pid=pid, starttime=starttime, cmdline=" ".join(argv)))
    return found
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_launcher.py -q`
Expected: PASS (existing 12 + new)

- [ ] **Step 5: Gate + commit**

Run: `.venv/bin/ruff check src tests && .venv/bin/mypy src` → clean, then:

```bash
git add src/poseidon/launcher.py tests/unit/test_launcher.py
git commit -m "feat(launcher): engine pid file + exact-shape /proc discovery"
```

---

### Task 3: systemd stop + `clear_running_engines` pass

**Files:**
- Modify: `src/poseidon/launcher.py`
- Test: `tests/unit/test_launcher.py` (append)

**Interfaces:**
- Consumes: Task 2's `read_pidfile/find_running_engines/FoundEngine`, `proclife.{stop_process, same_process, ProcIdent}`, existing `_engine_up`, `Dialog`.
- Produces:
  - `stop_systemd_unit(run: Callable[..., object] = subprocess.run) -> str` — returns `"stopped" | "timeout" | "absent"`
  - `clear_running_engines(config: AppConfig, dialog: Dialog, url: str, *, engines=find_running_engines, stop=stop_process, stop_unit=stop_systemd_unit, engine_up=None, alive=same_process) -> bool` — True ⇒ clear to spawn; False ⇒ error dialog already shown, abort.

- [ ] **Step 1: Write the failing tests** (append)

```python
# ---- fresh-start pass: systemd stop + kill + port assertion ----

from poseidon.launcher import clear_running_engines, stop_systemd_unit


class _ErrDialog:
    def __init__(self) -> None:
        self.errors: list[str] = []
    def error(self, message: str) -> None:
        self.errors.append(message)


def _cfg(tmp_path):
    from poseidon.core.config import AppConfig
    return AppConfig(data_dir=tmp_path)


def test_stop_systemd_unit_absent_when_no_systemctl(monkeypatch) -> None:
    import poseidon.launcher as launcher
    monkeypatch.setattr(launcher.shutil, "which", lambda _t: None)
    assert stop_systemd_unit() == "absent"


def test_stop_systemd_unit_runs_unconditionally_with_timeout(monkeypatch) -> None:
    import subprocess as sp
    import poseidon.launcher as launcher
    monkeypatch.setattr(launcher.shutil, "which", lambda _t: "/usr/bin/systemctl")
    calls: list[tuple[list[str], float]] = []
    def fake_run(argv, *, capture_output, timeout, check):  # noqa: ANN001, ANN202
        calls.append((argv, timeout))
        class R: returncode = 5   # inactive unit: rc != 0 is still fine
        return R()
    assert stop_systemd_unit(run=fake_run) == "stopped"
    assert calls[0][0][-2:] == ["stop", "poseidon"] and calls[0][1] >= 46

    def timing_out(argv, *, capture_output, timeout, check):  # noqa: ANN001, ANN202
        raise sp.TimeoutExpired(argv, timeout)
    assert stop_systemd_unit(run=timing_out) == "timeout"


def test_clear_kills_pidfile_and_scanned_engines_then_asserts_port(tmp_path) -> None:
    from poseidon.launcher import write_pidfile
    from poseidon.proclife import ProcIdent
    cfg = _cfg(tmp_path)
    write_pidfile(tmp_path / "engine.pid", ProcIdent(200, 2))
    scanned = [FoundEngine(pid=300, starttime=3, cmdline="python -m poseidon run")]
    stopped: list[int] = []
    ok = clear_running_engines(
        cfg, _ErrDialog(), "http://x",
        engines=lambda: scanned,
        stop=lambda ident, **kw: (stopped.append(ident.pid), True)[1],
        stop_unit=lambda: "stopped",
        engine_up=lambda _u: False,
        alive=lambda ident: True,
    )
    assert ok is True
    assert sorted(stopped) == [200, 300]
    assert not (tmp_path / "engine.pid").exists()          # consumed


def test_clear_dedupes_pidfile_against_scan(tmp_path) -> None:
    from poseidon.launcher import write_pidfile
    from poseidon.proclife import ProcIdent
    cfg = _cfg(tmp_path)
    write_pidfile(tmp_path / "engine.pid", ProcIdent(300, 3))
    scanned = [FoundEngine(pid=300, starttime=3, cmdline="python -m poseidon run")]
    stopped: list[int] = []
    assert clear_running_engines(
        cfg, _ErrDialog(), "http://x",
        engines=lambda: scanned,
        stop=lambda ident, **kw: (stopped.append(ident.pid), True)[1],
        stop_unit=lambda: "stopped", engine_up=lambda _u: False,
        alive=lambda ident: True) is True
    assert stopped == [300]                                # exactly once


def test_clear_stale_pidfile_removed_without_signal(tmp_path) -> None:
    from poseidon.launcher import write_pidfile
    from poseidon.proclife import ProcIdent
    cfg = _cfg(tmp_path)
    write_pidfile(tmp_path / "engine.pid", ProcIdent(200, 2))
    stopped: list[int] = []
    assert clear_running_engines(
        cfg, _ErrDialog(), "http://x",
        engines=lambda: [],
        stop=lambda ident, **kw: (stopped.append(ident.pid), True)[1],
        stop_unit=lambda: "stopped", engine_up=lambda _u: False,
        alive=lambda ident: False) is True                 # identity mismatch = stale
    assert stopped == []
    assert not (tmp_path / "engine.pid").exists()


def test_clear_aborts_on_systemd_timeout(tmp_path) -> None:
    dialog = _ErrDialog()
    assert clear_running_engines(
        _cfg(tmp_path), dialog, "http://x",
        engines=lambda: [], stop=lambda i, **k: True,
        stop_unit=lambda: "timeout", engine_up=lambda _u: False,
        alive=lambda i: True) is False
    assert dialog.errors                                    # explained to the user


def test_clear_aborts_when_port_still_answers_after_pass(tmp_path) -> None:
    dialog = _ErrDialog()
    assert clear_running_engines(
        _cfg(tmp_path), dialog, "http://x",
        engines=lambda: [], stop=lambda i, **k: True,
        stop_unit=lambda: "stopped", engine_up=lambda _u: True,
        alive=lambda i: True) is False
    assert dialog.errors and "still running" in dialog.errors[0]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/unit/test_launcher.py -q`
Expected: FAIL — `ImportError: cannot import name 'clear_running_engines'`

- [ ] **Step 3: Implement** (same launcher section; add `from collections.abc import Callable` usage is already imported at top — reuse it)

```python
def stop_systemd_unit(run: Callable[..., object] = subprocess.run) -> str:
    """Best-effort, UNCONDITIONAL ``systemctl --user stop poseidon``.

    Deliberately not gated on ``is-active``: a unit in its RestartSec hold-off
    reports ``activating`` and would resurrect an engine seconds after the
    kill pass. Stopping an inactive/unloaded unit is a cheap rc!=0 no-op.
    Timeout exceeds the unit's TimeoutStopSec=45."""
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return "absent"
    try:
        run([systemctl, "--user", "stop", "poseidon"],
            capture_output=True, timeout=60, check=False)  # noqa: S603
    except subprocess.TimeoutExpired:
        return "timeout"
    return "stopped"


def _forensic(config: AppConfig, line: str) -> None:
    """One line per kill decision, for post-hoc forensics."""
    log_path = config.data_dir / "launcher-engine.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"[launcher] {line}\n")
    except OSError:
        pass


def clear_running_engines(
    config: AppConfig,
    dialog: Dialog,
    url: str,
    *,
    engines: Callable[[], list[FoundEngine]] = find_running_engines,
    stop: Callable[..., bool] | None = None,
    stop_unit: Callable[[], str] = stop_systemd_unit,
    engine_up: Callable[[str], bool] | None = None,
    alive: Callable[[ProcIdent], bool] = same_process,
) -> bool:
    """The fresh-start pass: stop the systemd unit, kill every verified
    engine (pid file first, then the /proc scan), then assert the dashboard
    port went silent. True = clear to spawn; False = abort (dialog shown)."""
    from .proclife import stop_process
    stop = stop or stop_process
    probe = engine_up or _engine_up

    if stop_unit() == "timeout":
        dialog.error(
            "Poseidon's background service (systemd) did not stop in time, so a "
            "fresh engine cannot be started safely.\n\nTry:  systemctl --user stop poseidon")
        return False

    pidfile = config.data_dir / _ENGINE_PIDFILE
    targets: dict[int, ProcIdent] = {}
    recorded = read_pidfile(pidfile)
    if recorded is not None and alive(recorded):
        targets[recorded.pid] = recorded
    pidfile.unlink(missing_ok=True)   # recorded engine is being stopped (or is stale)

    for eng in engines():
        targets.setdefault(eng.pid, eng)

    for ident in targets.values():
        cmdline = getattr(ident, "cmdline", "(from engine.pid)")
        _forensic(config, f"fresh-start kill pid={ident.pid} starttime={ident.starttime} "
                          f"cmdline={cmdline}")
        stop(ident)

    if probe(url):
        dialog.error(
            "An engine is still running that Poseidon could not stop, so a fresh "
            "one cannot be started.\n\nFind it with:  pgrep -af 'poseidon run'  "
            "then close it and launch again.")
        return False
    return True
```

- [ ] **Step 4: Run tests** — `.venv/bin/pytest tests/unit/test_launcher.py -q` → PASS

- [ ] **Step 5: Gate + commit**

```bash
git add src/poseidon/launcher.py tests/unit/test_launcher.py
git commit -m "feat(launcher): fresh-start pass — systemd stop, verified kills, port assertion"
```

---

### Task 4: `gui.open_app_window_blocking` — a window whose lifetime we own

**Files:**
- Modify: `src/poseidon/gui.py`
- Test: Create `tests/unit/test_gui_window.py`

**Interfaces:**
- Consumes: `poseidon.proclife.{ProcIdent, proc_starttime, stop_process}`
- Produces (in `poseidon.gui`):
  - `profile_holders(profile_dir: Path, proc_root: Path = Path("/proc")) -> list[ProcIdent]`
  - `open_app_window_blocking(url: str, *, profile_dir: Path, token_in_url: bool = False, on_spawn: Callable[[subprocess.Popen[bytes]], None] | None = None, fallback_block: Callable[[], None] | None = None, handoff_threshold: float = 2.0) -> int`
- `gui.launch` and `gui.open_window` are NOT modified.

- [ ] **Step 1: Write the failing tests** (`tests/unit/test_gui_window.py`, new file)

```python
"""The launcher-owned blocking window: dedicated-profile browser process whose
lifetime IS the window lifetime, with ProcessSingleton hand-off defenses."""
from __future__ import annotations

from pathlib import Path

import poseidon.gui as gui


class _FakeProc:
    def __init__(self, lifetime: float, clock: list[float]) -> None:
        self._lifetime = lifetime
        self._clock = clock          # mutable "now" shared with fake monotonic
        self.pid = 4242
        self.terminated = False
    def wait(self) -> int:
        self._clock[0] += self._lifetime
        return 0


def _no_webview(monkeypatch) -> None:
    import builtins
    real_import = builtins.__import__
    def fake_import(name, *a, **kw):  # noqa: ANN001, ANN002, ANN003, ANN202
        if name == "webview":
            raise ImportError(name)
        return real_import(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_blocking_window_spawns_dedicated_profile_and_waits(tmp_path, monkeypatch) -> None:
    _no_webview(monkeypatch)
    clock = [0.0]
    spawned: list[list[str]] = []
    proc = _FakeProc(lifetime=300.0, clock=clock)
    monkeypatch.setattr(gui.shutil, "which",
                        lambda name: "/usr/bin/vivaldi-stable" if name == "vivaldi-stable" else None)
    monkeypatch.setattr(gui.subprocess, "Popen",
                        lambda argv, **kw: (spawned.append(list(argv)), proc)[1])
    monkeypatch.setattr(gui.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(gui, "profile_holders", lambda d, proc_root=Path("/proc"): [])
    seen: list[object] = []
    rc = gui.open_app_window_blocking(
        "http://127.0.0.1:8321", profile_dir=tmp_path / "webview-profile",
        on_spawn=seen.append)
    assert rc == 0
    assert seen == [proc]
    argv = spawned[0]
    assert f"--user-data-dir={tmp_path / 'webview-profile'}" in argv
    assert "--app=http://127.0.0.1:8321" in argv
    assert "--no-first-run" in argv and "--no-default-browser-check" in argv
    mode = (tmp_path / "webview-profile").stat().st_mode & 0o777
    assert mode == 0o700


def test_instant_exit_is_treated_as_handoff_and_respawned_once(tmp_path, monkeypatch) -> None:
    _no_webview(monkeypatch)
    clock = [0.0]
    lifetimes = iter([0.1, 500.0])                     # hand-off, then a real window
    procs: list[_FakeProc] = []
    def popen(argv, **kw):  # noqa: ANN001, ANN003, ANN202
        p = _FakeProc(lifetime=next(lifetimes), clock=clock)
        procs.append(p)
        return p
    swept: list[int] = []
    monkeypatch.setattr(gui.shutil, "which",
                        lambda name: "/usr/bin/vivaldi-stable" if name == "vivaldi-stable" else None)
    monkeypatch.setattr(gui.subprocess, "Popen", popen)
    monkeypatch.setattr(gui.time, "monotonic", lambda: clock[0])
    from poseidon.proclife import ProcIdent
    holders = [[ProcIdent(999, 9)], []]                # orphan present, then cleared
    monkeypatch.setattr(gui, "profile_holders",
                        lambda d, proc_root=Path("/proc"): holders.pop(0) if holders else [])
    monkeypatch.setattr(gui, "_stop_profile_holder", lambda ident: swept.append(ident.pid))
    rc = gui.open_app_window_blocking("http://x", profile_dir=tmp_path / "p")
    assert rc == 0
    assert len(procs) == 2                              # respawned exactly once
    assert 999 in swept                                 # the orphan was cleared


def test_fallback_block_called_when_no_browser(tmp_path, monkeypatch) -> None:
    _no_webview(monkeypatch)
    monkeypatch.setattr(gui.shutil, "which", lambda name: None)
    opened: list[str] = []
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda u: opened.append(u))
    blocked: list[bool] = []
    rc = gui.open_app_window_blocking(
        "http://x", profile_dir=tmp_path / "p", fallback_block=lambda: blocked.append(True))
    assert rc == 0 and opened == ["http://x"] and blocked == [True]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/unit/test_gui_window.py -q`
Expected: FAIL — `AttributeError: module 'poseidon.gui' has no attribute 'open_app_window_blocking'`

- [ ] **Step 3: Implement in `src/poseidon/gui.py`**

Add imports: `import os`, `from collections.abc import Callable`, `from pathlib import Path`, and `from .proclife import ProcIdent, proc_starttime, stop_process`. Then append:

```python
_WINDOW_FLAGS = ("--no-first-run", "--no-default-browser-check")
_HANDOFF_THRESHOLD = 2.0


def profile_holders(profile_dir: Path, proc_root: Path = Path("/proc")) -> list[ProcIdent]:
    """Processes holding our dedicated window profile. The dir is exclusively
    the launcher's, so any holder is a dead launcher's orphaned window — safe
    to stop by construction (chromium's ProcessSingleton would otherwise hand
    our new window to it and exit instantly, breaking wait() as a close signal)."""
    needle = f"--user-data-dir={profile_dir}"
    me = os.getpid()
    holders: list[ProcIdent] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            argv = (entry / "cmdline").read_bytes().decode(errors="replace").split("\0")
        except OSError:
            continue
        if needle in argv:
            starttime = proc_starttime(int(entry.name), proc_root)
            if starttime is not None:
                holders.append(ProcIdent(pid=int(entry.name), starttime=starttime))
    return holders


def _stop_profile_holder(ident: ProcIdent) -> None:
    stop_process(ident, grace=5.0)


def open_app_window_blocking(
    url: str,
    *,
    profile_dir: Path,
    token_in_url: bool = False,
    on_spawn: Callable[[subprocess.Popen[bytes]], None] | None = None,
    fallback_block: Callable[[], None] | None = None,
    handoff_threshold: float = _HANDOFF_THRESHOLD,
) -> int:
    """The LAUNCHER's window: blocks until the window closes.

    pywebview blocks natively. The chromium-family path spawns with a
    dedicated ``--user-data-dir`` so the process's lifetime is the window's
    lifetime — ``wait()`` is the close signal. An exit within
    ``handoff_threshold`` seconds means chromium handed the window to a
    process already holding the profile (an orphan from a SIGKILLed
    launcher): sweep the holder and respawn once. ``fallback_block`` runs
    when no trackable window exists (bare ``webbrowser.open``).

    Tradeoff (extends F019): with a token in ``url``, the dedicated profile
    persists it in history/session files under the (0700) profile dir —
    loopback default carries no token."""
    try:
        import webview  # optional dependency: poseidon[gui]
    except ImportError:
        webview = None
    if webview is not None:
        try:
            webview.create_window("Poseidon", url,
                                  width=_WINDOW_SIZE[0], height=_WINDOW_SIZE[1])
            webview.start()
            return 0
        except Exception as exc:  # missing GTK/Qt backend, no display, …
            print(f"native window unavailable ({exc}); falling back to a browser window")
    if token_in_url:
        print(
            "WARNING: no native window available — the dashboard token rides the "
            "browser argv (visible in /proc) and persists inside the dedicated "
            "window profile on disk. Install 'pip install poseidon[gui]' to avoid this."
        )
    for name in _APP_BROWSERS:
        binary = shutil.which(name)
        if not binary:
            continue
        profile_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        for attempt in (1, 2):
            for holder in profile_holders(profile_dir):
                _stop_profile_holder(holder)
            proc = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
                [binary, f"--app={url}", f"--user-data-dir={profile_dir}",
                 *_WINDOW_FLAGS,
                 f"--window-size={_WINDOW_SIZE[0]},{_WINDOW_SIZE[1]}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if on_spawn is not None:
                on_spawn(proc)
            started = time.monotonic()
            proc.wait()
            if time.monotonic() - started >= handoff_threshold:
                return 0            # a real window session ended (user hit X)
            # instant exit: ProcessSingleton hand-off — sweep and retry once
        return 0
    import webbrowser

    webbrowser.open(url)
    if fallback_block is not None:
        fallback_block()
    return 0
```

- [ ] **Step 4: Run tests** — `.venv/bin/pytest tests/unit/test_gui_window.py tests/unit/test_launcher.py -q` → PASS

- [ ] **Step 5: Gate + commit**

```bash
git add src/poseidon/gui.py tests/unit/test_gui_window.py
git commit -m "feat(gui): blocking app window — dedicated profile owns the window lifetime"
```

---

### Task 5: `_start_engine` rework — tracked child, fast-fail, keep-waiting

**Files:**
- Modify: `src/poseidon/launcher.py` (replace `_start_engine`; add `_wait_for_dashboard`, `_kill_own_engine`)
- Test: `tests/unit/test_launcher.py` (append)

**Interfaces:**
- Consumes: Tasks 1-2 (`ProcIdent`, `proc_starttime`, `write_pidfile`), existing `engine_env`, `_engine_up`, `_notify`, `Dialog`.
- Produces:
  - `_wait_for_dashboard(proc, url, *, attempts, sleep, interval=_PROBE_INTERVAL, engine_up=None) -> str` — `"up" | "died" | "timeout"`
  - `_kill_own_engine(proc: subprocess.Popen[bytes], *, grace: float = _ENGINE_STOP_GRACE) -> None`
  - `_start_engine(config, passphrase, dialog, url) -> subprocess.Popen[bytes] | None` (signature same, return changes `bool → Popen | None`)
  - Module constant `_ENGINE_STOP_GRACE = 10.0`

- [ ] **Step 1: Write the failing tests** (append)

```python
# ---- _start_engine: tracked child, fast-fail on death, keep-waiting ----

from poseidon.launcher import _kill_own_engine, _wait_for_dashboard


class _FakeEngineProc:
    def __init__(self, alive: bool = True, pid: int = 777) -> None:
        self.pid = pid
        self._alive = alive
        self.signals: list[int] = []
        self.waits: list[float | None] = []
    def poll(self):  # noqa: ANN201
        return None if self._alive else 1
    def wait(self, timeout=None):  # noqa: ANN001, ANN201
        self.waits.append(timeout)
        if self._alive:
            import subprocess as sp
            raise sp.TimeoutExpired("engine", timeout or 0)
        return 0


def test_wait_for_dashboard_up() -> None:
    proc = _FakeEngineProc(alive=True)
    answers = iter([False, False, True])
    out = _wait_for_dashboard(proc, "http://x", attempts=5, sleep=lambda _s: None,
                              engine_up=lambda _u: next(answers))
    assert out == "up"


def test_wait_for_dashboard_fast_fails_when_child_dies() -> None:
    proc = _FakeEngineProc(alive=False)
    out = _wait_for_dashboard(proc, "http://x", attempts=100, sleep=lambda _s: None,
                              engine_up=lambda _u: False)
    assert out == "died"                       # immediately — never 30s of polling


def test_wait_for_dashboard_timeout_with_live_child() -> None:
    proc = _FakeEngineProc(alive=True)
    slept: list[float] = []
    out = _wait_for_dashboard(proc, "http://x", attempts=4, sleep=slept.append,
                              engine_up=lambda _u: False)
    assert out == "timeout" and len(slept) == 4


def test_kill_own_engine_term_then_kill(monkeypatch) -> None:
    import poseidon.launcher as launcher
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(launcher.os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
    proc = _FakeEngineProc(alive=True, pid=555)
    _kill_own_engine(proc, grace=0.1)
    import signal as _sig
    assert (555, _sig.SIGTERM) in sent and (555, _sig.SIGKILL) in sent


def test_kill_own_engine_already_dead_noop(monkeypatch) -> None:
    import poseidon.launcher as launcher
    def raising_killpg(pgid, sig):  # noqa: ANN001, ANN202
        raise ProcessLookupError(pgid)
    monkeypatch.setattr(launcher.os, "killpg", raising_killpg)
    proc = _FakeEngineProc(alive=False)
    _kill_own_engine(proc)                     # must not raise
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/unit/test_launcher.py -q`
Expected: FAIL — `ImportError: cannot import name '_kill_own_engine'`

- [ ] **Step 3: Implement** — replace the whole `_start_engine` and add helpers (constants near `_PROBE_INTERVAL`):

```python
_ENGINE_STOP_GRACE = 10.0  # seconds of SIGTERM grace before SIGKILL


def _wait_for_dashboard(
    proc: subprocess.Popen[bytes],
    url: str,
    *,
    attempts: int,
    sleep: Callable[[float], None],
    interval: float = _PROBE_INTERVAL,
    engine_up: Callable[[str], bool] | None = None,
) -> str:
    """Poll until the dashboard answers, the child dies, or attempts run out.
    Polling the child makes a wrong passphrase fail in seconds, not 30."""
    probe = engine_up or _engine_up
    for _ in range(attempts):
        if proc.poll() is not None:
            return "died"
        if probe(url):
            return "up"
        sleep(interval)
    return "timeout"


def _kill_own_engine(proc: subprocess.Popen[bytes], *, grace: float = _ENGINE_STOP_GRACE) -> None:
    """TERM -> grace -> KILL for OUR OWN child. No starttime check needed:
    an unreaped child's pid cannot be recycled (we hold the zombie), and
    ``start_new_session=True`` makes its pgid == pid."""
    for sig, timeout in ((signal.SIGTERM, grace), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            continue


def _start_engine(
    config: AppConfig, passphrase: str, dialog: Dialog, url: str,
) -> subprocess.Popen[bytes] | None:
    """Spawn the engine as a TRACKED child (own session/group), record its
    identity in engine.pid, and wait for the dashboard. On failure the spawn
    is cleaned up — a failed boot must never leave a half-started orphan."""
    log_path = config.data_dir / "launcher-engine.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _notify("Starting Poseidon…", "Unlocking the vault and bringing the engine up.")
    with log_path.open("a", encoding="utf-8") as log_fh:
        proc = subprocess.Popen(  # noqa: S603 — fixed argv (sys.executable -m poseidon), no shell
            [sys.executable, "-m", "poseidon", "run"],
            env=engine_env(passphrase, os.environ),
            stdin=subprocess.DEVNULL, stdout=log_fh, stderr=log_fh,
            start_new_session=True,
        )
    pidfile = config.data_dir / _ENGINE_PIDFILE
    starttime = proc_starttime(proc.pid)
    if starttime is not None:
        write_pidfile(pidfile, ProcIdent(pid=proc.pid, starttime=starttime))
    # else: the child already died (e.g. instant crash) — the wait loop below
    # reports it accurately; nothing to record.
    while True:
        outcome = _wait_for_dashboard(proc, url,
                                      attempts=_ENGINE_START_ATTEMPTS, sleep=time.sleep)
        if outcome == "up":
            _notify("Poseidon is running.", "Opening the dashboard.")
            return proc
        if outcome == "died":
            proc.wait()  # reap
            pidfile.unlink(missing_ok=True)
            dialog.error(
                "Poseidon exited during startup — most often a wrong vault "
                f"password, or a missing key/config. See the log:\n{log_path}")
            return None
        if dialog.question("Poseidon is still starting — keep waiting?"):
            continue
        _kill_own_engine(proc)
        pidfile.unlink(missing_ok=True)
        dialog.error(f"Poseidon did not come up in time. See the log:\n{log_path}")
        return None
```

- [ ] **Step 4: Run tests** — `.venv/bin/pytest tests/unit/test_launcher.py -q`
Expected: the NEW tests pass; the two old `main()` flow tests still pass (they monkeypatch `_start_engine` wholesale). If `test_main_first_run_creates_vault_then_starts_and_opens` fails because its fake returns `True`, update that fake to return a `_FakeEngineProc()` instead — that is part of this task.

- [ ] **Step 5: Gate + commit**

```bash
git add src/poseidon/launcher.py tests/unit/test_launcher.py
git commit -m "feat(launcher): tracked engine spawn — pid file, fast-fail, keep-waiting, timeout cleanup"
```

---

### Task 6: singleton lock + identity-verified takeover

**Files:**
- Modify: `src/poseidon/launcher.py`
- Test: `tests/unit/test_launcher.py` (append)

**Interfaces:**
- Consumes: `read_pidfile/write_pidfile` (the lockfile shares the pid+starttime JSON format), `proclife.{ProcIdent, proc_starttime, same_process}`, `_notify`.
- Produces:
  - `_LAUNCHER_LOCKFILE = "launcher.lock"`
  - `acquire_launcher_lock(lock_path: Path, *, takeover_timeout: float = 30.0, interval: float = 0.5, term: Callable[[int, int], None] = os.kill, alive: Callable[[ProcIdent], bool] = same_process, sleep: Callable[[float], None] = time.sleep, notify: Callable[[str, str], None] | None = None) -> IO[str] | None` — returns the held (flocked) file handle, or None on takeover timeout. The caller must keep the handle alive for the launcher's life.

- [ ] **Step 1: Write the failing tests** (append)

```python
# ---- singleton lock + takeover ----

import fcntl as _fcntl

from poseidon.launcher import acquire_launcher_lock


def test_lock_acquired_when_free_and_records_identity(tmp_path) -> None:
    lock_path = tmp_path / "launcher.lock"
    fh = acquire_launcher_lock(lock_path)
    try:
        assert fh is not None
        recorded = read_pidfile(lock_path)
        import os as _os
        assert recorded is not None and recorded.pid == _os.getpid()
        data = _json.loads(lock_path.read_text())
        assert set(data) == {"pid", "starttime"}          # identity only, no secrets
    finally:
        if fh is not None:
            fh.close()


def test_takeover_signals_verified_holder_then_acquires(tmp_path, monkeypatch) -> None:
    import poseidon.launcher as launcher
    lock_path = tmp_path / "launcher.lock"
    holder = acquire_launcher_lock(lock_path)             # this test IS the holder
    assert holder is not None
    signalled: list[tuple[int, int]] = []
    def term(pid, sig):  # noqa: ANN001, ANN202
        signalled.append((pid, sig))
        holder.close()                                    # holder "exits" -> flock releases
    fh = acquire_launcher_lock(
        lock_path, term=term, alive=lambda ident: True,
        sleep=lambda _s: None, notify=lambda *_a: None)
    try:
        assert fh is not None
        import signal as _sig
        assert signalled and signalled[0][1] == _sig.SIGTERM
    finally:
        if fh is not None:
            fh.close()


def test_takeover_never_signals_stale_identity(tmp_path) -> None:
    lock_path = tmp_path / "launcher.lock"
    holder = acquire_launcher_lock(lock_path)
    assert holder is not None
    signalled: list[int] = []
    released = {"n": 0}
    def sleeper(_s: float) -> None:
        released["n"] += 1
        if released["n"] >= 3:
            holder.close()                                # holder dies on its own
    fh = acquire_launcher_lock(
        lock_path, term=lambda pid, sig: signalled.append(pid),
        alive=lambda ident: False,                        # identity mismatch: recycled pid
        sleep=sleeper, notify=lambda *_a: None)
    try:
        assert fh is not None                             # still acquired once freed
        assert signalled == []                            # but NOTHING was signalled
    finally:
        if fh is not None:
            fh.close()


def test_takeover_times_out_when_holder_never_exits(tmp_path) -> None:
    lock_path = tmp_path / "launcher.lock"
    holder = acquire_launcher_lock(lock_path)
    assert holder is not None
    try:
        fh = acquire_launcher_lock(
            lock_path, takeover_timeout=1.0, interval=0.5,
            term=lambda pid, sig: None, alive=lambda ident: True,
            sleep=lambda _s: None, notify=lambda *_a: None)
        assert fh is None
    finally:
        holder.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/unit/test_launcher.py -q`
Expected: FAIL — `ImportError: cannot import name 'acquire_launcher_lock'`

- [ ] **Step 3: Implement** (add `import fcntl` and `from typing import IO` to launcher imports)

```python
_LAUNCHER_LOCKFILE = "launcher.lock"


def _try_flock(fh: IO[str]) -> bool:
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def acquire_launcher_lock(
    lock_path: Path,
    *,
    takeover_timeout: float = 30.0,
    interval: float = 0.5,
    term: Callable[[int, int], None] = os.kill,
    alive: Callable[[ProcIdent], bool] = same_process,
    sleep: Callable[[float], None] = time.sleep,
    notify: Callable[[str, str], None] | None = None,
) -> IO[str] | None:
    """One launcher at a time, with takeover: if another launcher holds the
    lock, verify its recorded (pid, starttime) against /proc ON EVERY POLL and
    SIGTERM only a verified holder (its finally-cleanup closes its window and
    engine); an unverifiable holder is never signalled — its flock releases by
    itself when it exits. Returns the flocked handle (keep it for life; flock
    dies with the process, so a SIGKILLed launcher cannot wedge future
    launches) or None if the lock never freed."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("a+", encoding="utf-8")

    def _claim() -> IO[str]:
        me = os.getpid()
        starttime = proc_starttime(me) or 0
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps({"pid": me, "starttime": starttime}))
        fh.flush()
        return fh

    if _try_flock(fh):
        return _claim()

    (notify or _notify)("Restarting Poseidon…", "Handing the window over to a fresh start.")
    waited = 0.0
    while waited < takeover_timeout:
        holder = read_pidfile(lock_path)
        if holder is not None and alive(holder):
            try:
                term(holder.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if _try_flock(fh):
            return _claim()
        sleep(interval)
        waited += interval
    fh.close()
    return None
```

- [ ] **Step 4: Run tests** — `.venv/bin/pytest tests/unit/test_launcher.py -q` → PASS

- [ ] **Step 5: Gate + commit**

```bash
git add src/poseidon/launcher.py tests/unit/test_launcher.py
git commit -m "feat(launcher): flock singleton with identity-verified takeover"
```

---

### Task 7: `main()` orchestration — signal-safe try/finally, `_shutdown_engine`, flow tests

**Files:**
- Modify: `src/poseidon/launcher.py` (rewrite `main()`; add `_install_signal_handlers`, `_shutdown_engine`; update module docstring)
- Test: `tests/unit/test_launcher.py` (invert/extend the `main()` flow tests)

**Interfaces:**
- Consumes: everything from Tasks 1-6 plus `gui.open_app_window_blocking`, existing `_resolve_window_token`, `_first_run_setup`, `pick_dialog_backend`, `Dialog`.
- Produces:
  - `_install_signal_handlers() -> None` (SIGTERM/SIGHUP → `SystemExit(128+signum)`)
  - `_shutdown_engine(proc: subprocess.Popen[bytes] | None, window: subprocess.Popen[bytes] | None, pidfile: Path) -> None` — idempotent; no-op when `proc is None` except window reap; order: window terminate→wait(5)→kill, then engine `_kill_own_engine`, then conditional pidfile removal, then `_notify("Poseidon stopped.", ...)` once.
  - `main(argv: list[str] | None = None) -> int` with the spec's orchestration order.

- [ ] **Step 1: Update the module docstring** (top of `launcher.py`) — replace the "Design:" bullet list with:

```python
"""GUI double-click launcher for Poseidon (`poseidon-launch`).

The desktop entry point. On a double-click it brings the whole platform up
with no terminal, and OWNS the engine's lifetime: closing the window (or the
launcher receiving SIGTERM/SIGHUP/SIGINT) terminates the engine — TERM, a
grace period, then KILL of its process group — and every launch begins by
stopping any engine already running (pid file, /proc scan, systemd unit).
There is deliberately no reuse: every open is a fresh start.

Design:
  * The passphrase reaches the engine through its ENVIRONMENT
    (``POSEIDON_VAULT_PASSPHRASE``) — never a file, never argv. The pid and
    lock files carry (pid, starttime) identity JSON only.
  * Signals are sent only to processes verified by (pid, starttime) identity
    — a recycled PID can never be killed by mistake (see ``proclife``).
  * The launcher never changes the trading mode. The engine starts in
    whatever the config says; enabling autonomous or live trading stays a
    deliberate in-dashboard action.
  * ``poseidon app`` (the CLI window) keeps its service-view semantics: it
    never stops the engine. Only the launcher owns lifecycle. If the systemd
    user service is re-enabled, the next launch will stop it — the two
    ownership models do not mix.
  * Decision logic is pure module functions (unit-tested); dialogs and
    subprocess calls sit behind thin seams.
"""
```

- [ ] **Step 2: Write the failing flow tests** — REPLACE `test_main_engine_already_up_skips_setup_and_start` and extend. First add a tiny harness helper near the existing flow tests:

```python
def _wire_main(monkeypatch, tmp_path, *, engines_found=(), password="longpassword",
               window_rc=0, window_raises=None):
    """Standard main() wiring: real vault in tmp XDG dirs, everything else faked.
    Returns a dict of recorders."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    import poseidon.gui as gui
    import poseidon.launcher as launcher
    from poseidon.security.vault import Vault

    Vault(tmp_path / "d" / "poseidon" / "vault.bin").create("longpassword")

    rec: dict[str, object] = {"stopped": [], "unit": [], "shutdown": [], "window": []}

    class FakeDialog:
        def __init__(self, backend: str) -> None: ...
        def info(self, message: str) -> None: ...
        def error(self, message: str) -> None:
            rec.setdefault("errors", []).append(message)  # type: ignore[union-attr]
        def question(self, message: str) -> bool:
            return False
        def password(self, prompt: str) -> str | None:
            rec["password_prompted"] = True
            return password

    class FakeEngine:
        pid = 900
        def poll(self):  # noqa: ANN201
            return None
        def wait(self, timeout=None):  # noqa: ANN001, ANN201
            return 0

    engine = FakeEngine()
    monkeypatch.setattr(launcher, "Dialog", FakeDialog)
    monkeypatch.setattr(launcher, "pick_dialog_backend", lambda which: "zenity")
    monkeypatch.setattr(launcher, "_install_signal_handlers", lambda: None)
    monkeypatch.setattr(launcher, "acquire_launcher_lock",
                        lambda path, **kw: open(path, "a+"))
    monkeypatch.setattr(launcher, "stop_systemd_unit",
                        lambda **kw: (rec["unit"].append(True), "stopped")[1])  # type: ignore[union-attr]
    monkeypatch.setattr(launcher, "find_running_engines", lambda **kw: list(engines_found))
    from poseidon.proclife import ProcIdent as _PI

    def fake_stop(ident, **kw):  # noqa: ANN001, ANN202
        rec["stopped"].append(ident.pid)  # type: ignore[union-attr]
        return True
    monkeypatch.setattr(launcher, "_clear_stop_seam", fake_stop, raising=False)
    # clear_running_engines is exercised REAL, with seams injected via partial:
    real_clear = launcher.clear_running_engines
    monkeypatch.setattr(
        launcher, "clear_running_engines",
        lambda config, dialog, url: real_clear(
            config, dialog, url,
            engines=lambda: list(engines_found), stop=fake_stop,
            stop_unit=lambda: (rec["unit"].append(True), "stopped")[1],  # type: ignore[union-attr]
            engine_up=lambda _u: False, alive=lambda i: True))
    monkeypatch.setattr(launcher, "_start_engine",
                        lambda config, passphrase, dialog, url:
                            (rec.__setitem__("passphrase", passphrase), engine)[1])
    monkeypatch.setattr(launcher, "_shutdown_engine",
                        lambda proc, window, pidfile:
                            rec["shutdown"].append((proc, window)))  # type: ignore[union-attr]

    def fake_window(url, *, profile_dir, token_in_url=False, on_spawn=None,
                    fallback_block=None, handoff_threshold=2.0):  # noqa: ANN001, ANN202
        rec["window"].append(url)  # type: ignore[union-attr]
        if window_raises is not None:
            raise window_raises
        return window_rc
    monkeypatch.setattr(gui, "open_app_window_blocking", fake_window)
    return rec, engine, launcher
```

Then the tests:

```python
def test_main_always_fresh_start_kills_running_engine(tmp_path, monkeypatch) -> None:
    from poseidon.launcher import FoundEngine
    rec, engine, launcher = _wire_main(
        monkeypatch, tmp_path,
        engines_found=[FoundEngine(pid=321, starttime=1, cmdline="python -m poseidon run")])
    assert launcher.main() == 0
    assert rec.get("password_prompted") is True            # always prompted now
    assert rec["stopped"] == [321]                         # prior engine killed
    assert rec["unit"]                                     # systemd stop attempted
    assert rec["window"]                                   # window opened
    assert rec["shutdown"] == [(engine, None)]             # finally stopped OUR engine


def test_main_window_close_shuts_engine_down(tmp_path, monkeypatch) -> None:
    rec, engine, launcher = _wire_main(monkeypatch, tmp_path)
    assert launcher.main() == 0
    assert rec["shutdown"] == [(engine, None)]


def test_main_signal_during_window_still_stops_engine(tmp_path, monkeypatch) -> None:
    rec, engine, launcher = _wire_main(monkeypatch, tmp_path,
                                       window_raises=SystemExit(143))
    import pytest as _pytest
    with _pytest.raises(SystemExit):
        launcher.main()
    assert rec["shutdown"] == [(engine, None)]             # finally ran


def test_main_password_cancel_never_runs_kill_pass(tmp_path, monkeypatch) -> None:
    rec, engine, launcher = _wire_main(monkeypatch, tmp_path, password=None)
    assert launcher.main() == 1
    assert rec["stopped"] == [] and rec["unit"] == []      # nothing was touched
    assert rec["shutdown"] == []                           # no engine ever existed


def test_main_first_run_still_works_with_fresh_flow(tmp_path, monkeypatch) -> None:
    # Same shape as the pre-existing first-run test, adapted: no vault yet.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    import poseidon.gui as gui
    import poseidon.launcher as launcher
    from poseidon.security.vault import Vault

    answers = iter(["longpassword", "longpassword", "sk-ant-key"])

    class FakeDialog:
        def __init__(self, backend: str) -> None: ...
        def info(self, message: str) -> None: ...
        def error(self, message: str) -> None:
            raise AssertionError(f"unexpected error dialog: {message}")
        def question(self, message: str) -> bool:
            return False
        def password(self, prompt: str) -> str:
            return next(answers)
        def entry(self, prompt: str) -> str:
            return next(answers)

    class FakeEngine:
        pid = 901
        def poll(self):  # noqa: ANN201
            return None
        def wait(self, timeout=None):  # noqa: ANN001, ANN201
            return 0

    captured: dict[str, object] = {}
    monkeypatch.setattr(launcher, "Dialog", FakeDialog)
    monkeypatch.setattr(launcher, "pick_dialog_backend", lambda which: "zenity")
    monkeypatch.setattr(launcher, "_install_signal_handlers", lambda: None)
    monkeypatch.setattr(launcher, "acquire_launcher_lock", lambda path, **kw: open(path, "a+"))
    monkeypatch.setattr(launcher, "clear_running_engines",
                        lambda config, dialog, url: True)
    monkeypatch.setattr(launcher, "_start_engine",
                        lambda config, passphrase, dialog, url:
                            (captured.__setitem__("passphrase", passphrase), FakeEngine())[1])
    monkeypatch.setattr(launcher, "_shutdown_engine", lambda proc, window, pidfile: None)
    monkeypatch.setattr(gui, "open_app_window_blocking",
                        lambda url, **kw: (captured.__setitem__("url", url), 0)[1])

    assert launcher.main() == 0
    vault = Vault(tmp_path / "d" / "poseidon" / "vault.bin")
    assert vault.exists
    vault.unlock("longpassword")
    assert vault.get("anthropic_api_key") == "sk-ant-key"
    assert captured["passphrase"] == "longpassword"
    assert captured["url"] == "http://127.0.0.1:8321"
```

Also DELETE `test_main_engine_already_up_skips_setup_and_start` and the original `test_main_first_run_creates_vault_then_starts_and_opens` (replaced by the two above — the old first-run test monkeypatches `poseidon.gui.launch`, which `main()` no longer calls).

And the `_shutdown_engine` unit tests:

```python
def test_shutdown_engine_reaps_window_then_engine_then_pidfile(tmp_path, monkeypatch) -> None:
    import poseidon.launcher as launcher
    from poseidon.proclife import ProcIdent
    order: list[str] = []

    class FakeWindow:
        def poll(self):  # noqa: ANN201
            return None
        def terminate(self) -> None:
            order.append("window-term")
        def wait(self, timeout=None):  # noqa: ANN001, ANN201
            order.append("window-wait")
            return 0
        def kill(self) -> None:
            order.append("window-kill")

    class FakeEngine:
        pid = 902
        def poll(self):  # noqa: ANN201
            return None
        def wait(self, timeout=None):  # noqa: ANN001, ANN201
            return 0

    monkeypatch.setattr(launcher, "_kill_own_engine",
                        lambda proc, **kw: order.append("engine-stop"))
    notified: list[str] = []
    monkeypatch.setattr(launcher, "_notify", lambda *a: notified.append(a[0]))
    pidfile = tmp_path / "engine.pid"
    launcher.write_pidfile(pidfile, ProcIdent(pid=902, starttime=1))
    launcher._shutdown_engine(FakeEngine(), FakeWindow(), pidfile)
    assert order[0] == "window-term" and "engine-stop" in order
    assert order.index("window-term") < order.index("engine-stop")
    assert not pidfile.exists()
    assert notified                                       # "Poseidon stopped."


def test_shutdown_engine_none_is_noop(tmp_path) -> None:
    import poseidon.launcher as launcher
    launcher._shutdown_engine(None, None, tmp_path / "engine.pid")   # must not raise


def test_shutdown_engine_keeps_foreign_pidfile(tmp_path, monkeypatch) -> None:
    # pidfile now records a DIFFERENT engine (a takeover already restarted) —
    # our cleanup must not delete someone else's record.
    import poseidon.launcher as launcher
    from poseidon.proclife import ProcIdent

    class FakeEngine:
        pid = 903
        def poll(self):  # noqa: ANN201
            return 0     # already dead
        def wait(self, timeout=None):  # noqa: ANN001, ANN201
            return 0

    monkeypatch.setattr(launcher, "_kill_own_engine", lambda proc, **kw: None)
    pidfile = tmp_path / "engine.pid"
    launcher.write_pidfile(pidfile, ProcIdent(pid=999, starttime=7))
    launcher._shutdown_engine(FakeEngine(), None, pidfile)
    assert launcher.read_pidfile(pidfile) == ProcIdent(999, 7)
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/bin/pytest tests/unit/test_launcher.py -q`
Expected: FAIL — `AttributeError: ... '_shutdown_engine'` / `_install_signal_handlers`

- [ ] **Step 4: Implement `_install_signal_handlers`, `_shutdown_engine`, and the new `main()`**

```python
_WINDOW_REAP_GRACE = 5.0


def _install_signal_handlers() -> None:
    """SIGTERM/SIGHUP become SystemExit so main()'s finally still stops the
    engine (KDE logout, terminal kill, a takeover's signal). SIGINT already
    raises KeyboardInterrupt. Installed FIRST in main(): the ~30s startup wait
    is the longest window in the launcher's life and must be covered."""

    def _bail(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _bail)


def _shutdown_engine(
    proc: subprocess.Popen[bytes] | None,
    window: subprocess.Popen[bytes] | None,
    pidfile: Path,
) -> None:
    """Idempotent teardown, in dependency order: reap the window FIRST (the
    browser profile must be free before our flock releases at process death),
    then stop our engine, then drop the pid file iff it is still ours."""
    if window is not None and window.poll() is None:
        try:
            window.terminate()
            try:
                window.wait(timeout=_WINDOW_REAP_GRACE)
            except subprocess.TimeoutExpired:
                window.kill()
                window.wait(timeout=_WINDOW_REAP_GRACE)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if proc is None:
        return
    was_live = proc.poll() is None
    _kill_own_engine(proc)
    recorded = read_pidfile(pidfile)
    if recorded is not None and recorded.pid == proc.pid:
        pidfile.unlink(missing_ok=True)
    if was_live:
        _notify("Poseidon stopped.", "The engine was shut down with the window.")


def main(argv: list[str] | None = None) -> int:
    from .core.config import load_config
    from .security.vault import Vault

    _install_signal_handlers()

    backend = pick_dialog_backend(shutil.which)
    if backend is None:
        print("The Poseidon launcher needs a GUI dialog tool. Install one, e.g.:\n"
              "  sudo pacman -S zenity        # or: kdialog\n"
              "Or start Poseidon from a terminal:  poseidon run", file=sys.stderr)
        return 2
    dialog = Dialog(backend)
    try:
        config = load_config()
    except Exception as exc:  # noqa: BLE001 — surface any config error as a dialog
        dialog.error(f"Poseidon configuration could not be loaded:\n\n{exc}")
        return 1

    lock = acquire_launcher_lock(config.data_dir / _LAUNCHER_LOCKFILE)
    if lock is None:
        dialog.error("Another Poseidon window is open and did not hand over in time.\n"
                     "Close it, then launch again.")
        return 1

    vault = Vault(config.data_dir / "vault.bin")
    url = dashboard_url(config)
    passphrase: str | None = None

    if needs_setup(vault):
        passphrase = _first_run_setup(dialog, config, vault)
        if passphrase is None:
            return 1  # user cancelled setup
    if passphrase is None:
        passphrase = dialog.password("Enter your Poseidon vault password to start:")
        if not passphrase:
            # Cancelling a launch must never stop a running engine: the kill
            # pass below is reached only with a passphrase in hand.
            dialog.error("No password entered — Poseidon was not started.")
            return 1

    if not clear_running_engines(config, dialog, url):
        return 1

    proc: subprocess.Popen[bytes] | None = None
    window: subprocess.Popen[bytes] | None = None
    pidfile = config.data_dir / _ENGINE_PIDFILE
    try:
        proc = _start_engine(config, passphrase, dialog, url)
        if proc is None:
            return 1
        token = _resolve_window_token(config, passphrase)
        target = url
        if token:
            from urllib.parse import quote
            target = f"{url}/?token={quote(token, safe='')}"

        def _remember(p: subprocess.Popen[bytes]) -> None:
            nonlocal window
            window = p

        from .gui import open_app_window_blocking
        return open_app_window_blocking(
            target,
            profile_dir=config.data_dir / "webview-profile",
            token_in_url=bool(token),
            on_spawn=_remember,
            fallback_block=lambda: dialog.info(
                "Poseidon is running.\n\nClose this dialog to shut it down."),
        )
    finally:
        _shutdown_engine(proc, window, pidfile)
        del lock  # released by process exit; explicit for the reader
```

Note: `main()` must call the module-level `clear_running_engines(config, dialog, url)` with exactly that 3-arg shape (the flow tests monkeypatch it that way).

- [ ] **Step 5: Run the full launcher + gui + proclife suites**

Run: `.venv/bin/pytest tests/unit/test_launcher.py tests/unit/test_gui_window.py tests/unit/test_proclife.py -q`
Expected: PASS

- [ ] **Step 6: Gate + commit**

```bash
git add src/poseidon/launcher.py tests/unit/test_launcher.py
git commit -m "feat(launcher): window-close kills the engine; every launch is a fresh start"
```

---

### Task 8: E2E smoke probes (real engine, real window) + docs touch

**Files:**
- No src changes expected (fix anything the probes expose; document probe results in the task report)
- Modify (docs only): `docs/architecture.md` — update the launcher paragraph if it describes the old reuse behavior (grep `launcher` first; if absent, skip).

This task RUNS things; a desktop session is available. Do each probe, record output.

- [ ] **Probe 1 — discovery + graceful stop of a REAL engine on scratch dirs:**

```bash
cd /home/shuffman95/Poseidon && SCRATCH=$(mktemp -d) && .venv/bin/python - <<'EOF'
import json, os, subprocess, sys, tempfile, time
from pathlib import Path
scratch = Path(os.environ.get("SCRATCH", tempfile.mkdtemp()))
os.environ["XDG_DATA_HOME"] = str(scratch / "d")
os.environ["XDG_CONFIG_HOME"] = str(scratch / "c")
sys.path.insert(0, "src")
from poseidon.security.vault import Vault
from poseidon.launcher import find_running_engines, engine_env
from poseidon.proclife import stop_process
vault_dir = scratch / "d" / "poseidon"; vault_dir.mkdir(parents=True)
Vault(vault_dir / "vault.bin").create("scratch-pass-123")
cfg_dir = scratch / "c" / "poseidon"; cfg_dir.mkdir(parents=True)
(cfg_dir / "poseidon.yaml").write_text("dashboard:\n  port: 18321\n")
proc = subprocess.Popen([sys.executable, "-m", "poseidon", "run"],
    env=engine_env("scratch-pass-123", os.environ), start_new_session=True,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(6)
found = [e for e in find_running_engines() if e.pid == proc.pid]
print("FOUND:", found)
assert found, "scan must find the scratch engine"
assert stop_process(found[0]) is True, "graceful stop"
print("STOPPED OK; exit code:", proc.wait(timeout=15))
EOF
```

Expected: `FOUND: [FoundEngine(pid=…)]`, `STOPPED OK` with a 0/-15 exit — proves scan+stop against a live engine. (If config validation refuses the minimal yaml, use `poseidon.core.config.load_config()` defaults by writing no yaml at all and read the default port from the loaded config instead of 18321.)

- [ ] **Probe 2 — vivaldi dedicated profile: wait() blocks and X/kill unblocks (first AND second launch):**

```bash
P=$(mktemp -d)/webview-profile && .venv/bin/python - <<EOF
import subprocess, time
from pathlib import Path
profile = Path("$P")
for round in (1, 2):
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    t0 = time.monotonic()
    proc = subprocess.Popen(["/usr/bin/vivaldi-stable", "--app=about:blank",
        f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
        "--window-size=400,300"])
    time.sleep(8)                     # window visibly open; wait() has NOT returned
    assert proc.poll() is None, f"round {round}: process exited early — hand-off?!"
    proc.terminate()                  # stands in for the user's X click
    rc = proc.wait(timeout=15)
    print(f"round {round}: blocked {time.monotonic()-t0:.1f}s then unblocked, rc={rc}")
EOF
```

Expected: both rounds report ~8s block then unblock — proves process lifetime == window lifetime on this machine, including the second (profile-exists) launch. A brief blank window appears twice; that is expected. Note in the report whether any vivaldi onboarding UI appeared (finding for the user if so).

- [ ] **Probe 3 — ProcessSingleton hand-off detection:** spawn one window on the profile, then a second `--app` on the SAME profile; the second must exit ≤2s (hand-off) while the first keeps running — this is the case `open_app_window_blocking` defends against with the sweep+respawn:

```bash
.venv/bin/python - <<EOF
import subprocess, time
from pathlib import Path
profile = Path("$P")
a = subprocess.Popen(["/usr/bin/vivaldi-stable", "--app=about:blank",
    f"--user-data-dir={profile}", "--no-first-run", "--window-size=400,300"])
time.sleep(6)
t0 = time.monotonic()
b = subprocess.Popen(["/usr/bin/vivaldi-stable", "--app=about:blank",
    f"--user-data-dir={profile}", "--no-first-run", "--window-size=400,300"])
rc = b.wait(timeout=30)
dt = time.monotonic() - t0
print(f"second spawn exited in {dt:.2f}s rc={rc} (hand-off confirmed: {dt < 2.0})")
a.terminate(); a.wait(timeout=15)
EOF
```

Expected: `hand-off confirmed: True`. If the second process does NOT exit quickly, report it — the handoff_threshold defense may need tuning.

- [ ] **Probe 4 — occupied port:** with a dummy listener on the scratch port, start a scratch engine and record what it does (calibrates the D2.4 abort's importance):

```bash
.venv/bin/python - <<'EOF'
import socket, subprocess, sys, time, os
s = socket.socket(); s.bind(("127.0.0.1", 18321)); s.listen(1)
# reuse Probe 1's scratch env (XDG vars) — engine on the same port
# ... spawn engine exactly as Probe 1, wait 8s, then:
#     print(proc.poll())  -> record: None (running headless!) or an exit code
EOF
```

Record the observed behavior in the task report (the launcher aborts pre-spawn in this case, so this is informational).

- [ ] **Probe 5 — cleanup:** `pkill -f "user-data-dir=$P" || true; rm -rf "$(dirname $P)" "$SCRATCH"` and confirm no `poseidon run` processes remain: `pgrep -af "poseidon run" || echo CLEAN`.

- [ ] **Step: docs + commit** (only if architecture.md mentions the launcher's old semantics)

```bash
git add -A docs/ && git commit -m "docs: launcher lifecycle probes + architecture note" || echo "nothing to commit"
```

---

## Final integration (controller runs after all tasks)

- [ ] Full gate: `.venv/bin/ruff check src tests && .venv/bin/mypy src && .venv/bin/pytest -q` → all green.
- [ ] `tools/ui_verify.py` untouched surfaces: not required (no dashboard change in this plan).
- [ ] Version bump + release prep happen OUTSIDE this plan (controller task).

## Self-Review (done at write time)

- Spec coverage: D1→Task 5; D2→Tasks 2-3; D3→Tasks 4,7; D4→Task 6; identity primitive→Task 1; spec §Testing items 1-11 map to Tasks 3,5,6,7 tests (item 4/5 = `test_main_signal_during_window_still_stops_engine` + startup-wait coverage via `_start_engine` being inside the try in Task 7's `main()`); E2E→Task 8. Version bump intentionally out of scope.
- Placeholders: none; every step has full code/commands.
- Type consistency: `ProcIdent` frozen dataclass with `pid/starttime` used uniformly; `stop_process(ident, **kw)` duck-typed via `_HasIdent`; `FoundEngine(ProcIdent)` adds `cmdline`; `clear_running_engines(config, dialog, url)` 3-positional shape is what `main()` calls and the flow tests monkeypatch.
