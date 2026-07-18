# Installation

Poseidon targets CachyOS (Arch-based) and works on any modern Linux with
Python 3.11+.

## Option A — one-command installer (recommended)

```bash
git clone https://github.com/St3althWarri0r/Poseidon
cd Poseidon
./install.sh
```

The installer creates a dedicated virtualenv at
`~/.local/share/poseidon/venv`, installs the package *editable* (so
self-update via git works), links `~/.local/bin/poseidon`, writes the starter
config to `~/.config/poseidon/poseidon.yaml`, installs the systemd user
unit and desktop entry, and runs `poseidon doctor`.

## Option B — native Arch/CachyOS package

```bash
cd packaging
makepkg -si
```

Installs system-wide (`/usr/bin/poseidon`) with the systemd user unit at
`/usr/lib/systemd/user/poseidon.service`. Note: the native package
does not support `poseidon update apply` (pacman owns the files); update by
rebuilding the package.

## Option C — Docker

```bash
mkdir -p docker/secrets
printf '%s' 'your-vault-passphrase' > docker/secrets/vault_passphrase.txt
printf '%s' "$(openssl rand -hex 32)" > docker/secrets/dashboard_token.txt
chmod 600 docker/secrets/vault_passphrase.txt docker/secrets/dashboard_token.txt
docker compose -f docker/docker-compose.yml run --rm poseidon vault init
docker compose -f docker/docker-compose.yml run --rm poseidon vault set anthropic_api_key
docker compose -f docker/docker-compose.yml up -d
```

State lives in the `poseidon-data` volume; the dashboard binds to
`127.0.0.1:8321` on the host. `dashboard_token.txt` is the dashboard
bearer token (the container binds 0.0.0.0, so token auth is mandatory) —
open `http://127.0.0.1:8321/?token=<token>` or send
`Authorization: Bearer <token>`. The passphrase entered at `vault init`
must be exactly the contents of `docker/secrets/vault_passphrase.txt`:
the service unlocks the vault non-interactively from that file
(`POSEIDON_VAULT_PASSPHRASE_FILE`), and a mismatch leaves the container
in a restart loop with "wrong passphrase or corrupt vault".

Use the `discord`/`telegram`/`email`/`webhook` notification channels in
Docker (there is no desktop notification daemon in a container).

## First-run checklist

```bash
poseidon vault init                      # encrypted credential store
poseidon vault set anthropic_api_key     # console.anthropic.com
poseidon vault set finnhub_api_key       # each provider you enabled
$EDITOR ~/.config/poseidon/poseidon.yaml
poseidon config validate
poseidon doctor                          # everything green?
poseidon run                             # foreground first run
```

Dashboard: <http://127.0.0.1:8321>.

## 24/7 operation (systemd user service)

The service needs the vault passphrase without a prompt. Store it as an
encrypted systemd credential (see docs/security.md for details):

```bash
systemd-creds encrypt --user --name=poseidon-vault-passphrase \
    <(printf '%s' 'YOUR-PASSPHRASE') \
    ~/.config/poseidon/vault-passphrase.cred
systemctl --user daemon-reload
systemctl --user enable --now poseidon
loginctl enable-linger $USER    # keep running after logout / before login
journalctl --user -u poseidon -f
```

The unit uses `Type=notify` with `WatchdogSec=180` — Poseidon feeds the
watchdog from its health loop, so a hung process is restarted
automatically, and `Restart=always` covers crashes.

## Updating

- `poseidon update check` — fetch and report.
- `poseidon update apply` — fast-forward pull + reinstall (installer layout
  only), then `systemctl --user restart poseidon`.
- Automatic checks run on launch and daily; **auto-apply is on by default**
  (fast-forward pull + reinstall, then a restart notification — the running
  engine is never restarted for you). Set `updates.auto_apply: false` to be
  notified of updates without applying them.

## Uninstalling

```bash
systemctl --user disable --now poseidon
rm -rf ~/.local/share/poseidon ~/.local/bin/poseidon
rm -rf ~/.config/poseidon          # includes the vault — keys are gone
rm ~/.config/systemd/user/poseidon.service
```
