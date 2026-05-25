# Carball Tracker — Design System

This is the working design system as of 2026-05-15, lifted from
`src/carball/server.py`'s `_STYLE_TAG` and the parallel `.design/new_style.css`
working copy.

## Color

Dark default. Light theme overrides under `[data-theme="light"]`.

### Dark palette (default)

| Token             | Value                    | Use                                      |
|-------------------|--------------------------|------------------------------------------|
| `--accent`        | `#ff7a18`                | RL-orange brand, primary, winner highlight |
| `--accent-2`      | `#ff4d2d`                | hover / gradient pair                    |
| `--accent-soft`   | `rgba(255,122,24,0.12)`  | accent tints                             |
| `--accent-line`   | `rgba(255,122,24,0.32)`  | accent borders                           |
| `--team-blue`     | `#2d7dff`                | Team 0 (Blue)                            |
| `--team-blue-soft`| `rgba(45,125,255,0.14)`  | Team 0 tint                              |
| `--team-orng`     | `#ff7a18`                | Team 1 (Orange) — same as accent         |
| `--team-orng-soft`| `rgba(255,122,24,0.14)`  | Team 1 tint                              |
| `--good`          | `#34d399`                | wins, positive deltas                    |
| `--bad`           | `#f87171`                | losses, negative deltas                  |
| `--warn`          | `#fbbf24`                | streaks, hot indicators                  |
| `--bg`            | `#0a0d14`                | page background                          |
| `--bg-elev`       | `#0f131c`                | elevated section background              |
| `--card`          | `#131826`                | primary card surface                     |
| `--card-2`        | `#1a2030`                | nested / inner card                      |
| `--card-hover`    | `#1c2336`                | hover state                              |
| `--border`        | `rgba(255,255,255,0.08)` | hairlines                                |
| `--border-strong` | `rgba(255,255,255,0.14)` | emphasized borders                       |
| `--text`          | `#e8edf3`                | body text                                |
| `--text-dim`      | `#a5adba`                | secondary text (passes WCAG AA)          |
| `--text-faint`    | `#8a93a0`                | tertiary / metadata (passes WCAG AA)     |

### Light palette overrides

Defined under `[data-theme="light"]`. Same tokens, lighter values. Theme toggle
sets `data-theme="light"` on `<html>`.

### Strategy

**Restrained.** Tinted neutrals + one accent (orange) ≤10% surface, plus the two
team colors used only on team-identity surfaces (hero scoreboard sides, roster
stripes, radar polygon strokes). No glassmorphism. No radial-gradient page bg
(the "AI default" tell).

## Typography

Three faces, three roles. No more.

| Face                 | Role             | Where it appears                          |
|----------------------|------------------|-------------------------------------------|
| Bricolage Grotesque  | Body + headings  | All prose, h1/h2, page heads, brand name  |
| JetBrains Mono       | All numerics     | Scores, KPI values, table `.num` columns, badges, codeblocks |
| (Inter, system)      | Fallback only    | `font-family` stack tail                  |

- Body size: `14.5px`, line-height `1.55`, letter-spacing `-0.005em`.
- Headings use Bricolage with `font-variation-settings: "wdth" 92` for a slightly
  narrower, sport-tracker feel.
- All numbers (scores, table cells, KPI tiles, badges) are JetBrains Mono with
  `font-feature-settings: "tnum"` and `letter-spacing: -0.04em` on big numerics.
- Body line length capped at `~60-72ch` on `.who`, `.sub`, `.caption`, `.ov-desc`,
  `.note`, `.prose p`, `.prose li`.

## Spacing

No formal scale. Rhythm uses these px values: `6 / 8 / 10 / 12 / 14 / 18 / 22 / 24`.
Section padding `18-22px`. Card padding `14-20px`.

## Corner radius

**All rectangles are square (`border-radius: 0`).** Cards, sections, hero banner,
roster cards, radar cards, KPI tiles, buttons, chips, badges, pills, codeblocks.
Only circles (form-dots `50%`, info-indicator `50%`) keep curvature. This is a
deliberate choice and overrides the Claude-Design source.

## Borders

- Hairlines: `1px solid var(--border)` everywhere.
- Emphasized: `1px solid var(--border-strong)`.
- **No `border-left` / `border-right` colored stripes on cards** (side-tab
  antipattern, AI-default tell). Team identity inside roster cards is carried by
  the inline 4-px-wide `.roster-stripe` and the team-color score, not by an outer
  border accent.

## Shadows

Avoided. The body gradient was removed (dark-glow antipattern). Brand-logo
box-shadow removed. Form-dots green glow removed. The only motion-glow that
remains is the `.live-pip .dot` pulse animation, used in the nav only when an
active match is being ingested.

## Layout

### App shell

`.wrapper` (alias `.app-shell`) caps the page at `max-width: 1240px` with
`28px` horizontal padding.

### Nav

Three-column grid: brand left, navlinks centered, idle pip + theme toggle right.

### Match detail

Stacks vertically, top to bottom:

1. **Breadcrumb** — `← Matches / <match id>` (mono).
2. **Hero scoreboard** — three-column grid: Blue side (left, blue color block) |
   Middle (final / duration / arena meta) | Orange side (right, orange color block).
   Solid `var(--card)` background on each side, no gradient tint.
3. **Two roster cards** — Blue first, then Orange. Full-width per team. Each has a
   header with team name + score + Winner pill, and a roster table with per-player
   row + meta-line (platform + advanced stats inline when coverage ≥ 70%).
4. **Per-player radars** — two horizontal scroll-snap rails (one per team),
   each card `flex: 0 0 300px` with `scroll-snap-align: start`. NOT a multi-column
   grid. One player at a time, scroll to advance.
5. **Note** — explainer of the 70% coverage rule for advanced stats.

### Matches list

Page head → toolbar (segmented filter + bot toggle) → summary row (W-L, totals)
→ history table inside a square-cornered card.

### Dashboard

KPI tile row → `dash-grid` 2-column on wide (radar + sparkbar + movement on left,
recent form + records + recent matches on right). Filter chip ("Filter Bot matches",
**on by default**) sits between the profile header and KPIs.

### Overlay picker

Single-column 4-row stack. Each card is a two-column grid (info column left
`260-320px`, live iframe preview right, fills remaining width). Cards span the
full app shell.

## Components

### Cards (`.card`, `section`)

```
background: var(--card);
border: 1px solid var(--border);
border-radius: 0;
padding: 18px 20px;
```

### KPI tile (`.kpi`)

Square, padded `16px 18px 14px`. Label (10px uppercase, tracking 0.12em, dim),
value (26px, weight 800, mono, tabular). `.kpi.primary` has accent border tint.

### Chips

`.chip` is a square 3px-6px pill, uppercase 11px weight 700. Variants:
- `.chip.win` — green
- `.chip.loss` — red
- `.chip.mvp` — accent orange
- `.chip.bot` — faint
- `.chip.blue` / `.chip.orng` — team colors

### Badge (`.badge`)

W / L block, `28x24px` square, weight 800 mono. Win = green tint, Loss = red tint.

### Note (`.note`)

Square card with an inline circular "i" indicator on the left (NOT a side-stripe
border). Used for explainers / footnotes.

### Filter chip (`.filter-chip`)

Square pill (overridden from the design's 999px). `.active` state uses accent
color/border. Sits inside a `.filter-row` flex container.

### Radar SVG

Theme-aware via CSS variables on `.radar-svg` class. Polygon fill = team color
at `0.18` opacity, stroke at full color. Grid rings + spokes use `var(--border)`.
Labels use `var(--text)` (full contrast) uppercase + tracking. Values below
labels use `var(--text-dim)` mono.

## Motion

- Hover transitions: `all 140ms ease`.
- Theme transitions: `background 200ms ease, color 200ms ease`.
- Live pip pulse: `1.6s ease-out infinite` (only motion-glow allowed).
- No CSS-layout animations. No bounce. Exponential ease-out only.

## Absolute bans

Anti-patterns refused on this codebase:

- **No side-tab borders.** `border-left: Npx solid <color>` on rounded or square
  cards. Removed from `.roster-card`, `.radar-card`, `.note`, `.hint`.
- **No gradient text.** `background-clip: text` is never used.
- **No glassmorphism.** No `backdrop-filter: blur()` decoratively.
- **No abbreviated stat headers.** "G/A/Sv/Sh/D" is forbidden in any rendered surface.
- **No dark-glow gradients.** Body radial-gradient bg removed.
- **No em dashes.** Use commas, colons, semicolons, periods, parentheses.
- **No Inter as primary font.** It's overused in AI-default UIs.

## Accessibility

- WCAG AA contrast on body and large text. `--text-faint #8a93a0` was bumped
  from the design's `#5b6470` for this reason.
- Keyboard focus visible on all interactive elements (default browser focus
  outline is retained; not suppressed).
- Heading hierarchy contiguous (h1 → h2 → never skipped to h3).
- Filter-chip is a `<button>` (form submit) or `<a>` (link), never a `<div>`.
