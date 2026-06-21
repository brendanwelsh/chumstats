# Chumstats revamp — RL-correctness + multi-user + UX pass

Grounded in a 20-agent research/audit pass (real RL scoring/stat/boost/time rules
from RLBot + ballchasing + RL wiki) cross-checked against the codebase. **Every
numeric/unit claim from the audit is re-verified against the real DB before any
change** — the audit got several unit claims wrong (see below), so nothing is
applied blind.

## Ground-truth corrections (verified against real tick payloads)

- **`Speed` is km/h, not uu/s.** It tops out at **82.8 km/h = 2300 uu/s** (max car
  speed). Supersonic (2200 uu/s) = **79.2 km/h**. The audit's "set threshold to
  2200" would have made supersonic *always zero*. Real fix: `80.0 → 79.2`.
- **The live SUPERSONIC chip was permanently dead** — `cli.py` compared Speed
  (km/h, max ~83) against `2200`. Fixed to the shared threshold.
- **`Boost` is 0–100** (max exactly 100), not 0–255 — no rescale needed.
- **`GoalSpeed` is already km/h** (p50 79, max 142) — the "kph" label is correct.
- **Arena table**: research could not read ballchasing's token-gated `/api/maps`,
  so unverified ids (`paname_*`, `uf_*`, `mall_*`, `stadium_10a_p`,
  `neotokyo_arcade_p`) are left to a logged title-case fallback, not guessed.

## Done (committed on `fix/ingest-drain-decouple`)

- `fix(stats)` — supersonic threshold→79.2 (km/h) + dead live chip + shooting%
  `n/a` (no >100%) + `/compare` `None>=1000` crash + Forfeit on game-clock.
  Bumped `AGGREGATOR_VERSION`→2 (optional reprocess to re-derive in-window).
- `fix(arenas)` — single `arenas.py`; corrected names (street_p→Sovereign
  Heights, etc.); live view stops leaking raw ids; unknown ids logged.
- `feat(nav)` — OBS overlay + How-it-works moved to right-side buttons.
- (earlier) Discord per-game links + clean masked text.

## Remaining workstreams (priority order)

### P0 — correctness + public safety
- **XSS hardening.** RL display names are attacker-controllable; the real vector
  is unescaped name *text* (e.g. `>{name}</a>`), not the `onclick` rows (those are
  URL-`quote()`d). Needs a careful sweep + a malicious-name render test. **Next.**
- **Persist game-clock / OT / statfeed / crossbar.** The aggregator computes
  `regulation_seconds`/`overtime_seconds`/statfeed but `save_match` drops them;
  after the 14-day raw prune they're unrecoverable. Schema add + migration.
- **Identity = `primary_id`.** PK is `(match_id, name, team_num)` but dedup keys on
  `primary_id`; a rename dupes a row and a name-collision can roll back an upload.
  Migrate PK + `INSERT OR IGNORE` + route profiles by pid.
- **Heatmap normalization.** Touches are fixed-radius splats scaled by global
  count → a single touch renders fully hot. Replace with a binned 2D histogram +
  log/sqrt density normalized to per-map max.

### P1 — reframe + UX
- **Multi-user reframe.** Parameterize `/history`, `/opponents`, `/clan`, club,
  insights by subject; neutralize "you/us/them" so every uploader is first-class.
- **Splash + friend shortcuts.** `chumstats.com` opens on a neutral splash with
  quick-jump chips (2toes, Blazed, Vex, LLOL, owner — config-driven pids), not the
  owner profile.
- **Match-detail + history UX.** Kill owner-framed/redundant counts; per-player
  jump anchors; clickable players; collapsible teammates; swappable ball charts.
- **Stat-line consistency.** One `STAT_COLUMNS` (score-first, matching the Discord
  embed); reserve "Possession" for the time-weighted model vs "Touch share".
- **Filter consistency.** Several sections ignore active mode/platform/window.

### P2
- Stat-labeling honesty (movement/boost N/A for sampled opponents; air taxonomy).
- Upload-merge + reaggregation safety.

## Open questions (mostly resolved from data)

- ✅ Speed=km/h, Boost=0–100, GoalSpeed=km/h — all resolved above.
- ⏳ Friend chip primary_ids for the splash (2toes/Blazed/Vex/LLOL) — resolve from
  DB or make config-driven.
- ⏳ Unverified arena ids — verify against ballchasing `/api/maps` (token needed).
- ⏳ Per-viewer identity: is "self" only the startup-configured owner? Bounds how
  far "You won/lost" can go vs staying neutral.
