# Chumstats central server — deploy

Run this on an **always-on host** (a spare Mac/Linux box, NAS, mini PC, etc.).
It accepts match-summary uploads from each player's client, dedupes them, and
serves one unified group dashboard. Reach it privately over a VPN/LAN (e.g.
Tailscale) and/or publicly via a Cloudflare Tunnel.

These instructions use **launchd** (macOS). On Linux, swap the LaunchAgent for a
systemd unit running the same `chumstats serve` command.

## Architecture recap

```
   client A                 client B                 client C
 chumstats run            chumstats run            chumstats run
       │                        │                        │
       │  POST /api/v1/match-summary   X-Chumstats-Key: <key>
       └────────────────────────┴────────────────────────┘
                                │
                                ▼
                  central server  (`chumstats serve`)
                  ─ /api/v1/match-summary (auth via key)
                  ─ /dashboard, /player/<name>, /history
                  ─ central.db (group view)
                                │
                                ▼
                  Cloudflare Tunnel → stats.<your-domain>
                  VPN / LAN         → <server-host>:5050
```

The server doesn't ingest from RL — `chumstats serve` runs the FastAPI app and
(optionally) the Discord bot, nothing else. RL → server data flow is *always*
via client uploads.

## One-time install

```bash
ssh <your-server>
git clone https://github.com/brendanwelsh/chumstats.git ~/chumstats
cd ~/chumstats
./deploy/server/install.sh
```

The installer:
- Installs `uv` (userspace, no sudo) and fetches **CPython 3.12** — macOS ships
  an old system Python (3.9), so we don't depend on it or on Homebrew.
- Creates `~/chumstats/.venv` and `uv pip install -e .[server,bot]`
- Drops a starter `.env` from `.env.example` (edit before starting — keep
  `CHUMSTATS_SERVER_HOST=0.0.0.0` so the LAN/VPN can reach it)
- Writes `~/Library/LaunchAgents/com.chumstats.server.plist` and loads it
- Launches `chumstats --db ~/chumstats/data/central.db serve` at login + on crash

> **LAN/VPN works immediately — no domain required.** Once the service is up,
> reach it from any device on the same network (or VPN) at
> `http://<server-host>:5050/dashboard`. The Cloudflare Tunnel section below is
> only for public access from a real domain.

Edit `~/chumstats/.env` with your `CHUMSTATS_PUBLIC_URL` and optional Discord
vars, then reload:

```bash
launchctl kickstart -k "gui/$(id -u)/com.chumstats.server"
tail -f ~/chumstats/server.log
```

## Cloudflare Tunnel (public access)

```bash
brew install cloudflared        # or your platform's package
cloudflared tunnel login
cloudflared tunnel create chumstats
cloudflared tunnel route dns chumstats stats.<your-domain>
cp deploy/server/cloudflared-config.yml.example ~/.cloudflared/config.yml
# edit ~/.cloudflared/config.yml — fill in tunnel UUID + username
sudo cloudflared service install
```

After this, `https://stats.<your-domain>` reaches the server on port 5050.

## Provision yourself (and friends)

On the server, generate one API key per player:

```bash
~/chumstats/.venv/bin/chumstats --db ~/chumstats/data/central.db admin create-user \
  --primary-id 'Steam|7656...|0' \
  --name '@YourName'
# prints a 64-char API key — capture it

~/chumstats/.venv/bin/chumstats --db ~/chumstats/data/central.db admin list-users
```

Hand each player their API key out-of-band (e.g. Discord DM). They paste it into
their local `.env`:

```
CHUMSTATS_REMOTE_URL=https://stats.<your-domain>   # or http://<server-host>:5050 on a VPN/LAN
CHUMSTATS_API_KEY=<the-64-char-key-you-gave-them>
RL_PLAYER_PRIMARY_ID=Steam|...|0    # already required for the local pipeline
```

Then their next `chumstats run` automatically uploads each finalized match.

## Backfill existing matches

From a client (where `data/chumstats.db` lives):

```powershell
$env:CHUMSTATS_REMOTE_URL = "https://stats.your-domain.com"
$env:CHUMSTATS_API_KEY = "<your-key>"
.\.venv\Scripts\python.exe -m chumstats.cli --db data\chumstats.db push-history `
  --primary-id "Steam|7656...|0" --dry-run
# review the count, then re-run without --dry-run
```

Idempotent — safe to rerun. Server uses INSERT OR IGNORE on matches.

## Admin / lifecycle

| Action            | Command |
|-------------------|---------|
| View logs         | `tail -f ~/chumstats/server.log` |
| Restart           | `launchctl kickstart -k "gui/$(id -u)/com.chumstats.server"` |
| Stop              | `launchctl bootout "gui/$(id -u)/com.chumstats.server"` |
| List users        | `~/chumstats/.venv/bin/chumstats --db ~/chumstats/data/central.db admin list-users` |
| Update code       | `cd ~/chumstats && git pull && launchctl kickstart -k "gui/$(id -u)/com.chumstats.server"` |

## Auth model (what's protected, what's not)

- **`/api/v1/match-summary` POST** — requires valid `X-Chumstats-Key`; rejects
  attempts to claim another user's `primary_id` (403).
- **Everything else** (`/dashboard`, `/player/<name>`, `/history`, `/clan`,
  `/club/<name>`) — read-only HTML, NO auth. Anyone reaching the host can see
  group stats. If you want it private, put it behind Cloudflare Access (or keep
  it VPN-only).
- **Admin actions** (`chumstats admin create-user`, `list-users`) — CLI-only,
  not exposed as HTTP routes. Run over SSH on the server.

## Storage projection

Per match summary: ~36 KB. At ~50 matches/day across a friend group, ~5 GB after
10 years. SQLite handles this comfortably.
