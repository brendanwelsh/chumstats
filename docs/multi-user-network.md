# Multi-user Stats Network

Architecture for syncing every friend's local Chumstats tracker to a central
server. **The core is implemented and running**, not a spec — this doc is now
both the design rationale and a status record.

## Status (implemented vs. remaining)

| Piece | Status | Where |
|---|---|---|
| Central server (`chumstats serve`) | ✅ done, runs on an always-on host | `src/chumstats/server.py`, `cmd_serve` |
| Upload endpoint `POST /api/v1/match-summary` (key-auth, anti-impersonation) | ✅ done | `server.py`, `MatchSummaryUpload` |
| Client uploader | ✅ done | `src/chumstats/sync.py` (`MatchSyncer`) |
| Provisioning (`admin create-user` / `list-users`) | ✅ done | `cmd_admin_*` |
| Backfill (`push-history`) | ✅ done | `cmd_push_history` |
| Dedup by `MatchGuid` (first-writer-wins matches, per-user stat rows) | ✅ done | `store.py` upsert |
| Reachability over LAN/VPN (`<server-host>:5050`) | ✅ done | e.g. Tailscale MagicDNS |
| Public domain via Cloudflare Tunnel | ⏳ prepped, not deployed | `deploy/server/` |
| Postgres/Redis (below) | ❌ not done — runs on SQLite, which is plenty for one friend group | — |

The sketch below kept Postgres + Redis as the production target. In practice the
central server runs the **same SQLite schema** as the local client (`central.db`)
and that is more than enough at friend-group scale (~36 KB/match). The
"Architecture sketch" and "Server-side schema" sections are aspirational; treat
the rest as describing what actually ships.

## Goal

Every friend who runs `chumstats` locally automatically uploads their match
data to a central server. The central server:

- Dedupes matches when multiple friends played the same game.
- Aggregates a unified leaderboard across the whole friend group.
- Renders public/private dashboards under `stats.yourdomain.com`.

## Hard constraints (carried forward from the local design)

- **No third-party data services.** Server is self-hosted by the owner.
- **Friends opt in** to sharing. Local-only mode must keep working.
- **Match summaries only get pushed** — raw tick events stay on the client.
  Keeps server DB small.
- **Authoritative match ID is the RL `MatchGuid`.** Every participant's
  tracker observes the same GUID. First writer wins for the `matches` row;
  other writers add their `match_player_stats` row.

## Architecture sketch

```
┌────────────────┐  ┌────────────────┐  ┌────────────────┐
│ Friend A laptop │  │ Friend B laptop │  │ Friend C laptop │
│  chumstats run    │  │  chumstats run    │  │  chumstats run    │
└─────────┬───────┘  └─────────┬───────┘  └─────────┬───────┘
          │                    │                    │
          │   HTTPS POST  /api/v1/match-summary    │
          │   X-Chumstats-Key: <api_key>             │
          └────────────────────┴────────────────────┘
                                │
                                ▼
                  ┌─────────────────────────────────┐
                  │  stats.yourdomain.com (FastAPI) │
                  │  + Postgres + Redis cache       │
                  └─────────────────────────────────┘
                                │
                                ▼
                  ┌─────────────────────────────────┐
                  │  Unified web UI                 │
                  │   /                             │
                  │   /history (filtered to "you")  │
                  │   /leaderboard (group)          │
                  │   /clans / /opponents (group)   │
                  └─────────────────────────────────┘
```

## Identity & auth

- Owner runs `chumstats admin create-user <discord_id>` on the server.
- Server returns an **API key** (UUIDv4) and a **user_id**.
- User adds it to their local `.env` as `CHUMSTATS_API_KEY=...` and
  `CHUMSTATS_REMOTE_URL=https://stats.yourdomain.com`.
- Every upload carries `X-Chumstats-Key: <api_key>`.
- Server stores `(user_id, primary_id_steam_or_epic)` — so when an upload
  carries a `match_player_stats` row for `primary_id = "Steam|765...|0"`,
  the server checks the API key's owner matches the primary_id.
- **Prevents impersonation**: friend A can't upload a row claiming to be
  friend B even if they were in the same match.

## Upload protocol

Client calls `POST /api/v1/match-summary` on each `MatchEnded + MatchDestroyed`:

```json
{
  "match_id": "DEF164DE11F1524796454BB971A4B02F",
  "started_at": 1779060422.84,
  "ended_at": 1779060900.12,
  "arena": "mall_day_p",
  "team0_score": 5, "team1_score": 3,
  "team0_name": "501st", "team1_name": "TJI NOOBS FIRST DAY FF",
  "winner_team_num": 0,
  "is_online": true,
  "crossbar_hits": 2,
  "my_row": {
    "primary_id": "Steam|76561197985273611|0",
    "name": "@ChumtheWaters",
    "team_num": 1,
    "score": 633,
    "goals": 4, "assists": 0, "saves": 0, "shots": 7, "demos": 0,
    "ticks_total": 14400, "ticks_in_air": 3168, "ticks_on_wall": 432,
    "ticks_supersonic": 1152, "boost_used": 2358, "speed_sum": 17280000,
    "is_mvp": false
  },
  "ball_touches": [ {"t": 12.3, "x": 1200, "y": -500, "z": 95, ... }, ... ],
  "goal_events": [ ... ]
}
```

Server logic:

1. Find or create row in `matches` by `match_id`. First write wins; later
   writes only update `crossbar_hits` if the client value is higher
   (different observers may have different crossbar counts depending on
   timing).
2. UPSERT `match_player_stats` for `(match_id, primary_id)` — each user
   only writes THEIR OWN row.
3. Server denies writes where `my_row.primary_id` doesn't match the API
   key's registered `primary_id`. Rejects 403.

## Dedup heuristic

When friends A and B play a match together:
- Both see `MatchGuid = X`
- A uploads `(X, A.row)`. Server creates `matches[X]` + `match_player_stats[X, A]`.
- B uploads `(X, B.row)`. Server sees `matches[X]` exists, skips its
  insert; UPSERTs `match_player_stats[X, B]`.
- Server now has ONE `matches` row + TWO `match_player_stats` rows. Perfect.

If A and B see DIFFERENT GUIDs (shouldn't happen, but if RL's API drifts):
fallback dedup by `(started_at within 30s, team0_name, team1_name)` — but
flag the row for manual review on the admin page.

## Server-side schema

Reuse the existing local schema. Postgres for production:

```sql
-- Users
CREATE TABLE users (
    user_id     UUID PRIMARY KEY,
    discord_id  TEXT UNIQUE,
    primary_id  TEXT UNIQUE,         -- Steam|... or Epic|...
    display_name TEXT NOT NULL,
    api_key     TEXT UNIQUE NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Matches / match_player_stats / etc. — same schema as local SQLite,
-- with FKs to users(user_id) on the *_stats rows.
```

## Privacy / permissions

- **Group-public by default.** Everyone in the friend group sees everyone
  else's stats.
- Per-user opt-in for full public visibility (`/public/<display_name>`).
- Opt-out leaves stats local-only — that user never uploads.

## Rollout plan

1. Buy domain, point at a small VPS (Hetzner / Linode, $5/mo).
2. Deploy a thin FastAPI server with just the two endpoints
   (`/api/v1/match-summary`, `/api/v1/auth`).
3. Local client gets a new module `chumstats/sync.py` that hooks the
   existing `on_match` callback and POSTs to the remote.
4. New unified web frontend (likely reuse 90% of the existing templates
   but the data layer is now the central DB).
5. Migrate the owner's existing 60+ matches first to validate the upload
   path, then invite friends one at a time.

## Open questions

- **Schema migrations.** If we evolve the local schema, how do clients
  with older versions upload?
- **Backfill from older laptops.** A friend installing chumstats today
  loses their prior matches forever. Worth supporting an
  `import-jsonl-capture` migration tool.
- **Real-time sync of in-progress matches.** Out of scope for v1 - only
  push on `MatchEnded`. Could add WebSocket-based "live group view" later.
- **Replay store.** The owner could choose to also archive `goal_events`
  + `ball_touches` per match to enable group-level heatmaps / chemistry
  analysis. Optional opt-in per match.

## NON-goals

- We are **not** rebuilding ballchasing.com. No public match search,
  no third-party API, no replay file uploads.
- We are **not** running a SaaS. This serves one friend group on one
  owner-controlled domain.
