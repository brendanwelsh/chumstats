# Chumstats burndown — branch: `rebrand-chumstats`

## How the loop works (read this every fire)

**ACTIVE LOOP: `*/10 * * * *` ESPN scoreboard redesign (cron `344e94cc`).** Supersedes the
old 15-min burndown (`0d3a0624`, deleted — all finite items done). Each fire: pick ONE page
(rotate: match → player profile → players dir → results/history → opponents → clubs → compare)
and make it read like an **ESPN box score / ballchasing.gg** — full-width team-grouped tables,
stats in columns spanning the page (NOT split cramped panels / inline word-soup), team-vs-team
side-by-side, scoreboard readability, balanced. Test, commit (no AI trailer), deploy to
chumstats.com, screenshot to verify, log below. Also keep the 4-point validation sweep. Never
finishes — keep iterating balance changes.

## ESPN redesign log
- **Match · Players pane** — replaced cramped one-player-at-a-time panels (combat/activity/
  highlight word-soup) with full-width team-grouped box-score tables: *Involvement & positioning*
  (touches/share/thirds/demos/bars/goal%) + *Boost & movement* (BPM/boost/empty/full/spd/SS/
  ground/air/wall, "—" when no spectator coverage) + a touch-map grid. Verified live.
- **Match · structure** — nav chips now SWAP panes (overview/timeline/goalmap/us-vs-them/kickoff/
  players) instead of anchor-scroll; rosters side-by-side; all "YOU" framing removed; arenas
  normalized in the career per-arena breakdown.
- **Player profile** — replaced the weak radar (per-match avg scaled to a freak single-match
  peak → tiny blob) with 'Per-match averages vs the field' comparison bars (player avg, bar vs
  the best regular, tick at field avg, green when above). Fixed the `Blazed / Blazed` double-name.
- **Players directory** — already a clean full-width box score; added a leaderboard **rank (#)**
  column (ESPN-standings style), verified live. NOTE for a no-Chum pass: the RELATION column
  (teammate/opponent) is relative to the configured owner — owner-centric framing to revisit.
- **Results / history** — killed the dead gap between score and stat columns by adding a
  venue **Arena** column (normalized names) + capping the score-cell width, so each row reads
  as a balanced scoreboard line. Verified live.
- **Opponents** — already a clean box score, but it was hard-wired to the owner (owner-
  perspective: 'who Chum faced', Chum's W-L). Subject-parameterized it (`?pid=`/`?name=`) like
  /history; title reads '<name> — opponents' for a subject. Resolves the RELATION/opponents
  no-Chum flag at the source. Follow-up: add a discoverable link from player profiles.
- **Clubs (opposing clubs)** — already a box score; added a leaderboard **rank (#)** column and
  renamed the first-person **'Our goals' → 'Goals for'** (owner-tell). The page is still the
  owner-club's rivalries (CLUB RECORD etc.); full club-subject-param is a bigger follow-up.
- **Compare** — already a strong side-by-side (heatmaps + metric table per player); neutralized
  the owner framing: default to the top-3 most-played players (was owner-first), dropped the
  'Slot 1 (you)' label + 'defaults to you' copy. 0 you-leaks live.

**First full rotation complete** (match · profile · directory · history · opponents · clubs ·
compare). Loop now cycles back for deeper polish + continues the no-Chum sweep.

### Rotation 2
- **Player profile** — added quick-nav links (Matches / Opponents / Compare) to the player's
  subject-parameterized pages (`/history?pid=`, `/opponents?pid=`, `/compare?names=`). The
  subject-param features are now discoverable from any profile, not just the owner's. Verified live.
- **Validation sweep (data accuracy)** — caught lifetime air/wall/ground summing to ~97%
  (72/19/5) on the profile MOVEMENT section + compare table — same normalization bug as the
  match page, but the lifetime paths in analytics.py weren't fixed. Normalized both to the
  position-tick sum; now 74.4/20.1/5.6 = 100. Verified live.

### Still to repass (ESPN pass, top-down)
- [x] **Match · "Us vs them" → "Team comparison"** — DONE. Forced neutral Blue-vs-Orange always; nav chip "Teams"; also dropped the owner-perspective "vs your career" insights card. 0 us/them/your-career leaks live.
- [x] **Player profile** — radar→comparison-bars + double-name DONE. Follow-up: the 2-col detail-grid stat tables could go full-width box-score; the tiny WIN% sub-label tile.
- [ ] **Players directory / opponents / clubs / compare / results** — same box-score treatment.

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
- [x] **D — filter consistency** — DONE. Wired `window` + `platform` (opponent-platform EXISTS) on /history and `platform` on /opponents; compare/clan/club/live already suppress platform, so every shown filter now works. Verified filtering.
- [x] **E — multi-user reframe** — DONE. "Your line/insights" already neutralized (kill-Me item); `/history` now subject-parameterized (`?pid=`/`?name=`, title shows whose), and player-profile "view all" links there. Every uploader is first-class.

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
