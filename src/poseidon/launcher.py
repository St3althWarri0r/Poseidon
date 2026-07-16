"""GUI double-click launcher for Poseidon (`poseidon-launch`).

The desktop entry point. On a double-click it brings the whole platform up with
no terminal: it guides first-time setup (vault + Anthropic key) through GUI
dialogs, prompts for the vault passphrase, starts the engine in the background,
and opens the dashboard window.

Design:
  * The passphrase reaches the engine through its ENVIRONMENT
    (``POSEIDON_VAULT_PASSPHRASE``, read by ``Vault.unlock_from_environment``) —
    never a file, never argv, so it cannot leak via ``ps``. It is dropped from
    this process once the engine is spawned.
  * The launcher never changes the trading mode. The engine starts in whatever
    the config says (default: research + paper broker); enabling autonomous or
    live trading stays a deliberate in-dashboard action.
  * Decision logic is pure module functions (unit-tested); the dialog and
    subprocess calls sit behind thin seams.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .proclife import ProcIdent, proc_starttime, same_process

if TYPE_CHECKING:
    from .core.config import AppConfig
    from .security.vault import Vault

_ENGINE_START_ATTEMPTS = 60  # up to ~30s at the default 0.5s interval
_PROBE_INTERVAL = 0.5


# ------------------------------------------------------------------ pure core

def dashboard_url(config: AppConfig) -> str:
    """The loopback URL of the dashboard. Wildcard binds resolve to loopback;
    a bare IPv6 literal is bracketed for the URL (mirrors ``poseidon app``)."""
    host = config.dashboard.host
    if host in ("0.0.0.0", "::"):  # noqa: S104 — display only, not a bind
        host = "127.0.0.1"
    elif ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{config.dashboard.port}"


def needs_setup(vault: Vault) -> bool:
    """First run: no vault yet, so there is nothing to unlock and no keys."""
    return not vault.exists


def engine_env(passphrase: str, base_env: Mapping[str, str]) -> dict[str, str]:
    """A child environment carrying the vault passphrase, WITHOUT mutating the
    caller's environment. ``Vault.unlock_from_environment`` reads this key."""
    env = dict(base_env)
    env["POSEIDON_VAULT_PASSPHRASE"] = passphrase
    return env


def wait_until_up(probe: Callable[[], bool], *, attempts: int,
                  sleep: Callable[[float], None], interval: float = _PROBE_INTERVAL) -> bool:
    """Poll ``probe`` up to ``attempts`` times, sleeping ``interval`` after each
    failed try, until it returns True. ``sleep`` is injected for testing."""
    for _ in range(attempts):
        if probe():
            return True
        sleep(interval)
    return False


def pick_dialog_backend(which: Callable[[str], str | None]) -> str | None:
    """The GUI dialog tool to use, preferring zenity, then kdialog."""
    for tool in ("zenity", "kdialog"):
        if which(tool):
            return tool
    return None


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


_PY_EXE = re.compile(r"^python(\d+(\.\d+)*)?$")
# NOTE: a replaced/upgraded interpreter reads as "python3.x (deleted)" and will not match — the port assertion in clear_running_engines() backstops this.


def is_engine_cmdline(exe: str, argv: list[str]) -> bool:
    """True only for the two exact engine spawn shapes — this predicate
    authorizes a kill, so matching is strictly positional: a stray
    ``... -m poseidon run`` argument tail must NOT match, and an
    interpreter-flag form (``python -O -m poseidon run``) is deliberately
    rejected too (the fresh-start port assertion backstops any engine the
    scan misses)."""
    if not _PY_EXE.match(Path(exe).name):
        return False
    if not argv:
        return False
    if Path(argv[0]).name == "poseidon" and argv[1:2] == ["run"]:
        return True
    if argv[1:4] == ["-m", "poseidon", "run"]:
        return True
    return argv[1:3] == ["-mposeidon", "run"]


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
            exe = str((entry / "exe").readlink())
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
    alive: Callable[..., bool] = same_process,
) -> bool:
    """The fresh-start pass: stop the systemd unit, kill every verified
    engine (pid file first, then the /proc scan), then assert the dashboard
    port went silent. True = clear to spawn; False = abort (dialog shown).

    ``engine.pid`` is unlinked immediately only when stale/garbage. A LIVE
    recorded engine keeps its file until its stop is CONFIRMED, so a pass that
    dies mid-kill (launcher OOM-killed, etc.) leaves the on-disk record of the
    still-live engine for the next launch to retry."""
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
    recorded_pid: int | None = None
    if recorded is not None and alive(recorded):
        targets[recorded.pid] = recorded
        recorded_pid = recorded.pid   # keep the file until this stop is confirmed
    else:
        pidfile.unlink(missing_ok=True)   # stale or garbage — never a reason to signal

    for eng in engines():
        targets.setdefault(eng.pid, eng)

    for ident in targets.values():
        cmdline = getattr(ident, "cmdline", "(from engine.pid)")
        stopped = stop(ident)  # type: ignore[arg-type]  # ProcIdent meets it at runtime
        _forensic(config, f"fresh-start kill pid={ident.pid} starttime={ident.starttime} "
                          f"stopped={stopped} cmdline={cmdline}")
        if stopped and ident.pid == recorded_pid:
            pidfile.unlink(missing_ok=True)   # confirmed stopped — consume the record

    # Port assertion: backstop for false negatives in is_engine_cmdline matchers
    # (interpreter-flag forms, (deleted)-suffix exe after upgrade)
    if probe(url):
        dialog.error(
            "An engine is still running that Poseidon could not stop, so a fresh "
            "one cannot be started.\n\nFind it with:  pgrep -af 'poseidon run'  "
            "then close it and launch again.")
        return False
    return True


# ------------------------------------------------------------- dialog backend

class Dialog:
    """Blocking GUI dialogs via zenity or kdialog. Each call shells out with a
    fixed argv (no shell); a non-zero exit means the user cancelled."""

    def __init__(self, backend: str) -> None:
        self._backend = backend

    @staticmethod
    def _run(args: list[str]) -> tuple[int, str]:
        proc = subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603
        return proc.returncode, proc.stdout.strip()

    def error(self, message: str) -> None:
        if self._backend == "zenity":
            self._run(["zenity", "--error", "--width=440", "--title=Poseidon", "--text", message])
        else:
            self._run(["kdialog", "--title", "Poseidon", "--error", message])

    def info(self, message: str) -> None:
        if self._backend == "zenity":
            self._run(["zenity", "--info", "--width=440", "--title=Poseidon", "--text", message])
        else:
            self._run(["kdialog", "--title", "Poseidon", "--msgbox", message])

    def question(self, message: str) -> bool:
        if self._backend == "zenity":
            rc, _ = self._run(
                ["zenity", "--question", "--width=440", "--title=Poseidon", "--text", message])
        else:
            rc, _ = self._run(["kdialog", "--title", "Poseidon", "--yesno", message])
        return rc == 0

    def password(self, prompt: str) -> str | None:
        """A hidden-text field. Returns the entry, or None if cancelled."""
        if self._backend == "zenity":
            rc, out = self._run(
                ["zenity", "--entry", "--hide-text", "--width=460",
                 "--title=Poseidon", "--text", prompt])
        else:
            rc, out = self._run(["kdialog", "--title", "Poseidon", "--password", prompt])
        return out if rc == 0 else None

    def entry(self, prompt: str) -> str | None:
        if self._backend == "zenity":
            rc, out = self._run(
                ["zenity", "--entry", "--width=520", "--title=Poseidon", "--text", prompt])
        else:
            rc, out = self._run(["kdialog", "--title", "Poseidon", "--inputbox", prompt])
        return out if rc == 0 else None


def _notify(summary: str, body: str = "") -> None:
    """Best-effort desktop notification for background progress."""
    exe = shutil.which("notify-send")
    if exe:
        with subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            [exe, "-a", "Poseidon", summary, body],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ):
            pass


# --------------------------------------------------------------- orchestration

def _engine_up(url: str) -> bool:
    from .gui import engine_running
    return engine_running(url)


def _ensure_starter_config(config: AppConfig) -> None:
    """Write the starter poseidon.yaml if the user has none, so they can edit it
    later. Mirrors ``poseidon config example`` asset resolution."""
    from .core.config import default_config_dir
    target = config.config_path or (default_config_dir() / "poseidon.yaml")
    if target.exists():
        return
    example = Path(__file__).resolve().parent / "config" / "poseidon.example.yaml"
    if not example.is_file():
        example = Path(__file__).resolve().parents[2] / "config" / "poseidon.example.yaml"
    if example.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")


def _first_run_setup(dialog: Dialog, config: AppConfig, vault: Vault) -> str | None:
    """Guided first-run: create the vault, take the Anthropic key, write a
    starter config. Returns the new passphrase (so the caller need not re-ask)
    or None if the user cancelled."""
    dialog.info(
        "Welcome to Poseidon.\n\nFirst-time setup creates your encrypted vault and "
        "takes your API keys. Keys are stored encrypted on THIS machine only — "
        "nothing is uploaded.")
    while True:
        p1 = dialog.password("Create a vault password (at least 8 characters):")
        if p1 is None:
            return None
        if len(p1) < 8:
            dialog.error("The password must be at least 8 characters.")
            continue
        p2 = dialog.password("Repeat the vault password:")
        if p2 is None:
            return None
        if p1 != p2:
            dialog.error("The passwords did not match — please try again.")
            continue
        break
    vault.create(p1)
    key = dialog.password(
        "Paste your Anthropic (Claude) API key.\nGet one at console.anthropic.com — "
        "leave blank to add it later.")
    if key:
        vault.set(config.ai.api_key_credential, key.strip())
    else:
        dialog.info(
            "No Anthropic key stored yet. Poseidon will still start, but AI review "
            "cycles need one — add it later from the dashboard Account view or with "
            "`poseidon vault set anthropic_api_key`.")
    _ensure_starter_config(config)
    dialog.info(
        "Setup complete.\n\nPoseidon starts in RESEARCH mode with the PAPER broker — "
        "it cannot place real orders until you deliberately change that from the "
        "dashboard. Starting Poseidon now…")
    return p1


def _start_engine(config: AppConfig, passphrase: str, dialog: Dialog, url: str) -> bool:
    """Spawn the engine detached (survives the launcher/window closing), with the
    passphrase in its environment, then wait for the dashboard to answer."""
    log_path = config.data_dir / "launcher-engine.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _notify("Starting Poseidon…", "Unlocking the vault and bringing the engine up.")
    with log_path.open("a", encoding="utf-8") as log_fh:
        subprocess.Popen(  # noqa: S603 — fixed argv (sys.executable -m poseidon), no shell
            [sys.executable, "-m", "poseidon", "run"],
            env=engine_env(passphrase, os.environ),
            stdin=subprocess.DEVNULL, stdout=log_fh, stderr=log_fh,
            start_new_session=True,
        )
    if wait_until_up(lambda: _engine_up(url), attempts=_ENGINE_START_ATTEMPTS, sleep=time.sleep):
        _notify("Poseidon is running.", "Opening the dashboard.")
        return True
    dialog.error(
        "Poseidon did not come up in time.\n\nThe vault password may be wrong, or a "
        f"key/config may be missing. See the log:\n{log_path}")
    return False


def _resolve_window_token(config: AppConfig, passphrase: str | None) -> str | None:
    """The dashboard bearer token for the window URL, if one is configured.
    Loopback default has none. When a token IS configured we reuse the
    passphrase already entered to read it from the vault."""
    from .core.config import dashboard_token_from_env
    token = dashboard_token_from_env()
    if token is None and config.dashboard.auth_token_credential and passphrase:
        from .security.vault import Vault
        vault = Vault(config.data_dir / "vault.bin")
        try:
            vault.unlock(passphrase)
            token = vault.get(config.dashboard.auth_token_credential)
        except Exception:  # noqa: BLE001 — a missing/locked token just means no token
            token = None
    return token


def main(argv: list[str] | None = None) -> int:
    from .core.config import load_config
    from .gui import launch
    from .security.vault import Vault

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

    vault = Vault(config.data_dir / "vault.bin")
    url = dashboard_url(config)
    passphrase: str | None = None

    if needs_setup(vault):
        passphrase = _first_run_setup(dialog, config, vault)
        if passphrase is None:
            return 1  # user cancelled setup

    if not _engine_up(url):
        if passphrase is None:
            passphrase = dialog.password("Enter your Poseidon vault password to start:")
            if not passphrase:
                dialog.error("No password entered — Poseidon was not started.")
                return 1
        if not _start_engine(config, passphrase, dialog, url):
            return 1

    token = _resolve_window_token(config, passphrase)
    return launch(url, token)


if __name__ == "__main__":
    raise SystemExit(main())
