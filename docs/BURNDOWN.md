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

**To RE-DEPLOY after future code lands (editable, no reinstall needed):**
```
ssh welsh-macmini 'cd ~/ballshark && git fetch origin rebrand-chumstats \
  && git checkout -B rebrand-chumstats FETCH_HEAD \
  && launchctl kickstart -k gui/$(id -u)/com.welsh.ballshark'
```
(Schema-changing items A/B migrate on startup. Reinstall only if deps/entry change.)
After the loop lands code items + pushes, run the above so chumstats.com updates.

---

## [code] genuine fixes to grind (loop works these)

- [x] ~~**Scaling/console icons**~~ — RESOLVED as deploy-only; caps verified correct in code (see above). No code change needed; deploy fixes the live site.
- [x] **Heatmap: remove first touches** — DONE. Sequence-tag kickoff first-touches
      (first non-replay BallHit after start + after each goal) in `_build_playback_data`;
      `_ball_heatmap_svg` drops tagged touches; lifetime keeps the centre-box fallback.
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
