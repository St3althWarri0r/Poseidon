# Poseidon Desktop Launcher — Design (2026-07-13)

## Goal
A genuine double-click launcher on Linux: double-click an icon (Desktop or app
menu) and the platform comes up — guiding first-time setup, prompting for the
vault password, starting the engine, and opening the dashboard window — with no
terminal. Replaces the current `poseidon.desktop` that runs `poseidon app`,
which fails silently when the engine is down (`Terminal=false`, printed help
goes nowhere) and cannot unlock the vault without a terminal.

## Non-goals
- Not an AppImage/PyInstaller bundle — keeps the editable-venv + self-update model.
- Does NOT change trading mode. The engine starts in the configured mode
  (default: research + paper). The launcher never enables autonomous/live trading.
- Does NOT store the vault passphrase (user chose ask-each-cold-start).

## Behavior — `poseidon-launch`
1. Pick a GUI dialog backend: `zenity`, else `kdialog`, else a final stderr
   message. `notify-send` used for non-blocking progress when present.
2. Resolve config (host/port/data_dir) via `load_config`; compute the dashboard URL.
3. **First run** (vault file absent) → guided setup dialogs:
   - New vault passphrase (entered twice, min 8) → `Vault.create`.
   - Anthropic API key → `vault.set(ai.api_key_credential)`.
   - Optional broker/data keys (skippable).
   - Write the starter config if none exists.
4. **Engine probe**: GET `{url}/api/status` (loopback, `trust_env=False`).
   - Up → open the window.
   - Down → prompt vault passphrase → spawn `poseidon run` **detached** with
     `POSEIDON_VAULT_PASSPHRASE` in its environment → poll until up (~40s, with a
     progress popup) → open the window. On timeout/failure show an error popup
     pointing at the log path.
5. Open the window via `gui.launch(url, token)`.

## Security
- Passphrase reaches the engine via **environment only** (read by
  `Vault.unlock_from_environment`); never argv (invisible to `ps`), never a file.
  Cleared from the launcher's memory after spawn. Single-user-desktop threat
  model — same exposure as the existing passphrase-file path.
- Loopback-only probe with `trust_env=False` (no proxy routing).

## Components / files
- `src/poseidon/launcher.py` — bootstrap: pure logic core + thin dialog/subprocess seams.
- `src/poseidon/assets/poseidon.svg` — self-contained trident icon (+ PNGs at install).
- `packaging/poseidon.desktop` — `Exec=poseidon-launch`, `Icon=poseidon`.
- `pyproject.toml` — add the `poseidon-launch` console entry point; ship the asset.
- `install.sh` — install icon (hicolor), launcher, `.desktop` to the app menu AND
  `~/Desktop` (marked trusted/executable).
- `tests/unit/test_launcher.py` — unit tests for the core.

## Testable core (pure, TDD)
- `dashboard_url(config)` — host normalization, IPv6 bracketing, wildcard→loopback.
- `needs_first_run(vault)` — vault-absent detection.
- `wait_until_up(probe, deadline, sleep)` — poll-until-up with an injected probe.
- `engine_launch_env(passphrase, base_env)` — env dict with the passphrase, no mutation of the real env.
- `pick_dialog_backend(which)` — backend selection given a PATH-lookup seam.

## Verification
- New unit tests green; `ruff` + `mypy --strict` clean; existing 321-test gate unaffected.
- Full live start needs real keys + a display; that path is verified structurally
  and via a dry-run seam, with an explicit note on what was not run end-to-end.
