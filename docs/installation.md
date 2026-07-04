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
chmod 600 docker/secrets/vault_passphrase.txt
docker compose -f docker/docker-compose.yml up -d
```

State lives in the `poseidon-data` volume; the dashboard binds to
`127.0.0.1:8321` on the host. Initialize the vault once inside the
container:

```bash
docker exec -it poseidon poseidon vault init
docker exec -it poseidon poseidon vault set anthropic_api_key
```

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
- Automatic checks run daily; set `updates.auto_apply: true` to apply
  without asking (a restart notification is sent either way).

## Uninstalling

```bash
systemctl --user disable --now poseidon
rm -rf ~/.local/share/poseidon ~/.local/bin/poseidon
rm -rf ~/.config/poseidon          # includes the vault — keys are gone
rm ~/.config/systemd/user/poseidon.service
```
