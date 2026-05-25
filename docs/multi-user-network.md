# Multi-user Stats Network (Spec)

Long-term architecture for syncing every friend's local Carball tracker to a
central server hosted on the owner's domain. This is a SPEC — not yet
implemented. Drop here so we don't forget the design when we get to it.

## Goal

Every friend who runs `carball` locally automatically uploads their match
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
│  carball run    │  │  carball run    │  │  carball run    │
└─────────┬───────┘  └─────────┬───────┘  └─────────┬───────┘
          │                    │                    │
          │   HTTPS POST  /api/v1/match-summary    │
          │   X-Carball-Key: <api_key>             │
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

- Owner runs `carball admin create-user <discord_id>` on the server.
- Server returns an **API key** (UUIDv4) and a **user_id**.
- User adds it to their local `.env` as `CARBALL_API_KEY=...` and
  `CARBALL_REMOTE_URL=https://stats.yourdomain.com`.
- Every upload carries `X-Carball-Key: <api_key>`.
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
3. Local client gets a new module `carball/sync.py` that hooks the
   existing `on_match` callback and POSTs to the remote.
4. New unified web frontend (likely reuse 90% of the existing templates
   but the data layer is now the central DB).
5. Migrate the owner's existing 60+ matches first to validate the upload
   path, then invite friends one at a time.

## Open questions

- **Schema migrations.** If we evolve the local schema, how do clients
  with older versions upload?
- **Backfill from older laptops.** A friend installing carball today
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
