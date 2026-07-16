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
