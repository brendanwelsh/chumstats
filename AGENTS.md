# RLStats - context

## What & why
Self-hosted Rocket League stats tracker (product name **Ballshark**; package `ballshark`; the dir/repo are historically "carball-tracker" / RLStats). It reads from the local TCP Stats API that Rocket League itself opens (`127.0.0.1:49123` when `PacketSendRate` is set in `DefaultStatsAPI.ini`), persists every event to a local SQLite DB, and fans out to three surfaces: a Discord match-end embed, a browser/OBS overlay, and a local LAN dashboard. **Zero third-party data sources** (no ballchasing, tracker.gg, or Psyonix REST) — the only outbound traffic is your own Discord bot and optional upload to a self-hosted central server. Local-first is the point, not a side feature.

## Where it fits
Single-owner + friend-group tool, not a SaaS/public leaderboard/multi-tenant app. Two deployment shapes: (1) the owner runs `ballshark run` on their Windows gaming PC (full pipeline + dashboard); (2) friends run a packaged system-tray app (`ballshark-tray.pyw`, "friend mode": overlay-only, auto-uploads match summaries) that pushes to a central host. The central host is a Mac Mini running `ballshark serve` (no RL ingest, receives uploads, serves the unified dashboard, exposed via cloudflared). See `deploy/macmini/` and `deploy/windows/`.

## Run / build / test
Python >= 3.11. From repo root (PowerShell):
- Install: `python -m venv .venv` then `.\.venv\Scripts\python.exe -m pip install -e .[dev,server,bot]` (extras: `server`, `bot`, `tray`, `dev`).
- Enable Stats API: `python -m ballshark.cli setup` (detects RL install, edits `DefaultStatsAPI.ini`; restart RL after).
- Configure: copy `.env.example` -> `.env`, fill Discord token/channel + `RL_PLAYER_NAME` / `RL_PLAYER_PRIMARY_ID`.
- Run live: `python -m ballshark.cli run` (flags: `--no-bot --no-server --no-sync --no-prune`). Central host: `ballshark serve`.
- Tests: `.\.venv\Scripts\python.exe -m pytest tests/` (runs against real captures + golden fixtures).
- Lint: `ruff` (config in `pyproject.toml`, line-length 110).
- Backfill/dev without RL: `ballshark replay <file.jsonl>`, or record raw with `capture.ps1` / `capture.py`.

## Layout & key files
- `src/ballshark/cli.py` — `ballshark` entry point; subcommands run/serve/replay/reprocess/push-history/stats/dashboard/match/compare/player(s)/vs/setup/post-test/admin.
- `src/ballshark/models.py` — pydantic types for every observed Stats API event.
- `src/ballshark/ingest.py` — TCP client, brace-aware JSON splitter, reconnect.
- `src/ballshark/session.py` — MatchAggregator + SessionTracker (W/L, streak, derived metrics).
- `src/ballshark/store.py` — SQLite (matches, match_player_stats, match_extras, raw_events, users); prune/backfill/reaggregate.
- `src/ballshark/server.py` — FastAPI + WebSocket overlay/dashboard backend; `src/ballshark/overlay/` is the HTML/CSS/JS.
- `src/ballshark/bot.py` — discord.py poster + embed builder. `sync.py` — uploads to central server. `config.py` — env/.env loader.
- `tray.py`, `tray_config.py`, `tray_wizard.py`, `ballshark-tray.pyw` — friend tray app. `config_wizard.py` — RL-install detection + ini editing.
- `deploy/` — macmini (plist/cloudflared) + windows (PyInstaller `.spec`, build.ps1) packaging. `tests/` — pytest + `golden/` fixtures. `.design/`, `DESIGN.md`, `PRODUCT.md` — design system + product spec. Root `*.bat` — convenience launchers.

## Gotchas
- Stats API ini is read only at RL launch — restart RL after `setup`.
- Envelopes are `{"Event":..,"Data":"<json-string>"}`; the `Data` field is JSON-encoded — **parse twice**.
- Stats API does NOT emit MMR/rank, per-tick player XYZ, or full opponent movement stats — out of scope by design; don't chase replay-parser parity.
- Bots are `PrimaryId == "Unknown|0|0"`; online vs offline is distinguished by MatchGuid presence. Bot matches are filtered by default everywhere.
- Goal events arrive duplicated via replay echo — dedupe is intentional (see golden test).
- Legacy `CARBALL_*` env vars and `~/.carball` dir are still honored / auto-migrated (rename to Ballshark is recent); friend installs use `%LOCALAPPDATA%\ballshark\`.
- Default DB is `~/.ballshark/ballshark.db`; server binds `0.0.0.0:5050` by default (LAN-exposed) — set `BALLSHARK_SERVER_HOST=127.0.0.1` for loopback only.
- Tick firehose (UpdateState) is pruned from `raw_events` after a retention window (`tick_keep_days`, default 14); `reprocess` only touches matches still inside that window.
- Secrets live only in `.env` (gitignored) and `*.example` templates with placeholders — never commit real tokens/API keys. PRODUCT.md hard rules: spell out stat names (no G/A/Sv/Sh/D in UI), no em dashes, square corners, specific fonts.

## Status
Active; on branch `feat/ballshark-rebrand` (rename carball-tracker -> Ballshark plus multi-user sync server, system-tray friend app, analytics/dashboard, PyInstaller packaging). Version 0.0.1, pre-release. Core ingest/aggregation/persistence/Discord/overlay are working with a passing pytest suite; central-server sync and tray distribution are the newest, still-stabilizing pieces.
