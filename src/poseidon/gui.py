"""Desktop application window for the dashboard.

Poseidon's engine is a background service by design — an autonomous trader
must keep running when its window closes. The desktop app is therefore a
dedicated VIEW of the running engine, opened via ``poseidon app`` (and the
installed application-menu entry):

  1. pywebview, when installed (``pip install poseidon[gui]``) — a native
     GTK/Qt window, no browser involved;
  2. otherwise a Chromium-family browser in app mode (``--app=``) — its own
     window with no tabs or URL bar, indistinguishable from a native app;
  3. otherwise the default browser as a last resort.

If the engine is not running, ``poseidon app`` tries to start the systemd
user service, then explains what to do rather than opening a dead window.
"""

from __future__ import annotations

import shutil
import subprocess
import time

import httpx

_APP_BROWSERS = (
    "chromium", "chromium-browser", "google-chrome-stable", "google-chrome",
    "brave", "brave-browser", "vivaldi-stable", "vivaldi", "microsoft-edge-stable",
)
_WINDOW_SIZE = (1440, 900)


def engine_running(url: str, timeout: float = 2.0) -> bool:
    try:
        # trust_env=False: this is a loopback probe — it must never be routed
        # through an HTTP(S)_PROXY from the environment.
        return httpx.get(f"{url}/api/status", timeout=timeout, trust_env=False,
                         follow_redirects=False).status_code < 500
    except httpx.HTTPError:
        return False


def try_start_service() -> bool:
    """Best-effort start of the systemd user service (works when the vault
    passphrase is provisioned as a systemd credential — docs/security.md)."""
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return False
    result = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [systemctl, "--user", "start", "poseidon"],
        capture_output=True, timeout=30, check=False,
    )
    return result.returncode == 0


def open_window(url: str, *, token_in_url: bool = False) -> int:
    """Open the dashboard as a desktop window. Blocks until closed (native
    window) or hands off to the browser process. Returns an exit code.

    Tradeoff (F019): the pywebview path loads ``url`` in-process, so a
    ``?token=`` in it never touches a command line. Both browser fallbacks —
    ``--app=`` (Popen) and the last-resort ``webbrowser.open`` — put ``url`` in
    the child's argv, where the token is world-readable via /proc/<pid>/cmdline
    until the window closes. ``token_in_url`` lets the caller flag that case so
    we warn the operator and steer them to the leak-free native window.
    Eliminating the argv exposure entirely needs an out-of-band handoff
    (one-time token -> cookie) touching the server, the SPA and the websocket —
    disproportionate to this low-severity, loopback-default risk."""
    try:
        import webview  # optional dependency: poseidon[gui]
    except ImportError:
        webview = None
    if webview is not None:
        try:
            window_args = {"width": _WINDOW_SIZE[0], "height": _WINDOW_SIZE[1]}
            webview.create_window("Poseidon", url, **window_args)
            webview.start()
            return 0
        except Exception as exc:  # missing GTK/Qt backend, no display, …
            print(f"native window unavailable ({exc}); falling back to a browser window")
    if token_in_url:
        # Reached only when the native window is unavailable: the auth token is
        # about to ride the browser's argv (visible via /proc/<pid>/cmdline to
        # other local UIDs) for the life of the window. Covers both the --app
        # Popen and the webbrowser.open fallbacks below.
        print(
            "WARNING: no native window available, so the dashboard opens in a browser "
            "process with the auth token in its command line — readable via "
            "/proc/<pid>/cmdline by other local users until the window closes. "
            "Install the native window ('pip install poseidon[gui]') to avoid this."
        )
    for name in _APP_BROWSERS:
        binary = shutil.which(name)
        if binary:
            subprocess.Popen(  # noqa: S603 — fixed argv, no shell
                [binary, f"--app={url}",
                 f"--window-size={_WINDOW_SIZE[0]},{_WINDOW_SIZE[1]}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return 0
    import webbrowser

    webbrowser.open(url)
    return 0


def launch(url: str, token: str | None = None) -> int:
    """The `poseidon app` entry point: ensure the engine is up, open the window."""
    if not engine_running(url):
        print("Poseidon engine is not running — trying the systemd user service…")
        if try_start_service():
            for _ in range(30):
                if engine_running(url):
                    break
                time.sleep(0.5)
    if not engine_running(url):
        print(
            "Could not reach the Poseidon engine.\n"
            "Start it first with one of:\n"
            "  poseidon run                          # foreground, this terminal\n"
            "  systemctl --user start poseidon       # background service\n"
            "(For the service to start without a terminal, store the vault\n"
            " passphrase as a systemd credential — see docs/security.md.)"
        )
        return 1
    if token:
        from urllib.parse import quote

        url = f"{url}/?token={quote(token, safe='')}"
    return open_window(url, token_in_url=bool(token))
