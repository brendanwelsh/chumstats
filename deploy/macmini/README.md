# Ballshark central server — welsh-macmini deploy

The central server runs on welsh-macmini, accepts match-summary uploads from
friends' clients, and serves a unified group dashboard. Reachable internally
via Tailscale and publicly via Cloudflare Tunnel.

## Architecture recap

```
 Friend A laptop          Friend B laptop          Friend C laptop
 ballshark run              ballshark run              ballshark run
       │                        │                        │
       │  POST /api/v1/match-summary  X-Ballshark-Key: <key>
       └────────────────────────┴────────────────────────┘
                                │
                                ▼
                  welsh-macmini  (`ballshark serve`)
                  ─ /api/v1/match-summary (auth via key)
                  ─ /dashboard, /player/<name>, /history
                  ─ central.db (group view)
                                │
                                ▼
                  Cloudflare Tunnel → stats.<your-domain>
                  Tailscale         → 100.x.x.x:5050 (admin LAN)
```

The Mac Mini doesn't ingest from RL — `ballshark serve` runs the FastAPI app
and (optionally) the Discord bot, nothing else. RL → server data flow is
*always* via friend uploads.

## One-time install

```bash
ssh welsh-macmini                                  # Tailscale SSH (key auth)
git clone https://github.com/brendanwelsh/ballshark.git ~/ballshark
cd ~/ballshark
./deploy/macmini/install.sh
```

The installer:
- Installs `uv` (userspace, no sudo) and fetches **CPython 3.12** — macOS ships
  Python 3.9 and this Mac mini has no Homebrew, so we don't use system Python.
- Creates `~/ballshark/.venv` and `uv pip install -e .[server,bot]`
- Drops a starter `.env` from `.env.example` (edit before starting — make sure
  `BALLSHARK_SERVER_HOST=0.0.0.0` so the tailnet can reach it)
- Writes `~/Library/LaunchAgents/com.welsh.ballshark.plist` and loads it
- Launches `ballshark --db ~/ballshark/data/central.db serve` at login + on crash

> **Tailnet-only works today — no domain required.** Once the service is up,
> reach it from any tailnet device at `http://welsh-macmini:5050/dashboard`.
> The Cloudflare Tunnel section below is only needed for public access from a
> real domain.

Edit `~/ballshark/.env` with your real `BALLSHARK_PUBLIC_URL` and Discord vars,
then reload:

```bash
launchctl kickstart -k "gui/$(id -u)/com.welsh.ballshark"
tail -f ~/ballshark/server.log
```

## Cloudflare Tunnel (public access)

```bash
brew install cloudflared
cloudflared tunnel login
cloudflared tunnel create ballshark
cloudflared tunnel route dns ballshark stats.<your-domain>
cp deploy/macmini/cloudflared-config.yml.example ~/.cloudflared/config.yml
# edit ~/.cloudflared/config.yml — fill in tunnel UUID + username
sudo cloudflared service install
```

After this, `https://stats.<your-domain>` reaches the Mac Mini on port 5050.

## Provision yourself (and friends)

On the Mac Mini, generate one API key per friend:

```bash
~/ballshark/.venv/bin/ballshark --db ~/ballshark/data/central.db admin create-user \
  --primary-id 'Steam|76561197985273611|0' \
  --name '@ChumtheWaters'
# prints a 64-char API key — capture it

~/ballshark/.venv/bin/ballshark --db ~/ballshark/data/central.db admin list-users
```

Hand each friend their API key out-of-band (Discord DM). They paste into
their local `.env`:

```
BALLSHARK_REMOTE_URL=https://stats.<your-domain>
BALLSHARK_API_KEY=<the-64-char-key-you-gave-them>
RL_PLAYER_PRIMARY_ID=Steam|...|0    # already required for the local pipeline
```

Then their next `ballshark run` automatically uploads each finalized match.

## Backfill your existing matches

From your Windows PC (where `data/ballshark.db` lives):

```powershell
$env:BALLSHARK_REMOTE_URL = "https://stats.your-domain.com"
$env:BALLSHARK_API_KEY = "<your-key>"
.\.venv\Scripts\python.exe -m ballshark.cli --db data\ballshark.db push-history `
  --primary-id "Steam|76561197985273611|0" --dry-run
# review the count, then re-run without --dry-run
```

Idempotent — safe to rerun. Server uses INSERT OR IGNORE on matches.

## Admin / lifecycle

| Action            | Command |
|-------------------|---------|
| View logs         | `tail -f ~/ballshark/server.log` |
| Restart           | `launchctl kickstart -k "gui/$(id -u)/com.welsh.ballshark"` |
| Stop              | `launchctl bootout "gui/$(id -u)/com.welsh.ballshark"` |
| List users        | `~/ballshark/.venv/bin/ballshark --db ~/ballshark/data/central.db admin list-users` |
| Update code       | `cd ~/ballshark && git pull && launchctl kickstart -k "gui/$(id -u)/com.welsh.ballshark"` |

## Auth model (what's protected, what's not)

- **`/api/v1/match-summary` POST** — requires valid `X-Ballshark-Key`; rejects
  attempts to claim another user's `primary_id` (403).
- **Everything else** (`/dashboard`, `/player/<name>`, `/history`, `/clan`,
  `/club/<name>`) — read-only HTML, NO auth. Anyone reaching the public
  hostname can see group stats. If you want it private, put it behind
  Cloudflare Access (one-click in the CF dashboard).
- **Admin actions** (`ballshark admin create-user`, `list-users`) — CLI-only,
  not exposed as HTTP routes. Run via `ssh welsh-macmini`.

## Storage projection

Per match summary: ~36 KB. At ~50 matches/day across the friend group,
~5 GB after 10 years. The Mac Mini's SSD doesn't care.
