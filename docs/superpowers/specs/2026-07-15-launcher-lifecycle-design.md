# Launcher lifecycle — window-close kills the engine; every open is a fresh start

**Date:** 2026-07-15 · **Target:** v2.12.1 (patch) · **Scope:** `src/poseidon/launcher.py`, `src/poseidon/gui.py` (one new helper), `tests/unit/test_launcher.py` · **Engine code untouched.** · **Advisor-reviewed:** APPROVE-WITH-CHANGES, all 13 findings incorporated below.

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
- `packaging/poseidon.service` has `Restart=always`, `RestartSec=10`,
  `TimeoutStopSec=45` (unit currently disabled on the user's machine): any
  engine we kill while that unit is anywhere in its lifecycle can be
  resurrected by systemd.
- The engine's SIGTERM path is graceful and audit-finalizing
  (`app.py` `add_signal_handler → _shutdown.set → stop()` →
  `audit.append("system","shutdown")`), and the engine **does** spawn child
  processes (updater git/pip, notification channel helpers) — so killing the
  engine's *process group* is load-bearing, not just tidy.

## Design

### Process identity — the primitive everything else uses

A process is identified by **(pid, starttime)**, where starttime is read from
`/proc/<pid>/stat` as: take the substring after the **last** `)`, split on
whitespace, index 19 (field 22 of the full line — comm may contain spaces and
parens, so naive splitting is wrong; unit-tested with a hostile comm like
`(evil ) 2 (x`). A recycled PID can never impersonate a recorded process. Every
signal decision below re-verifies identity immediately before signaling; the
microscopic verify→signal window that remains is an accepted residual (there is
no atomic verify+kill on Linux) and is documented in the code.

**killpg rule:** signal a process *group* only when `os.getpgid(pid) == pid` —
the target is its own group leader. This covers every legitimate engine form
(launcher spawn via `start_new_session=True`, terminal `poseidon run` job
leader, systemd service) and refuses to blast a shared foreign group when some
wrapper spawned the engine as a non-leader; non-leaders get plain `os.kill`.

### D1. The launcher owns its engine (spawn → track → kill)

`_start_engine` keeps `start_new_session=True` and now:

- Returns the `subprocess.Popen` handle (was `bool`); `None` on failure.
- Writes `config.data_dir / "engine.pid"` right after the spawn: JSON
  `{"pid": <int>, "starttime": <int>}`. The write tolerates the
  child-already-dead race (wrong passphrase kills the engine in ~2 s): if
  starttime is unreadable, record the spawn as dead and fall through to the
  failure dialog rather than crashing.
- **Startup wait fails fast and cleans up:** the existing ~30 s
  `wait_until_up` loop additionally polls `proc.poll()` each iteration —
  child died → immediate, accurate error dialog (today a wrong passphrase
  makes the user wait the full 30 s). At timeout with a *live* child, ask
  `dialog.question("Poseidon is still starting — keep waiting?")`; Yes →
  another wait round (no kill-retry livelock on cold boots), No → graceful
  TERM sequence on the spawned group, remove the pid file, error dialog,
  return `None`. A failed boot never leaves a half-started orphan.

### D2. Fresh start: kill every running engine before spawning

New discovery + kill pass in `main()`, before `_start_engine` (the
`if not _engine_up(url)` reuse branch is **deleted** — we always start):

0. **systemd first, unconditionally:** if `systemctl` exists, run
   `systemctl --user stop poseidon` with a subprocess timeout (60 s >
   `TimeoutStopSec=45`); stopping an inactive/unloaded unit is a cheap rc≠0
   no-op. This is deliberately *not* gated on `is-active` — a unit in its
   `RestartSec` auto-restart hold-off reports `activating`, and gating would
   let systemd resurrect an engine seconds after our kill pass. If the stop
   itself times out, error dialog and **abort the launch** (never spawn under
   a live unit).
1. **Pid file** `data_dir/engine.pid` — trusted only if the recorded
   `(pid, starttime)` still matches `/proc`; otherwise stale → removed, no
   signal sent.
2. **`/proc` scan** — same-UID processes matching either exact spawn shape:
   `basename(argv[0]) == "poseidon" and argv[1] == "run"` (console script), or
   argv containing the adjacent pair `-m poseidon` (or fused `-mposeidon`)
   followed by `run`. Positional matching only — a stray
   `python something.py poseidon run` argument tail must NOT match. This
   catches engines the launcher never started: the stale-v2.8.0 incident
   class, terminal `poseidon run`s, systemd's `ExecStart` form, engines on a
   since-changed port. Every kill logs the victim's full cmdline to
   `launcher-engine.log` for forensics.
3. **Kill sequence** per verified engine, `stop_engine(pid, starttime, *,
   grace≈10 s)`: re-verify identity → TERM (killpg iff group leader, else
   kill) → poll for **identity** death (pid gone *or* starttime changed — a
   recycled pid must read as dead, or the SIGKILL escalation could land on an
   innocent) → still the same process after grace → re-verify → KILL → confirm.
   `ProcessLookupError` anywhere = already dead = success.
4. **Post-pass assertion:** `_engine_up(url)` must now be **False**. If
   something unkillable/unmatched still answers on the dashboard port, show
   "an engine is still running that Poseidon could not stop" and **exit
   without spawning** — the alternative is a fresh engine whose uvicorn bind
   fails inside an asyncio task while its kernel trades headless against the
   old engine's dashboard: a brand-new stale-engine class. This assertion
   closes the whole class regardless of scan quality.

After the pass, spawn fresh (D1). Every launch therefore prompts for the vault
password — the price of "never reuse", exactly what the user asked for.
Passphrase still travels ENV-only; the launcher still never touches trading
mode. **Cancelling the password prompt exits before the kill pass runs** —
choosing not to launch must never stop a running engine (pinned by test).

### D3. Window close is observable: the launcher's window blocks

New `gui.open_app_window_blocking(url, *, profile_dir, token_in_url,
on_spawn, fallback_block) -> int`, used **only by the launcher**
(`poseidon app`, `gui.launch`, `open_window` are untouched):

1. **pywebview**, if importable: `webview.start()` blocks natively (also the
   no-token-in-argv path). Not installed today; stays first for users who add
   it.
2. **Chromium-family `--app=` window** (vivaldi here): spawn with a dedicated
   profile `--user-data-dir=<data_dir>/webview-profile` (created `0700`) plus
   `--no-first-run --no-default-browser-check`; `proc.wait()` blocks until the
   window closes. The dedicated profile forces a separate browser process —
   **but only when no process already owns that profile** (chromium
   ProcessSingleton). Two defenses:
   - **Pre-spawn profile sweep:** kill any same-UID process whose cmdline
     contains `--user-data-dir=<our profile dir>` — the dir is exclusively
     ours, so any holder is a dead launcher's orphaned window and safe to kill
     by construction.
   - **Hand-off detection:** if `proc.wait()` returns within ~2 s, treat it as
     a ProcessSingleton hand-off, sweep the profile holder again, respawn once.
   The spawned window process is reported through `on_spawn` so the launcher's
   cleanup can close it.
3. **Last resort** (`webbrowser.open` — no trackable window): call
   `fallback_block()`; the launcher passes a blocking dialog — "Poseidon is
   running. Close this dialog to shut it down." Honest degraded mode;
   unreachable on this machine (vivaldi exists).

`main()` orchestration — **signal handlers are installed first thing** in
`main()` (SIGTERM/SIGHUP → raise `SystemExit`; SIGINT already raises
`KeyboardInterrupt`), and the spawn happens *inside* the `try`, because the
~30 s startup wait is the single longest window in the launcher's life and a
signal there (KDE logout, impatient user, a second launcher's takeover per D4)
must still stop the engine:

```
install signal handlers                      # BEFORE any engine may exist
acquire singleton lock (D4)
… setup / password (cancel ⇒ plain exit) …
systemd stop + kill pass + port assertion (D2)
proc = None; window = None
try:
    proc = _start_engine(...)                # None → error dialog, return 1
    token = _resolve_window_token(...)
    rc = open_app_window_blocking(..., on_spawn=capture into window)
finally:
    _shutdown_engine(proc, window, pidfile)  # no-op when proc is None
return rc
```

`_shutdown_engine` is idempotent (safe under the D1-timeout double-cleanup and
repeated signals), a **no-op when `proc` is None**, and does, in order:
window `terminate()` → `wait(≈5 s)` → `kill()` (the profile must be free
before the flock releases at process death, or the next launcher inherits the
hand-off problem); then the engine TERM → grace → KILL sequence via the same
identity-checked `stop_engine`; remove the pid file iff it still records our
engine; `_notify("Poseidon stopped.")`. Only SIGKILL of the launcher skips
this — and the next launch reaps the orphan via D2, including its orphaned
window via the profile sweep.

### D4. Singleton launcher with takeover

`data_dir/launcher.lock`, `fcntl.flock(LOCK_EX | LOCK_NB)`, lock file records
this launcher's `{"pid": …, "starttime": …}` (same identity format as
`engine.pid` — never a bare pid). If the lock is held, the second launcher
**takes over**:

- Read the lockfile and verify `(pid, starttime)` against `/proc` — **on every
  poll iteration**, immediately before any signal. Mismatch, garbage, or empty
  file ⇒ send nothing; just keep poll-acquiring (a flock held by an
  unverifiable holder resolves itself when the holder exits).
- SIGTERM the verified holder (its `finally` closes its window, engine, pid
  file), show `_notify("Restarting Poseidon…")` so the takeover isn't a silent
  pause, and poll-acquire the flock for **≥30 s** — the old launcher's worst
  legitimate case is real: engine TERM grace up to 10 s + engine `stop()`
  work + window reap ~5 s. On timeout: error dialog, exit.
- Lock held for the launcher's whole life; flock releases automatically on
  process death, so a SIGKILLed launcher can't wedge future launches.

## Alternatives considered

- **Engine self-terminates when no window is connected** (heartbeat/websocket
  presence): touches the engine and the SPA, and would kill headless
  `poseidon run` / systemd service deployments — the engine is *designed* to
  run windowless. Rejected.
- **Spawn the engine in the launcher's session/group** so it dies with the
  launcher: SIGKILL of the launcher still orphans it, terminal Ctrl+C
  semantics get messy, and it loses the one-killpg-reaps-all property (the
  engine has real children: updater, notifiers). Rejected.
- **Port-owner discovery only**: misses engines on a changed port and needs
  `/proc/net/tcp` inode matching for no gain over the cmdline scan. Rejected
  as primary (the pid file covers the common case precisely; the port
  *assertion* in D2.4 still backstops everything).
- **"Already running" dialog instead of takeover**: contradicts the mandate;
  an accidental double-click costs a clean restart, which is exactly the
  advertised semantics of the icon. Rejected.
- **systemd-credential mode** (passphrase provisioned as a systemd credential;
  launch = `systemctl --user restart poseidon`, close = `stop`): honors both
  mandates with *no password prompt*, but changes the deployment model and
  needs credential setup UX. Not now — noted as a future opt-in for the user.
- **"Reuse when engine version matches"**: not a legitimate alternative — the
  user said *never* reuse.

## Invariants preserved (asserted by tests)

- Passphrase reaches the engine via `POSEIDON_VAULT_PASSPHRASE` env only —
  never argv, never a file; `engine.pid` and `launcher.lock` bytes contain
  pid/starttime JSON only (negative test).
- Engine spawn argv is byte-identical `[sys.executable, "-m", "poseidon",
  "run"]` (captured and asserted).
- The launcher never changes trading mode.
- `poseidon app` CLI behavior and `gui.launch`/`open_window` are untouched.
- Engine internals untouched — graceful shutdown is its existing SIGTERM path
  (which appends the `shutdown` audit record itself; the launcher adds no
  audit writes).

## Testing

Pure/unit: starttime parse (hostile comm `(evil ) 2 (x`); pid-file round-trip +
stale rejection (starttime mismatch, dead pid, garbage JSON, passphrase-free
bytes); cmdline matcher — accepts `poseidon run` argv0-form, `-m poseidon run`,
fused `-mposeidon run`; rejects `python x.py poseidon run` tails, non-python
exe, non-adjacent tokens; `stop_engine` sequencing with injected
`kill/killpg/getpgid/starttime/sleep` seams — TERM-then-exit → no KILL;
refuses-to-die → KILL; already-dead → success; **pid recycled mid-grace (same
pid, new starttime) → treated as dead, no SIGKILL**; non-leader (`getpgid ≠
pid`) → `os.kill` not `killpg`; lockfile identity mismatch → no signal.

Faked-`main()` flow (existing style — real vault, faked dialogs/engine/window):

1. Engine already up → killed, fresh one started (inverts
   `test_main_engine_already_up_skips_setup_and_start`), password prompted.
2. Window close → engine group TERMed, KILLed after grace if needed, pid file
   removed, notify sent.
3. Spawn-timeout with dead child → immediate error, no 30 s wait; with live
   child + "No" → spawned group killed, pid file removed, error dialog.
4. `SystemExit`/signal during the window phase → `finally` stops engine **and
   terminates+reaps the window process**.
5. Signal during the *startup wait* (the blocker case) → engine still stopped.
6. Takeover: old launcher verified then signaled, lock acquired, fresh start;
   lockfile stale-identity → no signal sent.
7. Password cancel → exits **without** running the kill pass (a running engine
   survives a cancelled launch).
8. Scan-only engine (no pid file — crash between spawn and write) → found and
   killed.
9. Kill pass ran but port still answers → error dialog, **no spawn**.
10. First-run setup unchanged.
11. Spawn argv byte-identical assertion.

Manual/E2E smoke before release (not CI — the plan lists these as explicit
verify tasks): scratch XDG dirs + throwaway vault on a scratch port → real
`python -m poseidon run` spawn → discovery finds it → graceful stop; vivaldi
dedicated-profile first-ever launch (no onboarding window despite
`--no-first-run`; X → `proc.wait()` returns promptly) and second launch;
ProcessSingleton hand-off probe (pre-seeded profile holder → instant-exit →
sweep+respawn recovers); `poseidon run` under an occupied 8321 (calibrates the
D2.4 dialog text); KDE logout with window open → audit shows `shutdown`;
`stop()` timing under guardian/broker load (validates 10 s grace + 30 s
takeover); `systemctl is-active` output during auto-restart hold-off;
starttime-read race on an instantly-dead child (wrong passphrase).

## Residual risks (surfaced, accepted)

- **SIGKILL of the launcher** orphans engine + window until the next launch
  reaps both (kill pass + profile sweep). Deterministic self-heal.
- **verify→signal atomicity**: identity is re-checked immediately before each
  signal; the remaining microsecond window is not closable on Linux and is
  documented in code.
- **Last-resort browser path** (no chromium-family browser, no pywebview):
  degraded to the blocking dialog. Unreachable on this machine.
- **Grace-period tradeoff:** a wedged engine gets SIGKILL after ~10 s — same
  risk as the manual `kill` the user already performs; WAL-journaled aiosqlite
  + startup audit-chain verification cover it.
- **Token-in-profile:** with a configured dashboard token (non-loopback), the
  dedicated browser profile persists the `?token=` URL in history/session
  files under `data_dir` (0700). Extends the documented F019 argv tradeoff;
  loopback default (this machine) has no token.
- **Relaunch aborts an in-flight AI review cycle** (graceful SIGTERM
  mid-cycle): tokens/decision abandoned by design — "fresh start" is the
  user's chosen semantics; surfaced in the release notes.
- **Enabled systemd service:** we stop the unit unconditionally before the
  kill pass, but a user who re-enables and starts it later gets an engine the
  *next* launch will stop again — the two ownership models don't mix;
  documented in the launcher docstring.
