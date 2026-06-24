#!/bin/bash
# Chumstats central server installer (macOS; for Linux, use a systemd unit).
#
# Run over SSH on your always-on server host:
#     ssh <your-server>
#     curl -fsSL https://raw.githubusercontent.com/brendanwelsh/chumstats/main/deploy/server/install.sh | bash
# or after cloning the repo:
#     ./deploy/server/install.sh
#
# Idempotent — rerun to update.

set -euo pipefail

REPO_URL="${CHUMSTATS_REPO_URL:-https://github.com/brendanwelsh/chumstats.git}"
INSTALL_DIR="${CHUMSTATS_INSTALL_DIR:-$HOME/chumstats}"
DATA_DIR="${CHUMSTATS_DATA_DIR:-$INSTALL_DIR/data}"
VENV_DIR="$INSTALL_DIR/.venv"
PLIST_NAME="com.chumstats.server.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/$PLIST_NAME"

echo "==> chumstats server install"
echo "    INSTALL_DIR=$INSTALL_DIR"
echo "    DATA_DIR=$DATA_DIR"

# 1. Prereqs: git + a Python >= 3.11.
# macOS ships an old system python3 (3.9), so we use uv — a userspace tool (no
# sudo, no Homebrew required) that fetches a standalone CPython and builds the
# venv. This sidesteps the 3.9 floor without touching system Python.
command -v git >/dev/null || { echo "ERROR: git not found. Install Xcode CLT: xcode-select --install"; exit 1; }
UV="$HOME/.local/bin/uv"
if ! command -v uv >/dev/null && [ ! -x "$UV" ]; then
    echo "==> installing uv (userspace Python/venv manager)"
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
[ -x "$UV" ] || UV="$(command -v uv)"
echo "    uv = $("$UV" --version)"
echo "==> ensuring CPython 3.12 is available"
"$UV" python install 3.12

# 2. Clone or pull.
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "==> updating existing checkout"
    git -C "$INSTALL_DIR" pull --ff-only
else
    echo "==> cloning $REPO_URL"
    git clone "$REPO_URL" "$INSTALL_DIR"
fi

# 3. Venv + install (via uv, Python 3.12).
if [ ! -d "$VENV_DIR" ]; then
    echo "==> creating venv (python 3.12)"
    "$UV" venv --python 3.12 "$VENV_DIR"
fi
echo "==> installing chumstats + server + bot extras"
VIRTUAL_ENV="$VENV_DIR" "$UV" pip install -e "$INSTALL_DIR[server,bot]"

# 4. Data dir + .env stub.
mkdir -p "$DATA_DIR"
if [ ! -f "$INSTALL_DIR/.env" ]; then
    echo "==> writing starter .env (REVIEW IT BEFORE STARTING)"
    cp "$INSTALL_DIR/deploy/server/.env.example" "$INSTALL_DIR/.env"
    echo "    -> $INSTALL_DIR/.env"
    echo "    EDIT THIS FILE before loading the launchd service."
fi

# 5. launchd plist with paths substituted.
echo "==> installing LaunchAgent $PLIST_DEST"
mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s|@VENV_DIR@|$VENV_DIR|g" \
    -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" \
    -e "s|@DATA_DIR@|$DATA_DIR|g" \
    "$INSTALL_DIR/deploy/server/$PLIST_NAME.template" > "$PLIST_DEST"

# 6. Stop existing (if any) then load.
launchctl bootout "gui/$(id -u)/com.chumstats.server" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST"
launchctl enable "gui/$(id -u)/com.chumstats.server"
echo "==> launchd: loaded com.chumstats.server"

cat <<EOF

================================================================
chumstats server installed.

Next steps:
  1. Edit $INSTALL_DIR/.env (set CHUMSTATS_PUBLIC_URL, optional Discord vars).
  2. Provision yourself:
     $VENV_DIR/bin/chumstats --db $DATA_DIR/central.db admin create-user \\
       --primary-id 'Steam|7656...|0' --name '@YourName'
  3. From a client machine, paste the API key into .env on that machine
     as CHUMSTATS_API_KEY, set CHUMSTATS_REMOTE_URL=https://<your-domain>, then
         chumstats push-history --primary-id 'Steam|...|0'
     to backfill your existing matches.
  4. Wire up Cloudflare Tunnel (see deploy/server/cloudflared-config.yml.example).

Logs:    tail -f $INSTALL_DIR/server.log
Stop:    launchctl bootout gui/\$(id -u)/com.chumstats.server
Restart: launchctl kickstart -k gui/\$(id -u)/com.chumstats.server
================================================================
EOF
