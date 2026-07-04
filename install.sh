#!/usr/bin/env bash
# Poseidon — one-command installer for CachyOS / Arch (and other Linux).
#
#   git clone https://github.com/St3althWarri0r/Poseidon && cd Poseidon && ./install.sh
#
# What it does:
#   1. verifies python >= 3.11 (offers pacman hints on Arch/CachyOS)
#   2. creates a dedicated venv at ~/.local/share/poseidon/venv
#   3. installs poseidon (editable, so `poseidon update apply` works)
#   4. writes the starter config and installs the systemd user service
#   5. runs `poseidon doctor`
#
# Native package alternative: cd packaging && makepkg -si

set -euo pipefail

BOLD=$(tput bold 2>/dev/null || true); RESET=$(tput sgr0 2>/dev/null || true)
say()  { printf '%s==>%s %s\n' "$BOLD" "$RESET" "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/poseidon"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/poseidon"
VENV="$DATA_DIR/venv"
BIN_DIR="$HOME/.local/bin"

# 1. Python check ------------------------------------------------------------
PY=python3
command -v "$PY" >/dev/null || die "python3 not found. On CachyOS/Arch: sudo pacman -S python"
"$PY" - <<'EOF' || die "Python 3.11+ required. On CachyOS/Arch: sudo pacman -S python"
import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)
EOF
say "Python OK: $($PY --version)"

if ! command -v notify-send >/dev/null; then
  say "note: libnotify (notify-send) not found — desktop notifications need it:"
  say "      sudo pacman -S libnotify"
fi

# 2. venv ---------------------------------------------------------------------
say "Creating virtualenv at $VENV"
mkdir -p "$DATA_DIR" "$CONFIG_DIR" "$BIN_DIR"
"$PY" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip

# 3. install ------------------------------------------------------------------
say "Installing poseidon (editable) from $REPO_DIR"
"$VENV/bin/pip" install --quiet -e "$REPO_DIR"
ln -sf "$VENV/bin/poseidon" "$BIN_DIR/poseidon"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) say "note: add $BIN_DIR to your PATH to use the 'poseidon' command directly" ;;
esac

# 4. config + service -----------------------------------------------------------
if [[ ! -f "$CONFIG_DIR/poseidon.yaml" ]]; then
  cp "$REPO_DIR/config/poseidon.example.yaml" "$CONFIG_DIR/poseidon.yaml"
  say "Wrote starter configuration to $CONFIG_DIR/poseidon.yaml"
else
  say "Keeping existing configuration at $CONFIG_DIR/poseidon.yaml"
fi

SYSTEMD_DIR="$HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_DIR"
sed "s|ExecStart=.*|ExecStart=$VENV/bin/poseidon run|" \
  "$REPO_DIR/packaging/poseidon.service" > "$SYSTEMD_DIR/poseidon.service"
systemctl --user daemon-reload 2>/dev/null || true
say "Installed systemd user service (not yet enabled)"

APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$APPS_DIR"
cp "$REPO_DIR/packaging/poseidon.desktop" "$APPS_DIR/"

# 5. doctor ----------------------------------------------------------------------
say "Running self-diagnostics"
"$VENV/bin/poseidon" doctor || true

cat <<EOF

${BOLD}Poseidon installed.${RESET} Next steps:

  1. Create the credential vault and add your keys:
       poseidon vault init
       poseidon vault set anthropic_api_key
       poseidon vault set polygon_api_key          # and other providers you enabled
  2. Review the configuration:
       \$EDITOR $CONFIG_DIR/poseidon.yaml
       poseidon config validate
  3. First run (foreground):
       poseidon run
     Dashboard: http://127.0.0.1:8321
     Desktop window (own app window, no browser chrome):
       poseidon app        # also in your application menu as "Poseidon"
  4. 24/7 operation (after storing the vault passphrase as a systemd credential —
     see docs/security.md):
       systemctl --user enable --now poseidon
       loginctl enable-linger \$USER     # keep it running after logout

Start in 'research' mode with the paper broker (the default) and read
docs/user-guide.md before enabling autonomous trading.
EOF
