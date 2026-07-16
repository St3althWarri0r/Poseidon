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


# ---- signal handling: SIGTERM/SIGHUP become SystemExit so finally still runs ----


def test_install_signal_handlers_registers_term_and_hup(monkeypatch) -> None:
    import poseidon.launcher as launcher

    installed: dict[int, object] = {}
    monkeypatch.setattr(launcher.signal, "signal",
                        lambda sig, handler: installed.__setitem__(sig, handler))
    launcher._install_signal_handlers()
    assert set(installed) == {launcher.signal.SIGTERM, launcher.signal.SIGHUP}


def test_install_signal_handlers_handler_raises_128_plus_signum(monkeypatch) -> None:
    import pytest as _pytest

    import poseidon.launcher as launcher

    installed: dict[int, object] = {}
    monkeypatch.setattr(launcher.signal, "signal",
                        lambda sig, handler: installed.__setitem__(sig, handler))
    launcher._install_signal_handlers()
    # Snapshot BEFORE invoking either handler: the latch makes the handler
    # itself call (the monkeypatched) signal.signal on both signals, which
    # would otherwise overwrite `installed[SIGHUP]` with SIG_IGN while we are
    # still iterating and reading it live — invoking a non-callable sentinel
    # on the second loop turn instead of the handler.
    handlers = dict(installed)
    for sig in (launcher.signal.SIGTERM, launcher.signal.SIGHUP):
        with _pytest.raises(SystemExit) as exc_info:
            handlers[sig](sig, None)  # type: ignore[operator]
        assert exc_info.value.code == 128 + sig


def test_install_signal_handlers_latches_to_sig_ign_after_firing() -> None:
    # A takeover re-signals the victim on EVERY 0.5s poll, and the victim's
    # `finally` (window reap + engine kill) is not instant. Without a latch, a
    # second SIGTERM landing mid-teardown raises SystemExit AGAIN, unwinding
    # `_shutdown_engine` before it reaches the engine kill — leaving the
    # engine running headless. Pin: the REAL installed handler disarms both
    # signals to SIG_IGN the instant it first fires, so a takeover's repeated
    # signalling can only ever produce ONE SystemExit.
    import signal as _sig

    import pytest as _pytest

    import poseidon.launcher as launcher

    try:
        launcher._install_signal_handlers()
        term_handler = _sig.getsignal(_sig.SIGTERM)
        with _pytest.raises(SystemExit):
            term_handler(_sig.SIGTERM, None)  # type: ignore[operator]
        assert _sig.getsignal(_sig.SIGTERM) is _sig.SIG_IGN
        assert _sig.getsignal(_sig.SIGHUP) is _sig.SIG_IGN
    finally:
        # Restore real process-wide disposition so this test cannot leak
        # SIG_IGN into any test that runs after it in the same process.
        _sig.signal(_sig.SIGTERM, _sig.SIG_DFL)
        _sig.signal(_sig.SIGHUP, _sig.SIG_DFL)


# ---- main() orchestration (real vault + config; GUI/engine/window faked) ----

def _wire_main(monkeypatch, tmp_path, *, engines_found=(), password="longpassword",
               window_rc=0, window_raises=None, start_raises_after_spawn=None):
    """Standard main() wiring: real vault in tmp XDG dirs, everything else faked.
    Returns a dict of recorders.

    The fake ``_start_engine`` and ``open_app_window_blocking`` both hand their
    process handle to ``main()`` via the ``on_spawn`` callback (exactly as the
    real ones do), so ``rec["shutdown"]`` records the ENGINE and WINDOW handles
    that reach the teardown — proving both escaped to main()'s finally. Set
    ``start_raises_after_spawn`` to a raised exception to simulate a signal
    landing DURING the engine-startup wait (handle already forked)."""
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

    class FakeWindow:
        pid = 950
        def poll(self):  # noqa: ANN201
            return None
        def terminate(self) -> None: ...
        def wait(self, timeout=None):  # noqa: ANN001, ANN201
            return 0
        def kill(self) -> None: ...

    engine = FakeEngine()
    window_proc = FakeWindow()
    rec["window_proc"] = window_proc
    monkeypatch.setattr(launcher, "Dialog", FakeDialog)
    monkeypatch.setattr(launcher, "pick_dialog_backend", lambda which: "zenity")
    monkeypatch.setattr(launcher, "_install_signal_handlers", lambda: None)
    monkeypatch.setattr(launcher, "acquire_launcher_lock",
                        # SIM115: this fakes a lock HANDOFF (an open handle the
                        # caller holds for the launcher's life), not a read/write
                        # that should be scoped to a `with` block. mkdir mirrors
                        # the real seam, which is called before the data dir is
                        # guaranteed to exist (e.g. a genuinely fresh install).
                        lambda path, **kw: (path.parent.mkdir(parents=True, exist_ok=True),
                                            path.open("a+", encoding="utf-8"))[1])  # noqa: SIM115
    monkeypatch.setattr(launcher, "stop_systemd_unit",
                        lambda **kw: (rec["unit"].append(True), "stopped")[1])  # type: ignore[union-attr]
    monkeypatch.setattr(launcher, "find_running_engines", lambda **kw: list(engines_found))

    def fake_stop(ident, **kw):  # noqa: ANN001, ANN202
        rec["stopped"].append(ident.pid)  # type: ignore[union-attr]
        return True
    # clear_running_engines is exercised REAL, with seams injected via partial:
    real_clear = launcher.clear_running_engines
    monkeypatch.setattr(
        launcher, "clear_running_engines",
        lambda config, dialog, url: real_clear(
            config, dialog, url,
            engines=lambda: list(engines_found), stop=fake_stop,
            stop_unit=lambda: (rec["unit"].append(True), "stopped")[1],  # type: ignore[union-attr]
            engine_up=lambda _u: False, alive=lambda i: True))

    def fake_start(config, passphrase, dialog, url, *, on_spawn=None):  # noqa: ANN001, ANN202
        rec["passphrase"] = passphrase
        if on_spawn is not None:
            on_spawn(engine)              # hand the handle off at "fork", as the real one does
        if start_raises_after_spawn is not None:
            raise start_raises_after_spawn
        return engine
    monkeypatch.setattr(launcher, "_start_engine", fake_start)
    monkeypatch.setattr(launcher, "_shutdown_engine",
                        lambda proc, window, pidfile:
                            rec["shutdown"].append((proc, window)))  # type: ignore[union-attr]

    def fake_window(url, *, profile_dir, token_in_url=False, on_spawn=None,
                    fallback_block=None, handoff_threshold=2.0):  # noqa: ANN001, ANN202
        rec["window"].append(url)  # type: ignore[union-attr]
        if on_spawn is not None:
            on_spawn(window_proc)         # the window handle must reach _shutdown_engine too
        if window_raises is not None:
            raise window_raises
        return window_rc
    monkeypatch.setattr(gui, "open_app_window_blocking", fake_window)
    return rec, engine, launcher


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
    # finally stopped OUR engine AND reaped the window handle it spawned:
    assert rec["shutdown"] == [(engine, rec["window_proc"])]


def test_main_window_close_shuts_engine_down(tmp_path, monkeypatch) -> None:
    rec, engine, launcher = _wire_main(monkeypatch, tmp_path)
    assert launcher.main() == 0
    assert rec["shutdown"] == [(engine, rec["window_proc"])]


def test_main_signal_during_window_still_stops_engine(tmp_path, monkeypatch) -> None:
    rec, engine, launcher = _wire_main(monkeypatch, tmp_path,
                                       window_raises=SystemExit(143))
    import pytest as _pytest
    with _pytest.raises(SystemExit):
        launcher.main()
    # the window handoff happened before the signal, so finally reaps both:
    assert rec["shutdown"] == [(engine, rec["window_proc"])]


def test_main_signal_during_startup_wait_still_reaps_engine(tmp_path, monkeypatch) -> None:
    # The engine is Popen'd, then main() blocks ~30s waiting for the dashboard.
    # A signal (SystemExit) landing in that wait must still reap the engine —
    # which only works if the handle escaped to main()'s `proc` AT FORK, via
    # on_spawn, not via _start_engine's return value (it never returns here).
    rec, engine, launcher = _wire_main(monkeypatch, tmp_path,
                                       start_raises_after_spawn=SystemExit(143))
    import pytest as _pytest
    with _pytest.raises(SystemExit):
        launcher.main()
    assert rec["shutdown"] == [(engine, None)]             # window never opened
    assert rec["shutdown"][0][0] is engine                 # the ENGINE handle, NOT None


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
    monkeypatch.setattr(launcher, "acquire_launcher_lock",
                        lambda path, **kw: (path.parent.mkdir(parents=True, exist_ok=True),
                                            path.open("a+", encoding="utf-8"))[1])  # noqa: SIM115
    monkeypatch.setattr(launcher, "clear_running_engines",
                        lambda config, dialog, url: True)

    def fake_start(config, passphrase, dialog, url, *, on_spawn=None):  # noqa: ANN001, ANN202
        captured["passphrase"] = passphrase
        if on_spawn is not None:
            on_spawn(FakeEngine())
        return FakeEngine()
    monkeypatch.setattr(launcher, "_start_engine", fake_start)
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


# ---- _shutdown_engine: idempotent teardown, in dependency order ----


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


def test_shutdown_engine_idempotent_across_two_calls(tmp_path, monkeypatch) -> None:
    # main()'s finally can, in principle, run more than once' worth of teardown
    # (a signal during teardown). Two calls must not raise, must notify exactly
    # once, and must not double-unlink. Idempotence comes from poll() flipping
    # live->dead after the first stop and was_live being recomputed each call.
    import poseidon.launcher as launcher
    from poseidon.proclife import ProcIdent

    class FakeEngine:
        pid = 904
        def __init__(self) -> None:
            self._dead = False
        def poll(self):  # noqa: ANN201
            return 0 if self._dead else None
        def wait(self, timeout=None):  # noqa: ANN001, ANN201
            self._dead = True
            return 0

    monkeypatch.setattr(launcher, "_kill_own_engine", lambda proc, **kw: proc.wait())
    notified: list[str] = []
    monkeypatch.setattr(launcher, "_notify", lambda *a: notified.append(a[0]))
    pidfile = tmp_path / "engine.pid"
    launcher.write_pidfile(pidfile, ProcIdent(pid=904, starttime=1))
    engine = FakeEngine()
    launcher._shutdown_engine(engine, None, pidfile)
    launcher._shutdown_engine(engine, None, pidfile)      # second call: pure no-op
    assert notified == ["Poseidon stopped."]              # exactly one notify
    assert not pidfile.exists()                           # unlinked once, no double-unlink error


def test_shutdown_engine_skips_kill_for_already_dead_handle(tmp_path, monkeypatch) -> None:
    # The fail path hands us a handle _start_engine already reaped. Re-killing it
    # is unsafe (its pid may be recycled, and _kill_own_engine skips the identity
    # check), so a dead handle must NOT be signalled and must NOT notify — while
    # a live handle still is (guard against over-correction).
    import poseidon.launcher as launcher
    from poseidon.proclife import ProcIdent

    killed: list[int] = []
    notified: list[str] = []
    monkeypatch.setattr(launcher, "_kill_own_engine", lambda proc, **kw: killed.append(proc.pid))
    monkeypatch.setattr(launcher, "_notify", lambda *a: notified.append(a[0]))

    class DeadEngine:
        pid = 905
        def poll(self):  # noqa: ANN201
            return 0     # already reaped — an int, not None
        def wait(self, timeout=None):  # noqa: ANN001, ANN201
            return 0

    pidfile = tmp_path / "engine.pid"
    launcher.write_pidfile(pidfile, ProcIdent(pid=905, starttime=1))
    launcher._shutdown_engine(DeadEngine(), None, pidfile)
    assert killed == []                                   # dead handle never signalled
    assert notified == []                                 # and never announced "stopped"
    assert not pidfile.exists()                           # BUT its stale record is still dropped

    class LiveEngine:
        pid = 906
        def poll(self):  # noqa: ANN201
            return None  # still running
        def wait(self, timeout=None):  # noqa: ANN001, ANN201
            return 0

    launcher._shutdown_engine(LiveEngine(), None, tmp_path / "other.pid")
    assert killed == [906]                                # live handle IS killed
    assert notified == ["Poseidon stopped."]              # and announced once


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


def test_start_engine_hands_off_handle_before_the_wait(tmp_path, monkeypatch) -> None:
    # _start_engine BLOCKS in its wait loop for ~30s before returning the Popen.
    # If a signal (SystemExit) lands in that wait, the handle must ALREADY have
    # reached the caller via on_spawn — otherwise main()'s finally can't reap
    # the just-spawned engine and it orphans. Pin: on_spawn is called with the
    # real Popen BEFORE the wait, and the SystemExit propagates out.
    import pytest as _pytest

    import poseidon.launcher as launcher

    fake_proc = _FakeEngineProc(alive=True, pid=808)
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(launcher, "proc_starttime", lambda pid: 5)
    monkeypatch.setattr(launcher, "_notify", lambda *a: None)

    def boom(_url):  # noqa: ANN001, ANN202 — the engine_up probe raises on the first poll
        raise SystemExit(143)
    monkeypatch.setattr(launcher, "_engine_up", boom)

    spawned: list[object] = []
    with _pytest.raises(SystemExit) as exc_info:
        launcher._start_engine(_cfg(tmp_path), "pw", _ErrDialog(), "http://x",
                               on_spawn=spawned.append)
    assert exc_info.value.code == 143
    assert spawned == [fake_proc]                          # handle escaped BEFORE the raise


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


def test_takeover_signals_holder_once_per_identity_not_every_poll(tmp_path) -> None:
    # A verified-alive holder that is slow to tear down (its own window/engine
    # reap takes real time) stays the recorded identity across many polls.
    # Spamming SIGTERM at it every 0.5s is pure noise at best; pin that the
    # SAME identity is signalled exactly ONCE no matter how many polls it
    # takes for the flock to actually free.
    lock_path = tmp_path / "launcher.lock"
    holder = acquire_launcher_lock(lock_path)
    assert holder is not None
    signalled: list[tuple[int, int]] = []
    polls = {"n": 0}

    def term(pid, sig):  # noqa: ANN001, ANN202
        signalled.append((pid, sig))

    def sleeper(_s: float) -> None:
        polls["n"] += 1
        if polls["n"] >= 4:                # holder only "exits" after several polls
            holder.close()

    fh = acquire_launcher_lock(
        lock_path, term=term, alive=lambda ident: True,
        sleep=sleeper, notify=lambda *_a: None)
    try:
        assert fh is not None
        assert len(signalled) == 1         # ONE term call total, not once-per-poll
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
