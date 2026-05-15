"""Local HTTP + WebSocket server for the OBS-style overlay.

- GET /            -> serves overlay.html
- GET /overlay     -> alias
- GET /static/*    -> overlay.css, overlay.js, fonts, etc.
- WS  /ws          -> broadcasts events to all connected clients

The ingest pipeline pushes events into a Broadcaster. The Broadcaster fans
them out to every connected WS client as JSON lines of the shape:

    {"type": "match_end",   "data": {...MatchSummary...}}
    {"type": "match_start", "data": {"arena": "...", "team0_name": "..."}}
    {"type": "tick",        "data": {...selected UpdateState fields...}}
    {"type": "session",     "data": {...SessionTotals...}}
    {"type": "goal",        "data": {...GoalScored payload...}}
    {"type": "crossbar",    "data": {...CrossbarHit payload...}}

We throttle `tick` to ~4Hz so the overlay doesn't melt the browser.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from datetime import datetime
from urllib.parse import quote

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .session import MatchSummary, SessionTotals

log = logging.getLogger("carball.server")


OVERLAY_DIR = Path(__file__).resolve().parent / "overlay"


def _summary_to_dict(s: MatchSummary) -> dict[str, Any]:
    d = asdict(s)
    d["color_primary"] = {str(k): v for k, v in s.color_primary.items()}
    return d


class Broadcaster:
    """Holds the active WS connections and a fan-out queue."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._last_tick_ts = 0.0
        self.tick_min_interval = 0.25  # 4 Hz max

        # Sticky state so a fresh overlay client can resume context.
        self.last_summary: dict[str, Any] | None = None
        self.last_session: dict[str, Any] | None = None
        self.in_match: bool = False
        self.current_match_meta: dict[str, Any] | None = None

    async def register(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self.clients.add(ws)
        if self.last_session:
            await self._send_one(ws, {"type": "session", "data": self.last_session})
        if self.last_summary:
            await self._send_one(ws, {"type": "match_end", "data": self.last_summary})
        if self.in_match and self.current_match_meta:
            await self._send_one(ws, {"type": "match_start", "data": self.current_match_meta})

    async def unregister(self, ws: WebSocket) -> None:
        async with self._lock:
            self.clients.discard(ws)

    async def _send_one(self, ws: WebSocket, msg: dict) -> None:
        try:
            await ws.send_text(json.dumps(msg))
        except Exception:
            await self.unregister(ws)

    async def broadcast(self, msg: dict) -> None:
        text = json.dumps(msg)
        dead: list[WebSocket] = []
        async with self._lock:
            for ws in list(self.clients):
                try:
                    await ws.send_text(text)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.clients.discard(ws)

    # --- producer-side API: thread-safe-ish helpers called from ingest -----

    def push_match_end(self, summary: MatchSummary, totals: SessionTotals,
                       loop: asyncio.AbstractEventLoop) -> None:
        d = _summary_to_dict(summary)
        self.last_summary = d
        self.last_session = asdict(totals)
        self.in_match = False
        self.current_match_meta = None
        asyncio.run_coroutine_threadsafe(
            self.broadcast({"type": "match_end", "data": d}),
            loop,
        )
        asyncio.run_coroutine_threadsafe(
            self.broadcast({"type": "session", "data": self.last_session}),
            loop,
        )

    def push_match_start(self, meta: dict[str, Any],
                         loop: asyncio.AbstractEventLoop) -> None:
        self.in_match = True
        self.current_match_meta = meta
        asyncio.run_coroutine_threadsafe(
            self.broadcast({"type": "match_start", "data": meta}),
            loop,
        )

    def push_tick(self, payload: dict[str, Any],
                  loop: asyncio.AbstractEventLoop) -> None:
        now = time.monotonic()
        if now - self._last_tick_ts < self.tick_min_interval:
            return
        self._last_tick_ts = now
        asyncio.run_coroutine_threadsafe(
            self.broadcast({"type": "tick", "data": payload}),
            loop,
        )

    def push_event(self, type_: str, payload: dict[str, Any],
                   loop: asyncio.AbstractEventLoop) -> None:
        asyncio.run_coroutine_threadsafe(
            self.broadcast({"type": type_, "data": payload}),
            loop,
        )


def make_app(broadcaster: Broadcaster, *, store=None,
             self_primary_id: str | None = None,
             self_name: str | None = None) -> FastAPI:
    app = FastAPI(title="carball")

    if OVERLAY_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(OVERLAY_DIR)), name="static")

    @app.get("/")
    async def root_redirect():
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/dashboard")

    @app.get("/overlay.html")
    async def overlay_legacy() -> FileResponse:
        # Legacy URL kept for any saved OBS sources. New URLs use /overlay/<mode>.
        return FileResponse(str(OVERLAY_DIR / "overlay.html"))

    @app.get("/healthz")
    async def health() -> dict:
        return {"ok": True, "clients": len(broadcaster.clients)}

    @app.get("/dashboard")
    async def dashboard():
        """Career dashboard HTML page for the configured player."""
        if store is None or (not self_primary_id and not self_name):
            return HTMLResponse("<p>No player configured; set RL_PLAYER_NAME / RL_PLAYER_PRIMARY_ID in .env</p>")
        from .analytics import build_dashboard
        d = build_dashboard(store, primary_id=self_primary_id, name=self_name)
        return HTMLResponse(_dashboard_html(
            d, store=store, primary_id=self_primary_id, name=self_name,
            is_self=True,
        ))

    @app.get("/player/{name}")
    async def player_page(name: str):
        """Career dashboard for an arbitrary player name from the DB."""
        if store is None:
            return HTMLResponse("<p>No DB configured</p>")
        from .analytics import build_dashboard
        d = build_dashboard(store, name=name)
        if not d.overview.lines:
            return HTMLResponse(_not_found_html(name), status_code=404)
        return HTMLResponse(_dashboard_html(d, store=store, name=name, is_self=False))

    @app.get("/players")
    async def players_page(include_bots: int = 1):
        if store is None:
            return HTMLResponse("<p>No DB configured</p>")
        return HTMLResponse(_players_directory_html(
            store, self_primary_id=self_primary_id, include_bots=bool(include_bots),
        ))

    @app.get("/history")
    async def history_page(include_bots: int = 1):
        if store is None:
            return HTMLResponse("<p>No DB configured</p>")
        return HTMLResponse(_history_page_html(
            store, self_primary_id, self_name,
            include_bots=bool(include_bots),
        ))

    @app.get("/match/{match_id}")
    async def match_page(match_id: str):
        if store is None:
            return HTMLResponse("<p>No DB configured</p>")
        return HTMLResponse(_match_detail_html(store, match_id, self_primary_id, self_name))

    @app.get("/about")
    async def about_page():
        return HTMLResponse(_about_html())

    @app.get("/overlay-picker")
    @app.get("/overlay")
    async def overlay_picker(request: Request):
        host = request.headers.get("host", "127.0.0.1:5050")
        return HTMLResponse(_overlay_picker_html(host))

    @app.get("/overlay/{mode}")
    async def overlay_mode(mode: str):
        if mode not in ("live", "last", "session", "me"):
            return HTMLResponse(f"<p>Unknown overlay mode: {mode}</p>", status_code=404)
        # Serve the same shell, mode chosen by JS via path.
        path = OVERLAY_DIR / "overlay.html"
        return FileResponse(str(path))

    @app.get("/api/dashboard")
    async def api_dashboard() -> dict:
        if store is None or (not self_primary_id and not self_name):
            return {"error": "not configured"}
        from .analytics import build_dashboard
        d = build_dashboard(store, primary_id=self_primary_id, name=self_name)
        return {
            "player": d.player_label,
            "groups": {
                g.title: [{"label": ml.label, "value": ml.value, "comparison": ml.comparison} for ml in g.lines]
                for g in d.all_groups()
                if g.lines
            },
        }

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await broadcaster.register(ws)
        try:
            while True:
                # We don't expect inbound messages; this keeps the connection alive
                # and detects client-side closes.
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await broadcaster.unregister(ws)

    return app


# ---- shared helpers ----

ARENA_NICE = {
    "stadium_p":              "DFH Stadium",
    "stadium_day_p":          "DFH Stadium (Day)",
    "trainstation_night_p":   "Urban Central (Night)",
    "trainstation_p":         "Urban Central",
    "trainstation_dawn_p":    "Urban Central (Dawn)",
    "eurostadium_p":          "Mannfield",
    "eurostadium_night_p":    "Mannfield (Night)",
    "eurostadium_rainy_p":    "Mannfield (Stormy)",
    "park_p":                 "Beckwith Park",
    "park_night_p":           "Beckwith Park (Night)",
    "park_rainy_p":           "Beckwith Park (Stormy)",
    "hoopsstadium_p":         "Dunk House (Hoops)",
    "shattershot_p":          "Core 707 (Dropshot)",
    "stadium_winter_p":       "Snowy Stadium (Snow Day)",
    "wasteland_p":            "Wasteland",
    "chinastadium_p":         "Forbidden Temple",
    "neotokyo_standard_p":    "Neo Tokyo",
}


def _arena_nice(arena: str) -> str:
    if not arena:
        return "Unknown arena"
    return ARENA_NICE.get(arena.lower(), arena.replace("_", " "))


def _form_string(results: list[bool]) -> str:
    """Render last-N form like '✓ ✓ ✗ ✓ ✓' instead of 'WWLWW'."""
    return " ".join("✓" if w else "✗" for w in results)


def _radar_svg(values: list[tuple[str, float, float]], *,
               size: int = 340, color: str = "#60a5fa") -> str:
    """SVG radar / spider chart.

    `values` is a list of (label, magnitude, axis_max) tuples. Each axis
    has its own max scale — useful because RL stats vary (5 goals is a lot
    but 5 assists is rare). The polygon is filled at 20% opacity with a
    bright outline; grid rings at 25/50/75/100%; labels outside the chart
    with the actual numeric value underneath.
    """
    import math
    n = len(values)
    if n < 3:
        return "<!-- radar needs at least 3 axes -->"
    cx = cy = size / 2
    r = size * 0.32

    parts: list[str] = [
        f'<svg viewBox="0 0 {size} {size}" xmlns="http://www.w3.org/2000/svg" '
        f'role="img" aria-label="Radar chart" '
        f'style="width:100%;max-width:{size}px;display:block;margin:0 auto">'
    ]
    # grid rings
    for pct in (0.25, 0.5, 0.75, 1.0):
        ring = []
        for i in range(n):
            a = -math.pi / 2 + 2 * math.pi * i / n
            x = cx + r * pct * math.cos(a)
            y = cy + r * pct * math.sin(a)
            ring.append(f"{x:.1f},{y:.1f}")
        parts.append(
            f'<polygon points="{" ".join(ring)}" fill="none" '
            f'stroke="rgba(255,255,255,0.08)" stroke-width="1"/>'
        )
    # spokes
    for i in range(n):
        a = -math.pi / 2 + 2 * math.pi * i / n
        ex = cx + r * math.cos(a)
        ey = cy + r * math.sin(a)
        parts.append(
            f'<line x1="{cx}" y1="{cy}" x2="{ex:.1f}" y2="{ey:.1f}" '
            f'stroke="rgba(255,255,255,0.1)" stroke-width="1"/>'
        )
    # data polygon
    poly_pts: list[tuple[float, float]] = []
    for i, (_lbl, v, vmax) in enumerate(values):
        scale = (v / vmax) if vmax > 0 else 0
        scale = max(0.0, min(1.0, scale))
        a = -math.pi / 2 + 2 * math.pi * i / n
        x = cx + r * scale * math.cos(a)
        y = cy + r * scale * math.sin(a)
        poly_pts.append((x, y))
    pts_str = " ".join(f"{x:.1f},{y:.1f}" for x, y in poly_pts)
    parts.append(
        f'<polygon points="{pts_str}" fill="{color}33" '
        f'stroke="{color}" stroke-width="2"/>'
    )
    # vertex dots
    for x, y in poly_pts:
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="{color}"/>')
    # axis labels
    for i, (lbl, v, vmax) in enumerate(values):
        a = -math.pi / 2 + 2 * math.pi * i / n
        lx = cx + (r + 24) * math.cos(a)
        ly = cy + (r + 24) * math.sin(a)
        anchor = "middle"
        ca = math.cos(a)
        if ca > 0.25:    anchor = "start"
        elif ca < -0.25: anchor = "end"
        parts.append(
            f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anchor}" '
            f'dominant-baseline="middle" fill="#e6edf3" font-size="13" font-weight="600">{lbl}</text>'
        )
        v_str = f"{v:.2f}" if v < 10 else f"{v:.0f}"
        parts.append(
            f'<text x="{lx:.1f}" y="{ly + 15:.1f}" text-anchor="{anchor}" '
            f'dominant-baseline="middle" fill="#8b95a4" font-size="11">{v_str} / {vmax:.0f}</text>'
        )
    parts.append('</svg>')
    return "\n".join(parts)


def _radar_block_for_player(store, primary_id: str | None, name: str | None) -> str:
    """Compute the dashboard radar for one player and wrap it in HTML."""
    if not store or (not primary_id and not name):
        return ""
    where = "primary_id = ?" if primary_id else "name = ?"
    arg = primary_id or name
    with store._conn() as con:  # type: ignore[attr-defined]
        row = con.execute(f"""
            SELECT
                AVG(goals)   AS g,
                AVG(assists) AS a,
                AVG(saves)   AS sv,
                AVG(shots)   AS sh,
                AVG(demos)   AS d,
                COUNT(*)     AS n
            FROM match_player_stats WHERE {where}
        """, (arg,)).fetchone()
        peaks = con.execute("""
            SELECT
                MAX(goals)   AS g,
                MAX(assists) AS a,
                MAX(saves)   AS sv,
                MAX(shots)   AS sh,
                MAX(demos)   AS d
            FROM match_player_stats
        """).fetchone()
    if not row or not row["n"]:
        return ""
    values = [
        ("Goals",   row["g"]  or 0, max(peaks["g"]  or 0, 1)),
        ("Shots",   row["sh"] or 0, max(peaks["sh"] or 0, 1)),
        ("Demos",   row["d"]  or 0, max(peaks["d"]  or 0, 1)),
        ("Saves",   row["sv"] or 0, max(peaks["sv"] or 0, 1)),
        ("Assists", row["a"]  or 0, max(peaks["a"]  or 0, 1)),
    ]
    svg = _radar_svg(values)
    return (
        f'<section class="radar-section">'
        f'<h2>Radar - per-match averages</h2>'
        f'{svg}'
        f'<p class="caption">Each axis scaled to the highest single-match value in the DB. '
        f'Polygon = your per-match averages over {row["n"]} matches.</p>'
        f'</section>'
    )


def _kpi_tiles_from_dashboard(d) -> str:
    """Extract the 4 most-important numbers from the Overview/Averages
    groups into pill-shaped KPI tiles up top."""
    overview = {ml.label: ml for ml in d.overview.lines}
    averages = {ml.label: ml for ml in d.averages.lines}

    def tile(value: str, label: str, *, accent: str = "") -> str:
        klass = f"kpi {accent}".strip()
        return (f'<div class="{klass}">'
                f'<div class="kpi-value">{value}</div>'
                f'<div class="kpi-label">{label}</div></div>')

    tiles: list[str] = []
    if "Win-loss" in overview:
        wl = overview["Win-loss"]
        tiles.append(tile(wl.value, f"W-L · {wl.comparison}", accent="primary"))
    if "MVP count" in overview:
        tiles.append(tile(overview["MVP count"].value, "MVPs"))
    if "Goals/match" in averages:
        tiles.append(tile(averages["Goals/match"].value, "Goals / match"))
    if "Shooting %" in averages:
        tiles.append(tile(averages["Shooting %"].value, "Shooting %"))
    if "Matches" in overview:
        tiles.append(tile(overview["Matches"].value, "Total matches"))
    return f'<div class="kpi-row">{"".join(tiles)}</div>' if tiles else ""


def _match_history_rows(store, primary_id: str | None, name: str | None,
                        *, limit: int = 50,
                        include_bots: bool = True):
    """Shared query for recent matches. Returns sqlite Row objects."""
    if not store or (not primary_id and not name):
        return []
    where_clauses = []
    args: list = []
    if primary_id:
        where_clauses.append("mps.primary_id = ?")
        args.append(primary_id)
    else:
        where_clauses.append("mps.name = ?")
        args.append(name)
    if not include_bots:
        where_clauses.append(
            "NOT EXISTS (SELECT 1 FROM match_player_stats x WHERE x.match_id = m.id AND x.is_bot = 1)"
        )
    where_sql = " AND ".join(where_clauses)
    with store._conn() as con:
        return con.execute(f"""
            SELECT m.id, m.started_at, m.arena, m.is_online,
                   m.team0_score, m.team1_score,
                   m.team0_name, m.team1_name, m.winner_team_num,
                   mps.team_num, mps.goals, mps.assists, mps.saves,
                   mps.shots, mps.demos, mps.score, mps.is_mvp
            FROM match_player_stats mps
            JOIN matches m ON m.id = mps.match_id
            WHERE {where_sql}
            ORDER BY m.started_at DESC
            LIMIT ?
        """, (*args, limit)).fetchall()


def _match_history_html(store, primary_id: str | None, name: str | None, *,
                        limit: int = 12, include_bots: bool = True,
                        show_section_chrome: bool = True) -> str:
    rows = _match_history_rows(
        store, primary_id, name,
        limit=limit, include_bots=include_bots,
    )
    if not rows:
        body = "<p class='caption'>No matches recorded yet.</p>"
        if show_section_chrome:
            return f"<section><h2>Recent matches</h2>{body}</section>"
        return body

    body_rows: list[str] = []
    for r in rows:
        won = r["team_num"] == r["winner_team_num"]
        ts = datetime.fromtimestamp(r["started_at"]).strftime("%b %d · %H:%M")
        mvp = "<span class='mvp'>MVP</span>" if r["is_mvp"] else ""
        arena = _arena_nice(r["arena"] or "")
        mode = "Online" if r["is_online"] else "Offline"
        body_rows.append(f"""
          <tr class="match-row {'win' if won else 'loss'}" onclick="window.location='/match/{quote(r['id'], safe='')}'">
            <td><span class="badge {'win' if won else 'loss'}">{'W' if won else 'L'}</span></td>
            <td class="dim">{ts}</td>
            <td class="score-cell">{r["team0_name"]} <b>{r["team0_score"]}</b> - <b>{r["team1_score"]}</b> {r["team1_name"]}</td>
            <td class="dim">{arena}</td>
            <td class="dim">{mode}</td>
            <td class="num"><b>{r["goals"]}</b></td>
            <td class="num"><b>{r["assists"]}</b></td>
            <td class="num"><b>{r["saves"]}</b></td>
            <td class="num"><b>{r["shots"]}</b></td>
            <td class="num"><b>{r["demos"]}</b></td>
            <td>{mvp}</td>
          </tr>
        """)

    table_html = f"""
      <table class="history">
        <thead><tr>
          <th></th>
          <th>Date</th>
          <th>Score</th>
          <th>Arena</th>
          <th>Mode</th>
          <th class="num">Goals</th>
          <th class="num">Assists</th>
          <th class="num">Saves</th>
          <th class="num">Shots</th>
          <th class="num">Demos</th>
          <th></th>
        </tr></thead>
        <tbody>{"".join(body_rows)}</tbody>
      </table>
    """
    if not show_section_chrome:
        return table_html
    return f"""
      <section>
        <h2>Recent matches <a href="/history" class="see-all">view all</a></h2>
        {table_html}
      </section>
    """


def _players_directory_html(store, self_primary_id: str | None = None,
                            include_bots: bool = True) -> str:
    """Players table sorted by frequency-played, with teammates vs opponents
    split if we know who 'me' is. Click name -> /player/<name>."""
    bot_filter = "" if include_bots else "WHERE max_bot = 0"

    sql = f"""
        SELECT name, primary_id, n, goals, saves, assists, wins, max_bot AS is_bot,
               platform, was_teammate, was_opponent
        FROM (
            SELECT mps.name, mps.primary_id,
                   COUNT(*) AS n,
                   SUM(mps.goals)   AS goals,
                   SUM(mps.saves)   AS saves,
                   SUM(mps.assists) AS assists,
                   SUM(CASE WHEN mps.team_num = m.winner_team_num THEN 1 ELSE 0 END) AS wins,
                   MAX(mps.is_bot)  AS max_bot,
                   MIN(mps.platform) AS platform,
                   MAX(CASE WHEN ? != '' AND EXISTS(
                        SELECT 1 FROM match_player_stats z
                        WHERE z.match_id = m.id AND z.primary_id = ?
                          AND z.team_num = mps.team_num
                          AND NOT (z.primary_id = mps.primary_id AND z.name = mps.name)
                   ) THEN 1 ELSE 0 END) AS was_teammate,
                   MAX(CASE WHEN ? != '' AND EXISTS(
                        SELECT 1 FROM match_player_stats z
                        WHERE z.match_id = m.id AND z.primary_id = ?
                          AND z.team_num != mps.team_num
                   ) THEN 1 ELSE 0 END) AS was_opponent
            FROM match_player_stats mps
            JOIN matches m ON m.id = mps.match_id
            GROUP BY mps.name, mps.primary_id
        ) {bot_filter}
        ORDER BY n DESC, name
    """
    with store._conn() as con:
        rows = con.execute(sql, (
            self_primary_id or "", self_primary_id or "",
            self_primary_id or "", self_primary_id or "",
        )).fetchall()

    def _row(r) -> str:
        is_bot = bool(r["is_bot"])
        tag = "<span class='tag'>BOT</span>" if is_bot else ""
        href = f"/player/{quote(r['name'], safe='')}"
        n = r["n"] or 1
        wins = r["wins"] or 0
        winpct = (wins / n) * 100
        kind = []
        if r["was_teammate"]: kind.append("teammate")
        if r["was_opponent"]: kind.append("opponent")
        kind_str = " · ".join(kind) or "—"
        return f"""
          <tr class="player-row">
            <td><a class="player-link" href="{href}">{r["name"]}</a> {tag}</td>
            <td class="dim">{r["platform"] or '—'}</td>
            <td class="dim">{kind_str}</td>
            <td>{r["n"]}</td>
            <td><b>{wins}</b>-{r["n"] - wins} <span class="dim">({winpct:.0f}%)</span></td>
            <td>{r["goals"] or 0}</td>
            <td>{r["assists"] or 0}</td>
            <td>{r["saves"] or 0}</td>
          </tr>
        """

    body = f"""
      <h1>All players</h1>
      <p class="caption">Everyone we've recorded in any match. Click a name to see their full stats.</p>
      {_filter_chip_html('/players', 'Filter Bot matches', include_bots)}
      <section>
        <table class="players-table">
          <thead><tr>
            <th>Player</th><th>Platform</th><th>Relation</th>
            <th>Matches</th><th>W-L</th><th>Goals</th><th>Assists</th><th>Saves</th>
          </tr></thead>
          <tbody>
            {"".join(_row(r) for r in rows)}
          </tbody>
        </table>
      </section>
    """
    return _page_wrap("All players", body, active="players")


def _filter_chip_html(action: str, label: str, include_bots: bool, *, extra: str = "") -> str:
    """Compact pill-style filter: a single 'Filter bots' toggle. Submits on change."""
    bot_value = "0" if include_bots else "1"
    is_active = not include_bots  # "Filter bots" is ON when we're hiding bot games
    chip_class = "filter-chip active" if is_active else "filter-chip"
    arrow = "✓" if is_active else ""
    return f"""
      <form class="filter-row" method="get" action="{action}">
        <button type="submit" name="include_bots" value="{bot_value}" class="{chip_class}">
          <span class="chip-mark">{arrow}</span> {label}
        </button>
        {extra}
      </form>
    """


def _not_found_html(name: str) -> str:
    return _page_wrap("Not found", f"""
      <h1>Player not found</h1>
      <p class="caption">No matches in the DB for <b>{name}</b>. Check spelling or
      see <a href="/players">all players</a>.</p>
    """, status=404)


def _history_page_html(store, primary_id, name, *, include_bots=True) -> str:
    table = _match_history_html(
        store, primary_id, name,
        limit=200, include_bots=include_bots,
        show_section_chrome=False,
    )
    body = f"""
      <h1>Match history</h1>
      <p class="caption">Every recorded match. Click any row to drill in.</p>
      {_filter_chip_html('/history', 'Filter Bot matches', include_bots)}
      <section>{table}</section>
    """
    return _page_wrap("Match history", body, active="history")


def _match_detail_html(store, match_id: str, viewer_pid: str | None, viewer_name: str | None) -> str:
    with store._conn() as con:
        m = con.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
        if not m:
            return _page_wrap("Match not found", f"<h1>Match not found</h1><p class='caption'>No match with id <code>{match_id}</code>.</p>", status=404)
        players = con.execute(
            "SELECT * FROM match_player_stats WHERE match_id = ? ORDER BY score DESC", (match_id,)
        ).fetchall()
        extras = con.execute("SELECT * FROM match_extras WHERE match_id = ?", (match_id,)).fetchone()

    arena = _arena_nice(m["arena"] or "")
    started = datetime.fromtimestamp(m["started_at"]).strftime("%b %d, %Y at %H:%M")
    duration = extras["duration_seconds"] if extras else 0
    mm, ss = int(duration // 60), int(duration % 60)
    mode = "Online" if m["is_online"] else "Offline"

    t0_name = m["team0_name"] or "Blue"
    t1_name = m["team1_name"] or "Orange"
    t0_score = m["team0_score"]
    t1_score = m["team1_score"]
    winner = m["winner_team_num"]

    # Peak values across this match (for per-player radar scaling).
    peak_g  = max((p["goals"]   for p in players), default=1) or 1
    peak_a  = max((p["assists"] for p in players), default=1) or 1
    peak_sv = max((p["saves"]   for p in players), default=1) or 1
    peak_sh = max((p["shots"]   for p in players), default=1) or 1
    peak_d  = max((p["demos"]   for p in players), default=1) or 1

    def _team_roster(team_num: int) -> str:
        """Render one full-width team block: colored header bar + roster table + team total row."""
        team_players = sorted(
            [p for p in players if p["team_num"] == team_num],
            key=lambda p: -(p["score"] or 0),
        )
        tname = t0_name if team_num == 0 else t1_name
        tscore = t0_score if team_num == 0 else t1_score
        won = (team_num == winner)
        color_class = "team-blue" if team_num == 0 else "team-orng"
        result_badge = "<span class='roster-result win'>WIN</span>" if won else "<span class='roster-result loss'>LOSS</span>"

        # Team totals.
        t_goals   = sum(p["goals"]   or 0 for p in team_players)
        t_assists = sum(p["assists"] or 0 for p in team_players)
        t_saves   = sum(p["saves"]   or 0 for p in team_players)
        t_shots   = sum(p["shots"]   or 0 for p in team_players)
        t_demos   = sum(p["demos"]   or 0 for p in team_players)
        t_score   = sum(p["score"]   or 0 for p in team_players)

        rows: list[str] = []
        for p in team_players:
            is_viewer = (viewer_pid and p["primary_id"] == viewer_pid and p["name"] == viewer_name) or \
                        (viewer_name and p["name"] == viewer_name and not viewer_pid)
            mvp = " <span class='mvp'>MVP</span>" if p["is_mvp"] else ""
            bot = " <span class='tag'>BOT</span>" if p["is_bot"] else ""
            href = f"/player/{quote(p['name'], safe='')}"
            row_class = "viewer-row" if is_viewer else ""
            rows.append(f"""
              <tr class="{row_class}">
                <td class="player-cell"><a class="player-link" href="{href}">{p['name']}</a>{bot}{mvp}</td>
                <td class="num">{p['score']}</td>
                <td class="num">{p['goals']}</td>
                <td class="num">{p['assists']}</td>
                <td class="num">{p['saves']}</td>
                <td class="num">{p['shots']}</td>
                <td class="num">{p['demos']}</td>
              </tr>
            """)
            if p["ticks_total"] and p["ticks_total"] >= 200:
                ticks = p["ticks_total"]
                air = p["ticks_in_air"] / ticks * 100
                wall = p["ticks_on_wall"] / ticks * 100
                ground = p["ticks_on_ground"] / ticks * 100
                sup = p["ticks_supersonic"] / ticks * 100
                avg_sp = p["speed_sum"] / ticks
                rows.append(f"""
                  <tr class="adv-row">
                    <td colspan="7">
                      <span class="adv-label">advanced</span>
                      Supersonic <b>{sup:.0f}%</b>  ·
                      Air <b>{air:.0f}%</b>  ·
                      Wall <b>{wall:.0f}%</b>  ·
                      Ground <b>{ground:.0f}%</b>  ·
                      Avg speed <b>{avg_sp:.0f}</b>  ·
                      Boost used <b>{p['boost_used']:.0f}</b>
                    </td>
                  </tr>
                """)
            else:
                rows.append(f"""
                  <tr class="adv-row dim">
                    <td colspan="7">
                      <span class="adv-label">advanced</span>
                      <em>Spectator-only fields not available for this player in this match.</em>
                    </td>
                  </tr>
                """)

        return f"""
          <section class="roster {color_class}">
            <div class="roster-header">
              <span class="roster-name">{tname}</span>
              <span class="roster-score">{tscore}</span>
              {result_badge}
            </div>
            <table class="scoreboard">
              <thead><tr>
                <th class="player-cell">Player</th>
                <th class="num">Score</th>
                <th class="num">Goals</th>
                <th class="num">Assists</th>
                <th class="num">Saves</th>
                <th class="num">Shots</th>
                <th class="num">Demos</th>
              </tr></thead>
              <tbody>{"".join(rows)}</tbody>
              <tfoot>
                <tr class="team-total-row">
                  <td class="player-cell">Team total</td>
                  <td class="num">{t_score}</td>
                  <td class="num">{t_goals}</td>
                  <td class="num">{t_assists}</td>
                  <td class="num">{t_saves}</td>
                  <td class="num">{t_shots}</td>
                  <td class="num">{t_demos}</td>
                </tr>
              </tfoot>
            </table>
          </section>
        """

    def _radar_card(p) -> str:
        is_viewer = (viewer_pid and p["primary_id"] == viewer_pid and p["name"] == viewer_name)
        team_class = "team-blue" if p["team_num"] == 0 else "team-orng"
        marker = " <span class='you-tag'>YOU</span>" if is_viewer else ""
        bot = " <span class='tag'>BOT</span>" if p["is_bot"] else ""
        values = [
            ("Goals",   p["goals"],   peak_g),
            ("Shots",   p["shots"],   peak_sh),
            ("Demos",   p["demos"],   peak_d),
            ("Saves",   p["saves"],   peak_sv),
            ("Assists", p["assists"], peak_a),
        ]
        href = f"/player/{quote(p['name'], safe='')}"
        return f"""
          <div class="player-radar {team_class}">
            <div class="pr-header">
              <a class="player-link" href="{href}">{p['name']}</a>{bot}{marker}
              <span class="pr-score">{p['score']}</span>
            </div>
            {_radar_svg(values, size=220)}
          </div>
        """

    blue_radars = "".join(_radar_card(p) for p in sorted(
        [p for p in players if p["team_num"] == 0], key=lambda p: -(p["score"] or 0)))
    orng_radars = "".join(_radar_card(p) for p in sorted(
        [p for p in players if p["team_num"] == 1], key=lambda p: -(p["score"] or 0)))

    radars_html = f"""
      <section>
        <h2>Per-player radars  <span class="caption">scaled to this match's peaks</span></h2>
        <div class="team-radar-group">
          <div class="team-radar-label team-blue"><span class="team-stripe"></span>{t0_name}</div>
          <div class="player-radars">{blue_radars}</div>
        </div>
        <div class="team-radar-group">
          <div class="team-radar-label team-orng"><span class="team-stripe"></span>{t1_name}</div>
          <div class="player-radars">{orng_radars}</div>
        </div>
      </section>
    """

    # Hero scoreboard banner. Always blue on the left, orange on the right.
    blue_winner = "winner" if winner == 0 else ""
    orng_winner = "winner" if winner == 1 else ""
    body = f"""
      <div class="breadcrumb"><a href="/history">&larr; Back to matches</a></div>
      <header class="match-hero">
        <div class="hero-team hero-blue {blue_winner}">
          <div class="hero-team-name">{t0_name}</div>
          <div class="hero-team-score">{t0_score}</div>
        </div>
        <div class="hero-mid">
          <div class="hero-vs">FINAL</div>
          <div class="hero-meta">
            <span>{arena}</span>
            <span class="dot">·</span>
            <span>{mode}</span>
            <span class="dot">·</span>
            <span>{mm}:{ss:02d}</span>
          </div>
          <div class="hero-date">{started}</div>
        </div>
        <div class="hero-team hero-orng {orng_winner}">
          <div class="hero-team-score">{t1_score}</div>
          <div class="hero-team-name">{t1_name}</div>
        </div>
      </header>

      {_team_roster(0)}
      {_team_roster(1)}
      {radars_html}
    """
    return _page_wrap("Match detail", body, active="history")


def _about_html() -> str:
    body = """
      <h1>How Carball Tracker works</h1>
      <p class="caption">A short explainer of where the numbers come from, what's possible,
        and what's intentionally out of reach.</p>

      <section>
        <h2>Where the data comes from</h2>
        <p>Rocket League ships a built-in <b>Stats API</b>. When you set <code>PacketSendRate</code>
          to a non-zero value in <code>DefaultStatsAPI.ini</code>, the game opens a local TCP
          socket on <code>127.0.0.1:49123</code> and streams JSON events while you play.
          Carball Tracker connects to that socket, persists everything to a local SQLite database
          (<code>data/carball.db</code>), and turns the events into match summaries, lifetime
          stats, and the OBS overlay.</p>
        <p>No remote services are involved. No third-party APIs (no ballchasing, no tracker.gg).
          The only network call out is your Discord bot posting embeds to your channel.</p>
      </section>

      <section>
        <h2>What we capture for every player</h2>
        <p>These are emitted for everyone in every match - you, teammates, opponents, bots:</p>
        <ul>
          <li>Score, Goals, Assists, Saves, Shots, Demos, Touches</li>
          <li>Team affiliation, platform (Steam / Epic / Switch), MVP designation</li>
          <li>Match-level: final score, arena, winner, duration, crossbar hits, ball touches with XYZ</li>
        </ul>
      </section>

      <section>
        <h2>What we only capture for you and teammates</h2>
        <p>Psyonix marks several fields as <code>SPECTATOR</code>-only in the API spec, meaning
          they're emitted for the player you're spectating and your team, but omitted for the
          opposing team. The advanced stats panel reflects this:</p>
        <ul>
          <li>Current boost (0-100)</li>
          <li>Car speed</li>
          <li>On-wall / on-ground / has-car / is-boosting booleans</li>
        </ul>
        <p>Derived metrics that build on those fields - time on wall, time in air, supersonic
          percentage, average speed, boost used per match - are therefore reliable for you and
          your teammates, but missing or sparse for opponents. The match detail page surfaces
          this explicitly in the advanced row for each player.</p>
      </section>

      <section>
        <h2>What we don't have</h2>
        <ul>
          <li><b>MMR / rank / season stats</b> - not in the Stats API. Use the in-game scoreboard.</li>
          <li><b>Per-tick XYZ position</b> for players. The API gives speed and on-wall/on-ground
            booleans but no coordinates per player, so we can't draw positioning heatmaps from
            this source.</li>
          <li><b>Boost pad pickups, camera settings, ball touches per replay second</b> - those
            come from parsing the <code>.replay</code> file format, which is a separate (much
            heavier) project we deliberately don't do.</li>
        </ul>
      </section>

      <section>
        <h2>Match types</h2>
        <p>The Stats API does not emit a <code>playlist</code> field. We infer match type from
          what's visible in the stream:</p>
        <ul>
          <li><b>Exhibition</b> - the match GUID is empty (offline practice)</li>
          <li><b>Private / Tournament</b> - GUID present + at least one team has a custom name
            (anything other than "Blue" / "Orange")</li>
          <li><b>Casual vs Bots</b> - GUID present + a bot is in the player list</li>
          <li><b>Online Matchmaking</b> - GUID present + standard team names + no bots</li>
        </ul>
      </section>

      <section>
        <h2>Sharing the dashboard</h2>
        <p>The server binds to <code>0.0.0.0:5050</code> by default, so anyone on your LAN can
          hit the dashboard from their phone or laptop. To find your LAN URL, check the
          startup output of <code>run.bat</code> - it prints something like
          <code>http://192.168.1.42:5050/dashboard</code>. Lock it back down to loopback by
          setting <code>CARBALL_SERVER_HOST=127.0.0.1</code> in <code>.env</code>.</p>
      </section>

      <section>
        <h2>Limitations to keep in mind</h2>
        <ul>
          <li>You only get advanced stats for players you've actually been teammates with.</li>
          <li>"Head-to-head" only makes sense for friends you play repeatedly - random
            matchmaking opponents drift in and out of your database.</li>
          <li>The Stats API only emits during active matches. Free play, training mode,
            and replay viewing emit little or nothing useful.</li>
        </ul>
      </section>
    """
    return _page_wrap("How it works", body, active="about")


def _page_wrap(title: str, body_html: str, *, status: int = 200, active: str = "") -> str:
    """Common HTML chrome for non-dashboard pages."""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Carball Tracker - {title}</title>
{_STYLE_TAG}
</head><body>
<div class="wrapper">
  {_nav(active)}
  {body_html}
</div>
{_THEME_SCRIPT}
</body></html>"""


_LOGO_SVG = '''<svg class="brand-mark" viewBox="0 0 64 36" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
  <defs>
    <linearGradient id="cbflame" x1="0" y1="0" x2="40" y2="20" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="#ffd400"/><stop offset="0.55" stop-color="#ff7a18"/><stop offset="1" stop-color="#ff2d2d"/>
    </linearGradient>
    <radialGradient id="cbball" cx="9" cy="14" r="18" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="#ffffff"/><stop offset="1" stop-color="#cfd6e0"/>
    </radialGradient>
    <linearGradient id="cbcar" x1="32" y1="14" x2="60" y2="28" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="#ff9a3c"/><stop offset="1" stop-color="#e85a14"/>
    </linearGradient>
  </defs>

  <!-- Boost flame trail (left of car, pointing at the ball) -->
  <path d="M34 22 C 28 22, 22 18, 16 19 C 22 22, 28 23, 34 24 Z"
        fill="url(#cbflame)" opacity="0.85"/>
  <path d="M32 26 C 27 27, 22 25, 18 27 C 23 28, 28 29, 32 28 Z"
        fill="url(#cbflame)" opacity="0.55"/>

  <!-- Soccer ball (left) -->
  <circle cx="11" cy="18" r="9.5" fill="url(#cbball)" stroke="#0e121a" stroke-width="1.3"/>
  <polygon points="11,14 14.6,16.3 13.2,20.5 8.8,20.5 7.4,16.3" fill="#0e121a"/>
  <g stroke="#0e121a" stroke-width="1" stroke-linecap="round">
    <line x1="11" y1="14" x2="11" y2="9"/>
    <line x1="14.6" y1="16.3" x2="19.4" y2="14.5"/>
    <line x1="13.2" y1="20.5" x2="16.2" y2="24.8"/>
    <line x1="8.8" y1="20.5" x2="5.8" y2="24.8"/>
    <line x1="7.4" y1="16.3" x2="2.6" y2="14.5"/>
  </g>

  <!-- Car body (right) - simple stylized RL battle-car silhouette -->
  <!-- Lower chassis -->
  <path d="M34 26 L 36 22 L 42 20 L 48 16 L 56 17 L 62 21 L 62 26 Z"
        fill="url(#cbcar)" stroke="#0e121a" stroke-width="1.2" stroke-linejoin="round"/>
  <!-- Windshield -->
  <path d="M45 20 L 48 17 L 53 17.3 L 54 20 Z" fill="#0e121a" opacity="0.85"/>
  <!-- Rear spoiler / wing -->
  <path d="M34 22 L 36 19 L 38 19 L 38 22 Z" fill="#0e121a"/>
  <!-- Wheels -->
  <circle cx="41" cy="27" r="3.6" fill="#0e121a"/>
  <circle cx="41" cy="27" r="1.6" fill="#3a4150"/>
  <circle cx="56" cy="27" r="3.6" fill="#0e121a"/>
  <circle cx="56" cy="27" r="1.6" fill="#3a4150"/>
</svg>'''

def _nav(active: str = "") -> str:
    items = [
        ("dashboard", "/dashboard",     "Me"),
        ("history",   "/history",       "Matches"),
        ("players",   "/players",       "Players"),
        ("overlay",   "/overlay",       "Browser overlay"),
        ("about",     "/about",         "How it works"),
    ]
    parts = []
    for key, href, label in items:
        klass = "nav-link active" if key == active else "nav-link"
        parts.append(f'<a class="{klass}" href="{href}">{label}</a>')
    return f'''
<nav class="topnav">
  <a class="brand" href="/dashboard">
    {_LOGO_SVG}
    <span class="brand-text">Carball <span class="brand-text-dim">Tracker</span></span>
  </a>
  <div class="nav-links">{"".join(parts)}</div>
  <button id="theme-toggle" type="button" aria-label="Switch theme">
    <span class="theme-icon" id="theme-icon" aria-hidden="true">&#9788;</span>
    <span class="theme-label" id="theme-label">Switch to light</span>
  </button>
</nav>
'''

_THEME_SCRIPT = """
<script>
(function () {
  function updateButton(theme) {
    var icon = document.getElementById('theme-icon');
    var label = document.getElementById('theme-label');
    if (!icon || !label) return;
    if (theme === 'dark') {
      icon.innerHTML = '&#9788;';
      label.textContent = 'Switch to light';
    } else {
      icon.innerHTML = '&#9790;';
      label.textContent = 'Switch to dark';
    }
  }
  function set(t) {
    document.documentElement.setAttribute('data-theme', t);
    localStorage.setItem('carball-theme', t);
    updateButton(t);
  }
  var saved = localStorage.getItem('carball-theme');
  if (!saved) {
    var prefers = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches;
    saved = prefers ? 'light' : 'dark';
  }
  set(saved);
  var btn = document.getElementById('theme-toggle');
  if (btn) btn.addEventListener('click', function () {
    var cur = document.documentElement.getAttribute('data-theme') || 'dark';
    set(cur === 'dark' ? 'light' : 'dark');
  });
})();
</script>
"""

_STYLE_TAG = """
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
  :root {
    --bg:        #0b0e15;
    --bg-card:   rgba(255,255,255,0.03);
    --bg-hover:  rgba(255,255,255,0.06);
    --text:      #e6edf3;
    --text-dim:  #8b95a4;
    --border:    rgba(255,255,255,0.07);
    --good:      #4ade80;
    --bad:       #f87171;
    --accent:    #ff7a18;
    --accent-2:  #ff3d3d;
    --accent-bg: rgba(255,122,24,0.14);
    --team-blue: #1873ff;
    --team-orng: #ff7a18;
  }
  [data-theme="light"] {
    --bg:        #f6f7fb;
    --bg-card:   #ffffff;
    --bg-hover:  #f1f5f9;
    --text:      #0f172a;
    --text-dim:  #5b6573;
    --border:    rgba(0,0,0,0.08);
    --good:      #16a34a;
    --bad:       #dc2626;
    --accent:    #ea580c;
    --accent-2:  #dc2626;
    --accent-bg: rgba(234,88,12,0.10);
    --team-blue: #1873ff;
    --team-orng: #c26418;
  }
  * { box-sizing: border-box; }
  html, body { margin:0; padding:0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: "Inter", ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    font-size: 14px;
    line-height: 1.5;
    letter-spacing: -0.005em;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
    transition: background 200ms ease, color 200ms ease;
  }
  code, .mono {
    font-family: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace;
  }
  .wrapper { max-width: 1080px; margin: 0 auto; padding: 0 24px 48px 24px; }

  .topnav {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 0; margin-bottom: 20px;
    border-bottom: 1px solid var(--border);
    gap: 18px;
  }
  .brand {
    display: flex; align-items: center; gap: 10px;
    text-decoration: none; color: var(--text);
  }
  .brand-mark { width: 56px; height: 32px; flex-shrink: 0; }
  .brand-text { font-weight: 800; font-size: 18px; letter-spacing: -0.025em; }
  .brand-text-dim { color: var(--text-dim); font-weight: 500; letter-spacing: 0.02em; }
  .nav-links { display: flex; align-items: center; gap: 4px; flex: 1; justify-content: center; }
  .nav-link {
    color: var(--text-dim);
    text-decoration: none;
    font-size: 13px;
    letter-spacing: 0.02em;
    padding: 6px 14px;
    border-radius: 8px;
    transition: all 150ms ease;
  }
  .nav-link:hover { color: var(--text); background: var(--bg-hover); }
  .nav-link.active {
    color: var(--accent);
    background: var(--accent-bg);
    font-weight: 600;
  }
  #theme-toggle {
    display: inline-flex; align-items: center; gap: 8px;
    background: var(--bg-card); color: var(--text-dim);
    border: 1px solid var(--border); border-radius: 999px;
    padding: 6px 14px 6px 10px; cursor: pointer;
    font-family: inherit; font-size: 12px; font-weight: 600;
    transition: all 150ms ease;
  }
  #theme-toggle:hover { background: var(--accent-bg); color: var(--accent); border-color: var(--accent); }
  #theme-toggle .theme-icon { font-size: 14px; line-height: 1; }
  #theme-toggle .theme-label { letter-spacing: 0.01em; }

  h1 { margin: 4px 0 0 0; font-weight: 700; font-size: 24px; letter-spacing: -0.01em; }
  .who { color: var(--text-dim); font-size: 13px; margin: 4px 0 24px 0; }
  .caption { color: var(--text-dim); font-size: 12px; }

  section {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 18px 22px;
    margin: 0 0 14px 0;
  }
  h2 {
    margin: 0 0 12px 0;
    font-size: 11px; font-weight: 700;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }

  /* KPI tiles */
  .kpi-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 12px;
    margin: 0 0 14px 0;
  }
  .kpi {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 14px 18px;
  }
  .kpi.primary { border-color: var(--accent); background: var(--accent-bg); }
  .kpi-value {
    font-size: 24px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.01em;
  }
  .kpi-label {
    color: var(--text-dim);
    font-size: 11px;
    margin-top: 4px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }

  /* Tables */
  table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
  td, th { padding: 8px 12px; text-align: left; vertical-align: middle; }
  th { color: var(--text-dim); font-weight: 600; font-size: 11px;
       text-transform: uppercase; letter-spacing: 0.08em;
       border-bottom: 1px solid var(--border); }
  tr { border-bottom: 1px solid var(--border); }
  tr:last-child { border-bottom: none; }
  td.cmp, td.dim { color: var(--text-dim); font-size: 12px; }

  /* Stat-row table from Dashboard sections */
  section > table td:first-child { color: var(--text-dim); width: 38%; }

  /* History table */
  table.history { font-size: 13px; }
  table.history td:nth-child(1) { width: 44px; }
  table.history td:nth-child(2) { width: 120px; }
  table.history th.num, table.history td.num { text-align: center; width: 44px; }
  table.history td.score-cell { font-weight: 500; }
  .badge {
    display: inline-block;
    width: 26px; text-align: center;
    padding: 2px 0; border-radius: 6px;
    font-size: 11px; font-weight: 700;
  }
  .badge.win  { background: rgba(74,222,128,0.18); color: var(--good); }
  .badge.loss { background: rgba(248,113,113,0.18); color: var(--bad); }
  .match-row.win  td:first-child { color: var(--good); }
  .match-row.loss td:first-child { color: var(--bad); }
  .match-row { cursor: pointer; }
  .match-row:hover { background: var(--bg-hover); }

  /* Radar */
  .radar-section { display:flex; flex-direction:column; align-items:center; padding:20px; }
  .radar-section svg { margin: 4px 0 8px 0; }

  /* Players directory */
  .player-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
    gap: 12px;
    margin-top: 14px;
  }
  .player-card {
    display: flex; gap: 12px; align-items: center;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 14px;
    text-decoration: none;
    color: var(--text);
    transition: background 150ms ease, transform 150ms ease;
  }
  .player-card:hover { background: var(--bg-hover); transform: translateY(-1px); }
  .avatar {
    width: 44px; height: 44px; border-radius: 12px;
    background: var(--accent-bg);
    color: var(--accent);
    display: flex; align-items: center; justify-content: center;
    font-weight: 700; font-size: 14px; letter-spacing: 0.02em;
  }
  .pc-body { flex: 1; min-width: 0; }
  .pc-name { font-weight: 600; white-space: nowrap; text-overflow: ellipsis; overflow: hidden; }
  .pc-meta { color: var(--text-dim); font-size: 11px; margin-top: 2px; }
  .pc-stats { font-size: 12px; margin-top: 4px; color: var(--text-dim); display: flex; gap: 10px; }
  .pc-stats b { color: var(--text); font-weight: 600; }

  /* Players table */
  .players-table td:nth-child(1) { font-weight: 600; }
  .players-table .player-link { color: var(--text); text-decoration: none; border-bottom: 1px dotted var(--border); }
  .players-table .player-link:hover { color: var(--accent); border-color: var(--accent); }
  .tag {
    display: inline-block; padding: 1px 6px; border-radius: 4px;
    background: var(--bg-hover); color: var(--text-dim);
    font-size: 10px; font-weight: 600; letter-spacing: 0.08em;
  }
  .mvp {
    display: inline-block; padding: 1px 6px; border-radius: 4px;
    background: var(--accent-bg); color: var(--accent);
    font-size: 10px; font-weight: 700; letter-spacing: 0.08em;
  }

  /* Filter chips - one toggle per concept, no apply button */
  .filter-row {
    display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
    margin: 0 0 14px 0;
  }
  .filter-chip {
    display: inline-flex; align-items: center; gap: 6px;
    background: var(--bg-card);
    border: 1px solid var(--border);
    color: var(--text-dim);
    padding: 6px 14px;
    border-radius: 999px;
    font-size: 12px; font-weight: 600;
    cursor: pointer; text-decoration: none;
    transition: all 150ms ease;
    font-family: inherit;
  }
  .filter-chip:hover { color: var(--text); background: var(--bg-hover); }
  .filter-chip.active {
    background: var(--accent-bg);
    color: var(--accent);
    border-color: var(--accent);
  }
  .chip-mark {
    display: inline-block; width: 12px; text-align: center; opacity: 0.85;
  }

  /* Profile header bot badge */
  .profile-header {
    display: flex; align-items: center; gap: 12px;
  }
  .profile-bot-badge {
    display: inline-block;
    background: rgba(255,122,24,0.16); color: var(--accent);
    border: 1px solid var(--accent);
    padding: 3px 12px;
    border-radius: 999px;
    font-size: 11px; font-weight: 800; letter-spacing: 0.08em;
    vertical-align: middle;
  }

  .see-all {
    float: right; color: var(--accent); text-decoration: none;
    font-size: 11px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase;
  }
  .see-all:hover { text-decoration: underline; }

  .match-link { color: var(--text); text-decoration: none; }
  .match-link:hover { color: var(--accent); }

  /* Match detail */
  .breadcrumb { margin: 0 0 14px 0; }
  .breadcrumb a {
    color: var(--text-dim); text-decoration: none; font-size: 12px; font-weight: 600;
    letter-spacing: 0.02em; padding: 4px 10px; border-radius: 6px;
    background: var(--bg-card); border: 1px solid var(--border);
    transition: all 150ms ease;
  }
  .breadcrumb a:hover { color: var(--accent); border-color: var(--accent); }

  /* Hero scoreboard banner (ESPN/ballchasing style) */
  .match-hero {
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    align-items: stretch;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 14px;
    overflow: hidden;
    margin-bottom: 16px;
  }
  .hero-team {
    display: flex; align-items: center; gap: 18px;
    padding: 22px 28px;
    position: relative;
  }
  .hero-team.hero-blue {
    background: linear-gradient(90deg, rgba(24,115,255,0.22) 0%, rgba(24,115,255,0.04) 100%);
    justify-content: flex-end; text-align: right;
  }
  .hero-team.hero-orng {
    background: linear-gradient(270deg, rgba(255,122,24,0.22) 0%, rgba(255,122,24,0.04) 100%);
    justify-content: flex-start; text-align: left;
  }
  .hero-team-name {
    font-size: 18px; font-weight: 700; letter-spacing: -0.01em;
    color: var(--text);
    max-width: 220px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .hero-team-score {
    font-size: 56px; font-weight: 800; letter-spacing: -0.04em; line-height: 1;
    font-variant-numeric: tabular-nums;
  }
  .hero-blue .hero-team-score { color: var(--team-blue); }
  .hero-orng .hero-team-score { color: var(--team-orng); }
  .hero-team.winner::after {
    content: "WIN";
    position: absolute; top: 10px; right: 14px;
    font-size: 9px; font-weight: 800; letter-spacing: 0.12em;
    color: var(--good);
    background: rgba(74,222,128,0.16);
    padding: 2px 6px; border-radius: 3px;
  }
  .hero-orng.winner::after { right: auto; left: 14px; }
  .hero-mid {
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    padding: 12px 22px;
    background: var(--bg-hover);
    border-left: 1px solid var(--border);
    border-right: 1px solid var(--border);
    min-width: 200px;
  }
  .hero-vs {
    font-size: 11px; font-weight: 800; letter-spacing: 0.18em;
    color: var(--accent);
    background: var(--accent-bg);
    padding: 3px 10px; border-radius: 4px;
    margin-bottom: 8px;
  }
  .hero-meta {
    display: flex; gap: 6px; flex-wrap: wrap; justify-content: center;
    font-size: 12px; color: var(--text); font-weight: 500;
  }
  .hero-meta .dot { color: var(--text-dim); }
  .hero-date { margin-top: 4px; font-size: 11px; color: var(--text-dim); }
  @media (max-width: 700px) {
    .match-hero { grid-template-columns: 1fr; }
    .hero-mid { border-left: none; border-right: none;
      border-top: 1px solid var(--border); border-bottom: 1px solid var(--border); }
    .hero-team-score { font-size: 40px; }
  }

  /* Team roster sections (stacked vertically below the hero) */
  .roster {
    padding: 0;
    overflow: hidden;
    margin: 0 0 14px 0;
    position: relative;
  }
  .roster::before {
    content: ""; position: absolute; top: 0; bottom: 0; left: 0; width: 4px;
  }
  .roster.team-blue::before { background: var(--team-blue); }
  .roster.team-orng::before { background: var(--team-orng); }
  .roster-header {
    display: flex; align-items: center; gap: 14px;
    padding: 14px 22px;
    border-bottom: 1px solid var(--border);
    background: var(--bg-hover);
  }
  .roster-name { font-size: 18px; font-weight: 700; letter-spacing: -0.01em; color: var(--text); }
  .roster.team-blue .roster-name { color: var(--team-blue); }
  .roster.team-orng .roster-name { color: var(--team-orng); }
  .roster-score {
    font-size: 24px; font-weight: 800; letter-spacing: -0.02em;
    color: var(--text); font-variant-numeric: tabular-nums;
    margin-left: auto;
  }
  .roster-result {
    font-size: 10px; font-weight: 800; letter-spacing: 0.12em;
    padding: 4px 10px; border-radius: 4px;
  }
  .roster-result.win  { background: rgba(74,222,128,0.18); color: var(--good); }
  .roster-result.loss { background: rgba(248,113,113,0.16); color: var(--bad); }

  .scoreboard { width: 100%; margin: 0; }
  .scoreboard thead th {
    font-size: 10px; padding: 10px 14px;
    border-bottom: 1px solid var(--border);
  }
  .scoreboard td.num, .scoreboard th.num {
    text-align: right; font-variant-numeric: tabular-nums; width: 64px;
  }
  .scoreboard td.player-cell, .scoreboard th.player-cell {
    text-align: left; padding-left: 22px;
  }
  .scoreboard tbody td { padding: 10px 14px; font-size: 13px; }
  .scoreboard .viewer-row { background: var(--accent-bg); }
  .scoreboard .viewer-row td.player-cell::before { content: "\\25B6  "; color: var(--accent); }
  .scoreboard tr.adv-row td {
    background: rgba(255,255,255,0.02);
    color: var(--text-dim);
    font-size: 11.5px;
    padding: 4px 14px 8px 22px;
    border-bottom: 1px solid var(--border);
    text-align: left;
  }
  .scoreboard tr.adv-row b { color: var(--text); font-weight: 600; }
  .scoreboard tr.adv-row em {
    color: var(--text-dim); font-style: normal; opacity: 0.7;
  }
  .scoreboard tr.adv-row .adv-label {
    display: inline-block;
    background: var(--bg-hover); color: var(--text-dim);
    font-size: 9px; font-weight: 700; letter-spacing: 0.08em;
    text-transform: uppercase;
    padding: 1px 6px; border-radius: 3px;
    margin-right: 8px;
  }
  .scoreboard tfoot .team-total-row td {
    background: var(--bg-hover);
    font-weight: 700;
    color: var(--text);
    border-top: 1px solid var(--border);
  }

  /* Per-team radar groups */
  .team-radar-group { margin-top: 14px; }
  .team-radar-group:first-of-type { margin-top: 6px; }
  .team-radar-label {
    display: flex; align-items: center; gap: 10px;
    font-size: 12px; font-weight: 700; letter-spacing: 0.06em;
    text-transform: uppercase;
    margin: 0 0 8px 0;
    padding: 6px 0;
  }
  .team-radar-label.team-blue { color: var(--team-blue); }
  .team-radar-label.team-orng { color: var(--team-orng); }
  .team-radar-label .team-stripe {
    display: inline-block; width: 24px; height: 3px; border-radius: 2px;
  }
  .team-radar-label.team-blue .team-stripe { background: var(--team-blue); }
  .team-radar-label.team-orng .team-stripe { background: var(--team-orng); }

  .player-radars {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    gap: 12px;
  }
  .player-radar {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 12px;
  }
  .player-radar.team-blue { border-top: 3px solid var(--team-blue); }
  .player-radar.team-orng { border-top: 3px solid var(--team-orng); }
  .player-radar svg { width: 100%; height: auto; max-width: 220px; display: block; margin: 0 auto; }
  .pr-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; font-size: 13px; }
  .pr-score { color: var(--text-dim); font-variant-numeric: tabular-nums; font-weight: 600; }
  .you-tag {
    background: var(--accent); color: var(--bg);
    font-size: 9px; font-weight: 800; letter-spacing: 0.08em;
    padding: 2px 6px; border-radius: 3px;
    margin-left: 6px;
  }

  /* Overlay picker */
  .overlay-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
    gap: 16px; margin: 16px 0;
  }
  .overlay-card {
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 14px; padding: 18px; display: flex; flex-direction: column;
  }
  .overlay-card h3 {
    margin: 0 0 4px 0; font-size: 15px; font-weight: 700;
  }
  .overlay-card .ov-desc {
    color: var(--text-dim); font-size: 12px; margin: 0 0 14px 0; min-height: 30px;
  }
  /* Preview frame simulates "over gameplay" with a subtle dark grain */
  .overlay-preview {
    background:
      radial-gradient(ellipse at center, rgba(255,122,24,0.05) 0%, transparent 60%),
      linear-gradient(135deg, #1a1d24 0%, #0e1218 100%);
    border: 1px solid var(--border); border-radius: 10px;
    overflow: hidden; margin-bottom: 14px;
    display: flex; align-items: stretch;
  }
  .overlay-preview.live { padding: 8px; }
  .overlay-iframe {
    width: 100%; border: 0; background: transparent;
    pointer-events: none;
  }
  .setup-list {
    color: var(--text-dim); font-size: 13px; line-height: 1.8;
    padding-left: 20px;
  }
  .setup-list li { margin-bottom: 4px; }
  .setup-list code {
    background: var(--bg-hover); padding: 1px 6px; border-radius: 4px;
    font-size: 12px; color: var(--text);
  }
  .setup-list b { color: var(--text); }
  .copy-row {
    display: flex; gap: 8px; align-items: center;
    background: var(--bg); border: 1px solid var(--border);
    border-radius: 8px; padding: 4px 4px 4px 12px;
    font-size: 12px; font-family: ui-monospace, "SF Mono", Menlo, monospace;
  }
  .copy-row code { color: var(--text-dim); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .copy-btn {
    background: var(--accent); color: var(--bg); border: none;
    border-radius: 6px; padding: 6px 12px; cursor: pointer;
    font-size: 11px; font-weight: 700; letter-spacing: 0.04em;
  }
  .copy-btn:hover { filter: brightness(1.15); }
  .copy-btn.copied { background: var(--good); color: var(--bg); }
  .overlay-meta {
    margin-top: 12px; color: var(--text-dim); font-size: 11px; display: flex; gap: 12px;
  }
  .open-link { color: var(--accent); text-decoration: none; font-weight: 600; }
  .open-link:hover { text-decoration: underline; }
</style>
"""


def _overlay_picker_html(host: str) -> str:
    """Picker page with LIVE iframe previews + copy URLs for each overlay mode."""
    modes = [
        ("live",    "Live HUD",
         "Full BARL-style scoreboard during the match. Team names, scores, clock, and per-player stats — all live.",
         "640 x 230", "top-center", 230),
        ("last",    "Last Match Card",
         "Final scoreline + per-player stats from the most recent finished match. Persists between matches.",
         "640 x 260", "any corner", 260),
        ("session", "Session Tracker",
         "Running W-L, current streak, last-10 form (✓/✗), session totals. Tiny corner companion.",
         "340 x 110", "bottom-left", 110),
        ("me",      "My Stats Mini",
         "Just your Goals / Assists / Saves / Shots line. Smallest footprint — fits beside the RL boost gauge.",
         "280 x 50",  "any corner", 60),
    ]
    cards = []
    for mode, title, desc, size, place, prev_h in modes:
        url = f"http://{host}/overlay/{mode}"
        cards.append(f"""
          <div class="overlay-card">
            <h3>{title}</h3>
            <p class="ov-desc">{desc}</p>
            <div class="overlay-preview live" style="height:{prev_h + 20}px;">
              <iframe class="overlay-iframe" src="/overlay/{mode}" style="height:{prev_h + 16}px;"
                title="Preview of {title}"></iframe>
            </div>
            <div class="copy-row">
              <code id="url-{mode}">{url}</code>
              <button class="copy-btn" type="button" data-target="url-{mode}">Copy</button>
            </div>
            <div class="overlay-meta">
              <a class="open-link" href="/overlay/{mode}" target="_blank">Preview fullscreen ↗</a>
              <span class="dim">{size} · {place}</span>
            </div>
          </div>
        """)

    body = f"""
      <h1>Browser overlay</h1>
      <p class="caption">Drop these URLs into OBS as a Browser Source. Transparent by default
        so they composite cleanly over your gameplay capture.</p>

      <div class="overlay-grid">{"".join(cards)}</div>

      <section>
        <h2>OBS setup</h2>
        <ol class="setup-list">
          <li>In OBS: <b>+</b> under Sources → <b>Browser</b> → name it (e.g. "Live HUD").</li>
          <li>Paste the URL into the URL field and set Width/Height to the recommended size.</li>
          <li>Check <b>Shutdown source when not visible</b> and <b>Refresh when scene activates</b>.</li>
          <li>Position the source over your gameplay capture, avoiding RL's own UI corners.</li>
          <li>Carball must be running (<code>run.bat</code>) for the URL to respond.</li>
        </ol>
      </section>

      <script>
        document.querySelectorAll('.copy-btn').forEach(function(btn) {{
          btn.addEventListener('click', function() {{
            var code = document.getElementById(btn.dataset.target);
            if (!code) return;
            navigator.clipboard.writeText(code.textContent.trim()).then(function() {{
              btn.classList.add('copied');
              btn.textContent = 'Copied!';
              setTimeout(function() {{ btn.classList.remove('copied'); btn.textContent = 'Copy'; }}, 1400);
            }});
          }});
        }});
      </script>
    """
    return _page_wrap("Browser overlay", body, active="overlay")


def _dashboard_html(d, store=None, primary_id: str | None = None,
                    name: str | None = None, is_self: bool = False) -> str:
    """Render the Dashboard dataclass into a single-file HTML page."""
    kpis = _kpi_tiles_from_dashboard(d)
    radar = _radar_block_for_player(store, primary_id, name)
    history = _match_history_html(store, primary_id, name, limit=8)

    detail_sections: list[str] = []
    skip_titles = {"Overview", "Per-match averages"}  # superseded by KPIs
    for g in d.all_groups():
        if not g.lines or g.title in skip_titles:
            continue
        body = "\n".join(
            f'<tr><td>{ml.label}</td><td><b>{ml.value}</b></td><td class="cmp">{ml.comparison}</td></tr>'
            for ml in g.lines
        )
        detail_sections.append(f'<section><h2>{g.title}</h2><table>{body}</table></section>')

    page_title = "Career dashboard" if is_self else f"{name or d.player_label}"
    active = "dashboard" if is_self else ""

    # Detect bot status to surface a clear badge in the header.
    is_bot = False
    if store and (name or primary_id):
        try:
            with store._conn() as con:
                where = "primary_id = ?" if primary_id else "name = ?"
                arg = primary_id or name
                row = con.execute(f"SELECT MAX(is_bot) AS b FROM match_player_stats WHERE {where}", (arg,)).fetchone()
                is_bot = bool(row and row["b"])
        except Exception:
            pass
    bot_badge = '<span class="profile-bot-badge">BOT ACCOUNT</span>' if is_bot else ""

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Carball Tracker - {d.player_label}</title>
{_STYLE_TAG}
</head><body>
<div class="wrapper">
  {_nav(active)}
  <div class="profile-header">
    <h1>{page_title} {bot_badge}</h1>
  </div>
  <div class="who">{d.player_label}</div>
  {kpis}
  {radar}
  {history}
  {''.join(detail_sections)}
</div>
{_THEME_SCRIPT}
</body></html>"""


async def serve(app: FastAPI, host: str = "127.0.0.1", port: int = 5050) -> None:
    import uvicorn  # local import keeps cli imports cheap
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    await server.serve()
