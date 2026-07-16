# Launcher lifecycle — window-close kills the engine; every open is a fresh start

**Date:** 2026-07-15 · **Target:** v2.12.1 (patch) · **Scope:** `src/poseidon/launcher.py`, `src/poseidon/gui.py` (one new helper), `tests/unit/test_launcher.py` · **Engine code untouched.**

## Problem

Closing the Poseidon window (the `poseidon-launch` desktop app) leaves the engine
running forever, and the next launch silently *reattaches* to whatever engine is
already up. Real incident: a stale v2.8.0 engine kept trading for a day after the
window was closed and had to be found and `kill`ed by hand before the v2.12.0
relaunch.

Two mandated behaviors (user spec, verbatim intent):

1. **Window close = engine stop.** Hitting X must terminate the engine process
   immediately — same effect as `kill <engine-pid>` in a terminal.
2. **Every launch is a fresh start.** Kill any already-running engine first;
   never reuse one.

## Current mechanism (what changes)

- `_start_engine` spawns `[sys.executable, -m, poseidon, run]` with
  `start_new_session=True` — detached on purpose, PID discarded, so nothing can
  ever stop it.
- `main()` skips the start entirely when `_engine_up(url)` — the reuse branch.
- `gui.launch → open_window`: only the pywebview path blocks until the window
  closes. **Neither venv has pywebview installed**; the real path on this machine
  is the chromium-family `--app=` fallback (only vivaldi is installed), which
  `Popen`s and returns immediately — and if the user's browser is already
  running, the spawned process hands the window to it and exits at once. So
  today the launcher cannot even *see* the window close.
- `packaging/poseidon.service` has `Restart=always` (currently disabled on the
  user's machine): any engine we kill while that unit is *active* would be
  resurrected by systemd.

## Design

### D1. The launcher owns its engine (spawn → track → kill)

`_start_engine` keeps `start_new_session=True` (the engine gets its own process
group, so one `killpg` reaps the engine *and* any children, and a launcher crash
can never take the engine down mid-write — recovery is deterministic instead:
the next launch reaps it). New behavior:

- Return the `subprocess.Popen` handle (was `bool`); `None` on failure.
- Write `config.data_dir / "engine.pid"` after the spawn: JSON
  `{"pid": <int>, "starttime": <int>}` where `starttime` is field 22 of
  `/proc/<pid>/stat` (clock ticks since boot). PID + starttime uniquely
  identifies a process for the life of the boot — a recycled PID can never be
  mistaken for our engine.
- **Startup timeout now cleans up:** if the dashboard doesn't answer within the
  existing ~30 s window, kill the spawned engine's group (TERM → grace → KILL),
  remove the pid file, show the existing error dialog, return `None`. A failed
  boot must not leave a half-started orphan (today it does).

### D2. Fresh start: kill every running engine before spawning

New discovery + kill pass in `main()`, before `_start_engine` (the
`if not _engine_up(url)` reuse branch is **deleted** — we always start):

Discovery, three sources, deduped by PID, each candidate **verified** before any
signal is sent:

1. **Pid file** `data_dir/engine.pid` — trusted only if the recorded
   `starttime` still matches `/proc/<pid>/stat`; otherwise it is stale and is
   removed without killing anything.
2. **`/proc` scan** — any process of ours whose `/proc/<pid>/exe` basename
   starts with `python` and whose cmdline contains the adjacent tokens
   `…/poseidon` (basename) followed by `run`. This matches both spawn forms
   (`python -m poseidon run` and the `venv/bin/poseidon run` console script) and
   is what catches engines the launcher never started — the stale-v2.8.0
   incident class, terminal `poseidon run`s, and engines on a since-changed
   port. The exe-is-python requirement excludes false positives like an editor
   opened on files named `poseidon run`.
3. **systemd** — if `systemctl --user is-active poseidon` reports active, run
   `systemctl --user stop poseidon` (best-effort, like the existing
   `try_start_service`) *before* the kill pass, so `Restart=always` can't
   resurrect what we kill.

Kill sequence per engine, `stop_engine(pid, *, grace≈10 s)`:
`os.killpg(os.getpgid(pid), SIGTERM)` → poll for process exit → still alive →
`killpg(…, SIGKILL)` → confirm. The engine's own SIGTERM handler
(`app.py` `add_signal_handler → _shutdown.set`) gives it a graceful shutdown —
same as the manual `kill` the user performs today. Guards:

- `ProcessLookupError` anywhere = already dead = success.
- If `getpgid(pid)` equals **our own** process group (a mis-parented engine),
  fall back to `os.kill(pid, …)` — the launcher must never `killpg` itself.
- Only ever signal PIDs that passed verification above.

After the kill pass, spawn fresh (D1). Every launch therefore prompts for the
vault password — the price of "never reuse", and exactly what the user asked
for. Passphrase still travels ENV-only; the launcher still never touches
trading mode.

### D3. Window close is observable: the launcher's window blocks

New `gui.open_app_window_blocking(url, *, profile_dir, token_in_url) -> int`
used **only by the launcher** (`poseidon app` and its service-view semantics are
unchanged):

1. **pywebview**, if importable: `webview.start()` already blocks until the
   window closes. (Not installed today, but stays first for users who have it —
   it is also the no-token-in-argv path.)
2. **Chromium-family `--app=` window** (vivaldi here): spawn with a **dedicated
   profile** `--user-data-dir=<data_dir>/webview-profile` plus `--no-first-run
   --no-default-browser-check`. A dedicated profile forces a separate browser
   process even when the user's own browser is running, so the process lifetime
   *is* the window lifetime: `proc.wait()` blocks until X. This is the crux that
   makes behavior 1 implementable at all.
3. **Last resort** (`webbrowser.open` — no trackable window): call the injected
   `fallback_block()` callback; the launcher passes a blocking dialog — “Poseidon
   is running. Close this dialog to shut it down.” — honest degraded mode; on
   this machine it is unreachable (vivaldi exists).

The helper reports the spawned window process through an `on_spawn` callback
(the launcher stows the handle), so the `finally` cleanup can `terminate()` a
still-open window when the launcher itself is signalled — while the normal
path simply returns from `proc.wait()` when the user hits X.

`main()` structure becomes:

```
acquire singleton lock (D4)
… setup / password …
stop systemd unit if active; kill all discovered engines (D2)
proc = _start_engine(...)            # None → error dialog, exit 1
try:
    token = _resolve_window_token(...)
    rc = open_app_window_blocking(...)   # ← blocks while window is open
finally:
    _shutdown_engine(proc, pidfile)      # TERM → grace → KILL; rm pidfile; notify
return rc
```

`_shutdown_engine` is idempotent (second call is a no-op) and also
`terminate()`s the window process if it is still alive (signal path). SIGTERM /
SIGHUP handlers are installed to raise `SystemExit`, so killing the *launcher*
in a terminal runs the same `finally` cleanup; SIGINT already raises
`KeyboardInterrupt`. Only SIGKILL of the launcher can orphan the engine — and
the next launch reaps it via D2.

### D4. Singleton launcher with takeover

`data_dir/launcher.lock`, `fcntl.flock(LOCK_EX | LOCK_NB)`, lock file records
the launcher PID. If the lock is held, a second `poseidon-launch` **takes
over**: SIGTERM the recorded launcher (its `finally` closes its window and
engine), poll-acquire the lock for ≤15 s, then proceed with the normal fresh
start; if the lock never frees, error dialog and exit. No dialog on takeover —
"every open is a fresh start" is the user's stated model of the icon. The lock
is held for the launcher's whole life (flock releases automatically on process
death, so a SIGKILLed launcher can't wedge future launches).

## Alternatives considered

- **Engine self-terminates when no window is connected** (heartbeat/websocket
  presence): touches the engine and the SPA, and would kill headless
  `poseidon run` / systemd service deployments — the engine is *designed* to run
  windowless. Rejected.
- **Spawn the engine in the launcher's session/group** so it dies with the
  launcher: SIGKILL of the launcher still orphans it, terminal Ctrl+C semantics
  get messy, and it loses the clean one-killpg-reaps-all property. Rejected.
- **Port-owner discovery only** (who listens on 8321): misses engines on a
  changed port and needs `/proc/net/tcp` inode-matching for no gain over the
  cmdline scan; the actual incident engine is only reliably found by the scan.
  Rejected as the primary mechanism (the pid file already covers the common
  case precisely).
- **"Already running" dialog instead of takeover** (D4): safer-feeling but
  contradicts the mandate; the accidental-double-click cost is a clean
  restart, which is exactly the advertised semantics. Rejected.

## Invariants preserved (unchanged code paths asserted by tests)

- Passphrase reaches the engine via `POSEIDON_VAULT_PASSPHRASE` env only —
  never argv, never a file (`engine.pid` contains pid/starttime only).
- The launcher never changes trading mode; engine argv is byte-identical
  (`[sys.executable, -m, poseidon, run]`).
- `poseidon app` CLI behavior and `gui.launch`/`open_window` are untouched.
- Engine internals untouched — graceful shutdown is its existing SIGTERM path
  (which finalizes the audit trail itself; the launcher adds no audit writes).

## Testing (all launcher I/O stays behind seams, real vault crypto in main() tests)

Pure/unit: pid-file round-trip + stale rejection (starttime mismatch, dead pid,
garbage JSON); cmdline matcher (accepts both engine forms; rejects editor-like
argv, non-python exe); `stop_engine` sequencing with injected
`killpg/getpgid/kill/sleep` (TERM-then-exit → no KILL; refuses-to-die → KILL;
already-dead → success; own-pgid → `os.kill` fallback); lock takeover decision.

Faked-`main()` flow (existing style — real vault, faked dialogs/engine/window):

1. Engine already up → it is **killed** and a fresh one started (inverts
   `test_main_engine_already_up_skips_setup_and_start`), password prompted.
2. Window close → engine group TERMed, KILLed after grace if needed, pid file
   removed, notify sent.
3. Spawn-timeout → spawned group killed + pid file removed + error dialog.
4. `SystemExit`/signal during the window phase → `finally` still stops engine.
5. Second-launcher takeover → old launcher signaled, lock acquired, fresh start.
6. First-run setup unchanged (existing test still passes with the new flow).

Manual/E2E smoke before release (not CI): scratch `XDG_*` dirs + throwaway
vault/passphrase on a scratch port → real `python -m poseidon run` spawn →
`find_running_engines` sees it → `stop_engine` gracefully stops it; and a real
vivaldi `--app` window with the dedicated profile: `wait()` returns when the
window is closed.

## Residual risks (surfaced, accepted)

- **SIGKILL of the launcher** orphans the engine until the next launch (no
  cleanup can run). Deterministically self-heals at the next fresh start.
- **Last-resort browser path** (no chromium-family browser, no pywebview) can't
  observe the window; degraded to the blocking dialog. Unreachable on this
  machine.
- **Grace-period tradeoff:** a wedged engine gets SIGKILL after ~10 s — same
  risk profile as the manual `kill` the user already performs; the DB is
  WAL-journaled aiosqlite and the audit chain is verified at next startup.
- **Enabled systemd service:** we stop an *active* unit before killing, but a
  user who re-enables the service later gets an engine the *next* launch will
  stop again — the two ownership models don't mix; documented in the launcher
  docstring.
