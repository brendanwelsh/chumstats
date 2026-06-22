# Chumstats burndown — branch: `rebrand-chumstats`

## How the loop works (read this every fire)

Loop: `/loop 15min "complete all the burndown"` (cron job `0d3a0624`).
Each fire: read this file, pick the next unchecked **[code]** item top-down,
implement + test (`.venv/Scripts/python -m pytest -q`), commit, check it off here
(commit that too), repeat. **Do NOT auto-run items marked [BLOCKED — needs user OK].**
When all `[code]` items are done: push the branch, delete stale
`origin/fix/local-portal-scope`, then notify the user and `CronDelete 0d3a0624`.

---

## ⚠️ Root cause of most "still broken" reports: STALE DEPLOY

`chumstats.com` (macmini) WAS running ~29-commit-old code. **DEPLOYED now** (see
below) — these were all stale-deploy, not code bugs, and are now RESOLVED on the
live site:

- [x] Homescreen/splash "doesn't exist" → `f9650cc` splash now live (verified 200 + chips).
- [x] Map/arena names "not correct" → `arenas.py` (`b905395`) live. (Exception: the
      genuinely-unverified ids `uf_*`/`mall_*`/`paname_*`/etc. still title-case — see code item.)
- [x] Console/platform icons "huge" → caps live (`svg.plat-ic` 16px, `.sf-chip-ic svg` 20px, `.plat-ico` 12px).
- [x] Player cards not stacked → `9f3fd97` collapsible cards live.

## [x] DEPLOYED to chumstats.com (macmini) — done

Cut over in-place at `~/ballshark` (NOT via install.sh — the macmini uses a
hand-rolled `com.welsh.ballshark` editable deploy, not `com.ballshark.server`):
checked out `rebrand-chumstats`, `uv pip install -e .[server,bot]` (registers
`chumstats`), repointed the plist program `…/.venv/bin/ballshark` → `…/chumstats`,
kept the `--db ~/ballshark/data/central.db` path + `com.welsh.ballshark` label,
reloaded. Verified chumstats.com `/` = splash (200), `/dashboard` 200, Chumstats
brand, no "ballshark". Central DB backed up: `data/central.db.bak-rebrand`.

**RE-DEPLOY is USER-GATED — the loop does NOT auto-deploy.** Pushing new code to
the live public site each fire was (correctly) blocked by the auto-mode classifier:
the "Full rebrand deploy" OK covered the one-time cutover, not unattended redeploys.
So: the loop commits + pushes code only; deploying to chumstats.com is a separate,
user-approved batch. When the user OKs a deploy, run (editable, no reinstall):
```
ssh welsh-macmini 'cd ~/ballshark && git fetch origin rebrand-chumstats \
  && git checkout -B rebrand-chumstats FETCH_HEAD \
  && launchctl kickstart -k gui/$(id -u)/com.welsh.ballshark'
```
(Schema-changing items A/B migrate on startup. Reinstall only if deps/entry change.)

---

## ⚡ P0 — UI/UX overhaul + de-"Me" reframe (June 2026 audit; screenshots in `.shots/`)

Goal: an **all-matches / all-players** tracker like **ballchasing.gg** — tight, professional,
**no overlap**, **no "Me"/owner framing**, one-page feel. The loop's standing mission now
also includes **auditing every page's datasets for gaps each pass** and **re-screenshotting
to verify** (Playwright works; `.venv/Scripts/python` + chromium installed). Do these BEFORE
the older C/D/E below (E is subsumed by the reframe item).

- [x] **Kill all "Me"/owner framing** — DONE (core). Removed "Me" nav; brand → splash;
      `/dashboard` redirects to `/`; neutralized copy (Your insights/goals, you've faced,
      Your line, vs your average). Follow-ups: delete the dead `_dashboard_html(is_self)`
      path; `/history` still renders the configured owner (subject-param `?pid=` = old item E).
- [ ] **Filter overlap (left) + redundancy** — sidebar overlaps at the user's width (asked
      20+ times; width-specific — chips overflow when narrow). Harden: `min-width:0` +
      overflow guards on `.side-filters`/`.sf-chip`; verify 1200–1500px. Remove the REDUNDANT
      inline mode/bots filter row that duplicates the sidebar (seen on /opponents).
- [ ] **Black-screen flash on navigation** — dark `--bg` paints before content (FOUC). Add
      `<meta name="color-scheme" content="dark light">`, set html bg immediately, remove any
      bg transition that flashes.
- [x] **Scrollable 6-player selector** — DONE. Match detail per-player breakdown is now a scrollable tab selector + one visible panel (SPA, JS toggle, no reload); top-nav chips drive it. Collapsible <details> removed.
      scrollable segmented selector across the up-to-6 players; one-page-app layout.
- [ ] **Pressure & share always 50/50** — data bug; find the calc, fix or remove if not real.
- [ ] **Touches-per-player half-bar** (match history) — broken/half-rendered; fix or remove.
- [ ] **Data-gap audit (RECURRING)** — each pass, audit one page's datasets vs the DB; flag
      missing / placeholder / always-constant values; fix or document. Screenshot to verify.
- [x] **Club "1ST DAY PEWPING" noise** — removed the our-team-name suffix from the /clan title
      (it was the user's own RL club name leaking onto the opponent-clubs page).
- [x] **>200 matches** — history limit 200 → 2000 (proper pagination/infinite-scroll is the
      real follow-up for ballchasing-style scale).

## [code] genuine fixes to grind (loop works these)

- [x] ~~**Scaling/console icons**~~ — RESOLVED as deploy-only; caps verified correct in code (see above). No code change needed; deploy fixes the live site.
- [x] **Heatmap: remove first touches** — DONE. Sequence-tag kickoff first-touches
      (first non-replay BallHit after start + after each goal) in `_build_playback_data`;
      `_ball_heatmap_svg` drops tagged touches; lifetime keeps the centre-box fallback.
- [x] **Per-match touches = spot icons, not heatmap** — DONE. `_touch_spots_svg`
      renders one `.tspot` marker per touch (kickoff dropped) for the per-match roster
      mini-map; lifetime/career keeps the density heatmap.
- [x] **Demo-location map** — INVESTIGATED → **NOT POSSIBLE**. The `Demolish`
      `StatfeedEvent` payload is only `{EventName, Type, MainTarget, SecondaryTarget}`
      (attacker/victim names) — **no X/Y/Z**. Same for every StatfeedEvent (Save,
      Assist, EpicSave, Shot). RL's Stats API doesn't emit positions for these, so a
      demo map can't be built from captured data.
- [x] **Spatial-data gap analysis** — DONE (see "## Spatial data" below).
- **[BLOCKED — needs source]** ~~Arena names: unverified ids~~ (`uf_*`, `mall_*`,
      `paname_*`, `stadium_10a_p`, `neotokyo_arcade_p`) — web search (Liquipedia / RLStats /
      ballchasing) found no authoritative id→name mapping, and they're not in RLBot's dict.
      Per the project's no-guess principle, keep the title-case fallback (deployed, fine).
      Resolve later with a ballchasing `/api/maps` token. NOT auto-run by the loop.
- [x] **A — persist game length** — DONE. Added `regulation_seconds`/`overtime_seconds`
      columns + additive migration; persisted in `save_match` + upload path + sync payload.
      (Statfeed stays recoverable from kept raw_events — no column needed.) Migrates on deploy.
- [x] **B — identity PK migration** — DONE. `match_player_stats` re-keyed to
      `(match_id, primary_id, team_num)` via drift-proof rebuild (swap PK clause in live
      DDL, INSERT OR IGNORE). Idempotent; runs on startup. Tested: data preserved,
      same-name/diff-pid rows coexist, re-init no-ops. (Schema migration — runs on deploy.)
- [x] **C — stat-line consistency** — DONE. Added single-source STAT_COLUMNS + _stat_cols_th/_td; players directory now score-first with the full block; other tables already canonical.
- [ ] **D — filter consistency** — add `platform` filter to opponents/compare/clan/club; `window` to history.
- [ ] **E — multi-user reframe** — neutralize "Your line / Your insights" labels; make `/history` subject-parameterized (`?pid=`).

## Housekeeping

- [x] Push `rebrand-chumstats`; delete stale `origin/fix/local-portal-scope`. (done)

---

## Spatial data — what carries location, what's mappable

Captured `raw_events` types: BallHit, GoalScored, CrossbarHit, UpdateState (ticks),
StatfeedEvent, + lifecycle.

**Has location (X/Y):**
- `BallHit` → `Ball.Location` — **MAPPED** (touch heatmap + per-match spot map).
- `GoalScored` → `ImpactLocation` — **MAPPED** (goal map).
- `CrossbarHit` → `BallLocation` — available, **not mapped** (could add a crossbar map; minor).
- `UpdateState` ticks → `Game.Cars[].Location` (every player) + `Ball.Location` —
  available but tick-heavy and **pruned after ~14 days** (recent matches only). Could
  power a true player-positioning heatmap / ball-possession-zone map (bigger feature).

**No location (names only) → cannot be mapped:**
- `StatfeedEvent` (Demolish, Save, EpicSave, Assist, Shot, …) — attacker/victim names
  only. So **demo / save / assist location maps are impossible** from this data. (Shot
  *origin* is approximated via the pre-goal BallHit, not the Shot statfeed.)

**Optional net-new maps (need user OK — not auto-built by the loop):**
1. Crossbar-hit map — trivial; location already captured.
2. Player-positioning heatmap from `UpdateState` car positions — higher value, but
   tick-dependent (recent matches only) + heavier to compute/render.

---

_Source of truth for the autonomous burndown loop. Update checkboxes as items land._
