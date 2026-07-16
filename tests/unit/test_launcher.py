"""Pure logic of the desktop launcher (dialog/subprocess seams excluded).

The launcher's I/O (zenity/kdialog dialogs, spawning the engine, opening the
window) lives behind thin wrappers; everything decision-shaped is a pure
function tested here.
"""

from __future__ import annotations

import json as _json

from poseidon.core.config import AppConfig, DashboardConfig
from poseidon.launcher import (
    FoundEngine,
    _kill_own_engine,
    _wait_for_dashboard,
    acquire_launcher_lock,
    clear_running_engines,
    dashboard_url,
    engine_env,
    find_running_engines,
    is_engine_cmdline,
    needs_setup,
    pick_dialog_backend,
    read_pidfile,
    stop_systemd_unit,
    wait_until_up,
    write_pidfile,
)
from poseidon.proclife import ProcIdent


def _config(host: str = "127.0.0.1", port: int = 8321) -> AppConfig:
    # A non-loopback host must carry an auth token or config validation refuses
    # to construct (the exposed-dashboard guard) — dashboard_url only reads
    # host/port, so a placeholder credential keeps the config valid.
    loopback = host in ("127.0.0.1", "localhost", "::1")
    return AppConfig(dashboard=DashboardConfig(
        host=host, port=port,
        auth_token_credential="" if loopback else "dashboard_token"))


def test_dashboard_url_loopback_default() -> None:
    assert dashboard_url(_config()) == "http://127.0.0.1:8321"


def test_dashboard_url_wildcard_binds_resolve_to_loopback() -> None:
    assert dashboard_url(_config(host="0.0.0.0")) == "http://127.0.0.1:8321"
    assert dashboard_url(_config(host="::")) == "http://127.0.0.1:8321"


def test_dashboard_url_brackets_bare_ipv6() -> None:
    assert dashboard_url(_config(host="::1", port=9000)) == "http://[::1]:9000"


class _Vault:
    def __init__(self, exists: bool) -> None:
        self.exists = exists


def test_needs_setup_true_when_vault_absent() -> None:
    assert needs_setup(_Vault(exists=False)) is True
    assert needs_setup(_Vault(exists=True)) is False


def test_engine_env_adds_passphrase_without_mutating_base() -> None:
    base = {"PATH": "/usr/bin", "HOME": "/home/x"}
    env = engine_env("hunter2", base)
    assert env["POSEIDON_VAULT_PASSPHRASE"] == "hunter2"
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/home/x"
    assert "POSEIDON_VAULT_PASSPHRASE" not in base  # base untouched


def test_wait_until_up_returns_true_when_probe_succeeds() -> None:
    calls = {"n": 0}
    slept: list[float] = []

    def probe() -> bool:
        calls["n"] += 1
        return calls["n"] >= 3  # up on the third poll

    assert wait_until_up(probe, attempts=5, sleep=slept.append, interval=0.1) is True
    assert calls["n"] == 3
    assert slept == [0.1, 0.1]  # slept between the first two failed polls only


def test_wait_until_up_gives_up_after_attempts() -> None:
    slept: list[float] = []
    assert wait_until_up(lambda: False, attempts=4, sleep=slept.append, interval=0.5) is False
    assert len(slept) == 4


def test_pick_dialog_backend_prefers_zenity() -> None:
    assert pick_dialog_backend(lambda t: f"/usr/bin/{t}") == "zenity"


def test_pick_dialog_backend_falls_back_to_kdialog() -> None:
    which = {"kdialog": "/usr/bin/kdialog"}.get
    assert pick_dialog_backend(which) == "kdialog"


def test_pick_dialog_backend_none_when_no_gui() -> None:
    assert pick_dialog_backend(lambda _t: None) is None


# ---- main() orchestration (real vault + config; GUI/engine/window faked) ----

def test_main_first_run_creates_vault_then_starts_and_opens(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    import poseidon.gui
    import poseidon.launcher as launcher
    from poseidon.security.vault import Vault

    answers = iter(["longpassword", "longpassword", "sk-ant-key"])

    class FakeDialog:
        def __init__(self, backend: str) -> None: ...
        def info(self, message: str) -> None: ...
        def error(self, message: str) -> None:
            raise AssertionError(f"unexpected error dialog: {message}")
        def password(self, prompt: str) -> str:
            return next(answers)
        def entry(self, prompt: str) -> str:
            return next(answers)

    captured: dict[str, object] = {}

    def fake_start(config, passphrase, dialog, url):  # noqa: ANN001, ANN202
        captured["passphrase"] = passphrase
        captured["url"] = url
        return _FakeEngineProc()

    launched: dict[str, object] = {}
    monkeypatch.setattr(launcher, "Dialog", FakeDialog)
    monkeypatch.setattr(launcher, "pick_dialog_backend", lambda which: "zenity")
    monkeypatch.setattr(launcher, "_engine_up", lambda url: False)  # down -> must start it
    monkeypatch.setattr(launcher, "_start_engine", fake_start)
    monkeypatch.setattr(poseidon.gui, "launch",
                        lambda url, token=None: (launched.update(url=url, token=token), 0)[1])

    assert launcher.main() == 0
    # The vault was really created in the tmp data dir, with the pasted key.
    vault = Vault(tmp_path / "d" / "poseidon" / "vault.bin")
    assert vault.exists
    vault.unlock("longpassword")
    assert vault.get("anthropic_api_key") == "sk-ant-key"
    # The engine start got that same passphrase; the window opened on loopback.
    assert captured["passphrase"] == "longpassword"
    assert captured["url"] == "http://127.0.0.1:8321"
    assert launched["url"] == "http://127.0.0.1:8321"
    assert launched["token"] is None  # loopback default has no bearer token


def test_main_engine_already_up_skips_setup_and_start(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    import poseidon.gui
    import poseidon.launcher as launcher
    from poseidon.security.vault import Vault

    Vault(tmp_path / "d" / "poseidon" / "vault.bin").create("longpassword")  # not first run

    class FakeDialog:
        def __init__(self, backend: str) -> None: ...
        def error(self, message: str) -> None:
            raise AssertionError(f"unexpected error dialog: {message}")

    def no_start(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("engine must not be started when it is already up")

    launched: dict[str, object] = {}
    monkeypatch.setattr(launcher, "Dialog", FakeDialog)
    monkeypatch.setattr(launcher, "pick_dialog_backend", lambda which: "zenity")
    monkeypatch.setattr(launcher, "_engine_up", lambda url: True)  # already running
    monkeypatch.setattr(launcher, "_start_engine", no_start)
    monkeypatch.setattr(poseidon.gui, "launch",
                        lambda url, token=None: (launched.update(url=url), 0)[1])

    assert launcher.main() == 0
    assert launched["url"] == "http://127.0.0.1:8321"


# ---- process lifecycle: pid file, engine matcher, /proc scan ----


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
    path.write_text('{"pid": true, "starttime": 1}')
    assert read_pidfile(path) is None                       # bool is not a pid


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


def test_engine_matcher_never_pattern_scans_argv() -> None:
    # A script that merely RECEIVES "-m poseidon run" as its own arguments must
    # NOT match — this predicate authorizes a kill, so matching is strictly
    # positional, never a scan over argv.
    py = "/usr/bin/python3"
    assert is_engine_cmdline(py, [py, "x.py", "-m", "poseidon", "run"]) is False


def test_engine_matcher_requires_real_interpreter_basename() -> None:
    argv = ["/usr/bin/python3", "-m", "poseidon", "run"]
    assert is_engine_cmdline("/usr/bin/python-wrapper", argv) is False
    assert is_engine_cmdline("/usr/bin/python3-config", argv) is False
    assert is_engine_cmdline("/usr/bin/python", argv) is True
    assert is_engine_cmdline("/usr/bin/python3", argv) is True
    assert is_engine_cmdline("/usr/bin/python3.14", argv) is True


def test_engine_matcher_rejects_interpreter_flag_form_by_design() -> None:
    # Documented false negative: positional strictness rejects interpreter
    # flags before -m (``python -O -m poseidon run``). Deliberate — the
    # fresh-start port assertion backstops any engine the scan misses.
    py = "/usr/bin/python3"
    assert is_engine_cmdline(py, [py, "-O", "-m", "poseidon", "run"]) is False


def _fake_proc(tmp_path, pid: int, exe: str, argv: list[str], starttime: int,
               *, with_exe: bool = True) -> None:
    d = tmp_path / str(pid)
    d.mkdir()
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    tail = f"S 1 {pid} {pid} 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 {starttime} 0 0"
    (d / "stat").write_text(f"{pid} ({argv[0][:15]}) {tail}")
    if with_exe:
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


def test_find_running_engines_skips_entries_without_readable_exe(tmp_path) -> None:
    # A matching cmdline whose ``exe`` link cannot be read (absent here; another
    # user's process in production) is skipped via the OSError path.
    py = "/usr/bin/python3.14"
    _fake_proc(tmp_path, 200, py, [py, "-m", "poseidon", "run"], 66, with_exe=False)
    assert find_running_engines(proc_root=tmp_path) == []


# ---- fresh-start pass: systemd stop + kill + port assertion ----


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
        class R:  # noqa: E701
            returncode = 5   # inactive unit: rc != 0 is still fine
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


# ---- pass ordering + pid-file retention through the kill loop ----


def test_clear_order_is_unit_then_kills_then_probe(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    write_pidfile(tmp_path / "engine.pid", ProcIdent(200, 2))
    scanned = [FoundEngine(pid=300, starttime=3, cmdline="python -m poseidon run")]
    order: list[str] = []
    assert clear_running_engines(
        cfg, _ErrDialog(), "http://x",
        engines=lambda: scanned,
        stop=lambda ident, **kw: (order.append(f"kill:{ident.pid}"), True)[1],
        stop_unit=lambda: (order.append("unit"), "stopped")[1],
        engine_up=lambda _u: (order.append("probe"), False)[1],
        alive=lambda ident: True) is True
    assert order == ["unit", "kill:200", "kill:300", "probe"]


def test_clear_unit_timeout_short_circuits_the_pass(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    write_pidfile(tmp_path / "engine.pid", ProcIdent(200, 2))
    order: list[str] = []
    assert clear_running_engines(
        cfg, _ErrDialog(), "http://x",
        engines=lambda: [FoundEngine(pid=300, starttime=3, cmdline="python -m poseidon run")],
        stop=lambda ident, **kw: (order.append(f"kill:{ident.pid}"), True)[1],
        stop_unit=lambda: (order.append("unit"), "timeout")[1],
        engine_up=lambda _u: (order.append("probe"), False)[1],
        alive=lambda ident: True) is False
    assert order == ["unit"]                               # nothing runs after the timeout
    assert (tmp_path / "engine.pid").exists()              # record untouched


def test_clear_keeps_pidfile_when_live_engine_stop_unconfirmed(tmp_path) -> None:
    # A LIVE recorded engine whose stop() did NOT confirm must keep its on-disk
    # record: the next launch retries it, and the port assertion still backstops.
    cfg = _cfg(tmp_path)
    write_pidfile(tmp_path / "engine.pid", ProcIdent(200, 2))
    dialog = _ErrDialog()
    assert clear_running_engines(
        cfg, dialog, "http://x",
        engines=lambda: [],
        stop=lambda ident, **kw: False,                    # kill not confirmed
        stop_unit=lambda: "stopped",
        engine_up=lambda _u: True,                         # engine indeed still up
        alive=lambda ident: True) is False
    assert (tmp_path / "engine.pid").exists()              # record survives for retry
    log = (tmp_path / "launcher-engine.log").read_text(encoding="utf-8")
    assert "stopped=False" in log                          # attempt AND outcome logged


# ---- _start_engine: tracked child, fast-fail on death, keep-waiting ----


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


# ---- singleton lock + takeover ----


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


def test_takeover_signals_verified_holder_then_acquires(tmp_path) -> None:
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
