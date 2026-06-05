#!/bin/bash
# Ballshark central server installer for welsh-macmini (macOS).
#
# Run via Tailscale SSH:
#     ssh welsh-macmini
#     curl -fsSL https://raw.githubusercontent.com/<you>/RLStats/main/deploy/macmini/install.sh | bash
# or after cloning the repo:
#     ./deploy/macmini/install.sh
#
# Idempotent — rerun to update.

set -euo pipefail

REPO_URL="${BALLSHARK_REPO_URL:-https://github.com/welsh/RLStats.git}"
INSTALL_DIR="${BALLSHARK_INSTALL_DIR:-$HOME/ballshark}"
DATA_DIR="${BALLSHARK_DATA_DIR:-$INSTALL_DIR/data}"
VENV_DIR="$INSTALL_DIR/.venv"
PLIST_NAME="com.welsh.ballshark.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/$PLIST_NAME"

echo "==> ballshark server install"
echo "    INSTALL_DIR=$INSTALL_DIR"
echo "    DATA_DIR=$DATA_DIR"

# 1. Prereqs.
command -v python3 >/dev/null || { echo "ERROR: python3 not found. Install from python.org or via brew."; exit 1; }
command -v git     >/dev/null || { echo "ERROR: git not found. Install Xcode CLT: xcode-select --install"; exit 1; }
PYVER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "    python3 = $PYVER"
[[ "$PYVER" < "3.11" ]] && { echo "ERROR: need Python >= 3.11"; exit 1; }

# 2. Clone or pull.
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "==> updating existing checkout"
    git -C "$INSTALL_DIR" pull --ff-only
else
    echo "==> cloning $REPO_URL"
    git clone "$REPO_URL" "$INSTALL_DIR"
fi

# 3. Venv + install.
if [ ! -d "$VENV_DIR" ]; then
    echo "==> creating venv"
    python3 -m venv "$VENV_DIR"
fi
echo "==> installing ballshark + server + bot extras"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -e "$INSTALL_DIR[server,bot]"

# 4. Data dir + .env stub.
mkdir -p "$DATA_DIR"
if [ ! -f "$INSTALL_DIR/.env" ]; then
    echo "==> writing starter .env (REVIEW IT BEFORE STARTING)"
    cp "$INSTALL_DIR/deploy/macmini/.env.example" "$INSTALL_DIR/.env"
    echo "    -> $INSTALL_DIR/.env"
    echo "    EDIT THIS FILE before loading the launchd service."
fi

# 5. launchd plist with paths substituted.
echo "==> installing LaunchAgent $PLIST_DEST"
mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s|@VENV_DIR@|$VENV_DIR|g" \
    -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" \
    -e "s|@DATA_DIR@|$DATA_DIR|g" \
    "$INSTALL_DIR/deploy/macmini/$PLIST_NAME.template" > "$PLIST_DEST"

# 6. Stop existing (if any) then load.
launchctl bootout "gui/$(id -u)/com.welsh.ballshark" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST"
launchctl enable "gui/$(id -u)/com.welsh.ballshark"
echo "==> launchd: loaded com.welsh.ballshark"

cat <<EOF

================================================================
ballshark server installed.

Next steps:
  1. Edit $INSTALL_DIR/.env (set BALLSHARK_PUBLIC_URL, optional Discord vars).
  2. Provision yourself:
     $VENV_DIR/bin/ballshark --db $DATA_DIR/central.db admin create-user \\
       --primary-id 'Steam|76561197985273611|0' --name '@ChumtheWaters'
  3. From your Windows PC, paste the API key into .env on that machine
     as BALLSHARK_API_KEY, set BALLSHARK_REMOTE_URL=https://<your-domain>, then
         ballshark push-history --primary-id 'Steam|...|0'
     to backfill your existing matches.
  4. Wire up Cloudflare Tunnel (see deploy/macmini/cloudflared-config.yml.example).

Logs:    tail -f $INSTALL_DIR/server.log
Stop:    launchctl bootout gui/\$(id -u)/com.welsh.ballshark
Restart: launchctl kickstart -k gui/\$(id -u)/com.welsh.ballshark
================================================================
EOF
