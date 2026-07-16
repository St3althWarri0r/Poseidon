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

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING

from .proclife import ProcIdent, proc_starttime, same_process

if TYPE_CHECKING:
    from .core.config import AppConfig
    from .security.vault import Vault

_ENGINE_START_ATTEMPTS = 60  # up to ~30s at the default 0.5s interval
_PROBE_INTERVAL = 0.5
_ENGINE_STOP_GRACE = 10.0  # seconds of SIGTERM grace before SIGKILL


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
    takeover_timeout: float = 45.0,
    interval: float = 0.5,
    term: Callable[[int, int], None] = os.kill,
    alive: Callable[[ProcIdent], bool] = same_process,
    sleep: Callable[[float], None] = time.sleep,
    notify: Callable[[str, str], None] | None = None,
) -> IO[str] | None:
    """One launcher at a time, with takeover: if another launcher holds the
    lock, verify its recorded (pid, starttime) against /proc ON EVERY POLL and
    SIGTERM a verified holder — but only the FIRST time we see that identity,
    not on every poll: the victim's own teardown (window reap up to ~10s +
    engine TERM/KILL grace) takes real time, and re-signalling a process
    already tearing down is noise at best. An unverifiable holder is never
    signalled — its flock releases by itself when it exits. ``takeover_timeout``
    defaults wide enough (45s) to outlast that worst-case teardown so a
    slow-but-correct victim is not abandoned with a spurious "did not hand
    over" error. Returns the flocked handle (keep it for life; flock dies with
    the process, so a SIGKILLed launcher cannot wedge future launches) or None
    if the lock never freed."""
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
    signalled: ProcIdent | None = None
    while waited < takeover_timeout:
        holder = read_pidfile(lock_path)
        if holder is not None and alive(holder) and holder != signalled:
            with contextlib.suppress(ProcessLookupError):
                term(holder.pid, signal.SIGTERM)
            signalled = holder
        if _try_flock(fh):
            return _claim()
        sleep(interval)
        waited += interval
    fh.close()
    return None


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
        stopped = stop(ident)
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
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, sig)
        try:
            proc.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            continue


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


def _start_engine(
    config: AppConfig, passphrase: str, dialog: Dialog, url: str,
    *, on_spawn: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> subprocess.Popen[bytes] | None:
    """Spawn the engine as a TRACKED child (own session/group), record its
    identity in engine.pid, and wait for the dashboard. On failure the spawn
    is cleaned up — a failed boot must never leave a half-started orphan.

    ``on_spawn`` receives the child handle THE INSTANT it is forked — before
    the pid-file write and before the blocking dashboard wait below. This is
    the hand-off that makes the ~30s wait signal-safe: a SIGTERM/SIGHUP/SIGINT
    landing mid-wait unwinds ``main()`` through its ``finally``, which can only
    reap the engine if the handle already reached it (this function does not
    return until the wait finishes, so its return value is too late)."""
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
    if on_spawn is not None:
        on_spawn(proc)  # hand off at fork — must precede the blocking wait (signal safety)
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


_WINDOW_REAP_GRACE = 5.0


def _install_signal_handlers() -> None:
    """SIGTERM/SIGHUP become SystemExit so main()'s finally still stops the
    engine (KDE logout, terminal kill, a takeover's signal). SIGINT already
    raises KeyboardInterrupt. Installed FIRST in main(): the ~30s startup wait
    is the longest window in the launcher's life and must be covered."""

    def _bail(signum: int, _frame: object) -> None:
        # Latch: further SIGTERM/SIGHUP are ignored so the one SystemExit's
        # finally-teardown (window reap + engine kill) runs to completion — a
        # takeover re-signals every poll, and a second raise here would abort
        # teardown before the engine is killed.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
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
    then stop our engine, then drop the pid file iff it is still ours.

    The engine teardown lives in a ``finally`` around the window reap so it
    runs even if that reap is interrupted by ANY exception — including a
    ``KeyboardInterrupt`` (SIGINT/Ctrl+C during the up-to-~10s window.wait())
    or a ``SystemExit``, which the reap's ``except (OSError,
    TimeoutExpired)`` deliberately does NOT catch. The engine must still die;
    the interrupting exception then continues to propagate."""
    try:
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
    finally:
        # Only signal a LIVE engine. `_kill_own_engine` skips the (pid,
        # starttime) identity check on purpose — it assumes we still hold the
        # child's zombie so the pid can't be recycled. The failure path breaks
        # that assumption (`_start_engine` already reaped the child before
        # returning None), so an unguarded re-kill could `killpg` a recycled
        # pgid. Gate on liveness to close that window; the pidfile cleanup
        # stays UNCONDITIONAL so a stale record still recording our pid is
        # dropped even for a dead engine.
        if proc is not None:
            was_live = proc.poll() is None
            if was_live:
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
            # Cancelling here only skips THIS launcher's kill pass and the new
            # engine spawn below — by itself it stops nothing. If this launch
            # took over a prior launcher (acquire_launcher_lock, above), that
            # prior launcher's engine was already torn down by the takeover
            # BEFORE we ever reached this prompt — the intended "every launch
            # is a fresh start" semantics, not a side effect of cancelling now.
            dialog.error("No password entered — Poseidon was not started.")
            return 1

    if not clear_running_engines(config, dialog, url):
        return 1

    proc: subprocess.Popen[bytes] | None = None
    window: subprocess.Popen[bytes] | None = None
    pidfile = config.data_dir / _ENGINE_PIDFILE
    try:
        def _remember_proc(p: subprocess.Popen[bytes]) -> None:
            nonlocal proc
            proc = p

        # Bind proc via the callback, NOT the return value: _start_engine blocks
        # in its ~30s dashboard wait before returning, so a signal mid-wait would
        # unwind past `proc = ...` and orphan the just-spawned engine. on_spawn
        # binds it at fork; the return value is still checked for the fail path.
        if _start_engine(config, passphrase, dialog, url, on_spawn=_remember_proc) is None:
            return 1
        token = _resolve_window_token(config, passphrase)
        target = url
        if token:
            from urllib.parse import quote
            target = f"{url}/?token={quote(token, safe='')}"

        def _remember_window(p: subprocess.Popen[bytes]) -> None:
            nonlocal window
            window = p

        from .gui import open_app_window_blocking
        return open_app_window_blocking(
            target,
            profile_dir=config.data_dir / "webview-profile",
            token_in_url=bool(token),
            on_spawn=_remember_window,
            fallback_block=lambda: dialog.info(
                "Poseidon is running.\n\nClose this dialog to shut it down."),
        )
    finally:
        _shutdown_engine(proc, window, pidfile)
        # Deleting the last reference closes the file handle HERE, releasing
        # the flock at this exact point (end of the finally, after teardown) —
        # not merely "by process exit". That ordering is what keeps a new
        # launcher's takeover kill-pass correctly serialized behind this one's
        # teardown finishing first.
        del lock


if __name__ == "__main__":
    raise SystemExit(main())
