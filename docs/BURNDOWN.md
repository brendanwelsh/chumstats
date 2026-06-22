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
- [x] **Filter overlap (left) + redundancy** — DONE. Converted the left filter rail to a
      compact top horizontal bar (ballchasing-style) — no left column, so the overlap is
      eliminated by construction. Removed the redundant inline mode/bots toolbars on
      history + opponents (now in the bar). Verified live across pages.
- [x] **Black-screen flash on navigation** — DONE. Theme was applied at end-of-body (paint
      dark, then switch). Now `data-theme` is set in a tiny `<head>` script before first paint
      + a `color-scheme` meta. No more flash.
- [x] **Scrollable 6-player selector** — DONE. Match detail per-player breakdown is now a
      scrollable tab selector + one visible panel (SPA, JS toggle, no reload); top-nav chips
      drive it. Collapsible `<details>` removed.
- [x] **Pressure & share always 50/50** — DONE. Root cause: uploaded matches batch all BallHits under one received_at, so time-interval possession/pressure collapsed (0% / 50-50). Recomputed from touch counts + positions ("Touch share" / "Field tilt"). Verified varied (38/62, 57/43).
- [x] **Touches-per-player half-bar** (match history) — DONE. Removed the per-row touch-share bar (read as half a bar chart); the list is cleaner, touch share/field tilt live on the match page.
- [x] **Ground/air/wall not summing to 100** — DONE. Normalize the three position
      categories to their own sum (shares of *classified* position time) so they always
      total 100% (was e.g. 68/0/10 = 78% when ~22% of a player's ticks lacked a clean
      classification — air also under-counted for the owner's own captured ticks).
> The **VALIDATION SWEEP (recurring)** now lives at the very bottom of this file — it runs
> AFTER the finite items (D/E) so it can't starve them. Never check it off; it keeps the loop alive.
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
- [ ] **D — filter consistency** — `window`→history DONE (route + query wired, verified filters). STILL TODO: `platform` (opponent-platform) filter on opponents/compare/clan/club (each needs an opp-platform EXISTS subquery).
- [ ] **E — multi-user reframe** — neutralize "Your line / Your insights" labels; make `/history` subject-parameterized (`?pid=`).

## Housekeeping

- [x] Push `rebrand-chumstats`; delete stale `origin/fix/local-portal-scope`. (done)

## ♻️ VALIDATION SWEEP (RECURRING — never check off; keeps the loop alive)

Reached only after the finite items above (D/E) are done, so it can't starve them. Each
15-min fire picks ONE page (rotate: splash → players → player profile → history → match
detail → opponents → clubs → compare), validates all four dimensions, then fixes + deploys
+ screenshots what it finds:

- [ ] (this stays unchecked forever — it's the standing mission)
  1. **Stat accuracy** — recompute a couple of the page's headline numbers straight from
     `data/central.db` (on the macmini) and diff against what the page renders; flag mismatches.
  2. **Correct/complete data** — no missing / placeholder / always-constant / not-summing-
     to-100 values. Fix or document.
  3. **No UI overlap / clunk** — Playwright screenshot at 1280–1500px; nothing overlaps,
     layout is tight (ballchasing-style).
  4. **No Chum/owner perspective** — grep the rendered page for `Me|you|your|our|us|@ChumtheWaters`;
     everything reads neutral (all-players).

  Log findings under "## Validation log" below. Re-screenshot to verify each fix.

## Validation log
_(loop appends per-page findings + fixes here)_

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
