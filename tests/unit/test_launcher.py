"""Pure logic of the desktop launcher (dialog/subprocess seams excluded).

The launcher's I/O (zenity/kdialog dialogs, spawning the engine, opening the
window) lives behind thin wrappers; everything decision-shaped is a pure
function tested here.
"""

from __future__ import annotations

from poseidon.core.config import AppConfig, DashboardConfig
from poseidon.launcher import (
    dashboard_url,
    engine_env,
    needs_setup,
    pick_dialog_backend,
    wait_until_up,
)


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
        return True

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
