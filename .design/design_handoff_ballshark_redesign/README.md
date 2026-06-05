# Handoff: Ballshark — Dashboard + OBS Overlay Redesign

## Overview

This is a full visual redesign of **ballshark** (the self-hosted Rocket League stats tracker at `brendanwelsh/ballshark-tracker`). It covers two things the existing project already ships:

1. **The local dashboard** served by `src/ballshark/server.py` — match list, match detail (with per-player radars), player profiles, lifetime stats.
2. **The OBS browser-source overlay** in `src/ballshark/overlay/{overlay.html, overlay.css, overlay.js}` — four variants (live scoreboard, last-match recap, session strip, me-only pill).

The goal is to take this design into the real repo and progress it with Claude Code.

## About the Design Files

The files in `prototype/` are **design references created in HTML/JSX** — a Babel-in-the-browser React prototype with mock data. They are **not production code to ship as-is**. The task is to recreate this design **inside the existing `ballshark-tracker` codebase**, swapping the mock data layer for the real WebSocket / SQLite-backed endpoints that `server.py` already exposes.

The existing codebase is **Python (FastAPI) + vanilla HTML/CSS/JS** for the overlay. The dashboard portion of `server.py` is large (~69KB) — most likely it's already rendering HTML server-side or serving a static SPA. The recreation should follow whichever pattern is already there. **Do not add React/Vue/etc. without checking what `server.py` currently does.** If the dashboard is currently vanilla JS, port the design as vanilla JS + the existing `styles.css` pattern. If it's already a React/Vite frontend, port to that.

## Fidelity

**High-fidelity.** Pixel-perfect mocks with final colors, type, spacing, layout, and interaction states. Recreate exactly. The mock data file (`prototype/mock-data.js`) defines the full data shape the UI consumes — that's the contract between the backend and the new frontend.

## Files to map into the repo

```
prototype/Ballshark.html   →  src/ballshark/dashboard/index.html  (or whatever server.py serves)
prototype/styles.css             →  shared stylesheet, see "Design Tokens" below
prototype/screens/dashboard.jsx  →  dashboard "home" view
prototype/screens/matches.jsx    →  /matches list view
prototype/screens/match-detail.jsx → /matches/<id> view
prototype/screens/players.jsx    →  /players list + /players/<id> profile
prototype/screens/overlays.jsx   →  reference only — this is the showcase page
                                    documenting the 4 OBS overlay variants.
                                    The actual overlay code lives at:
                                    src/ballshark/overlay/{overlay.html, overlay.css, overlay.js}
prototype/components.jsx         →  shared UI primitives (PageHead, Chip, Radar,
                                    PlayerLink, Sparkline, etc.) — port as
                                    components/partials in target stack
prototype/app.jsx                →  top-level shell + nav + routing
prototype/mock-data.js           →  REFERENCE ONLY — defines the data shape.
                                    Swap for real data from server.py endpoints.
prototype/tweaks-panel.jsx       →  Design-tool only. Do not ship.
```

## Design Tokens

All tokens are defined as CSS custom properties at the top of `prototype/styles.css`. Lift them verbatim.

### Colors (dark — default)

| Token | Value | Use |
|---|---|---|
| `--accent` | `#ff7a18` | RL-orange — brand, primary CTA, winner highlight |
| `--accent-2` | `#ff4d2d` | hover/gradient pair |
| `--accent-soft` | `rgba(255,122,24,0.12)` | accent tints |
| `--accent-line` | `rgba(255,122,24,0.32)` | accent borders |
| `--team-blue` | `#2d7dff` | Team 0 (Blue) |
| `--team-orng` | `#ff7a18` | Team 1 (Orange) |
| `--good` | `#34d399` | wins, positive deltas |
| `--bad` | `#f87171` | losses, negative deltas |
| `--warn` | `#fbbf24` | streaks, "hot" indicators |
| `--bg` | `#0a0d14` | page background |
| `--bg-elev` | `#0f131c` | elevated section |
| `--card` | `#131826` | primary card surface |
| `--card-2` | `#1a2030` | nested / inner card |
| `--card-hover` | `#1c2336` | hover state |
| `--border` | `rgba(255,255,255,0.08)` | hairlines |
| `--border-strong` | `rgba(255,255,255,0.14)` | emphasized borders |
| `--text` | `#e8edf3` | body text |
| `--text-dim` | `#8b95a4` | secondary text |
| `--text-faint` | `#5b6470` | tertiary / metadata |

### Light theme overrides

Defined under `[data-theme="light"]`. Same token names, lighter values. Theme toggle should set `data-theme="light"` on `<html>`.

### Type

- Body: **Inter**, weights 400 / 500 / 600 / 700 / 800 / 900. Loaded from Google Fonts via `@import` at the top of `styles.css`.
- Mono / tabular numbers: **JetBrains Mono**. Used for IDs, OBS source URLs, code blocks.
- Tabular numbers helper class: `.tnum` → `font-variant-numeric: tabular-nums`. Apply to every score, stat, time, percentage.
- Base size: `14px` / `1.5` line-height / `-0.005em` letter-spacing.

### Spacing

No formal scale — mocks use direct px values. Common rhythm: `6 / 8 / 10 / 12 / 14 / 18 / 24`. Cards use `padding: 14px` or `16px`. Section gaps `18px`.

### Border radius

- Cards & tables: `12px`
- Buttons / chips / pills: `8px`
- Inputs / small controls: `6–8px`
- **Overlay cards: `0` (square, intentional — see "Recent design decisions" below)**

### Shadows

Used sparingly. `--glow` is the accent-tinted ring for hover on active items. Overlay cards use `box-shadow: 0 2px 0 rgba(0,0,0,0.35)` for a flat, restrained drop.

## Screens / Views

### 1. Dashboard (home)

`prototype/screens/dashboard.jsx`. Top-level landing page after the user opens `http://127.0.0.1:5050/`.

- **Session strip** — current W-L, streak, form dots (last 6 games), running totals.
- **Hero card** — most recent match: arena, teams, score, your line, link into match detail.
- **Mini chart** — sparkline of last N matches' score / win rate.
- **Quick links** — to matches, players, overlay setup.

### 2. Matches list

`prototype/screens/matches.jsx`. Sortable / filterable table of every match in the DB.

- Toolbar: filter chips for mode (1s/2s/3s), result (win/loss), arena, online vs offline.
- Columns: date, mode, arena, teams + score, your G/A/Sv/Sh/D, MVP marker, duration.
- Row click → match detail.
- Empty state when filters return nothing.

### 3. Match detail

`prototype/screens/match-detail.jsx`. The most data-rich view.

- **Hero scoreboard** — two team cards with stripe color, name, score, Win/Loss pill, mode + arena + duration in the middle.
- **Two roster cards** (blue, orange) — full per-player table with Score / G / A / Sv / Sh / D / Touch, MVP chip, bot chip, "YOU" marker.
  - Each player row has a meta-line under their name with platform + supersonic % + air % + boost (advanced fields only available for your own team — see footnote on screen).
- **Per-player radars** — **horizontal scroll-snap rail** of radar cards (G / Sh / D / Sv / A), scaled to this match's peaks. **Players are NOT laid out side-by-side as a grid** — they're in a horizontally-scrolling rail (`overflow-x: auto; scroll-snap-type: x mandatory;`). Each card is `flex: 0 0 300px`. Blue team rail, then orange team rail.
- **Footnote** — explains the SPECTATOR-only fields limitation for opponents.

### 4. Players

`prototype/screens/players.jsx`. Two views:

- **List view** — every player you've shared a match with, sortable by encounters / your win rate vs them / their average score.
- **Profile** — clicking a player: head card (name, platform, bot flag), all matches they've appeared in, head-to-head if it's an opponent you've played multiple times.

### 5. Overlays showcase

`prototype/screens/overlays.jsx`. **This is a documentation/preview page** — it shows the four OBS browser-source overlays composited on a faux RL field background, with a tab-bar to switch between them. The actual overlay files live elsewhere in the repo.

The four variants:

| Path | Width | Use |
|---|---|---|
| `/overlay/live` | 480px | Full scoreboard with both rosters + score + clock. Updates ~4 Hz during play. |
| `/overlay/last` | 420px | Same layout, frozen on final state. Between-match highlight card. |
| `/overlay/session` | 280px | W-L + streak + form dots. No in-match data. |
| `/overlay/me` | 240px | Tiny pill with just your G/A/Sv/Sh. Cleanest. |

## Recent design decisions (latest iteration)

These are deliberate choices made in the last revision — keep them on port:

1. **OBS overlay cards are solid, square, and borderless.**
   - `background: #0a0d14` (fully opaque — no rgba/transparency, no `backdrop-filter`).
   - `border: 0`, `border-radius: 0`.
   - Drop-shadow is a flat `0 2px 0 rgba(0,0,0,0.35)` only.
   - Team color is a **full solid block** in the header bar (`#2d7dff` and `#ff7a18`) with white text, not a tinted overlay.
   - The OBS toolbar (tab strip inside the showcase) is also square, borderless tabs separated by 1px dividers.

2. **Per-player radars are a horizontal scroll-snap rail**, not a multi-column grid.
   ```css
   .radar-grid {
     display: flex;
     gap: 12px;
     overflow-x: auto;
     scroll-snap-type: x mandatory;
   }
   .radar-card {
     flex: 0 0 300px;
     scroll-snap-align: start;
   }
   ```
   The user explicitly does not want players packed side-by-side. Each player gets dedicated horizontal space; you scroll to advance.

3. **Restrained, compact density.** No glassmorphism, no heavy gradients, minimal rounded corners on the overlay specifically. The dashboard portion can be slightly more relaxed but overall: tight padding, small type for metadata (9–11px), large type only for scores and primary KPIs.

## Interactions & Behavior

- **Match row click** → navigate to match detail.
- **Player name link** → navigate to player profile. Bots are not clickable.
- **Theme toggle** in nav → flips `data-theme` between `dark` (default) and `light`.
- **Radar rail** → native scroll with snap. No JS needed beyond the CSS above. Optional: add arrow buttons that scroll by `card-width + gap` on click.
- **OBS overlay tabs** (showcase only) → swap which variant is rendered on the stage.
- **WebSocket reconnect** → the existing `overlay.js` already handles `ws://127.0.0.1:5050/ws` with sticky state replay on reconnect. Preserve that.

## State Management

The dashboard reads from `server.py`'s existing endpoints (see `src/ballshark/server.py` and `src/ballshark/store.py`). The mock data file `prototype/mock-data.js` documents the exact shape the UI consumes per entity:

- **Match**: `{ id, started_at, mode, arena, is_online, team0_name, team1_name, team0_score, team1_score, winner_team_num, duration_seconds, crossbar_hits, players: [...] }`
- **MatchPlayer**: `{ name, primary_id, team_num, score, goals, assists, saves, shots, demos, touches, is_mvp, is_bot, platform, ticks_total, ticks_supersonic, ticks_in_air, ticks_on_wall, ticks_on_ground, boost_used }`
- **Session**: `{ wins, losses, streak, goals, assists, saves, shots, demos, form: [bool] }`

These line up with the existing `matches` / `match_player_stats` / `match_extras` tables documented in the repo README. No new columns required.

## Assets

No images required. Icons are inline SVG or CSS shapes. Team stripes are solid color blocks.

The screenshots / uploads under `uploads/` in the prototype project were **reference material from the existing app, BARL streams, and the Rocket League in-game scoreboard** — they informed the design but are not assets to ship.

## Implementation order suggestion

1. Lift the design tokens (`:root` + `[data-theme="light"]` blocks) into the existing stylesheet. Do this first — every other screen depends on them.
2. Rebuild the **OBS overlay** (`src/ballshark/overlay/`) — smallest scope, easiest test (just open `http://127.0.0.1:5050/overlay/live`). Get the four variants pixel-matching the prototype.
3. Rebuild the **match detail** view — most data-rich, exercises the radar component which you'll reuse.
4. Rebuild **matches list** + **dashboard** — both consume already-built primitives.
5. Rebuild **players** view last.

## Files in this bundle

```
design_handoff_ballshark_redesign/
├── README.md                          ← this file
└── prototype/
    ├── Ballshark.html           ← entry point, lists all script tags in order
    ├── styles.css                     ← all tokens + every style
    ├── app.jsx                        ← shell + nav + routing
    ├── components.jsx                 ← shared primitives (Chip, Radar, Sparkline, PlayerLink, PageHead)
    ├── mock-data.js                   ← data shape contract (replace with real API)
    ├── tweaks-panel.jsx               ← design-tool, do not ship
    └── screens/
        ├── dashboard.jsx
        ├── matches.jsx
        ├── match-detail.jsx
        ├── players.jsx
        └── overlays.jsx               ← OBS showcase page (documents the 4 overlay variants)
```

To preview the prototype before porting: open `prototype/Ballshark.html` in a browser. It self-hosts React + Babel from unpkg and runs entirely client-side off `mock-data.js`.
