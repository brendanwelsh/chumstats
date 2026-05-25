# Carball Tracker — Product Context

## Product purpose

A self-hosted Rocket League stats tracker for a single owner and their friend group. It
sits between the in-game Stats API (a local TCP socket the game opens when
`PacketSendRate` is set in `DefaultStatsAPI.ini`) and three downstream surfaces:

1. A **local dashboard** website with career stats, match history, per-player profiles,
   per-match detail with team rosters and radar charts.
2. A **Discord bot** that posts match-end embeds to a private server.
3. An **OBS browser-source overlay** with four variants (live scoreboard, last-match
   recap, session strip, me-only pill) for streaming.

It is explicitly **not** a public leaderboard, not a SaaS product, not multi-tenant.
No third-party data feeds (no ballchasing.com, no tracker.gg, no Psyonix REST). Everything
runs from the local TCP socket and a SQLite file at `data/carball.db`.

## Register

**Product.** This is application UI for a known, returning user — not a marketing
surface. Density matters. Information hierarchy beats decoration. The owner will look
at the dashboard daily; visual novelty wears off, scannable layout doesn't.

## Users

### Primary user — the owner

A competitive-leaning Rocket League player who wants their own stats without giving
data to a third party. Plays 3s casually, against bots solo for warmup, and plays
private matches with the same friend group most nights. They care about:

- Did I win or lose this session? Streak? Form?
- How am I doing vs my own baseline (per-match averages, single-match records)?
- Who do I usually play with, and what's our head-to-head record vs other players?
- Watching match recaps come in via the Discord bot during a session.

They are technical (comfortable editing `.env`, reading SQL, deploying to a private
GitHub repo). They appreciate sharp craft and do not need handholding. They will not
read long help text; they will read the code.

### Secondary users — friends in the same Discord

Visiting players who appear as teammates or opponents in matches the owner records.
They get a profile page (lifetime stats vs the owner). They are not authenticated;
the dashboard is read-only LAN-accessible at `0.0.0.0:5050`.

## Tone

- Direct, technical, no marketing language. The product talks to the owner like a
  teammate who's read the spec.
- Stat names are spelled out: "Goals", "Assists", "Saves", "Shots", "Demos". The
  abbreviations G/A/Sv/Sh/D are forbidden in user-facing text — they look like
  cryptic spreadsheet headers.
- Honest about limitations. The Stats API doesn't give MMR, doesn't give per-tick
  XYZ positions, doesn't fully cover opponents' movement stats. The "How it works"
  page says so directly.
- No em dashes. (Impeccable skill rule.)

## Anti-references

- **Tracker.gg / ballchasing.gg detail pages**: too dense, too many ads, too SaaS-cream.
  The team-vs-team scoreboard pattern is good; the chrome is not.
- **Default shadcn/AI-default dashboards**: glowy radial gradients, side-tab cards,
  rounded everything, Inter for 100% of text. The "AI made this" tell. Refused.
- **ESPN's score page in 2026**: too cluttered with ads/promos, but the team-vs-team
  scoreboard rhythm is correct inspiration.
- **BARL streams (BARL = the broadcast UI used on RL pro streams)**: the right vibe
  for the OBS overlay — solid square cards, team-color blocks for headers, white text
  on team color in the scoreboard header.

## Strategic principles

1. **Local-first, no third parties.** The README and the about page both reinforce
   this. It's a competitive feature, not just a privacy claim.
2. **Match-as-narrative.** Every match is a story (who did what, when, with whom).
   The match detail page should read top-to-bottom like a recap: hero scoreboard →
   team rosters → per-player radars → footnote on data limits.
3. **Team-vs-team always.** Players are always grouped by team. Not flat tables.
   Not "all players in score order." Always Blue vs Orange.
4. **Sport-tracker, not data tool.** This is a scoreboard, not a Grafana panel.
   Big scores in display type, mono numerics, brand-orange accent, dark default.
5. **Filter bots by default.** Bot-stomp matches inflate goals-per-match. They
   should be available (toggle in nav) but not the default view.

## What's in scope

- Match ingest, persistence, per-match detail, lifetime aggregates, head-to-head.
- Discord embed posting.
- 4 OBS overlay variants over WebSocket.
- LAN-shared dashboard, dark/light theme toggle.

## What's deliberately out of scope

- Public hosting / multi-tenant / auth. Single-owner, LAN-only.
- Importing replays via the `.replay` file format (separate, much heavier project).
- MMR / rank / season stats. Not in the Stats API; use the in-game scoreboard.
- AI predictions, "smart insights", recommendations. Stats only.

## Hard rules

- No abbreviated stat headers (G/A/Sv/Sh/D). Spell them out.
- Filter bots is ON by default everywhere, including the career dashboard.
- Hide advanced stats (boost/speed/wall/ground/supersonic) when spectator-tick
  coverage is under 70% of match duration. Otherwise the numbers come from goal-cam
  blips and mislead.
- Square corners everywhere except true circles (form dots, info indicator).
- Bricolage Grotesque for headings and body. JetBrains Mono for all numerics
  (scores, KPI values, table `.num` columns). No Inter as primary face.
- WCAG AA contrast (4.5:1 body, 3:1 large text). No `text-faint` darker than `#8a93a0`
  on the dark theme `--bg`.
