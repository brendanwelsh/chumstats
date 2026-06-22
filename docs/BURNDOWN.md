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

`chumstats.com` (macmini) runs **old code** — ~29 commits behind, and the rebrand
isn't pushed. Verified: chumstats.com `/` redirects to the owner profile, not the
neutral splash. So these reports are **deploy** problems, not code bugs — the fix
is already on the branch:

- Homescreen/splash "doesn't exist" → built in `f9650cc` (neutral splash + quick-jump chips).
- Map/arena names "not correct" → `arenas.py` corrected in `b905395` (deploy fixes the mapped ones).
- Console/platform icons "huge" → **CONFIRMED deploy-only**: caps are correct in current
  code (`svg.plat-ic` 16px, `.sf-chip-ic svg` 20px, overlay `.plat-ico` 12px). Deploy fixes it.
- Player cards not stacked → `9f3fd97` collapsible cards.

## [BLOCKED — needs user OK] Deploy the branch to chumstats.com (macmini)

Push `rebrand-chumstats`; on the macmini run `deploy/server/install.sh`. This
**renames the live launchd service** (`com.ballshark.server` → `com.chumstats.server`),
migrates the data dir, and restarts. Outward-facing change to the live public
site → requires explicit go-ahead before executing.

---

## [code] genuine fixes to grind (loop works these)

- [x] ~~**Scaling/console icons**~~ — RESOLVED as deploy-only; caps verified correct in code (see above). No code change needed; deploy fixes the live site.
- [ ] **Heatmap: remove first touches** — kickoff dead-centre already excluded (`760f8ed`);
      extend to all kickoff first-touches if that's the intent.
- [ ] **Per-match touches = spot icons, not heatmap** — in the per-match view, render
      each touch as a discrete spot/marker; keep the density heatmap only for the
      aggregate/career view (too few touches per match for a meaningful heatmap).
- [ ] **Demo-location map** — investigate whether demo events carry x/y location in the
      captured data; if yes, add a demo map alongside the goal/shot maps.
- [ ] **Spatial-data gap analysis** — enumerate captured location data (ball + player
      positions, goals, shots, touches, demos); list what maps we *could* add and what's
      missing; write findings into this file.
- [ ] **Arena names: unverified ids** — give real names to `uf_*`, `mall_*`, `paname_*`,
      `stadium_10a_p`, `neotokyo_arcade_p` (currently title-case fallback). Needs a source.
- [ ] **A — persist game length** — add `regulation_seconds`/`overtime_seconds` (+ statfeed)
      columns to the `matches` schema + migration; persist in `save_match`.
- [ ] **B — identity PK migration** — `match_player_stats` PK → `(match_id, primary_id, team_num)` + migration.
- [ ] **C — stat-line consistency** — one shared score-first `STAT_COLUMNS` across all web tables.
- [ ] **D — filter consistency** — add `platform` filter to opponents/compare/clan/club; `window` to history.
- [ ] **E — multi-user reframe** — neutralize "Your line / Your insights" labels; make `/history` subject-parameterized (`?pid=`).

## Housekeeping

- [x] Push `rebrand-chumstats`; delete stale `origin/fix/local-portal-scope`. (done)

---

_Source of truth for the autonomous burndown loop. Update checkboxes as items land._
