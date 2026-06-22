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
import html
import json
import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from datetime import datetime
from urllib.parse import quote

from fastapi import FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .session import MatchSummary, SessionTotals
from .arenas import arena_nice as _arena_nice


# ---- Multi-user sync wire models (POST /api/v1/match-summary body) ----

class PlayerRowUpload(BaseModel):
    primary_id: str
    name: str
    team_num: int
    goals: int = 0
    shots: int = 0
    assists: int = 0
    saves: int = 0
    demos: int = 0
    touches: int = 0
    score: int = 0
    is_bot: bool = False
    is_mvp: bool = False
    platform: str = "Unknown"
    ticks_total: int = 0
    ticks_on_wall: int = 0
    ticks_on_ground: int = 0
    ticks_in_air: int = 0
    ticks_boosting: int = 0
    ticks_supersonic: int = 0
    ticks_zero_boost: int = 0
    ticks_full_boost: int = 0
    speed_sum: float = 0.0
    speed_max: float = 0.0
    boost_used: float = 0.0


class MatchSummaryUpload(BaseModel):
    match_id: str
    started_at: float
    ended_at: float
    arena: str
    team0_score: int
    team1_score: int
    team0_name: str = "Blue"
    team1_name: str = "Orange"
    team0_color: str = ""
    team1_color: str = ""
    winner_team_num: int
    is_online: bool = True
    crossbar_hits: int = 0
    parser_version: int = 0
    duration_seconds: float = 0.0
    my_row: PlayerRowUpload
    other_rows: list[PlayerRowUpload] = Field(default_factory=list)
    # Match-level data needed for heatmaps & goal-sequence views. Not "owned"
    # by any one player — first writer wins on the server side so friends
    # can't overwrite each other's earlier upload.
    ball_touches: list[dict] = Field(default_factory=list)
    goal_events: list[dict] = Field(default_factory=list)
    # Lifecycle/scoring raw events (BallHit, GoalScored, CrossbarHit,
    # StatfeedEvent, MatchCreated/Initialized/Ended/Destroyed, RoundStarted,
    # etc.). Excludes UpdateState (the 30 Hz position firehose) to keep the
    # payload to ~30 KB gzipped. Each entry: {"event": str, "payload": str}.
    # First-writer-wins per match — server skips if any raw_events exist.
    raw_events: list[dict] = Field(default_factory=list)


class RegisterRequest(BaseModel):
    """Self-registration with the shared friend-group join password. The friend's
    own detected RL identity is registered and the server issues them a personal
    API key bound to that primary_id (one password to share; per-friend keys
    under the hood)."""
    join_password: str
    primary_id: str
    display_name: str = ""


log = logging.getLogger("chumstats.server")


OVERLAY_DIR = Path(__file__).resolve().parent / "overlay"

# Monochrome brand glyphs for the opponent-platform filter chips. Vendored as
# inline-ready SVGs under overlay/icons/platforms/ (see SOURCES.md) and loaded
# once at import. Rendered inline so they inherit currentColor and tint to the
# accent on hover/active, matching the rest of the sidebar selector.
_PLATFORM_ICONS = {
    name: (OVERLAY_DIR / "icons" / "platforms" / f"{name}.svg")
    .read_text(encoding="utf-8")
    .strip()
    for name in ("steam", "epic", "playstation", "xbox", "switch")
}


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
        # Set by the ingest thread (via cli on_status) so /healthz + the tray can
        # tell "RL not open" apart from "server idle between matches".
        self.rl_connected: bool = False

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

    def tick_due(self) -> bool:
        """Cheap pre-check for producers: True if enough time has passed that a
        tick would actually be broadcast. Lets the ingest thread skip building a
        full 30 Hz tick payload that push_tick would only throttle away (~26 of
        every 30 are dropped at 4 Hz). push_tick stays the authority that updates
        the throttle clock; this never mutates it."""
        return (time.monotonic() - self._last_tick_ts) >= self.tick_min_interval

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


# Whether THIS server has a live feed (a local RL ingest). The central `serve`
# host has none, so its Live nav link + pip are hidden. Set by make_app().
_LIVE_AVAILABLE = True


def make_app(broadcaster: Broadcaster, *, store=None,
             self_primary_id: str | None = None,
             self_name: str | None = None,
             friend_mode: bool = False,
             live_enabled: bool = True) -> FastAPI:
    """Build the FastAPI app.

    When `friend_mode=True`, only the LIVE view + OBS overlay routes are
    registered. All analytical pages (/dashboard, /history, /player, etc.)
    and the upload/whoami endpoints are omitted entirely — they 404 instead
    of returning data. Used by the friend tray's local server so the
    canonical analytics experience lives on the central host.

    `live_enabled=False` marks a server with no RL ingest (the central `serve`
    host): the /live feed is structurally dead there, so the Live nav link +
    pip are hidden (the route itself still exists for direct hits).
    """
    global _LIVE_AVAILABLE
    _LIVE_AVAILABLE = live_enabled
    app = FastAPI(title="chumstats")

    if OVERLAY_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(OVERLAY_DIR)), name="static")

    def _gated_get(*args, **kwargs):
        """Like app.get(...) but a no-op (function not registered) under friend_mode.
        Use for analytical / upload / admin routes that should only exist on the
        central server."""
        if friend_mode:
            return lambda fn: fn
        return app.get(*args, **kwargs)

    def _gated_post(*args, **kwargs):
        if friend_mode:
            return lambda fn: fn
        return app.post(*args, **kwargs)

    @app.get("/")
    async def root_redirect():
        from fastapi.responses import RedirectResponse
        if friend_mode:
            return RedirectResponse(url="/overlay-picker")
        # Central host: a neutral landing splash with quick-jump player chips,
        # not a redirect straight into the owner's profile.
        return HTMLResponse(_splash_html(store, self_primary_id))

    @app.get("/overlay.html")
    async def overlay_legacy() -> FileResponse:
        # Legacy URL kept for any saved OBS sources. New URLs use /overlay/<mode>.
        return FileResponse(str(OVERLAY_DIR / "overlay.html"))

    @app.get("/healthz")
    async def health() -> dict:
        return {"ok": True, "clients": len(broadcaster.clients),
                "rl_connected": broadcaster.rl_connected}

    @_gated_get("/dashboard")
    async def dashboard():
        """Legacy owner 'Me' dashboard — retired. This is an all-players tracker,
        so send it to the neutral splash. Any single player's career still lives
        at /player/<name> (parameterized, not owner-specific)."""
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/")

    @_gated_get("/player/{name}")
    async def player_page(name: str, include_bots: int = 0,
                          mode: int | None = None,
                          platform: str | None = None,
                          window: str | None = None,
                          pid: str | None = None):
        """Career dashboard for an arbitrary player. Prefer the stable primary_id
        (?pid=) so two players who share a display name don't merge into one
        record; fall back to name-only for id-less links."""
        if store is None:
            return HTMLResponse("<p>No DB configured</p>")
        from .analytics import build_dashboard
        mode_filter = mode if mode in (1, 2, 3, 4) else None
        window_days = {"today": 1, "7d": 7, "30d": 30}.get(window or "", None)
        d = build_dashboard(store, primary_id=pid or None, name=name,
                            include_bots=bool(include_bots),
                            mode_filter=mode_filter,
                            platform_filter=platform or None,
                            window_days=window_days)
        if not d.overview.lines:
            return HTMLResponse(_not_found_html(name), status_code=404)
        return HTMLResponse(_dashboard_html(d, store=store, name=name,
                                            primary_id=pid or None, is_self=False,
                                            include_bots=bool(include_bots)))

    @_gated_get("/players")
    async def players_page(include_bots: int = 0, mode: int | None = None,
                            platform: str | None = None,
                            window: str | None = None,
                            sort: str = "frequency",
                            relation: str = "all"):
        if store is None:
            return HTMLResponse("<p>No DB configured</p>")
        mode_filter = mode if mode in (1, 2, 3, 4) else None
        window_days = {"today": 1, "7d": 7, "30d": 30}.get(window or "", None)
        if sort not in ("frequency", "name", "platform", "goals", "wins"):
            sort = "frequency"
        if relation not in ("all", "teammates", "opponents"):
            relation = "all"
        return HTMLResponse(_players_directory_html(
            store, self_primary_id=self_primary_id, include_bots=bool(include_bots),
            mode_filter=mode_filter, platform_filter=platform or None,
            window_days=window_days, sort=sort, relation=relation,
        ))

    @_gated_get("/history")
    async def history_page(include_bots: int = 0, mode: int | None = None,
                            window: str | None = None, platform: str | None = None,
                            pid: str | None = None, name: str | None = None,
                            sort: str = "recent"):
        if store is None:
            return HTMLResponse("<p>No DB configured</p>")
        mode_filter = mode if mode in (1, 2, 3, 4) else None
        window_days = {"today": 1, "7d": 7, "30d": 30}.get(window or "", None)
        if sort not in ("recent", "score", "goals", "saves", "best"):
            sort = "recent"
        # Default Matches view = ALL recorded matches (neutral, not the owner's
        # stat line). A specific player's history is shown only via ?pid=/?name=.
        all_matches = not (pid or name)
        subj_pid, subj_name = pid, name
        if pid and not name:
            with store._conn() as con:
                r = con.execute("SELECT name FROM match_player_stats WHERE "
                                "primary_id = ? ORDER BY rowid DESC LIMIT 1", (pid,)).fetchone()
                if r:
                    subj_name = r["name"]
        return HTMLResponse(_history_page_html(
            store, subj_pid, subj_name,
            include_bots=bool(include_bots),
            mode_filter=mode_filter,
            window_days=window_days,
            platform_filter=platform or None,
            sort=sort, is_self=False, all_matches=all_matches,
        ))

    @_gated_get("/match/{match_id}")
    async def match_page(match_id: str):
        if store is None:
            return HTMLResponse("<p>No DB configured</p>")
        return HTMLResponse(_match_detail_html(store, match_id, self_primary_id, self_name))

    @_gated_get("/about")
    async def about_page():
        return HTMLResponse(_about_html())

    @_gated_get("/opponents")
    async def opponents_page():
        # Retired — redundant with All Players (same data). Any old link / bookmark
        # lands on the directory, where the with/vs relation split lives.
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/players")

    @_gated_get("/compare")
    async def compare_page(
        names: list[str] = Query(default_factory=list),
        include_bots: int = 0,
        mode: int | None = None,
        window: str | None = None,
        last: int = 20,
    ):
        if store is None:
            return HTMLResponse("<p>No DB configured</p>")
        slots = list(names)
        if not slots:
            # Default to the top 3 most-played (non-bot) players so the page is
            # useful with no params — neutral, not owner-first.
            try:
                with store._conn() as con:
                    top_rows = con.execute(
                        "SELECT name FROM match_player_stats WHERE is_bot = 0 "
                        "GROUP BY name ORDER BY COUNT(*) DESC LIMIT 3"
                    ).fetchall()
                slots = [r["name"] for r in top_rows]
            except Exception:
                pass
        slots = (slots + ["", "", ""])[:3]
        mode_filter = mode if mode in (1, 2, 3, 4) else None
        window_days = {"today": 1, "7d": 7, "30d": 30}.get(window or "", None)
        return HTMLResponse(_compare_page_html(
            store, slots, self_name=self_name,
            include_bots=bool(include_bots),
            mode_filter=mode_filter,
            window_days=window_days,
            last_n=(last if last and last > 0 else None),
        ))

    @app.get("/live")
    async def live_page():
        return HTMLResponse(_live_page_html(self_name=self_name, friend_mode=friend_mode))

    @_gated_get("/clan")
    async def clan_page(
        names: list[str] = Query(default_factory=list),
        include_bots: int = 0,
        mode: int | None = None,
        window: str | None = None,
    ):
        if store is None:
            return HTMLResponse("<p>No DB configured</p>")
        # Default "Our club" = the owner + their regular crew (teammates with
        # >5 games on the owner's team), so the club page reflects the actual
        # friend group, not a club of one. Explicit ?names= overrides.
        members = [n for n in names if n]
        if not members and self_name:
            members = [self_name]
            try:
                with store._conn() as con:
                    crew = con.execute(
                        "SELECT t.name AS name, COUNT(*) AS n FROM match_player_stats me "
                        "JOIN match_player_stats t ON t.match_id = me.match_id "
                        "AND t.team_num = me.team_num AND t.name != me.name "
                        "WHERE me.name = ? AND t.is_bot = 0 "
                        "GROUP BY t.name HAVING n > 5 ORDER BY n DESC LIMIT 5",
                        (self_name,),
                    ).fetchall()
                members += [r["name"] for r in crew]
            except Exception:
                pass
        mode_filter = mode if mode in (1, 2, 3, 4) else None
        window_days = {"today": 1, "7d": 7, "30d": 30}.get(window or "", None)
        return HTMLResponse(_clan_page_html(
            store, members, self_name=self_name,
            include_bots=bool(include_bots),
            mode_filter=mode_filter,
            window_days=window_days,
        ))

    @_gated_get("/club/{club_name}")
    async def club_detail(club_name: str, include_bots: int = 0,
                           mode: int | None = None, window: str | None = None):
        if store is None:
            return HTMLResponse("<p>No DB configured</p>")
        mode_filter = mode if mode in (1, 2, 3, 4) else None
        window_days = {"today": 1, "7d": 7, "30d": 30}.get(window or "", None)
        return HTMLResponse(_club_detail_html(
            store, club_name, self_primary_id, self_name,
            include_bots=bool(include_bots),
            mode_filter=mode_filter, window_days=window_days,
        ))

    @app.get("/overlay-picker")
    @app.get("/overlay")
    async def overlay_picker(request: Request):
        host = request.headers.get("host", "127.0.0.1:5050")
        return HTMLResponse(_overlay_picker_html(host, friend_mode=friend_mode))

    @app.get("/overlay/{mode}")
    async def overlay_mode(mode: str):
        if mode not in ("live", "last", "session", "me"):
            return HTMLResponse(f"<p>Unknown overlay mode: {mode}</p>", status_code=404)
        # Serve the same shell, mode chosen by JS via path.
        path = OVERLAY_DIR / "overlay.html"
        return FileResponse(str(path))

    @app.get("/api/player-form")
    async def api_player_form(names: list[str] = Query(default_factory=list)) -> dict:
        """Last-10 form + career snapshot per player. Used by /live to render a
        pre-match scouting card when MatchCreated arrives."""
        if store is None or not names:
            return {"players": {}}
        out: dict[str, dict] = {}
        with store._conn() as con:
            for name in names:
                if not name:
                    continue
                last10 = con.execute("""
                    SELECT mps.team_num, m.winner_team_num
                    FROM match_player_stats mps
                    JOIN matches m ON m.id = mps.match_id
                    WHERE mps.name = ?
                    ORDER BY m.started_at DESC
                    LIMIT 10
                """, (name,)).fetchall()
                form = "".join(
                    "W" if r["team_num"] == r["winner_team_num"] else "L"
                    for r in last10
                )
                career = con.execute("""
                    SELECT COUNT(*) AS n,
                           SUM(CASE WHEN mps.team_num = m.winner_team_num THEN 1 ELSE 0 END) AS wins,
                           AVG(mps.goals)   AS g,
                           AVG(mps.assists) AS a,
                           AVG(mps.saves)   AS sv,
                           AVG(mps.shots)   AS sh,
                           SUM(mps.is_mvp)  AS mvps,
                           MIN(mps.platform) AS platform
                    FROM match_player_stats mps
                    JOIN matches m ON m.id = mps.match_id
                    WHERE mps.name = ?
                """, (name,)).fetchone()
                if not career or not career["n"]:
                    out[name] = {"form": "", "matches": 0}
                    continue
                out[name] = {
                    "form": form,
                    "matches": career["n"] or 0,
                    "wins":   career["wins"] or 0,
                    "losses": (career["n"] or 0) - (career["wins"] or 0),
                    "win_pct": ((career["wins"] or 0) / max(1, career["n"] or 1)) * 100,
                    "avg_goals":   round(career["g"]   or 0, 2),
                    "avg_assists": round(career["a"]   or 0, 2),
                    "avg_saves":   round(career["sv"]  or 0, 2),
                    "avg_shots":   round(career["sh"]  or 0, 2),
                    "mvps":        career["mvps"] or 0,
                    "platform":    career["platform"] or "",
                }
        return {"players": out}

    @_gated_get("/api/dashboard")
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

    @_gated_get("/api/v1/whoami")
    async def api_whoami(
        x_chumstats_key: str | None = Header(default=None, alias="X-Chumstats-Key"),
    ) -> dict:
        """Validate an API key. Used by the friend tray's setup wizard to check
        the URL+key combo before saving config."""
        if store is None:
            raise HTTPException(status_code=503, detail="server has no store configured")
        if not x_chumstats_key:
            raise HTTPException(status_code=401, detail="missing X-Chumstats-Key header")
        user = store.get_user_by_api_key(x_chumstats_key)
        if not user:
            raise HTTPException(status_code=401, detail="invalid api key")
        return {
            "ok": True,
            "user_id": user["user_id"],
            "display_name": user["display_name"],
            "primary_id": user["primary_id"],
        }

    @_gated_post("/api/v1/register")
    async def api_register(payload: RegisterRequest) -> dict:
        """Self-registration with the shared friend-group join password. A friend
        installs the app, enters ONE shared password, and the server issues them a
        personal API key bound to their detected RL account (created on first
        join; returning friends get their existing key back). Enable by setting
        CHUMSTATS_JOIN_PASSWORD on the central host."""
        import os as _os
        if store is None:
            raise HTTPException(status_code=503, detail="server has no store configured")
        join_pw = _os.environ.get("CHUMSTATS_JOIN_PASSWORD", "").strip()
        if not join_pw:
            raise HTTPException(status_code=403,
                                detail="self-registration is disabled (no join password set on the server)")
        if payload.join_password.strip() != join_pw:
            raise HTTPException(status_code=403, detail="wrong join password")
        if not payload.primary_id or payload.primary_id.startswith("Unknown"):
            raise HTTPException(status_code=400,
                                detail="couldn't detect your Rocket League account yet -- play a match first, then retry")
        user = store.get_user_by_primary_id(payload.primary_id)
        if not user:
            user = store.create_user(payload.primary_id,
                                     payload.display_name or payload.primary_id)
        return {
            "ok": True,
            "api_key": user["api_key"],
            "display_name": user["display_name"],
            "primary_id": user["primary_id"],
        }

    @_gated_post("/api/v1/match-summary")
    async def api_match_summary_upload(
        payload: MatchSummaryUpload,
        x_chumstats_key: str | None = Header(default=None, alias="X-Chumstats-Key"),
    ) -> dict:
        """Friend-client → central-server match upload. See docs/multi-user-network.md."""
        if store is None:
            raise HTTPException(status_code=503, detail="server has no store configured")
        if not x_chumstats_key:
            raise HTTPException(status_code=401, detail="missing X-Chumstats-Key header")
        user = store.get_user_by_api_key(x_chumstats_key)
        if not user:
            raise HTTPException(status_code=401, detail="invalid api key")
        if payload.my_row.primary_id != user["primary_id"]:
            raise HTTPException(
                status_code=403,
                detail=f"my_row.primary_id ({payload.my_row.primary_id}) does not match "
                       f"authenticated user's primary_id ({user['primary_id']})",
            )
        try:
            result = store.upsert_uploaded_match(
                payload.model_dump(), owner_primary_id=user["primary_id"],
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception:
            log.exception("upsert_uploaded_match failed for match %s", payload.match_id)
            raise HTTPException(status_code=500, detail="internal error")
        return {
            "ok": True,
            "match_id": payload.match_id,
            "user_id": user["user_id"],
            **result,
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

def _form_string(results: list[bool]) -> str:
    """Render last-N form like '✓ ✓ ✗ ✓ ✓' instead of 'WWLWW'."""
    return " ".join("✓" if w else "✗" for w in results)


def _radar_svg(values: list[tuple[str, float, float]], *,
               size: int = 280, color: str | None = None) -> str:
    """Radar / spider chart matching the Claude Design Radar component.

    Theme-aware via CSS vars on .radar-svg class. Each axis has its own
    max - 5 goals is a lot, but 5 assists is rare.
    """
    import math
    n = len(values)
    if n < 3:
        return "<!-- radar needs at least 3 axes -->"
    cx = cy = size / 2
    r = size * 0.30
    accent = color or "var(--accent)"

    parts: list[str] = [
        f'<svg viewBox="0 0 {size} {size}" xmlns="http://www.w3.org/2000/svg" '
        f'class="radar-svg" role="img" aria-label="Radar chart" '
        f'style="width:100%;max-width:{size}px;height:auto;display:block;margin:0 auto">'
    ]

    for pct in (0.25, 0.5, 0.75, 1.0):
        ring = []
        for i in range(n):
            a = -math.pi / 2 + 2 * math.pi * i / n
            x = cx + r * pct * math.cos(a)
            y = cy + r * pct * math.sin(a)
            ring.append(f"{x:.1f},{y:.1f}")
        parts.append(
            f'<polygon points="{" ".join(ring)}" fill="none" '
            f'class="radar-grid" stroke-width="1"/>'
        )
    for i in range(n):
        a = -math.pi / 2 + 2 * math.pi * i / n
        ex = cx + r * math.cos(a)
        ey = cy + r * math.sin(a)
        parts.append(
            f'<line x1="{cx}" y1="{cy}" x2="{ex:.1f}" y2="{ey:.1f}" '
            f'class="radar-spoke" stroke-width="1"/>'
        )

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
        f'<polygon points="{pts_str}" fill="{accent}" fill-opacity="0.18" '
        f'stroke="{accent}" stroke-width="2" stroke-linejoin="round"/>'
    )
    for x, y in poly_pts:
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{accent}"/>')

    # Stat -> icon mapping. Same set as _stat_icon_html's primary keys.
    icon_map = {
        "goals":   "/static/icons/Goal_points_icon.png",
        "assists": "/static/icons/Assist_points_icon.png",
        "saves":   "/static/icons/Save_points_icon.png",
        "shots":   "/static/icons/Shot_on_Goal_points_icon.png",
        "demos":   "/static/icons/Demolition_points_icon.png",
        "touches": "/static/icons/First_Touch_points_icon.png",
    }
    label_dist = r + 28
    label_fs = max(11, int(size * 0.044))
    value_fs = label_fs - 1
    icon_size = max(14, int(size * 0.07))
    for i, (lbl, v, vmax) in enumerate(values):
        a = -math.pi / 2 + 2 * math.pi * i / n
        lx = cx + label_dist * math.cos(a)
        ly = cy + label_dist * math.sin(a)
        anchor = "middle"
        ca = math.cos(a)
        if ca > 0.3:    anchor = "start"
        elif ca < -0.3: anchor = "end"
        # Place icon ABOVE the text label (radial-out direction)
        icon_url = icon_map.get(lbl.strip().lower())
        if icon_url:
            ix = lx - icon_size / 2
            iy = ly - icon_size - 4
            parts.append(
                f'<image href="{icon_url}" x="{ix:.1f}" y="{iy:.1f}" '
                f'width="{icon_size}" height="{icon_size}" '
                f'preserveAspectRatio="xMidYMid meet"/>'
            )
            text_y = ly + 8
        else:
            text_y = ly - 4
        parts.append(
            f'<text x="{lx:.1f}" y="{text_y:.1f}" text-anchor="{anchor}" '
            f'class="radar-label" font-size="{label_fs}" font-weight="700" '
            f'style="letter-spacing:0.04em;text-transform:uppercase">{lbl}</text>'
        )
        if isinstance(v, float) and v < 10 and v != int(v):
            v_str = f"{v:.2f}"
        else:
            v_str = f"{int(v)}"
        parts.append(
            f'<text x="{lx:.1f}" y="{ly + 10:.1f}" text-anchor="{anchor}" '
            f'class="radar-value" font-size="{value_fs}" font-weight="500" '
            f'style="font-variant-numeric:tabular-nums">{v_str}</text>'
        )
    parts.append('</svg>')
    return "\n".join(parts)


# --- Derived stats from raw_events --------------------------------------
# The match_player_stats table stores what RL hands us directly. Several
# pro-scene numbers can be derived from raw_events:
#   - demos_received (StatfeedEvent.Demolish.SecondaryTarget)
#   - special highlights (EpicSave, AerialGoal, BicycleHit, FlipReset, etc.)
#   - goal speed per scorer (GoalScored.GoalSpeed)
#   - crossbar hits per player (CrossbarHit.BallLastTouch.Player.Name)
#   - goal locations (GoalScored.ImpactLocation) for the mini-pitch map
#
# Computed on-demand to keep the schema stable.

_SPECIAL_EVENTS = (
    "EpicSave", "AerialGoal", "BicycleHit", "FlipReset",
    "HatTrick", "LongGoal", "BackwardsGoal", "Savior", "LowFive",
)


def _empty_derived() -> dict:
    return {
        "demos_given": 0, "demos_received": 0,
        "goal_count": 0, "goal_speed_sum": 0.0,
        "crossbar_hits": 0, "highlights": 0,
        **{f"n_{e.lower()}": 0 for e in _SPECIAL_EVENTS},
    }


# RL point-icon catalog. Filenames sit in src/chumstats/overlay/icons/ and are
# served by the StaticFiles mount at /static/icons/. Keys here are the RL
# StatfeedEvent.EventName values (plus a handful of synthesized kinds like
# "Goal" from GoalScored). If a key isn't here we render no icon and fall
# back to the text label, which all rows already include.
# Soft competitive-RL benchmarks. Numbers approximate Champion-to-GC tier
# play (the band where mechanics start to dominate) - they're meant as
# orientation, not targets. Labels must match the row labels rendered in
# `_compare_page_html` exactly.
_PRO_BENCHMARKS: dict[str, str] = {
    "Win rate":                   "55-60%",
    "MVP rate":                   "18%",
    "Shooting %":                 "30%",
    "Score / touch":              "1.5",
    "Goal participation":         "65%",
    "Avg goal speed (kph)":       "85",
    "Demos delivered (total)":    "--",
    "Demos received (total)":     "--",
    "Demos delivered / match":    "1.5",
    "Demos received / match":     "1.0",
    "Demo K/D":                   "1.5",
    "Crossbar hits":              "--",
    "Goals":                      "0.9 / match",
    "Assists":                    "0.7 / match",
    "Saves":                      "1.2 / match",
    "Shots":                      "3.0 / match",
    "Demos":                      "1.5 / match",
    "Score":                      "450 / match",
    "Touches":                    "30 / match",
    "Supersonic %":               "35%",
    "Time in air %":              "20%",
    "Time on wall %":              "6%",
    "Time on ground %":            "55%",
    "Avg speed":                   "1400",
    "Boost used":                  "1400 / match",
    "Time near-empty %":           "8%",
    "Time at 100 boost %":         "5%",
    "BPM (boost used per minute)": "400",
    "Defensive third %":           "30%",
    "Neutral third %":             "45%",
    "Offensive third %":           "25%",
    # Highlights are aspirational; not really expected per-match.
    "Epic saves":      "--",
    "Aerial goals":    "--",
    "Bicycle hits":    "--",
    "Flip resets":     "--",
    "Hat tricks":      "--",
    "Long goals":      "--",
    "Backwards goals": "--",
    "Saviors":         "--",
    "Low fives":       "--",
    "Total highlights":"--",
}


_RL_ICON_FOR_EVENT: dict[str, str] = {
    "Goal":          "Goal_points_icon.png",
    "AerialGoal":    "Aerial_Goal_points_icon.png",
    "BackwardsGoal": "Backwards_Goal_points_icon.png",
    "LongGoal":      "Long_Goal_points_icon.png",
    "BicycleGoal":   "Bicycle_Goal_points_icon.png",
    "PoolShot":      "Pool_Shot_points_icon.png",
    "Swish":         "Swish_Goal_points_icon.png",
    "Turtle":        "Turtle_Goal_points_icon.png",
    "OvertimeGoal":  "Overtime_Goal_points_icon.png",
    "Shot":          "Shot_on_Goal_points_icon.png",
    "Save":          "Save_points_icon.png",
    "EpicSave":      "Epic_Save_points_icon.png",
    "Savior":        "Savior_points_icon.png",
    "Assist":        "Assist_points_icon.png",
    "Playmaker":     "Playmaker_points_icon.png",
    "AerialHit":     "Aerial_Hit_points_icon.png",
    "BicycleHit":    "Bicycle_Hit_points_icon.png",
    "BulletHit":     "Bullet_Hit_points_icon.png",
    "FirstTouch":    "First_Touch_points_icon.png",
    "CenterBall":    "Center_Ball_points_icon.png",
    "ClearBall":     "Clear_Ball_points_icon.png",
    "Juggle":        "Juggle_points_icon.png",
    "FlipReset":     "Juggle_points_icon.png",  # closest visual match
    "HatTrick":      "Hat_Trick_points_icon.png",
    "LowFive":       "Low_Five_points_icon.png",
    "HighFive":      "High_Five_points_icon.png",
    "Demolish":      "Demolition_points_icon.png",
    "Demolition":    "Demolition_points_icon.png",
    "Damage":        "Damage_points_icon.png",
    "UltraDamage":   "Ultra_Damage_points_icon.png",
    "Extermination": "Extermination_points_icon.png",
    "MVP":           "MVP_points_icon.png",
    "Win":           "Win_points_icon.png",
}


def _platform_icon_html(platform: str | None, size: int = 14) -> str:
    """Return a small SVG icon for a player's platform. Original concept
    drawings (not trademarked logos) - shared by the sidebar filter and
    per-row platform badges across the site."""
    if not platform:
        return ''
    p = platform.lower()
    sz = f'width="{size}" height="{size}"'
    if "steam" in p:
        return (
            f'<svg class="plat-ic" viewBox="0 0 24 24" {sz} '
            'fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
            'title="Steam">'
            '<circle cx="12" cy="12" r="8"/>'
            '<circle cx="12" cy="12" r="2.4" fill="currentColor"/>'
            '<path d="M12 2 V5.5 M12 18.5 V22 M2 12 H5.5 M18.5 12 H22 M5 5 L7.5 7.5 M16.5 16.5 L19 19 M5 19 L7.5 16.5 M16.5 7.5 L19 5"/>'
            '</svg>'
        )
    if "epic" in p:
        return (
            f'<svg class="plat-ic" viewBox="0 0 24 24" {sz} fill="currentColor" title="Epic">'
            '<rect x="2.5" y="2" width="19" height="20" fill="none" stroke="currentColor" stroke-width="2"/>'
            '<rect x="6.5" y="6"  width="11" height="2.5"/>'
            '<rect x="6.5" y="10.75" width="8.5" height="2.5"/>'
            '<rect x="6.5" y="15.5" width="11" height="2.5"/>'
            '</svg>'
        )
    if "ps" in p or "playstation" in p:
        return (
            f'<svg class="plat-ic" viewBox="0 0 24 24" {sz} '
            'fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round" '
            'title="PlayStation">'
            '<path d="M5 9 Q5 6 8 6 H16 Q19 6 19 9 V14 Q19 17 16 17 H13.5 L12 19 L10.5 17 H8 Q5 17 5 14 Z"/>'
            '<line x1="7" y1="11" x2="9" y2="11"/>'
            '<line x1="8" y1="10" x2="8" y2="12"/>'
            '<circle cx="14.5" cy="10.5" r="0.7" fill="currentColor"/>'
            '<circle cx="16.5" cy="12.5" r="0.7" fill="currentColor"/>'
            '</svg>'
        )
    if "xbox" in p:
        return (
            f'<svg class="plat-ic" viewBox="0 0 24 24" {sz} '
            'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" title="Xbox">'
            '<circle cx="12" cy="12" r="9"/>'
            '<line x1="7.5" y1="7.5" x2="16.5" y2="16.5"/>'
            '<line x1="16.5" y1="7.5" x2="7.5" y2="16.5"/>'
            '</svg>'
        )
    if "switch" in p or "nintendo" in p:
        return (
            f'<svg class="plat-ic" viewBox="0 0 24 24" {sz} '
            'fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" title="Switch">'
            '<rect x="2" y="3" width="20" height="18"/>'
            '<rect x="7.5" y="5.5" width="9" height="13" fill="none"/>'
            '<circle cx="4.75" cy="8" r="1" fill="currentColor"/>'
            '<circle cx="19.25" cy="16" r="1" fill="currentColor"/>'
            '</svg>'
        )
    return ''


def _stacked_bar(segments: list[tuple[str, float, str]], height: int = 10) -> str:
    """Render a stacked horizontal bar from (label, percent, color) tuples.
    Segments should sum to ~100 (we don't normalize). Each segment carries a
    title attribute for hover-to-read."""
    total = sum(max(0, p) for _, p, _ in segments) or 100
    parts: list[str] = []
    for label, pct, color in segments:
        if pct <= 0:
            continue
        w = pct / total * 100
        parts.append(
            f'<span class="seg" style="width:{w:.2f}%;background:{color}" '
            f'title="{label} {pct:.0f}%"></span>'
        )
    return f'<div class="stacked-bar" style="--bar-h:{height}px">{"".join(parts)}</div>'


def _stat_icon_html(name: str, *, size: int = 14) -> str:
    """Map a human stat label like 'Goals' or 'Avg goal speed (kph)' to the
    matching RL point-icon HTML. Returns empty string for stats with no icon.
    Uses substring matching so 'Goals', 'Avg goal speed', 'Demos delivered'
    and 'Shooting %' all hit a sensible icon. More-specific keys first."""
    if not name:
        return ""
    lname = name.lower().strip()
    table = (
        # Multi-word keys first to avoid being shadowed by their leading word.
        ("flip reset",   "Juggle"),
        ("hat trick",    "HatTrick"),
        ("high five",    "HighFive"),
        ("low five",     "LowFive"),
        ("first touch",  "FirstTouch"),
        ("long goal",    "LongGoal"),
        # Specific single keys.
        ("backwards",    "BackwardsGoal"),
        ("aerial",       "AerialGoal"),
        ("bicycle",      "BicycleHit"),
        ("bullet",       "BulletHit"),
        ("ultra",        "UltraDamage"),
        ("extermination","Extermination"),
        ("epic",         "EpicSave"),
        ("playmaker",    "Playmaker"),
        ("savior",       "Savior"),
        ("juggle",       "Juggle"),
        ("center",       "CenterBall"),
        ("clear",        "ClearBall"),
        ("damage",       "Damage"),
        ("overtime",     "OvertimeGoal"),
        ("pool",         "PoolShot"),
        ("swish",        "Swish"),
        ("turtle",       "Turtle"),
        ("crossbar",     "Shot"),
        ("hat",          "HatTrick"),
        ("shoot",        "Shot"),
        ("shot",         "Shot"),
        ("save",         "Save"),
        ("assist",       "Assist"),
        ("demo",         "Demolition"),
        ("mvp",          "MVP"),
        ("goal",         "Goal"),
        ("win",          "Win"),
        ("touch",        "FirstTouch"),
    )
    for keyword, key in table:
        if keyword in lname:
            return _rl_icon_html(key, size=size, alt="")
    return ""


def _rl_icon_html(event_key: str, *, size: int = 18, alt: str | None = None) -> str:
    """Return an <img> tag for an RL point icon, or empty string if no match.

    `event_key` matches RL's StatfeedEvent EventName (case-sensitive).
    Filenames contain spaces, which we URL-encode in the src."""
    from urllib.parse import quote
    fname = _RL_ICON_FOR_EVENT.get(event_key)
    if not fname:
        return ""
    src = f"/static/icons/{quote(fname)}"
    alt_attr = alt if alt is not None else event_key
    return (f'<img class="rl-icon" src="{src}" alt="{alt_attr}" '
            f'width="{size}" height="{size}" loading="lazy" />')


def _mvp_callout_html(players, viewer_pid: str | None, viewer_name: str | None) -> str:
    """Compact MVP banner for the hero. Calls the player out by name (with a
    'YOU' chip if the viewer is the MVP), color-coded by team."""
    from urllib.parse import quote
    mvps = [p for p in players if p["is_mvp"]]
    if not mvps:
        return ""
    parts = []
    for p in mvps:
        team_cls = "team-blue" if p["team_num"] == 0 else "team-orng"
        is_self = (viewer_pid and p["primary_id"] == viewer_pid) or \
                  (viewer_name and p["name"] == viewer_name and not viewer_pid)
        self_tag = ""  # neutral all-players view — no "you" framing
        href = f"/player/{quote(p['name'], safe='')}"
        name_html = (f'<span class="hero-mvp-name">{html.escape(p["name"])}</span>'
                     if p["is_bot"] else
                     f'<a class="hero-mvp-name" href="{href}">{html.escape(p["name"])}</a>')
        parts.append(
            f'<span class="hero-mvp-entry {team_cls}">'
            f'{name_html}{self_tag}'
            f'</span>'
        )
    icon = _rl_icon_html("MVP", size=16, alt="")
    return (
        f'<div class="hero-mvp">'
        f'{icon}<span class="hero-mvp-tag">MVP</span>'
        f'{" ".join(parts)}'
        f'</div>'
    )


def _team_coverage_note(team_players, duration_seconds: float) -> str:
    """If any player on this team has under 70% spectator coverage, emit a
    single shared note explaining why movement/boost stats are hidden.
    Saves repeating the same disclaimer per opponent card."""
    expected = max(int(duration_seconds * 30), 1)
    low_cov = [p for p in team_players
               if (p["ticks_total"] or 0) / expected < 0.70]
    if not low_cov:
        return ""
    return (
        '<span class="eyebrow-note">'
        'No movement/boost stats <a href="/about">(spectator-only fields)</a>'
        '</span>'
    )


def _derive_match_extras(store, match_id: str) -> dict[str, dict]:
    """Walk raw_events for a single match. Returns:
       result[player_name] = {derived stats dict}
       result["__goal_locations__"] = [{x, y, speed, scorer, scorer_team, assister}, ...]"""
    import json as _json
    from collections import defaultdict
    out: dict[str, dict] = defaultdict(_empty_derived)
    goal_locations: list[dict] = []
    with store._conn() as con:
        rows = con.execute(
            "SELECT event, payload FROM raw_events WHERE match_id = ? AND "
            "event IN ('GoalScored','CrossbarHit','StatfeedEvent')",
            (match_id,),
        ).fetchall()
    for r in rows:
        try:
            d = _json.loads(r["payload"])
        except Exception:
            continue
        e = r["event"]
        if e == "GoalScored":
            scorer = (d.get("Scorer") or {}).get("Name") or ""
            if scorer:
                out[scorer]["goal_count"] += 1
                out[scorer]["goal_speed_sum"] += float(d.get("GoalSpeed") or 0)
            loc = d.get("ImpactLocation") or {}
            # Skip envelopes without a scorer (the API sometimes emits a
            # follow-up GoalScored with Scorer=null right after the real one,
            # which would create gaps in the rendered legend numbering).
            if scorer and loc.get("Y") is not None:
                goal_locations.append({
                    "x": float(loc.get("X") or 0),
                    "y": float(loc.get("Y") or 0),
                    "speed": float(d.get("GoalSpeed") or 0),
                    "scorer": scorer,
                    "scorer_team": (d.get("Scorer") or {}).get("TeamNum"),
                    "assister": (d.get("Assister") or {}).get("Name") or "",
                })
        elif e == "CrossbarHit":
            who = ((d.get("BallLastTouch") or {}).get("Player") or {}).get("Name") or ""
            if who:
                out[who]["crossbar_hits"] += 1
        elif e == "StatfeedEvent":
            ev = d.get("EventName") or ""
            main = (d.get("MainTarget") or {}).get("Name") or ""
            sec = (d.get("SecondaryTarget") or {}).get("Name") or ""
            if ev == "Demolish":
                if main: out[main]["demos_given"] += 1
                if sec:  out[sec]["demos_received"] += 1
            elif ev in _SPECIAL_EVENTS and main:
                out[main][f"n_{ev.lower()}"] += 1
                out[main]["highlights"] += 1
    out["__goal_locations__"] = goal_locations
    return dict(out)


def _lifetime_derived(store, name: str | None, match_ids: set | None = None) -> dict:
    """Aggregate derived stats across the matches a player appeared in. Single
    bulk scan of raw_events filtered to those matches. When `match_ids` is given
    (a last-N-games scope), the scan is restricted to it so the compare page can
    window every section consistently."""
    import json as _json
    totals = _empty_derived()
    totals["goal_participation_num"] = 0
    totals["goal_participation_den"] = 0
    if not store or not name:
        return totals
    if match_ids is not None and not match_ids:
        return totals
    scope_sql = "SELECT DISTINCT match_id FROM match_player_stats WHERE name = ?"
    scope_args: list = [name]
    gp_filter = ""
    if match_ids is not None:
        ph = ",".join("?" * len(match_ids))
        scope_sql = f"SELECT match_id FROM match_player_stats WHERE name = ? AND match_id IN ({ph})"
        scope_args = [name, *match_ids]
        gp_filter = f" AND mps.match_id IN ({ph})"
    with store._conn() as con:
        for row in con.execute(f"""
            SELECT mps.goals, mps.assists,
                   (SELECT SUM(goals) FROM match_player_stats x
                    WHERE x.match_id = mps.match_id AND x.team_num = mps.team_num) AS team_goals
            FROM match_player_stats mps WHERE mps.name = ?{gp_filter}
        """, ([name, *match_ids] if match_ids is not None else [name])).fetchall():
            totals["goal_participation_num"] += (row["goals"] or 0) + (row["assists"] or 0)
            totals["goal_participation_den"] += row["team_goals"] or 0
        rows = con.execute(f"""
            SELECT event, payload FROM raw_events
            WHERE event IN ('GoalScored','CrossbarHit','StatfeedEvent')
              AND match_id IN ({scope_sql})
        """, scope_args).fetchall()
    for r in rows:
        try:
            d = _json.loads(r["payload"])
        except Exception:
            continue
        e = r["event"]
        if e == "GoalScored":
            scorer = (d.get("Scorer") or {}).get("Name") or ""
            if scorer == name:
                totals["goal_count"] += 1
                totals["goal_speed_sum"] += float(d.get("GoalSpeed") or 0)
        elif e == "CrossbarHit":
            who = ((d.get("BallLastTouch") or {}).get("Player") or {}).get("Name") or ""
            if who == name:
                totals["crossbar_hits"] += 1
        elif e == "StatfeedEvent":
            ev = d.get("EventName") or ""
            main = (d.get("MainTarget") or {}).get("Name") or ""
            sec = (d.get("SecondaryTarget") or {}).get("Name") or ""
            if ev == "Demolish":
                if main == name: totals["demos_given"] += 1
                if sec == name:  totals["demos_received"] += 1
            elif ev in _SPECIAL_EVENTS and main == name:
                totals[f"n_{ev.lower()}"] += 1
                totals["highlights"] += 1
    return totals


def _lifetime_touch_data(store, name: str | None, match_ids: set | None = None) -> dict:
    """Aggregate BallHit positions across every match the player appeared in,
    plus a defensive/neutral/offensive third breakdown using the team_num
    they played on in each individual match (so a player who switches sides
    still has correct attacking-half attribution).

    Returns a playback-shaped dict so it plugs into `_ball_heatmap_svg`."""
    import json as _json
    rl_len, rl_wid = 10240, 8192
    vb_w, vb_h = 880, 380
    pitch_w, pitch_h = 800, 320
    pad_x, pad_y = (vb_w - pitch_w) / 2, (vb_h - pitch_h) / 2

    def project(rx: float, ry: float) -> tuple[float, float]:
        ry = max(min(ry, rl_len / 2 + 280), -(rl_len / 2 + 280))
        rx = max(min(rx, rl_wid / 2 + 280), -(rl_wid / 2 + 280))
        px = pad_x + ((ry + rl_len / 2) / rl_len) * pitch_w
        py = pad_y + ((rx + rl_wid / 2) / rl_wid) * pitch_h
        return round(px, 1), round(py, 1)

    empty = {
        "ball_track": [],
        "svg": {"vb_w": vb_w, "vb_h": vb_h, "pitch_w": pitch_w,
                "pitch_h": pitch_h, "pad_x": pad_x, "pad_y": pad_y},
        "thirds": {"def": 0, "neu": 0, "off": 0},
        "touches": 0,
        "matches_with_touches": 0,
    }
    if not store or not name:
        return empty
    if match_ids is not None and not match_ids:
        return empty

    team_filter = ""
    base_args: list = [name]
    if match_ids is not None:
        ph = ",".join("?" * len(match_ids))
        team_filter = f" AND match_id IN ({ph})"
        base_args = [name, *match_ids]
    with store._conn() as con:
        team_by_match = {
            r["match_id"]: r["team_num"]
            for r in con.execute(
                f"SELECT match_id, team_num FROM match_player_stats WHERE name = ?{team_filter}",
                base_args,
            )
        }
        if not team_by_match:
            return empty
        # Limit raw event scan to BallHits from this player's (in-scope) matches.
        rows = con.execute(f"""
            SELECT match_id, payload FROM raw_events
            WHERE event = 'BallHit'
              AND match_id IN (SELECT match_id FROM match_player_stats WHERE name = ?{team_filter})
        """, base_args).fetchall()

    touches: list[dict] = []
    thirds = {"def": 0, "neu": 0, "off": 0}
    seen_matches: set[str] = set()
    for r in rows:
        try:
            d = _json.loads(r["payload"])
        except Exception:
            continue
        players_arr = d.get("Players") or []
        # Credit the touch only to the actual toucher (Players[0]), matching the
        # per-match path. The old `any(name in Players)` over-credited a teammate's
        # hit to this player.
        if (players_arr[0].get("Name") if players_arr else "") != name:
            continue
        loc = (d.get("Ball") or {}).get("Location") or {}
        x = float(loc.get("X") or 0)
        y = float(loc.get("Y") or 0)
        team = team_by_match.get(r["match_id"], 0)
        sx, sy = project(x, y)
        touches.append({
            "x": x, "y": y, "sx": sx, "sy": sy,
            "player": name, "team": team, "t": 0,
        })
        seen_matches.add(r["match_id"])
        # Defensive-third sign: Team 0 defends +Y, so signed_y = y * (+1 if team 0
        # else -1) gives a defensive-positive value.
        signed_y = y * (1 if team == 0 else -1)
        if signed_y > 1707:
            thirds["def"] += 1
        elif signed_y < -1707:
            thirds["off"] += 1
        else:
            thirds["neu"] += 1

    return {
        "ball_track": touches,
        "svg": {"vb_w": vb_w, "vb_h": vb_h, "pitch_w": pitch_w,
                "pitch_h": pitch_h, "pad_x": pad_x, "pad_y": pad_y},
        "thirds": thirds,
        "touches": len(touches),
        "matches_with_touches": len(seen_matches),
    }


def _lifetime_shot_data(store, name: str | None) -> dict:
    """Aggregate where the player SCORES FROM: the scorer's last ball-touch
    before each of their goals (the shot), across every match. Derived live from
    raw_events (BallHit + GoalScored) — the touch immediately before a goal is
    the shot, and replay/kickoff touches arrive after it, so stream order alone
    locates it (no stored backfill needed). Returns a playback-shaped dict so it
    plugs straight into `_ball_heatmap_svg`."""
    import json as _json
    rl_len, rl_wid = 10240, 8192
    vb_w, vb_h = 880, 380
    pitch_w, pitch_h = 800, 320
    pad_x, pad_y = (vb_w - pitch_w) / 2, (vb_h - pitch_h) / 2

    def project(rx: float, ry: float) -> tuple[float, float]:
        ry = max(min(ry, rl_len / 2 + 280), -(rl_len / 2 + 280))
        rx = max(min(rx, rl_wid / 2 + 280), -(rl_wid / 2 + 280))
        px = pad_x + ((ry + rl_len / 2) / rl_len) * pitch_w
        py = pad_y + ((rx + rl_wid / 2) / rl_wid) * pitch_h
        return round(px, 1), round(py, 1)

    empty = {
        "ball_track": [],
        "svg": {"vb_w": vb_w, "vb_h": vb_h, "pitch_w": pitch_w,
                "pitch_h": pitch_h, "pad_x": pad_x, "pad_y": pad_y},
        "shots": 0,
        "matches_with_goals": 0,
    }
    if not store or not name:
        return empty

    with store._conn() as con:
        team_by_match = {
            r["match_id"]: r["team_num"]
            for r in con.execute(
                "SELECT match_id, team_num FROM match_player_stats WHERE name = ?", (name,))
        }
        if not team_by_match:
            return empty
        rows = con.execute("""
            SELECT match_id, event, payload FROM raw_events
            WHERE event IN ('BallHit', 'GoalScored')
              AND match_id IN (SELECT DISTINCT match_id FROM match_player_stats WHERE name = ?)
            ORDER BY match_id, received_at, id
        """, (name,)).fetchall()

    shots: list[dict] = []
    seen_matches: set[str] = set()
    cur_match = None
    last_xy: tuple[float, float] | None = None  # player's most recent touch this match
    for r in rows:
        if r["match_id"] != cur_match:
            cur_match = r["match_id"]
            last_xy = None
        try:
            d = _json.loads(r["payload"])
        except Exception:
            continue
        if r["event"] == "BallHit":
            if any((pp.get("Name") or "") == name for pp in (d.get("Players") or [])):
                loc = (d.get("Ball") or {}).get("Location") or {}
                last_xy = (float(loc.get("X") or 0), float(loc.get("Y") or 0))
        elif (d.get("Scorer") or {}).get("Name") == name and last_xy is not None:
            x, y = last_xy
            sx, sy = project(x, y)
            shots.append({
                "x": x, "y": y, "sx": sx, "sy": sy,
                "player": name, "team": team_by_match.get(r["match_id"], 0), "t": 0,
            })
            seen_matches.add(r["match_id"])

    return {
        "ball_track": shots,
        "svg": {"vb_w": vb_w, "vb_h": vb_h, "pitch_w": pitch_w,
                "pitch_h": pitch_h, "pad_x": pad_x, "pad_y": pad_y},
        "shots": len(shots),
        "matches_with_goals": len(seen_matches),
    }


def _recent_form_html(store, primary_id: str | None, name: str | None,
                      *, include_bots: bool = True, limit: int = 10) -> str:
    """Form-dot strip of the last N matches. Replaces the stale ASCII
    'Last 10  ✓ ✗ ✗ ✓ ✓' rendering with proper team-colored pills."""
    if not store or (not primary_id and not name):
        return ""
    rows = _match_history_rows(store, primary_id, name,
                               limit=limit, include_bots=include_bots)
    if not rows:
        return ""
    # `rows` is newest-first; render oldest-first for natural left-to-right reading.
    dots = []
    wins = 0
    losses = 0
    g = a = sv = 0
    n = len(rows)
    for r in reversed(rows):
        won = r["team_num"] == r["winner_team_num"]
        if won:
            wins += 1
        else:
            losses += 1
        g += r["goals"] or 0
        a += r["assists"] or 0
        sv += r["saves"] or 0
        dots.append(f'<span class="d {"win" if won else "loss"}"></span>')

    win_pct = (wins / n * 100) if n else 0
    avg_line = (
        f'<li><b>{wins}-{losses}</b> <span>record ({win_pct:.0f}% win rate)</span></li>'
        f'<li><b>{g / n:.2f}</b> <span>goals / match</span></li>'
        f'<li><b>{a / n:.2f}</b> <span>assists / match</span></li>'
        f'<li><b>{sv / n:.2f}</b> <span>saves / match</span></li>'
    )
    return f"""
      <section class="form-section">
        <h2>Last {n}</h2>
        <div class="form-strip">
          <div class="form-dots">{"".join(dots)}</div>
          <ul class="rc-stat-line form-avgs">{avg_line}</ul>
        </div>
      </section>
    """


def _player_ball_section_html(store, name: str | None) -> str:
    """Lifetime ball-control card for the player profile: touch heatmap +
    BPM + per-third breakdown + headline numbers. Reuses the same heatmap
    machinery as the match-detail and compare pages."""
    if not store or not name:
        return ""
    from .analytics import _lifetime_row
    with store._conn() as con:
        row = _lifetime_row(con, None, name)
    td = _lifetime_touch_data(store, name)
    if not td.get("touches"):
        return ""

    touches = td["touches"]
    matches = td["matches_with_touches"] or 1
    touches_per_match = touches / matches
    thirds = td["thirds"]
    tt = max(1, sum(thirds.values()))
    def_pct = thirds["def"] / tt * 100
    neu_pct = thirds["neu"] / tt * 100
    off_pct = thirds["off"] / tt * 100

    bpm = None
    if row and (row.get("ticks") or 0) >= 1000:
        minutes = row["ticks"] / 30 / 60
        if minutes:
            bpm = (row.get("boost_used") or 0) / minutes
    bpm_html = (f'<li><b>{bpm:.0f}</b> <span>BPM (lifetime avg)</span></li>'
                if bpm is not None else "")

    name_slug = "".join(ch if ch.isalnum() else "_" for ch in name)
    heatmap = _ball_heatmap_svg(td, key=f"player-{name_slug}")

    touch_card = f"""
      <div class="card insights-card">
        <div class="section-title">
          <span>Where they touch the ball (lifetime)</span>
          <span class="dim" style="text-transform:none;letter-spacing:0">
            Every BallHit this player has been on across
            {td['matches_with_touches']} stored match{'' if td['matches_with_touches'] == 1 else 'es'},
            rotated so they always attack &#8594; (right). Brighter = more touches.
            Kickoff first-touches (always dead-centre) are excluded so they don't
            skew the map.
          </span>
        </div>
        <div class="insights-heatmap">
          <div class="hm-wrap">{heatmap}</div>
        </div>
        <ul class="rc-stat-line" style="margin-top:14px">
          <li><b>{touches}</b> <span>total touches</span></li>
          <li><b>{touches_per_match:.1f}</b> <span>per match</span></li>
          {bpm_html}
          <li><b>{def_pct:.0f}%</b> <span>defensive third</span></li>
          <li><b>{neu_pct:.0f}%</b> <span>neutral</span></li>
          <li><b>{off_pct:.0f}%</b> <span>offensive third</span></li>
        </ul>
      </div>
    """

    # Shot map: where this player's goals were actually struck from (their last
    # touch before each goal), not the goal-line crossing. Reliable for all.
    sd = _lifetime_shot_data(store, name)
    shot_card = ""
    if sd["shots"]:
        shotmap = _ball_heatmap_svg(sd, key=f"shots-{name_slug}", exclude_center=False)
        shot_card = f"""
      <div class="card insights-card">
        <div class="section-title">
          <span>Where they score from</span>
          <span class="dim" style="text-transform:none;letter-spacing:0">
            Shot location of {sd['shots']} goal{'' if sd['shots'] == 1 else 's'} across
            {sd['matches_with_goals']} match{'' if sd['matches_with_goals'] == 1 else 'es'},
            rotated so they always attack &#8594; (right). Brighter = scored more from there.
          </span>
        </div>
        <div class="insights-heatmap">
          <div class="hm-wrap">{shotmap}</div>
        </div>
      </div>
    """
    return touch_card + shot_card


def _radar_block_for_player(store, primary_id: str | None, name: str | None,
                            *, include_bots: bool = True) -> str:
    """Compute the dashboard radar for one player and wrap it in HTML."""
    if not store or (not primary_id and not name):
        return ""
    where = "primary_id = ?" if primary_id else "name = ?"
    arg = primary_id or name
    bot_filter = "" if include_bots else (
        " AND match_id IN (SELECT m.id FROM matches m WHERE NOT EXISTS "
        "(SELECT 1 FROM match_player_stats x WHERE x.match_id = m.id AND x.is_bot = 1))"
    )
    with store._conn() as con:  # type: ignore[attr-defined]
        row = con.execute(f"""
            SELECT
                AVG(goals)   AS g,
                AVG(assists) AS a,
                AVG(saves)   AS sv,
                AVG(shots)   AS sh,
                AVG(demos)   AS d,
                COUNT(*)     AS n
            FROM match_player_stats WHERE {where}{bot_filter}
        """, (arg,)).fetchone()
        # Reference = the field of per-player per-match averages (regulars only).
        # Scaling against the best regular's average (not a freak single game)
        # keeps the bars meaningful instead of a tiny blob near zero.
        field = con.execute("""
            SELECT AVG(ag) mg, MAX(ag) xg, AVG(aa) ma, MAX(aa) xa,
                   AVG(asv) msv, MAX(asv) xsv, AVG(ash) msh, MAX(ash) xsh,
                   AVG(ad) md, MAX(ad) xd
            FROM (SELECT AVG(goals) ag, AVG(assists) aa, AVG(saves) asv,
                         AVG(shots) ash, AVG(demos) ad
                  FROM match_player_stats GROUP BY primary_id HAVING COUNT(*) >= 5)
        """).fetchone()
    if not row or not row["n"]:
        return ""
    # (label, player per-match avg, field avg, field best-regular)
    stats = [
        ("Goals",   row["g"]  or 0, field["mg"]  or 0, field["xg"]  or 1),
        ("Assists", row["a"]  or 0, field["ma"]  or 0, field["xa"]  or 1),
        ("Saves",   row["sv"] or 0, field["msv"] or 0, field["xsv"] or 1),
        ("Shots",   row["sh"] or 0, field["msh"] or 0, field["xsh"] or 1),
        ("Demos",   row["d"]  or 0, field["md"]  or 0, field["xd"]  or 1),
    ]
    rows_html = ""
    for label, val, favg, fmax in stats:
        fmax = fmax or 1
        pct = min(100, val / fmax * 100)
        avg_pct = min(100, (favg or 0) / fmax * 100)
        tone = "good" if val >= (favg or 0) else "bad"
        rows_html += (
            f'<div class="skill-row">'
            f'<div class="skill-label">{label}</div>'
            f'<div class="skill-track">'
            f'<div class="skill-fill {tone}" style="width:{pct:.0f}%"></div>'
            f'<div class="skill-avg" style="left:{avg_pct:.0f}%" '
            f'title="field average {favg:.2f}"></div></div>'
            f'<div class="skill-val">{val:.2f}<span class="skill-favg">avg {favg:.2f}</span></div>'
            f'</div>'
        )
    return (
        f'<section class="card skill-card">'
        f'<div class="section-title"><span>Per-match averages vs the field</span>'
        f'<span class="dim">over {row["n"]} matches &middot; bar = vs the best regular, '
        f'tick = field average</span></div>'
        f'{rows_html}'
        f'</section>'
    )


def _kpi_tiles_from_dashboard(d) -> str:
    """Extract the 4 most-important numbers from the Overview/Averages
    groups into pill-shaped KPI tiles up top."""
    overview = {ml.label: ml for ml in d.overview.lines}
    averages = {ml.label: ml for ml in d.averages.lines}

    def tile(value: str, label: str, *, accent: str = "") -> str:
        klass = f"kpi {accent}".strip()
        icon = _stat_icon_html(label, size=14)
        return (f'<div class="{klass}">'
                f'<div class="kpi-value">{value}</div>'
                f'<div class="kpi-label">{icon}{label}</div></div>')

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
    if "Goal difference" in overview:
        gd = overview["Goal difference"]
        tiles.append(tile(gd.value, "Goal diff",
                          accent="primary" if gd.value.startswith("+") else ""))
    if "Clean sheets" in overview:
        tiles.append(tile(overview["Clean sheets"].value, "Clean sheets"))
    return f'<div class="kpi-row">{"".join(tiles)}</div>' if tiles else ""


def _match_history_rows(store, primary_id: str | None, name: str | None,
                        *, limit: int = 50,
                        include_bots: bool = True,
                        mode_filter: int | None = None,
                        window_days: int | None = None,
                        platform_filter: str | None = None):
    """Shared query for recent matches. Returns sqlite Row objects.

    `team_size` is derived as max(team0_count, team1_count) so it reflects the
    playlist mode (1v1, 2v2, 3v3, 4v4) even if a player left mid-match.
    `mode_filter`, when set, restricts results to that playlist size.
    """
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
    team_size_sql = """(
        SELECT MAX(c) FROM (
            SELECT team_num, COUNT(*) AS c
            FROM match_player_stats
            WHERE match_id = m.id
            GROUP BY team_num
        )
    )"""
    if mode_filter is not None:
        where_clauses.append(f"{team_size_sql} = ?")
        args.append(mode_filter)
    if window_days and window_days > 0:
        import time as _time
        where_clauses.append("m.started_at >= ?")
        args.append(_time.time() - window_days * 86400)
    if platform_filter:
        where_clauses.append(
            "EXISTS (SELECT 1 FROM match_player_stats op2 WHERE op2.match_id = m.id "
            "AND op2.team_num != mps.team_num AND op2.platform LIKE '%' || ? || '%')")
        args.append(platform_filter)
    where_sql = " AND ".join(where_clauses)
    # Per-match team touches (used for possession indicator in history row)
    t0_touches_sql = """(
        SELECT COALESCE(SUM(touches), 0) FROM match_player_stats
        WHERE match_id = m.id AND team_num = 0
    )"""
    t1_touches_sql = """(
        SELECT COALESCE(SUM(touches), 0) FROM match_player_stats
        WHERE match_id = m.id AND team_num = 1
    )"""
    with store._conn() as con:
        return con.execute(f"""
            SELECT m.id, m.started_at, m.arena, m.is_online,
                   m.team0_score, m.team1_score,
                   m.team0_name, m.team1_name, m.winner_team_num,
                   mps.team_num, mps.goals, mps.assists, mps.saves,
                   mps.shots, mps.demos, mps.score, mps.is_mvp,
                   {team_size_sql}  AS team_size,
                   {t0_touches_sql} AS t0_touches,
                   {t1_touches_sql} AS t1_touches
            FROM match_player_stats mps
            JOIN matches m ON m.id = mps.match_id
            WHERE {where_sql}
            ORDER BY m.started_at DESC
            LIMIT ?
        """, (*args, limit)).fetchall()


def _match_mvp_lookup(store, match_ids: list[str]) -> dict[str, dict]:
    """Resolve {match_id: {name, team_num, is_bot}} for the MVP of each match
    in `match_ids`. Single SQL pass with IN clause so it stays cheap for any
    history view length."""
    out: dict[str, dict] = {}
    if not store or not match_ids:
        return out
    placeholders = ",".join("?" * len(match_ids))
    with store._conn() as con:
        for r in con.execute(
            f"SELECT match_id, name, team_num, is_bot FROM match_player_stats "
            f"WHERE is_mvp = 1 AND match_id IN ({placeholders})",
            tuple(match_ids),
        ):
            # If a match somehow has multiple MVP rows, the highest-scoring
            # team-num=0 wins arbitrarily; that's fine for display.
            out.setdefault(r["match_id"], {
                "name": r["name"], "team_num": r["team_num"], "is_bot": bool(r["is_bot"]),
            })
    return out


def _mvp_cell_html(mvp: dict | None, *, viewer_is_mvp: bool = False) -> str:
    """Render a compact MVP cell with the player's name color-coded by team
    and 'YOU' suffix when the viewer is the MVP. Empty string when no MVP."""
    if not mvp:
        return ""
    from urllib.parse import quote
    team_cls = "team-blue" if mvp["team_num"] == 0 else "team-orng"
    name = mvp["name"]
    href = f"/player/{quote(name, safe='')}"
    name_html = (f'<span class="mvp-name">{html.escape(name)}</span>'
                 if mvp["is_bot"] else
                 f'<a class="mvp-name" href="{href}" onclick="event.stopPropagation()">{html.escape(name)}</a>')
    you = ''  # neutral all-players view — no "you" framing
    icon = _rl_icon_html("MVP", size=14, alt="")
    return (
        f'<span class="mvp-cell {team_cls}" title="MVP of this match">'
        f'{icon}{name_html}{you}</span>'
    )


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

    mvps = _match_mvp_lookup(store, [r["id"] for r in rows])

    body_rows: list[str] = []
    for r in rows:
        won = r["team_num"] == r["winner_team_num"]
        ts_iso = datetime.fromtimestamp(r["started_at"]).isoformat()
        ts_fallback = datetime.fromtimestamp(r["started_at"]).strftime("%b %d, %Y")
        arena = _arena_nice(r["arena"] or "")
        # Online is the default ~95% of matches and reads as noise; only flag
        # explicit offline / exhibition matches.
        mode_html = ('' if r["is_online"]
                     else '<span class="chip">Offline</span>')
        mvp_html = _mvp_cell_html(mvps.get(r["id"]), viewer_is_mvp=bool(r["is_mvp"]))
        body_rows.append(f"""
          <tr class="match-row {'win' if won else 'loss'}" onclick="window.location='/match/{quote(r['id'], safe='')}'">
            <td><span class="badge {'win' if won else 'loss'}">{'W' if won else 'L'}</span></td>
            <td class="dim"><time datetime="{ts_iso}">{ts_fallback}</time></td>
            <td class="score-cell">
              <span class="score-team team-blue" title="{html.escape(r["team0_name"])}">{html.escape(r["team0_name"])}</span>
              <b class="tnum">{r["team0_score"]}</b>
              <span class="dim">-</span>
              <b class="tnum">{r["team1_score"]}</b>
              <span class="score-team team-orng" title="{html.escape(r["team1_name"])}">{html.escape(r["team1_name"])}</span>
            </td>
            <td class="dim">{arena}{(' &middot; ' + mode_html) if mode_html else ''}</td>
            <td class="num"><b>{r["goals"]}</b></td>
            <td class="num"><b>{r["assists"]}</b></td>
            <td class="num"><b>{r["saves"]}</b></td>
            <td class="num"><b>{r["shots"]}</b></td>
            <td class="num"><b>{r["demos"]}</b></td>
            <td>{mvp_html}</td>
          </tr>
        """)

    table_html = f"""
      <table class="history">
        <thead><tr>
          <th></th>
          <th>Date</th>
          <th>Score</th>
          <th>Arena</th>
          <th class="num">{_stat_icon_html("Goals")}Goals</th>
          <th class="num">{_stat_icon_html("Assists")}Assists</th>
          <th class="num">{_stat_icon_html("Saves")}Saves</th>
          <th class="num">{_stat_icon_html("Shots")}Shots</th>
          <th class="num">{_stat_icon_html("Demos")}Demos</th>
          <th>{_stat_icon_html("MVP")}MVP</th>
        </tr></thead>
        <tbody>{"".join(body_rows)}</tbody>
      </table>
    """
    if not show_section_chrome:
        return table_html
    # "View all" goes to this subject's full history (?pid=), so it works on any
    # player's profile — not just the configured owner.
    view_all = f"/history?pid={quote(primary_id, safe='')}" if primary_id else "/history"
    return f"""
      <section>
        <h2>Recent matches <a href="{view_all}" class="see-all">view all</a></h2>
        {table_html}
      </section>
    """


# Canonical per-player stat columns, score-first to match the Discord embed
# (bot.py _SB_HEADER). Single source of truth for stat-table column ORDER so the
# web tables can't drift apart. Tables may append context-specific extras
# (Touches on the match roster, MVP). Keys map to row/dict fields.
STAT_COLUMNS = [
    ("score",   "Score"),
    ("goals",   "Goals"),
    ("assists", "Assists"),
    ("saves",   "Saves"),
    ("shots",   "Shots"),
    ("demos",   "Demos"),
]


def _stat_cols_th() -> str:
    """Header cells for the canonical stat block (see STAT_COLUMNS)."""
    return "".join(f'<th class="num">{h}</th>' for _, h in STAT_COLUMNS)


def _stat_cols_td(r) -> str:
    """Body cells for the canonical stat block, pulling each key off a row/dict."""
    keys = r.keys() if hasattr(r, "keys") else r
    return "".join(
        f'<td class="num tnum">{(r[k] if k in keys else 0) or 0}</td>'
        for k, _ in STAT_COLUMNS
    )


def _players_directory_html(store, self_primary_id: str | None = None,
                            include_bots: bool = True,
                            mode_filter: int | None = None,
                            platform_filter: str | None = None,
                            window_days: int | None = None,
                            sort: str = "frequency",
                            relation: str = "all") -> str:
    """Players table sorted by frequency-played, with teammates vs opponents
    split if we know who 'me' is. Click name -> /player/<name>."""
    bot_filter = "" if include_bots else "WHERE max_bot = 0"

    inner_clauses: list[str] = []
    if mode_filter is not None:
        inner_clauses.append(f"""(SELECT MAX(c) FROM (
            SELECT team_num, COUNT(*) AS c FROM match_player_stats
            WHERE match_id = m.id GROUP BY team_num
        )) = {int(mode_filter)}""")
    if platform_filter:
        # All-players directory: filter to players ON this platform (their own
        # account platform) — the intuitive meaning here, not the opponent-
        # platform EXISTS used on /history (where Steam matched ~everything).
        inner_clauses.append(
            f"mps.platform LIKE '%' || {repr(platform_filter)} || '%'"
        )
    if window_days and window_days > 0:
        import time as _time
        cutoff = _time.time() - window_days * 86400
        inner_clauses.append(f"m.started_at >= {cutoff}")
    inner_where = (" WHERE " + " AND ".join(inner_clauses)) if inner_clauses else ""

    sql = f"""
        SELECT name, primary_id, n, goals, saves, assists, shots, demos, score,
               wins, max_bot AS is_bot, platform, was_teammate, was_opponent
        FROM (
            SELECT mps.name, mps.primary_id,
                   COUNT(*) AS n,
                   SUM(mps.goals)   AS goals,
                   SUM(mps.saves)   AS saves,
                   SUM(mps.assists) AS assists,
                   SUM(mps.shots)   AS shots,
                   SUM(mps.demos)   AS demos,
                   SUM(mps.score)   AS score,
                   SUM(CASE WHEN mps.team_num = m.winner_team_num THEN 1 ELSE 0 END) AS wins,
                   MAX(mps.is_bot)  AS max_bot,
                   MIN(mps.platform) AS platform,
                   SUM(CASE WHEN ? != '' AND EXISTS(
                        SELECT 1 FROM match_player_stats z
                        WHERE z.match_id = m.id AND z.primary_id = ?
                          AND z.team_num = mps.team_num
                          AND NOT (z.primary_id = mps.primary_id AND z.name = mps.name)
                   ) THEN 1 ELSE 0 END) AS was_teammate,
                   SUM(CASE WHEN ? != '' AND EXISTS(
                        SELECT 1 FROM match_player_stats z
                        WHERE z.match_id = m.id AND z.primary_id = ?
                          AND z.team_num != mps.team_num
                   ) THEN 1 ELSE 0 END) AS was_opponent
            FROM match_player_stats mps
            JOIN matches m ON m.id = mps.match_id
            {inner_where}
            GROUP BY mps.name, mps.primary_id
        ) {bot_filter}
        ORDER BY
            CASE WHEN ? = 'platform' THEN platform END ASC,
            CASE WHEN ? = 'name'     THEN name END ASC,
            CASE WHEN ? = 'goals'    THEN -goals END ASC,
            CASE WHEN ? = 'wins'     THEN -wins END ASC,
            n DESC, name
    """
    with store._conn() as con:
        rows = con.execute(sql, (
            self_primary_id or "", self_primary_id or "",
            self_primary_id or "", self_primary_id or "",
            sort, sort, sort, sort,
        )).fetchall()

    def _row(r, rank) -> str:
        is_bot = bool(r["is_bot"])
        tag = "<span class='tag'>BOT</span>" if is_bot else ""
        _pid = r["primary_id"] if "primary_id" in r.keys() else ""
        href = f"/player/{quote(r['name'], safe='')}"
        # Route by the stable primary_id so a shared display name doesn't merge
        # two distinct accounts.
        if _pid and not _pid.startswith("Unknown") and not is_bot:
            href += f"?pid={quote(_pid, safe='')}"
        n = r["n"] or 1
        wins = r["wins"] or 0
        winpct = (wins / n) * 100
        # Neutral all-players list — no owner-relative "with/vs" framing. Platform
        # shows the brand logo in a square box (consistent with the filter).
        return f"""
          <tr class="player-row">
            <td class="num tnum rank">{rank}</td>
            <td><a class="player-link" href="{href}">{html.escape(r["name"])}</a> {tag}</td>
            <td class="plat-cell" title="{html.escape(r["platform"] or '')}">{_platform_icon_html(r["platform"], size=15)}</td>
            <td class="num tnum">{r["n"]}</td>
            <td class="num tnum" style="white-space:nowrap"><b>{wins}</b><span class="dim">-{r["n"] - wins}</span> <span class="dim">({winpct:.0f}%)</span></td>
            {_stat_cols_td(r)}
          </tr>
        """

    # Relation filter: teammates only / opponents only / both. Filter the
    # query result client-side since the SQL already returns was_teammate
    # and was_opponent flags.
    if relation == "teammates":
        rows = [r for r in rows if r["was_teammate"]]
    elif relation == "opponents":
        rows = [r for r in rows if r["was_opponent"]]

    def _params_with(override: dict) -> str:
        parts = []
        for k, v in [("sort", sort), ("relation", relation)]:
            actual = override.get(k, v)
            if actual and actual != ("frequency" if k == "sort" else "all"):
                parts.append(f"{k}={actual}")
        return "?" + "&".join(parts) if parts else ""
    def sort_chip(label: str, s: str) -> str:
        active = " active" if sort == s else ""
        return f'<a class="{active.strip()}" href="/players{_params_with({"sort": s})}">{label}</a>'
    def relation_chip(label: str, r: str) -> str:
        active = " active" if relation == r else ""
        return f'<a class="{active.strip()}" href="/players{_params_with({"relation": r})}">{label}</a>'
    sort_toolbar = f"""
      <div class="toolbar" style="margin: 12px 0 16px">
        <div class="seg" title="Sort players">
          {sort_chip("Most matches", "frequency")}
          {sort_chip("Name", "name")}
          {sort_chip("Platform", "platform")}
          {sort_chip("Goals",  "goals")}
          {sort_chip("Wins",   "wins")}
        </div>
        <div class="seg" title="Filter by relationship">
          {relation_chip("Everyone", "all")}
          {relation_chip("Teammates", "teammates")}
          {relation_chip("Opponents", "opponents")}
        </div>
        <div style="margin-left:auto;font-size:12px;color:var(--text-dim)">
          {len(rows)} player{'s' if len(rows) != 1 else ''}
        </div>
      </div>
    """
    body = f"""
      <h1>All players</h1>
      <p class="caption">Everyone we've recorded in any match. Click a name to see their full stats.</p>
      {sort_toolbar}
      <section>
        <table class="players-table">
          <thead><tr>
            <th class="num rank">#</th>
            <th>Player</th><th>Platform</th>
            <th class="num">Matches</th><th class="num">W-L</th>
            {_stat_cols_th()}
          </tr></thead>
          <tbody>
            {"".join(_row(r, i + 1) for i, r in enumerate(rows))}
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
      <p class="caption">No matches in the DB for <b>{html.escape(name)}</b>. Check spelling or
      see <a href="/players">all players</a>.</p>
    """, status=404)


def _all_matches_html(store, *, include_bots=True, mode_filter=None,
                      window_days=None, platform_filter=None, sort="recent") -> str:
    """Neutral ALL-MATCHES view (the default Matches page): every recorded match —
    score, arena, mode, MVP — not tied to any one player. No per-player stat line."""
    ts_sql = ("(SELECT MAX(c) FROM (SELECT team_num, COUNT(*) c FROM "
              "match_player_stats WHERE match_id=m.id GROUP BY team_num))")
    clauses, args = [], []
    if mode_filter is not None:
        clauses.append(f"{ts_sql} = ?"); args.append(mode_filter)
    if window_days and window_days > 0:
        import time as _t
        clauses.append("m.started_at >= ?"); args.append(_t.time() - window_days * 86400)
    if not include_bots:
        clauses.append("NOT EXISTS (SELECT 1 FROM match_player_stats x "
                       "WHERE x.match_id=m.id AND x.is_bot=1)")
    if platform_filter:
        clauses.append("EXISTS (SELECT 1 FROM match_player_stats op "
                       "WHERE op.match_id=m.id AND op.platform LIKE '%' || ? || '%')")
        args.append(platform_filter)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    order = ("(m.team0_score + m.team1_score) DESC, m.started_at DESC"
             if sort == "goals" else "m.started_at DESC")
    with store._conn() as con:
        rows = con.execute(
            f"SELECT m.id, m.started_at, m.arena, m.team0_score, m.team1_score, "
            f"m.winner_team_num, {ts_sql} AS team_size FROM matches m{where} "
            f"ORDER BY {order} LIMIT 2000", tuple(args)).fetchall()
        mvp_map = {}
        for mr in con.execute("SELECT match_id, name, primary_id, team_num, is_bot "
                              "FROM match_player_stats WHERE is_mvp=1"):
            mvp_map.setdefault(mr["match_id"], mr)
    body_rows = []
    for r in rows:
        ts_iso = datetime.fromtimestamp(r["started_at"]).isoformat() if r["started_at"] else ""
        ts_fb = datetime.fromtimestamp(r["started_at"]).strftime("%b %d, %Y") if r["started_at"] else "—"
        sz = r["team_size"]; mode_lbl = f"{sz}v{sz}" if sz else ""
        w = r["winner_team_num"]
        c0 = "vs-score win" if w == 0 else "vs-score"
        c1 = "vs-score win" if w == 1 else "vs-score"
        mvp = _mvp_cell_html(mvp_map.get(r["id"]), viewer_is_mvp=False)
        body_rows.append(
            f'<tr class="row click match-row" onclick="window.location=\'/match/{quote(r["id"], safe="")}\'">'
            f'<td class="dim tnum"><time datetime="{ts_iso}">{ts_fb}</time></td>'
            f'<td class="score-cell"><div class="vs-line">'
            f'<span class="vs-team blue">Blue</span><span class="{c0}">{r["team0_score"]}</span>'
            f'<span class="vs-sep">vs</span><span class="{c1}">{r["team1_score"]}</span>'
            f'<span class="vs-team orng">Orange</span></div></td>'
            f'<td class="dim arena-cell">{html.escape(_arena_nice(r["arena"] or ""))}</td>'
            f'<td class="dim">{mode_lbl}</td><td>{mvp}</td></tr>')
    total = len(rows)
    parts = []
    if mode_filter: parts.append(f"{mode_filter}v{mode_filter}")
    if window_days: parts.append({1: "today", 7: "last 7 days", 30: "last 30 days"}.get(window_days, ""))
    if platform_filter: parts.append(f"vs {html.escape(platform_filter)}")
    fsum = (" · " + ", ".join(p for p in parts if p)) if any(parts) else ""
    table = (f'<table class="history"><thead><tr><th style="width:110px">When</th>'
             f'<th>Score</th><th>Arena</th><th>Mode</th>'
             f'<th>{_stat_icon_html("MVP")}MVP</th></tr></thead>'
             f'<tbody>{"".join(body_rows)}</tbody></table>'
             if body_rows else '<div class="empty">No matches for this filter.</div>')
    body = (f'<div class="page-head"><div><h1>All matches</h1>'
            f'<div class="sub">{total} match{"es" if total != 1 else ""} recorded{fsum}</div>'
            f'</div></div>{table}')
    return _page_wrap("All matches", body, active="history")


def _history_page_html(store, primary_id, name, *,
                       include_bots=True,
                       mode_filter: int | None = None,
                       window_days: int | None = None,
                       platform_filter: str | None = None,
                       sort: str = "recent",
                       is_self: bool = True,
                       all_matches: bool = False) -> str:
    if all_matches:
        return _all_matches_html(store, include_bots=include_bots, mode_filter=mode_filter,
                                 window_days=window_days, platform_filter=platform_filter, sort=sort)
    rows = _match_history_rows(
        store, primary_id, name, limit=2000,
        include_bots=include_bots, mode_filter=mode_filter,
        window_days=window_days, platform_filter=platform_filter,
    )
    # Sort options. "recent" is the source order (already DESC by started_at).
    if sort == "score":
        rows = sorted(rows, key=lambda r: -(r["score"] or 0))
    elif sort == "goals":
        rows = sorted(rows, key=lambda r: -(r["goals"] or 0))
    elif sort == "saves":
        rows = sorted(rows, key=lambda r: -(r["saves"] or 0))
    elif sort == "best":
        # Wins first (highest score), then losses
        rows = sorted(rows, key=lambda r: (
            0 if r["team_num"] == r["winner_team_num"] else 1,
            -(r["score"] or 0),
        ))
    # else: keep insertion order (recent first)

    total = len(rows)
    wins = sum(1 for r in rows if r["team_num"] == r["winner_team_num"])
    losses = total - wins
    win_pct = (wins / total * 100) if total else 0.0
    total_goals = sum(r["goals"] or 0 for r in rows)
    total_assists = sum(r["assists"] or 0 for r in rows)
    total_saves = sum(r["saves"] or 0 for r in rows)

    mvps = _match_mvp_lookup(store, [r["id"] for r in rows])

    body_rows: list[str] = []
    for r in rows:
        won = r["team_num"] == r["winner_team_num"]
        ts_iso = datetime.fromtimestamp(r["started_at"]).isoformat()
        ts_fallback = datetime.fromtimestamp(r["started_at"]).strftime("%b %d, %Y")
        # Hide "Online" mode (it's the default ~95% of matches); only chip
        # when the match was offline.
        offline_chip = ('' if r["is_online"]
                        else '<span class="chip">Offline</span>')
        # Cap at 4: a mid-match sub/extra in the feed can push MAX(roster) past
        # the real playlist size, producing an impossible "5v5"/"6v6".
        ts = min(r["team_size"] or 0, 4)
        size_chip = (f'<span class="chip mode-chip">{ts}v{ts}</span>' if ts else '')
        mvp_html = _mvp_cell_html(mvps.get(r["id"]), viewer_is_mvp=bool(r["is_mvp"]))
        t0_winner = "winner" if r["winner_team_num"] == 0 else ""
        t1_winner = "winner" if r["winner_team_num"] == 1 else ""
        chips = " ".join(c for c in (size_chip, offline_chip) if c)
        # (Per-row touch-share bar removed — it read as "half a bar chart" on the
        # list; touch share / field tilt live on the match-detail page instead.)
        # Generic Blue / Orange labels keep the cell narrow regardless of how
        # long the actual club tag is. Hover the label to see the full tag.
        body_rows.append(f"""
          <tr class="row click match-row {'win' if won else 'loss'}" onclick="window.location='/match/{quote(r['id'], safe='')}'">
            <td><span class="badge {'win' if won else 'loss'}">{'W' if won else 'L'}</span></td>
            <td class="dim tnum"><time datetime="{ts_iso}">{ts_fallback}</time></td>
            <td class="score-cell">
              <div class="vs-line">
                <span class="vs-team blue" title="{html.escape(r['team0_name'])}">Blue</span>
                <span class="vs-score {t0_winner}">{r['team0_score']}</span>
                <span class="vs-sep">vs</span>
                <span class="vs-score {t1_winner}">{r['team1_score']}</span>
                <span class="vs-team orng" title="{html.escape(r['team1_name'])}">Orange</span>
              </div>
              {('<div class="row-chips">' + chips + '</div>') if chips else ''}
            </td>
            <td class="dim arena-cell">{html.escape(_arena_nice(r['arena'] or ''))}</td>
            <td class="num tnum"><b>{r['goals']}</b></td>
            <td class="num tnum"><b>{r['assists']}</b></td>
            <td class="num tnum"><b>{r['saves']}</b></td>
            <td class="num tnum"><b>{r['shots']}</b></td>
            <td>{mvp_html}</td>
          </tr>
        """)

    table_html = f"""
      <table class="history">
        <thead><tr>
          <th style="width:40px"></th>
          <th style="width:110px">When</th>
          <th>Score</th>
          <th>Arena</th>
          <th class="num">{_stat_icon_html("Goals")}Goals</th>
          <th class="num">{_stat_icon_html("Assists")}Assists</th>
          <th class="num">{_stat_icon_html("Saves")}Saves</th>
          <th class="num">{_stat_icon_html("Shots")}Shots</th>
          <th>{_stat_icon_html("MVP")}MVP</th>
        </tr></thead>
        <tbody>{"".join(body_rows)}</tbody>
      </table>
    """ if body_rows else '<div class="empty">No matches recorded for this filter.</div>'

    # All three toolbars (mode / sort / bots) compose through query params.
    def _url(mode_=..., sort_=..., bots_=...) -> str:
        m = mode_filter if mode_ is ... else mode_
        s = sort if sort_ is ... else sort_
        b = include_bots if bots_ is ... else bots_
        parts = []
        if m is not None: parts.append(f"mode={m}")
        if s and s != "recent": parts.append(f"sort={s}")
        if b: parts.append("include_bots=1")
        return "/history" + ("?" + "&".join(parts) if parts else "")

    def mode_chip(label: str, m: int | None) -> str:
        active = " active" if mode_filter == m else ""
        return f'<a class="{active.strip()}" href="{_url(mode_=m)}">{label}</a>'
    def sort_chip(label: str, s: str) -> str:
        active = " active" if sort == s else ""
        return f'<a class="{active.strip()}" href="{_url(sort_=s)}">{label}</a>'

    bots_chip_state = "active" if not include_bots else ""
    bots_target_url = _url(bots_=not include_bots)

    filter_summary = ""
    if mode_filter is not None:
        filter_summary = f" &middot; <span class='dim'>filter: {mode_filter}v{mode_filter}</span>"
    if sort != "recent":
        filter_summary += f" &middot; <span class='dim'>sort: {sort}</span>"

    body = f"""
      <div class="page-head">
        <div>
          <h1>{"Match history" if is_self else html.escape(name or "") + " — matches"}</h1>
          <div class="sub">{total} match{'es' if total != 1 else ''} shown{filter_summary}</div>
        </div>
      </div>

      <div class="toolbar">
        <div class="seg" title="Sort matches">
          {sort_chip("Recent", "recent")}
          {sort_chip("Score",  "score")}
          {sort_chip("Goals",  "goals")}
          {sort_chip("Saves",  "saves")}
          {sort_chip("Wins first", "best")}
        </div>
        <div style="margin-left:auto;font-size:12px;color:var(--text-dim)">
          {total} match{'es' if total != 1 else ''}
        </div>
      </div>

      <div class="summary-row">
        <span>Showing:</span>
        <span><b class="tnum">{wins}-{losses}</b> &middot; <span class="dim">{win_pct:.1f}% win rate</span></span>
        <span class="dim">&middot;</span>
        <span><b class="tnum">{total_goals}</b> goals</span>
        <span><b class="tnum">{total_assists}</b> assists</span>
        <span><b class="tnum">{total_saves}</b> saves</span>
      </div>

      <div class="card" style="padding:0;overflow:hidden">
        {table_html}
      </div>
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
    # Render the start timestamp as ISO-8601 so a small <time> formatter can
    # rewrite it to 12hr local on the client. Server-side fallback is also
    # 12hr-ish so it's readable even if JS doesn't run.
    started_iso = datetime.fromtimestamp(m["started_at"]).isoformat()
    started_fallback = datetime.fromtimestamp(m["started_at"]).strftime("%b %d, %Y")
    duration = extras["duration_seconds"] if extras else 0
    mode = "Online" if m["is_online"] else "Offline"

    t0_name = m["team0_name"] or "Blue"
    t1_name = m["team1_name"] or "Orange"
    t0_score = m["team0_score"]
    t1_score = m["team1_score"]
    winner = m["winner_team_num"]
    winner_is_0 = (winner == 0)

    # Derived per-player stats (demos received, highlights, etc.) + the
    # goal-location list for the mini-pitch map.
    derived = _derive_match_extras(store, match_id)
    goal_locations = derived.pop("__goal_locations__", [])
    # Team-level goal counts for goal participation %.
    team_goals = {0: 0, 1: 0}
    for p in players:
        team_goals[p["team_num"]] += p["goals"] or 0

    # Playback dataset is reused by both the playback widget and the insights
    # card, so we compute it once up front.
    playback_data = _build_playback_data(
        store, match_id, m["started_at"], t0_name, t1_name, duration,
    )

    # True gameplay duration: ClockUpdatedSeconds fires once per game-clock
    # second, so its range captures actual play time. The wall-clock duration
    # in match_extras inflates this by ~30s per goal (goal replays) plus
    # podium / kickoff countdowns.
    clock_map = playback_data["clock_map"]
    if clock_map:
        reg_clocks = [tg for _, tg in clock_map if tg >= 0]
        ot_clocks  = [-tg for _, tg in clock_map if tg < 0]
        # Nominal whole-minute regulation + OT elapsed - identical to the
        # aggregator/Discord derivation so every surface shows the same length.
        regulation_played = (round(max(reg_clocks) / 60) * 60) if reg_clocks else 0
        overtime_played = max(ot_clocks) if ot_clocks else 0
        game_duration = regulation_played + overtime_played
        is_overtime_match = overtime_played > 0
    else:
        game_duration = duration  # fallback if we never captured clock ticks
        is_overtime_match = False
    g_mm, g_ss = int(game_duration // 60), int(game_duration % 60)

    # Per-player touch counts for the mini-heatmap thumbnails + the BPM /
    # touch-share tiles on each radar card.
    player_touches: dict[str, int] = {}
    team_touches = {0: 0, 1: 0}
    for bh in playback_data["ball_track"]:
        n = bh["player"]
        if not n:
            continue
        player_touches[n] = player_touches.get(n, 0) + 1
        if bh["team"] in (0, 1):
            team_touches[bh["team"]] += 1

    # Radar axis scaling: previously used per-axis MAX (peak) value which made
    # the chart wildly skewed - one player with 7 goals would push every axis
    # to a scale where average finishers look tiny. Switch to scaling at 2x
    # the per-axis MEAN so an average performer reads at half-radius (the
    # "this is normal" zone), strong performers fill toward the edge, and a
    # huge outlier extends slightly past 1.0 (clamped visually).
    def _scale_max(key: str) -> float:
        vals = [(p[key] or 0) for p in players]
        if not vals:
            return 1.0
        avg = sum(vals) / len(vals)
        # Use 2 * avg, floor at 1, so radar has meaningful resolution.
        return max(1.0, avg * 2.0)
    peak_g  = _scale_max("goals")
    peak_a  = _scale_max("assists")
    peak_sv = _scale_max("saves")
    peak_sh = _scale_max("shots")
    peak_d  = _scale_max("demos")

    def _roster_card(team_num: int) -> str:
        team_players = sorted(
            [p for p in players if p["team_num"] == team_num],
            key=lambda p: -(p["score"] or 0),
        )
        tname = t0_name if team_num == 0 else t1_name
        tscore = t0_score if team_num == 0 else t1_score
        is_winner = (team_num == winner)
        color_class = "team-blue" if team_num == 0 else "team-orng"

        t_goals   = sum(p["goals"]   or 0 for p in team_players)
        t_assists = sum(p["assists"] or 0 for p in team_players)
        t_saves   = sum(p["saves"]   or 0 for p in team_players)
        t_shots   = sum(p["shots"]   or 0 for p in team_players)
        t_demos   = sum(p["demos"]   or 0 for p in team_players)
        t_score   = sum(p["score"]   or 0 for p in team_players)
        t_touches = sum(p["touches"] or 0 for p in team_players) if "touches" in (team_players[0].keys() if team_players else []) else 0

        rows: list[str] = []
        for p in team_players:
            is_viewer = (viewer_pid and p["primary_id"] == viewer_pid and p["name"] == viewer_name) or \
                        (viewer_name and p["name"] == viewer_name and not viewer_pid)
            mvp = " <span class='chip mvp'>MVP</span>" if p["is_mvp"] else ""
            bot = " <span class='chip bot'>BOT</span>" if p["is_bot"] else ""
            you = ""  # neutral all-players view — no "you" framing
            href = f"/player/{quote(p['name'], safe='')}"
            link_cls = "player-link"  # neutral — no owner self-highlight (was orange)
            nm = html.escape(p["name"] or "")  # attacker-controllable
            name_link = (f"<span class='{link_cls}' style='cursor:default;color:var(--text-faint)'>{nm}</span>"
                         if p["is_bot"] else
                         f"<a class='{link_cls}' href='{href}'>{nm}</a>")
            platform = p["platform"] if "platform" in p.keys() else ""
            # The roster table stays clean - basic counts only. All advanced
            # stats (where you are on the field, boost usage, speed) move into
            # the per-player section below the table.
            meta_line = f'<div class="meta-line"><span>{platform or "Unknown platform"}</span></div>'
            touches_cell = f"<td class='num tnum'>{p['touches'] or 0}</td>" if "touches" in p.keys() else "<td></td>"
            rows.append(f"""
              <tr>
                <td class="player-cell">{name_link}{you}{bot}{meta_line}</td>
                <td class="num tnum"><b>{p['score']}</b></td>
                <td class="num tnum">{p['goals']}</td>
                <td class="num tnum">{p['assists']}</td>
                <td class="num tnum">{p['saves']}</td>
                <td class="num tnum">{p['shots']}</td>
                <td class="num tnum">{p['demos']}</td>
                {touches_cell}
                <td>{mvp}</td>
              </tr>
            """)

        winner_chip = "<span class='chip win'>Winner</span>" if is_winner else ""
        return f"""
          <div class="roster-card {color_class}">
            <div class="roster-head">
              <div class="roster-team">
                <span class="roster-stripe"></span>
                <span>{html.escape(tname)}</span>
                {winner_chip}
              </div>
              <span class="roster-score tnum">{tscore}</span>
            </div>
            <table>
              <thead><tr>
                <th>Player</th>
                <th class="num">Score</th>
                <th class="num">{_stat_icon_html("Goals")}Goals</th>
                <th class="num">{_stat_icon_html("Assists")}Assists</th>
                <th class="num">{_stat_icon_html("Saves")}Saves</th>
                <th class="num">{_stat_icon_html("Shots")}Shots</th>
                <th class="num">{_stat_icon_html("Demos")}Demos</th>
                <th class="num">Touches</th>
                <th>{_stat_icon_html("MVP")}MVP</th>
              </tr></thead>
              <tbody>
                {"".join(rows)}
                <tr class="total-row">
                  <td>Team total</td>
                  <td class="num tnum">{t_score}</td>
                  <td class="num tnum">{t_goals}</td>
                  <td class="num tnum">{t_assists}</td>
                  <td class="num tnum">{t_saves}</td>
                  <td class="num tnum">{t_shots}</td>
                  <td class="num tnum">{t_demos}</td>
                  <td class="num tnum">{t_touches}</td>
                  <td></td>
                </tr>
              </tbody>
            </table>
          </div>
        """

    def _radar_card(p, active: bool = False) -> str:
        is_viewer = (viewer_pid and p["primary_id"] == viewer_pid and p["name"] == viewer_name)
        team_class = "team-blue" if p["team_num"] == 0 else "team-orng"
        color = "var(--team-blue)" if p["team_num"] == 0 else "var(--team-orng)"
        marker = ""  # neutral all-players view — no "you" framing
        mvp = " <span class='chip mvp' style='margin-left:6px'>MVP</span>" if p["is_mvp"] else ""
        bot = " <span class='chip bot' style='margin-left:6px'>BOT</span>" if p["is_bot"] else ""
        slug = "".join(ch if ch.isalnum() else "_" for ch in (p["name"] or "")) or "p"
        href = f"/player/{quote(p['name'], safe='')}"
        nm = html.escape(p["name"] or "")  # attacker-controllable
        name_link = (f"<span class='player-link' style='cursor:default;color:var(--text-faint)'>{nm}</span>"
                     if p["is_bot"] else
                     f"<a class='player-link' href='{href}'>{nm}</a>")

        ticks = p["ticks_total"] or 0
        expected_ticks = max(int(duration * 30), 1)
        cov = ticks / expected_ticks

        # Movement + boost (only meaningful at >=70% coverage).
        if cov >= 0.70 and ticks >= 200:
            sup = p["ticks_supersonic"] / ticks * 100
            # Ground/air/wall partition POSITION time, but some ticks lack a clean
            # position classification (so g+a+w can fall short of ticks_total and
            # read e.g. 68/0/10 = 78%). Normalise to their own sum so the three
            # always read as shares of classified position time and total 100%.
            pos_ticks = ((p["ticks_on_ground"] or 0) + (p["ticks_in_air"] or 0)
                         + (p["ticks_on_wall"] or 0)) or 1
            air = (p["ticks_in_air"] or 0) / pos_ticks * 100
            wall = (p["ticks_on_wall"] or 0) / pos_ticks * 100
            ground = (p["ticks_on_ground"] or 0) / pos_ticks * 100
            avg_sp = p["speed_sum"] / ticks
            boost = p["boost_used"]
            zero_pct = (p["ticks_zero_boost"] or 0) / ticks * 100
            full_pct = (p["ticks_full_boost"] or 0) / ticks * 100
            boosting_pct = (p["ticks_boosting"] or 0) / ticks * 100
            # Stacked bar visualisations - same data as before but visual.
            pos_bar = _stacked_bar([
                ("Ground", ground, "#3b82f6"),
                ("Air",    air,    "#a78bfa"),
                ("Wall",   wall,   "#fb923c"),
            ])
            speed_bar = _stacked_bar([
                ("Slow",       max(0, 100 - boosting_pct - sup), "#475569"),
                ("Boosting",   boosting_pct,                     "#fb923c"),
                ("Supersonic", sup,                              "#ef4444"),
            ])
            boost_bar = _stacked_bar([
                ("At 0",  zero_pct,                          "#ef4444"),
                ("Mid",   max(0, 100 - zero_pct - full_pct), "#facc15"),
                ("At 100", full_pct,                         "#34d399"),
            ])
            movement_html = f"""
              <div class="rc-adv-section">
                <div class="rc-adv-title">Position (ground / air / wall)</div>
                {pos_bar}
                <ul class="rc-stat-line">
                  <li><b>{ground:.0f}%</b> <span>ground</span></li>
                  <li><b>{air:.0f}%</b> <span>air</span></li>
                  <li><b>{wall:.0f}%</b> <span>wall</span></li>
                </ul>
              </div>
              <div class="rc-adv-section">
                <div class="rc-adv-title">Speed profile</div>
                {speed_bar}
                <ul class="rc-stat-line">
                  <li><b>{avg_sp:.0f}</b> <span>avg speed</span></li>
                  <li><b>{boosting_pct:.0f}%</b> <span>boosting</span></li>
                  <li><b>{sup:.0f}%</b> <span>supersonic</span></li>
                </ul>
              </div>
              <div class="rc-adv-section">
                <div class="rc-adv-title">Boost distribution</div>
                {boost_bar}
                <ul class="rc-stat-line">
                  <li><b>{boost:.0f}</b> <span>used</span></li>
                  <li><b>{zero_pct:.0f}%</b> <span>near-empty boost</span></li>
                  <li><b>{full_pct:.0f}%</b> <span>at 100 boost</span></li>
                </ul>
              </div>
            """
        else:
            # No movement-stats section for players without coverage. The single
            # team-level disclaimer above the orange roster makes it clear why.
            movement_html = ""

        # Combat + scoring quality (works for everyone, opponents included).
        ext = derived.get(p["name"]) or _empty_derived()
        my_team_goals = team_goals.get(p["team_num"], 0) or 0
        contrib = (p["goals"] or 0) + (p["assists"] or 0)
        gp_pct = (contrib / my_team_goals * 100) if my_team_goals else 0
        avg_goal_speed = (ext["goal_speed_sum"] / ext["goal_count"]) if ext["goal_count"] else 0

        # Activity section: BPM, touch share, position-thirds. Touch metrics
        # come from BallHit positions filtered to this player; they work for
        # everyone (the BallHit envelope always includes the toucher). BPM
        # requires spectator coverage so we only show it when available.
        my_touches = player_touches.get(p["name"], 0)
        my_team_total = team_touches.get(p["team_num"], 0) or 1
        touch_share = my_touches / my_team_total * 100
        # Defensive/neutral/offensive thirds from this player's BallHit Y.
        defensive_y_sign = 1 if p["team_num"] == 0 else -1  # +Y defended by Blue
        def_n = neu_n = off_n = 0
        for bh in playback_data["ball_track"]:
            if bh["player"] != p["name"]:
                continue
            signed_y = bh["y"] * defensive_y_sign
            if signed_y > 1707:
                def_n += 1
            elif signed_y < -1707:
                off_n += 1
            else:
                neu_n += 1
        total_thirds = def_n + neu_n + off_n or 1
        def_pct = def_n / total_thirds * 100
        neu_pct = neu_n / total_thirds * 100
        off_pct = off_n / total_thirds * 100

        # BPM only when we have boost data.
        bpm_tile = ""
        if cov >= 0.70 and ticks >= 200 and duration > 0:
            bpm = (p["boost_used"] or 0) / (duration / 60.0)
            bpm_tile = f'<li><b>{bpm:.0f}</b> <span>BPM</span></li>'

        # Mini touch-spot map (only rendered when this player has touches).
        # Per-match shows discrete spots, not a density heatmap — a handful of
        # touches in one game reads truer as markers than as a blurred field.
        mini_heatmap = ""
        if my_touches:
            # Slug the player name so any per-SVG ids stay unique.
            name_slug = "".join(ch if ch.isalnum() else "_" for ch in p["name"])
            # Per-match mini: keep the literal pitch orientation (matches the
            # playback above) since the player is on one team this match.
            hm = _touch_spots_svg(playback_data, player_filter=p["name"],
                                  compact=True, key=name_slug, orient=False)
            mini_heatmap = (
                f'<div class="rc-adv-section">'
                f'<div class="rc-adv-title">Where they touched the ball '
                f'<span class="dim" style="font-weight:400">· kickoffs excluded</span></div>'
                f'<div class="rc-mini-heatmap">{hm}</div>'
                f'</div>'
            )

        activity_html = f"""
          <div class="rc-adv-section">
            <div class="rc-adv-title">Activity</div>
            <ul class="rc-stat-line">
              <li><b>{my_touches}</b> <span>touches ({touch_share:.0f}% of team)</span></li>
              {bpm_tile}
              <li><b>{def_pct:.0f}%</b> <span>defensive third</span></li>
              <li><b>{neu_pct:.0f}%</b> <span>neutral</span></li>
              <li><b>{off_pct:.0f}%</b> <span>offensive third</span></li>
            </ul>
          </div>
        """

        combat_html = f"""
          <div class="rc-adv-section">
            <div class="rc-adv-title">Combat &amp; scoring quality</div>
            <ul class="rc-stat-line">
              <li><b>{ext['demos_received']}</b> <span>demos taken</span></li>
              <li><b>{ext['crossbar_hits']}</b> <span>crossbars</span></li>
              <li><b>{gp_pct:.0f}%</b> <span>goal participation</span></li>
              <li><b>{avg_goal_speed:.0f}</b> <span>kph avg goal</span></li>
            </ul>
          </div>
        """

        # Special highlights - one tile per event type, only render non-zero.
        highlight_pairs = [
            ("EpicSave",      "Epic saves"),
            ("AerialGoal",    "Aerial goals"),
            ("BicycleHit",    "Bicycle hits"),
            ("FlipReset",     "Flip resets"),
            ("HatTrick",      "Hat tricks"),
            ("LongGoal",      "Long goals"),
            ("BackwardsGoal", "Backwards goals"),
            ("Savior",        "Saviors"),
            ("LowFive",       "Low fives"),
        ]
        highlight_tiles = []
        for raw_ev, label in highlight_pairs:
            count = ext[f"n_{raw_ev.lower()}"]
            if count:
                icon = _rl_icon_html(raw_ev, size=16, alt="")
                highlight_tiles.append(
                    f'<li class="rc-stat-line-highlight">{icon}<b>{count}</b> '
                    f'<span>{label.lower()}</span></li>'
                )
        if highlight_tiles:
            highlights_html = f"""
              <div class="rc-adv-section">
                <div class="rc-adv-title">Highlights ({ext['highlights']} total)</div>
                <ul class="rc-stat-line">{"".join(highlight_tiles)}</ul>
              </div>
            """
        else:
            highlights_html = ""

        # Highlight badges in the header row when player produced any specials.
        header_badges = ""
        if ext["highlights"]:
            header_badges = f' <span class="chip highlight-chip" title="Special moments">{ext["highlights"]} HL</span>'

        # Collapsible: the viewer's card opens by default, everyone else's
        # collapses to just the name row -- fixes the tall 6-card vertical stack.
        # G/A/Sv/Sh/D/Score live once in the roster table above; this card holds
        # only the non-roster detail (combat extras, highlights, movement, map).
        return f"""
          <div class="player-panel {team_class}{' active' if active else ''}" id="pp-{slug}" role="tabpanel">
            <div class="pp-head">{name_link}{mvp}{bot}{marker}{header_badges}</div>
            <div class="rc-body">
              <div class="rc-adv">
                {combat_html}
                {activity_html}
                {highlights_html}
                {movement_html}
                {mini_heatmap}
              </div>
            </div>
          </div>
        """

    blue_players = sorted([p for p in players if p["team_num"] == 0], key=lambda p: -(p["score"] or 0))
    orng_players = sorted([p for p in players if p["team_num"] == 1], key=lambda p: -(p["score"] or 0))

    blue_loss_class = "" if winner_is_0 else "loss"
    orng_loss_class = "loss" if winner_is_0 else ""
    blue_result = "win" if winner_is_0 else "loss"
    orng_result = "loss" if winner_is_0 else "win"

    # Match-context tag, from the real game clock (not a wall-clock guess):
    # any overtime clock ticks => Overtime; a very short match => Forfeit.
    if game_duration < 180:
        match_context = "Forfeit"
        match_context_class = "ff"
    elif is_overtime_match:
        match_context = "Overtime"
        match_context_class = "ot"
    else:
        match_context = "Regulation"
        match_context_class = "reg"

    # In-page section nav: a REAL tab-swap (show one pane, hide the rest) via the
    # JS below — not anchor-scroll down a long page. Player chips jump to the
    # Players pane and select that player. Neutral all-players view: no "you".
    def _mn_chip(target, label, cls=""):
        return (f'<button type="button" class="mn-chip {cls}" '
                f'data-target="{target}">{label}</button>')
    _sec_chips = [
        _mn_chip("overview", "Overview", "active"),
        _mn_chip("timeline", "Timeline"),
        _mn_chip("goalmap", "Goal map") if playback_data.get("goals") else "",
        _mn_chip("compare", "Teams"),
        _mn_chip("kickoff", "Kickoff"),
        _mn_chip("players", "Players"),
    ]
    all_pl = blue_players + orng_players
    # Section tabs only — the per-player name chips were redundant (the Players
    # tab's box score shows every player at once) and read as "weird beside the
    # pages". Clean separation: nav = pages; the Players tab = players.
    match_nav = '<nav class="match-nav">' + "".join(c for c in _sec_chips if c) + '</nav>'

    # ---- Per-player advanced stats as ESPN/ballchasing box scores ------------
    # Full-width team-grouped tables (all players in rows, stats in columns)
    # instead of cramped one-player-at-a-time panels of inline word-soup.
    def _adv(p):
        ticks = p["ticks_total"] or 0
        cov = ticks / max(int(duration * 30), 1)
        has_mov = cov >= 0.70 and ticks >= 200
        ext = derived.get(p["name"]) or _empty_derived()
        # Use the official `touches` stat (matches the roster + team-comparison),
        # not the playback BallHit count, which undercounts and would show a
        # different number for the same player on the same page.
        my_touches = p["touches"] or 0
        team_tot = sum((q["touches"] or 0) for q in all_pl
                       if q["team_num"] == p["team_num"]) or 1
        dsign = 1 if p["team_num"] == 0 else -1
        dn = nn = on = 0
        for bh in playback_data["ball_track"]:
            if bh["player"] != p["name"]:
                continue
            sy = bh["y"] * dsign
            if sy > 1707:
                dn += 1
            elif sy < -1707:
                on += 1
            else:
                nn += 1
        tt = dn + nn + on or 1
        my_tg = team_goals.get(p["team_num"], 0) or 0
        contrib = (p["goals"] or 0) + (p["assists"] or 0)
        d = {
            "name": p["name"], "team": p["team_num"], "is_bot": p["is_bot"], "is_mvp": p["is_mvp"],
            "touches": my_touches, "touch_share": my_touches / team_tot * 100,
            "def": dn / tt * 100, "neu": nn / tt * 100, "off": on / tt * 100,
            "demos_taken": ext["demos_received"], "crossbars": ext["crossbar_hits"],
            "gp": (contrib / my_tg * 100) if my_tg else 0,
            "bpm": None, "boost_used": None, "zero": None, "full": None,
            "avg_sp": None, "sup": None, "ground": None, "air": None, "wall": None,
        }
        if has_mov:
            pos = ((p["ticks_on_ground"] or 0) + (p["ticks_in_air"] or 0)
                   + (p["ticks_on_wall"] or 0)) or 1
            d.update(
                boost_used=p["boost_used"] or 0,
                bpm=((p["boost_used"] or 0) / (duration / 60.0)) if duration > 0 else 0,
                zero=(p["ticks_zero_boost"] or 0) / ticks * 100,
                full=(p["ticks_full_boost"] or 0) / ticks * 100,
                avg_sp=(p["speed_sum"] or 0) / ticks,
                sup=(p["ticks_supersonic"] or 0) / ticks * 100,
                ground=(p["ticks_on_ground"] or 0) / pos * 100,
                air=(p["ticks_in_air"] or 0) / pos * 100,
                wall=(p["ticks_on_wall"] or 0) / pos * 100,
            )
        return d

    _adv_rows = [_adv(p) for p in all_pl]
    _pct = lambda v: f"{v:.0f}%"
    _num = lambda v: f"{v:.0f}"

    def _bs_table(title, sub, cols):
        head = "".join(f'<th class="num">{h}</th>' for h, _, _ in cols)
        def _cell(r, k, fmt):
            v = r.get(k)
            return '<td class="num dim">&mdash;</td>' if v is None else f'<td class="num tnum">{fmt(v)}</td>'
        def _row(r):
            tc = "team-blue" if r["team"] == 0 else "team-orng"
            nm = html.escape(r["name"] or "")
            link = (f"<span class='player-link' style='color:var(--text-faint)'>{nm}</span>"
                    if r["is_bot"] else
                    f"<a class='player-link' href='/player/{quote(r['name'], safe='')}'>{nm}</a>")
            mvp = " <span class='chip mvp'>MVP</span>" if r["is_mvp"] else ""
            return (f'<tr class="{tc}"><td class="player-cell"><span class="bs-sw"></span>'
                    f'{link}{mvp}</td>{"".join(_cell(r, k, f) for _, k, f in cols)}</tr>')
        return (f'<div class="bs-card"><div class="bs-title">{title}'
                f'<span class="dim">{sub}</span></div>'
                f'<table class="bs-table"><thead><tr><th>Player</th>{head}</tr></thead>'
                f'<tbody>{"".join(_row(r) for r in _adv_rows)}</tbody></table></div>')

    box_involve = _bs_table(
        "Involvement &amp; positioning", "touches, ball-share, field thirds, combat",
        [("Touches", "touches", _num), ("Share", "touch_share", _pct),
         ("Def", "def", _pct), ("Neu", "neu", _pct), ("Off", "off", _pct),
         ("Demos", "demos_taken", _num), ("Bars", "crossbars", _num), ("Goal%", "gp", _pct)])
    box_boost = _bs_table(
        "Boost &amp; movement", "needs spectator coverage &middot; &mdash; = unavailable",
        [("BPM", "bpm", _num), ("Boost", "boost_used", _num),
         ("Full%", "full", _pct), ("Spd", "avg_sp", _num), ("SS%", "sup", _pct),
         ("Grnd", "ground", _pct), ("Air", "air", _pct), ("Wall", "wall", _pct)])

    _tmaps = []
    for _p in all_pl:
        if player_touches.get(_p["name"], 0):
            _slug = "".join(ch if ch.isalnum() else "_" for ch in (_p["name"] or ""))
            _hm = _touch_spots_svg(playback_data, player_filter=_p["name"],
                                   compact=True, key=_slug, orient=False)
            _tc = "team-blue" if _p["team_num"] == 0 else "team-orng"
            _tmaps.append(f'<div class="bs-tmap {_tc}"><div class="bs-tmap-name">'
                          f'{html.escape(_p["name"] or "")}</div>{_hm}</div>')
    box_tmaps = (f'<div class="bs-card"><div class="bs-title">Touch maps'
                 f'<span class="dim">kickoffs excluded</span></div>'
                 f'<div class="bs-tmap-grid">{"".join(_tmaps)}</div></div>') if _tmaps else ""

    players_box = box_involve + box_boost + box_tmaps

    # Match-played title: arena + game-clock duration + "Regulation/Overtime"
    # context replaces the hex match GUID, which was meaningless to humans.
    body = f"""
      <div class="breadcrumb">
        <a href="/history">&larr; Matches</a>
        <span style="margin:0 6px">/</span>
        <span>{arena} &middot;
          <time datetime="{started_iso}">{started_fallback}</time>
        </span>
      </div>

      <header class="match-hero">
        <div class="side left">
          <div class="team-stripe"></div>
          <div class="team-meta">
            <div class="team-tag">Blue &middot; Team 0</div>
            <div class="team-name" title="{html.escape(t0_name)}">{html.escape(t0_name)}</div>
            <span class="result-pill {blue_result}">{"Win" if winner_is_0 else "Loss"}</span>
          </div>
          <div class="score-display tnum {blue_loss_class}" style="margin-left:auto">{t0_score}</div>
        </div>

        <div class="middle">
          <div class="hero-duration tnum" title="Gameplay clock time">{g_mm}:{g_ss:02d}</div>
          <div class="hero-context">
            <span class="hero-ctx-final">Final</span>
            <span class="hero-ctx-pill {match_context_class}">{match_context}</span>
          </div>
          <div class="hero-meta">
            <span>{arena}</span>
            <span class="hero-meta-sep">&middot;</span>
            <time datetime="{started_iso}">{started_fallback}</time>
            <span class="hero-meta-sep">&middot;</span>
            <span>{mode}</span>
          </div>
          {_mvp_callout_html(players, viewer_pid, viewer_name)}
        </div>

        <div class="side right">
          <div class="team-stripe"></div>
          <div class="team-meta">
            <div class="team-tag">Orange &middot; Team 1</div>
            <div class="team-name" title="{html.escape(t1_name)}">{html.escape(t1_name)}</div>
            <span class="result-pill {orng_result}">{"Loss" if winner_is_0 else "Win"}</span>
          </div>
          <div class="score-display tnum {orng_loss_class}" style="margin-right:auto">{t1_score}</div>
        </div>
      </header>

      {match_nav}

      <section class="md-pane active" data-pane="overview">
        <div class="md-rosters">
          {_roster_card(0)}
          {_roster_card(1)}
        </div>
      </section>

      <section class="md-pane" data-pane="timeline">
        {_match_events_html(playback_data)}
      </section>

      <section class="md-pane" data-pane="goalmap">
        {_goal_map_html(playback_data)}
      </section>

      <section class="md-pane" data-pane="compare">
        {_match_compare_html(players, viewer_pid, viewer_name, t0_name, t1_name, playback_data)}
        {_match_insights_html(playback_data, t0_name, t1_name)}
      </section>

      <section class="md-pane" data-pane="kickoff">
        {_kickoff_card_html(playback_data, players, viewer_pid, viewer_name)}
      </section>

      <section class="md-pane" data-pane="players">
        {players_box}
      </section>
      <script>
      (function(){{
        var nav = document.querySelector('.match-nav'); if(!nav) return;
        function showPane(name){{
          document.querySelectorAll('.md-pane').forEach(function(p){{
            p.classList.toggle('active', p.dataset.pane === name);
          }});
          document.querySelectorAll('.mn-chip').forEach(function(c){{
            if(!c.dataset.pp) c.classList.toggle('active', c.dataset.target === name);
          }});
          nav.scrollIntoView({{block:'nearest'}});
        }}
        function activatePlayer(id){{
          document.querySelectorAll('.player-panel').forEach(function(p){{ p.classList.toggle('active', p.id === id); }});
          document.querySelectorAll('.pp-tab').forEach(function(t){{ t.classList.toggle('active', t.dataset.pp === id); }});
        }}
        document.querySelectorAll('.mn-chip').forEach(function(el){{
          el.addEventListener('click', function(){{
            if (el.dataset.target) showPane(el.dataset.target);
            if (el.dataset.pp) activatePlayer(el.dataset.pp);
          }});
        }});
        document.querySelectorAll('.pp-tab').forEach(function(el){{
          el.addEventListener('click', function(){{ if (el.dataset.pp) activatePlayer(el.dataset.pp); }});
        }});
      }})();
      </script>
    """
    return _page_wrap("Match detail", body, active="history", with_sidebar=False)


def _build_playback_data(store, match_id: str, started_at: float,
                         t0_name: str, t1_name: str, duration_seconds: float) -> dict:
    """Pull every spatial event for the match and turn it into a JSON-friendly
    structure for the client-side playback engine.

    Output keys:
      meta:        arena/teams/duration
      svg:         viewbox + projection constants
      duration:    seconds (playback runs in wall-clock seconds from kickoff)
      ball_track:  [{t, x, y, z, sx, sy, player, team}, ...] one entry per BallHit
      events:      [{t, kind, label, player, team, sx?, sy?, secondary?}, ...]
                   ordered, used for the right-hand event list AND for visual
                   pulses on the pitch
      goals:       [{t, scorer, team, speed, assister?, chain:[{sx,sy,player,team}]}]
                   the pre-goal touch chain, used as a static overlay on the pitch
      clock_map:   [[t_wall, t_game_remaining], ...] for the in-game clock display
    """
    import json as _json

    # Field dimensions (Rocket League world coords).
    rl_len = 10240   # Y range total (-5120 to +5120)
    rl_wid = 8192    # X range total (-4096 to +4096)

    # SVG viewbox.
    vb_w, vb_h = 880, 380
    pitch_w, pitch_h = 800, 320
    pad_x, pad_y = (vb_w - pitch_w) / 2, (vb_h - pitch_h) / 2

    def project(rx: float, ry: float) -> tuple[float, float]:
        # Clamp slightly past the field so a goal recorded just past the line
        # still draws inside the visible box.
        ry = max(min(ry, rl_len / 2 + 280), -(rl_len / 2 + 280))
        rx = max(min(rx, rl_wid / 2 + 280), -(rl_wid / 2 + 280))
        px = pad_x + ((ry + rl_len / 2) / rl_len) * pitch_w
        py = pad_y + ((rx + rl_wid / 2) / rl_wid) * pitch_h
        return round(px, 1), round(py, 1)

    with store._conn() as con:
        rows = con.execute(
            "SELECT received_at, event, payload FROM raw_events "
            "WHERE match_id = ? AND event IN "
            "  ('BallHit','GoalScored','CrossbarHit','StatfeedEvent','ClockUpdatedSeconds',"
            "   'ReplayPlaybackStart','ReplayPlaybackEnd','ReplayWillEnd') "
            "ORDER BY received_at",
            (match_id,),
        ).fetchall()

    # Build replay intervals first so we can drop any BallHit / ClockUpdate
    # events that fired during a goal-replay. RL re-broadcasts ball motion
    # during each replay, which otherwise pollutes the heatmap and makes the
    # playback time jump around.
    replay_intervals: list[tuple[float, float]] = []
    _pending_replay_start: float | None = None
    for r in rows:
        t = max(0.0, r["received_at"] - started_at)
        if r["event"] == "ReplayPlaybackStart":
            _pending_replay_start = t
        elif r["event"] == "ReplayPlaybackEnd" and _pending_replay_start is not None:
            replay_intervals.append((_pending_replay_start, t))
            _pending_replay_start = None
    # If the recording ended mid-replay, close the last interval at the
    # final event time so we still suppress its noise.
    if _pending_replay_start is not None and rows:
        last_t = max(0.0, rows[-1]["received_at"] - started_at)
        replay_intervals.append((_pending_replay_start, last_t))

    def in_replay(t: float) -> bool:
        for a, b in replay_intervals:
            if a <= t <= b:
                return True
        return False

    ball_track: list[dict] = []
    events: list[dict] = []
    goals_raw: list[dict] = []
    clock_map: list[list[float]] = []

    statfeed_kinds = {
        "Demolish":      ("demo",       "Demo"),
        "EpicSave":      ("epic-save",  "Epic save"),
        "AerialGoal":    ("aerial",     "Aerial goal"),
        "BicycleHit":    ("bicycle",    "Bicycle hit"),
        "BackwardsGoal": ("aerial",     "Backwards goal"),
        "LongGoal":      ("aerial",     "Long goal"),
        "FlipReset":     ("flip-reset", "Flip reset"),
        "HatTrick":      ("hat-trick",  "Hat trick"),
        "Savior":        ("epic-save",  "Savior"),
        "LowFive":       ("celebrate",  "Low five"),
        "Shot":          ("shot",       "Shot on goal"),
        "Save":          ("save",       "Save"),
        "Assist":        ("assist",     "Assist"),
    }

    # A kickoff first-touch is the first (non-replay) BallHit after the match
    # opens and after every goal. Tag those so heatmaps/spot-maps can drop them
    # — they always land at dead-centre and otherwise dominate the picture.
    kickoff_pending = True

    for r in rows:
        t = max(0.0, r["received_at"] - started_at)
        try:
            d = _json.loads(r["payload"])
        except Exception:
            continue
        e = r["event"]

        if e == "BallHit":
            # Skip replays so the slow-mo cinematic of each goal doesn't
            # duplicate every ball touch into the playback track.
            if in_replay(t):
                continue
            ball = d.get("Ball") or {}
            loc = ball.get("Location") or {}
            x = float(loc.get("X") or 0)
            y = float(loc.get("Y") or 0)
            z = float(loc.get("Z") or 0)
            sx, sy = project(x, y)
            # Pseudo-3D elevation: lift the touch above the pitch baseline by
            # an amount proportional to ball height. Cap at ~70 pixels so even
            # ceiling touches stay inside the viewbox.
            z_lift = min(70.0, max(0.0, z) * 0.04)
            esy = round(sy - z_lift, 1)
            players_arr = d.get("Players") or []
            who = players_arr[0] if players_arr else {}
            is_kickoff = kickoff_pending
            kickoff_pending = False
            ball_track.append({
                "t": round(t, 2),
                "x": round(x, 0), "y": round(y, 0), "z": round(z, 0),
                "sx": sx, "sy": sy, "esy": esy,
                "lift": round(z_lift, 1),
                "aerial": z > 200,
                "player": who.get("Name") or "",
                "team": who.get("TeamNum") if who.get("TeamNum") in (0, 1) else None,
                "speed": round(float(ball.get("PostHitSpeed") or 0), 0),
                "is_kickoff": is_kickoff,
            })

        elif e == "GoalScored":
            scorer = d.get("Scorer") or {}
            name = scorer.get("Name") or ""
            if not name:
                continue  # duplicate envelope after the real goal
            # Play restarts with a kickoff after every goal.
            kickoff_pending = True
            team = scorer.get("TeamNum")
            assister = (d.get("Assister") or {}).get("Name") or ""
            imp = d.get("ImpactLocation") or {}
            ix, iy = float(imp.get("X") or 0), float(imp.get("Y") or 0)
            isx, isy = project(ix, iy)
            speed = round(float(d.get("GoalSpeed") or 0), 0)
            goals_raw.append({
                "t": round(t, 2),
                "scorer": name,
                "team": team,
                "speed": speed,
                "assister": assister,
                "isx": isx, "isy": isy,
            })
            events.append({
                "t": round(t, 2), "kind": "goal", "team": team,
                "label": "Goal", "player": name,
                "assister": assister, "speed": speed,
                "sx": isx, "sy": isy,
                "event": "Goal",
            })

        elif e == "CrossbarHit":
            blt = (d.get("BallLastTouch") or {}).get("Player") or {}
            loc = d.get("BallLocation") or {}
            sx, sy = project(float(loc.get("X") or 0), float(loc.get("Y") or 0))
            events.append({
                "t": round(t, 2), "kind": "crossbar",
                "team": blt.get("TeamNum"), "label": "Crossbar",
                "player": blt.get("Name") or "",
                "sx": sx, "sy": sy,
                "speed": round(float(d.get("BallSpeed") or 0), 0),
                "event": "CrossbarHit",
            })

        elif e == "StatfeedEvent":
            ev_name = d.get("EventName") or ""
            kind_label = statfeed_kinds.get(ev_name)
            if not kind_label:
                continue
            kind, label = kind_label
            main = d.get("MainTarget") or {}
            sec = d.get("SecondaryTarget") or {}
            events.append({
                "t": round(t, 2), "kind": kind, "label": label,
                "player": main.get("Name") or "",
                "team": main.get("TeamNum"),
                "secondary": sec.get("Name") or "",
                "event": ev_name,
            })

        elif e == "ClockUpdatedSeconds":
            if in_replay(t):
                continue  # game clock freezes during replays anyway
            ts = d.get("TimeSeconds")
            if ts is None:
                continue
            ts = float(ts)
            if d.get("bOvertime"):
                ts = -ts  # negative so we know it's OT
            clock_map.append([round(t, 2), ts])

    # Build pre-goal chains: last 3 BallHits before each goal.
    for g in goals_raw:
        chain = []
        for bh in reversed(ball_track):
            if bh["t"] >= g["t"]:
                continue
            chain.append({
                "sx": bh["sx"], "sy": bh["sy"],
                "player": bh["player"], "team": bh["team"],
                "t": bh["t"],
            })
            if len(chain) >= 3:
                break
        chain.reverse()
        # End the chain at the impact location so the visual shows the ball
        # going into the net.
        chain.append({
            "sx": g["isx"], "sy": g["isy"],
            "player": g["scorer"], "team": g["team"],
            "t": g["t"], "impact": True,
        })
        g["chain"] = chain

    # Correlate each BallHit with the nearest event by the same player within
    # a small window (1.5s after the hit) so we can decorate touches with the
    # RL point icon that matches their result. Goals are nailed to the *last*
    # BallHit before the GoalScored timestamp regardless of player.
    EVENT_WINDOW = 1.5  # seconds
    GOAL_LOOKBACK = 3.0
    # Index events by time for cheap forward search.
    events_sorted = sorted([
        (ev["t"], ev.get("event") or "", ev.get("player") or "", ev["kind"])
        for ev in events
    ], key=lambda e: e[0])
    # Tag each BallHit. For non-goal events we want the BallHit immediately
    # *before* the event by the same player.
    bh_by_t = sorted(range(len(ball_track)), key=lambda i: ball_track[i]["t"])
    for ev in events:
        ev_t = ev["t"]
        ev_kind = ev["kind"]
        ev_event = ev.get("event") or ""
        ev_player = ev.get("player") or ""
        if not ev_event:
            continue
        # Goal: bind to the last BallHit before ev_t (any player).
        if ev_kind == "goal":
            best = None
            for idx in reversed(bh_by_t):
                bh = ball_track[idx]
                if bh["t"] > ev_t:
                    continue
                if ev_t - bh["t"] > GOAL_LOOKBACK:
                    break
                best = idx
                break
            if best is not None and not ball_track[best].get("event"):
                ball_track[best]["event"] = ev_event
            continue
        # Other events: same player, hit happens within EVENT_WINDOW before
        # the event was emitted.
        best = None
        for idx in reversed(bh_by_t):
            bh = ball_track[idx]
            if bh["t"] > ev_t:
                continue
            if ev_t - bh["t"] > EVENT_WINDOW:
                break
            if bh["player"] == ev_player:
                best = idx
                break
        if best is not None and not ball_track[best].get("event"):
            ball_track[best]["event"] = ev_event

    # Attach the resolved icon URL to each touch so the JS can render an
    # <image> without re-computing the mapping. Done after correlation since
    # we only have `event` on touches that matched.
    from urllib.parse import quote as _q
    for bh in ball_track:
        ev_key = bh.get("event")
        if ev_key and ev_key in _RL_ICON_FOR_EVENT:
            bh["icon"] = f"/static/icons/{_q(_RL_ICON_FOR_EVENT[ev_key])}"
    for ev in events:
        ev_key = ev.get("event")
        if ev_key and ev_key in _RL_ICON_FOR_EVENT:
            ev["icon"] = f"/static/icons/{_q(_RL_ICON_FOR_EVENT[ev_key])}"

    # If the duration we were given is junk (rare), fall back to the last
    # event time we recorded.
    if not duration_seconds or duration_seconds < 10:
        duration_seconds = max(
            (ball_track[-1]["t"] if ball_track else 0),
            (events[-1]["t"]     if events     else 0),
            10.0,
        )

    return {
        "meta": {"t0_name": t0_name, "t1_name": t1_name},
        "svg": {
            "vb_w": vb_w, "vb_h": vb_h,
            "pitch_w": pitch_w, "pitch_h": pitch_h,
            "pad_x": pad_x, "pad_y": pad_y,
        },
        "duration": round(duration_seconds, 1),
        "ball_track": ball_track,
        "events": events,
        "goals": goals_raw,
        "clock_map": clock_map,
    }


def _match_compare_html(players, viewer_pid, viewer_name,
                        t0_name: str, t1_name: str, playback: dict | None = None) -> str:
    """Us-vs-them team comparison (touch share, shots, saves, demos, posts) +
    per-player touch%, ported from the liked Discord embed. Viewer-relative
    US/THEM when a viewer resolves, else a neutral Blue-vs-Orange framing.
    Every stat here is reported for ALL players (unlike movement/boost)."""
    if not players:
        return ""
    # Neutral all-players view: always Blue (team 0) vs Orange (team 1), never
    # "us/them" — that would assume the configured owner is in this match.
    us, them, viewer_relative = 0, 1, False
    up = [p for p in players if p["team_num"] == us]
    tp = [p for p in players if p["team_num"] == them]

    def tot(pl, k):
        return sum((p[k] or 0) for p in pl)

    us_touch, them_touch = tot(up, "touches"), tot(tp, "touches")
    total = us_touch + them_touch
    us_poss = round(us_touch / total * 100) if total else 0
    rows = [
        ("Touch share", f"{us_poss}%", f"{(100 - us_poss) if total else 0}%"),
        ("Shots", tot(up, "shots"), tot(tp, "shots")),
        ("Saves", tot(up, "saves"), tot(tp, "saves")),
        ("Demos", tot(up, "demos"), tot(tp, "demos")),
    ]
    if playback:
        evs = playback.get("events") or []
        pu = sum(1 for e in evs if e.get("kind") == "crossbar" and e.get("team") == us)
        pt = sum(1 for e in evs if e.get("kind") == "crossbar" and e.get("team") == them)
        if pu or pt:
            rows.append(("Posts", pu, pt))

    us_cls = "team-blue" if us == 0 else "team-orng"
    them_cls = "team-blue" if them == 0 else "team-orng"
    us_label = "Us" if viewer_relative else html.escape(
        (t0_name if us == 0 else t1_name) or ("Blue" if us == 0 else "Orange"))
    them_label = "Them" if viewer_relative else html.escape(
        (t0_name if them == 0 else t1_name) or ("Blue" if them == 0 else "Orange"))
    body_rows = "".join(
        f'<tr><td class="cmp-label">{lbl}</td>'
        f'<td class="num tnum">{u}</td><td class="num tnum">{t}</td></tr>'
        for lbl, u, t in rows
    )
    pp = ""
    if total:
        def share(pl):
            return " ".join(
                f'<span class="cmp-pp">{html.escape((p["name"] or "")[:12])} '
                f'{round((p["touches"] or 0) / total * 100)}%</span>'
                for p in sorted(pl, key=lambda p: -(p["touches"] or 0))
            )
        pp = (f'<div class="cmp-touch"><div class="cmp-pp-title">Touch %</div>'
              f'<div class="cmp-pp-row {us_cls}">{share(up)}</div>'
              f'<div class="cmp-pp-row {them_cls}">{share(tp)}</div></div>')
    return f"""
      <section id="compare" class="card mn-target">
        <div class="section-title"><span>Team comparison</span></div>
        <table class="cmp-table">
          <thead><tr><th></th>
            <th class="num {us_cls}">{us_label}</th>
            <th class="num {them_cls}">{them_label}</th></tr></thead>
          <tbody>{body_rows}</tbody>
        </table>
        {pp}
      </section>
    """


def _goal_map_html(playback: dict) -> str:
    """Standalone goal-location map: where each goal was struck from (the
    scorer's last touch), with the build-up traced back through the chain.
    Reuses the shared pitch primitives (.pb-field/.pb-midline). This used to
    live inside the now-removed ball-replay widget's 'Goals' mode."""
    goals = playback.get("goals") or []
    if not goals:
        return ""
    svg = playback["svg"]
    vb_w, vb_h = svg["vb_w"], svg["vb_h"]
    pad_x, pad_y = svg["pad_x"], svg["pad_y"]
    pitch_w, pitch_h = svg["pitch_w"], svg["pitch_h"]
    layers = []
    for gi, g in enumerate(goals):
        chain = g.get("chain") or []
        if not chain:
            continue
        color = "var(--team-blue)" if g.get("team") == 0 else "var(--team-orng)"
        shot = chain[-2] if len(chain) >= 2 else chain[-1]  # scorer's last touch
        tip = f'Goal {gi + 1}: {g.get("scorer") or "?"}'
        if g.get("assister"):
            tip += f'  (assist: {g["assister"]})'
        if g.get("speed"):
            tip += f'  ·  {g["speed"]} km/h'
        pts = " ".join(f'{p["sx"]:.1f},{p["sy"]:.1f}' for p in chain)
        layers.append(
            f'<g class="gm-goal"><title>{html.escape(tip)}</title>'
            f'<polyline points="{pts}" fill="none" stroke="{color}" '
            f'stroke-width="2" stroke-dasharray="3 3" stroke-linecap="round" '
            f'stroke-linejoin="round" stroke-opacity="0.55" />'
            f'<circle cx="{shot["sx"]:.1f}" cy="{shot["sy"]:.1f}" r="7.5" '
            f'fill="{color}" stroke="var(--card)" stroke-width="2" />'
            f'<text x="{shot["sx"]:.1f}" y="{shot["sy"]:.1f}" dy="3.4" '
            f'text-anchor="middle" fill="var(--card)" font-size="9" font-weight="800" '
            f'style="font-family: JetBrains Mono, monospace">{gi + 1}</text>'
            f'</g>'
        )
    if not layers:
        return ""
    pitch = (
        f'<svg viewBox="0 0 {vb_w} {vb_h}" class="hm-pitch" xmlns="http://www.w3.org/2000/svg">'
        f'<rect class="pb-field" x="{pad_x:.1f}" y="{pad_y:.1f}" width="{pitch_w}" height="{pitch_h}" />'
        f'<line class="pb-midline" x1="{vb_w/2:.1f}" y1="{pad_y:.1f}" '
        f'x2="{vb_w/2:.1f}" y2="{pad_y + pitch_h:.1f}" />'
        f'<circle class="pb-midcircle" cx="{vb_w/2:.1f}" cy="{vb_h/2:.1f}" r="48" fill="none" />'
        f'{"".join(layers)}'
        f'</svg>'
    )
    return f"""
      <section id="goalmap" class="card">
        <div class="section-title">
          <span>Goal map</span>
          <span class="dim" style="text-transform:none;letter-spacing:0">
            Where each goal was struck from, traced back through the build-up.
          </span>
        </div>
        <div style="max-width:560px;margin:8px auto 0">{pitch}</div>
      </section>
    """


def _match_events_html(playback: dict) -> str:
    """Standalone, server-rendered match timeline -- the 'history of events' the
    owner likes, decoupled from the (removed) ball-replay engine: no JS, no
    seek. Goals carry a running score; every name is escaped (attacker-
    controllable). Reuses the shared .pb-event* classes (also used by the live
    feed)."""
    events = playback.get("events") or []
    # Uploaded matches collapse every event timestamp to one value, so a per-event
    # M:SS would just repeat (e.g. "8:29" on every row). Detect that and show a
    # play ordinal (#1, #2, …) instead — honest, since the list is already ordered.
    has_timing = len({round(ev.get("t", 0), 1) for ev in events}) > 2
    rows = []
    b = o = 0  # running score, blue-orange
    for i, ev in enumerate(events):
        t = ev.get("t", 0)
        ts = (f"{int(t // 60)}:{int(t % 60):02d}" if has_timing else f"#{i + 1}")
        team = ev.get("team")
        team_cls = "team-blue" if team == 0 else "team-orng" if team == 1 else ""
        kind = ev.get("kind", "")
        label = ev.get("label", "")
        player = ev.get("player") or ""
        body = f"<b>{html.escape(player)}</b>" if player else ""
        if kind == "goal":
            if team == 0:
                b += 1
            elif team == 1:
                o += 1
            meta = []
            if ev.get("speed"):
                meta.append(f"{ev['speed']:.0f} km/h")
            if ev.get("assister"):
                meta.append(f"assist: <b>{html.escape(ev['assister'])}</b>")
            meta_html = (" <span class='pb-event-meta'>" + " &middot; ".join(meta)
                         + "</span>") if meta else ""
            body = (f"<span class='pb-event-score tnum'>{b}&ndash;{o}</span> "
                    f"{body}{meta_html}")
        elif kind == "demo" and ev.get("secondary"):
            body += f" demolished <b>{html.escape(ev['secondary'])}</b>"
        icon_html = _rl_icon_html(ev.get("event") or "", size=20, alt=label)
        rows.append(
            f'<li class="pb-event pb-event-{kind} {team_cls}">'
            f'<span class="pb-event-time tnum">{ts}</span>'
            f'<span class="pb-event-tag">{icon_html}{label}</span>'
            f'<span class="pb-event-body">{body}</span>'
            f'</li>'
        )
    if not rows:
        rows = ['<li class="pb-event-empty">No events recorded for this match.</li>']
    return f"""
      <section id="timeline" class="card">
        <div class="section-title">
          <span>Match timeline</span>
          <span class="dim" style="text-transform:none;letter-spacing:0">
            Every goal, save, demo and crossbar hit, in order.
          </span>
        </div>
        <ol class="pb-events pb-events-full">{"".join(rows)}</ol>
      </section>
    """


_LIVE_JS = r"""
(function() {
  var root = document.getElementById('live-root');
  if (!root) return;
  var selfName = root.dataset.selfName || '';

  var pip = document.getElementById('live-pip');
  var pipText = document.getElementById('live-pip-text');
  var meta = document.getElementById('live-meta');
  var hero = document.getElementById('live-hero');
  var idle = document.getElementById('live-idle');
  var rostersEl = document.getElementById('live-rosters');
  var eventsCard = document.getElementById('live-events-card');
  var eventsEl = document.getElementById('live-events');

  var t0Name = document.getElementById('live-t0-name');
  var t1Name = document.getElementById('live-t1-name');
  var t0Score = document.getElementById('live-t0-score');
  var t1Score = document.getElementById('live-t1-score');
  var clockEl = document.getElementById('live-clock');
  var periodEl = document.getElementById('live-period');
  var otPill = document.getElementById('live-ot-pill');
  var arenaEl = document.getElementById('live-arena');
  var ballSpeedEl = document.getElementById('live-ball-speed');

  var inMatch = false;
  var lastTick = null;
  var eventCount = 0;
  var maxEvents = 50;

  function setPipState(state, text) {
    pip.className = 'live-pip ' + state;
    pipText.textContent = text;
  }
  function fmtClock(secs, isOT) {
    if (secs == null) return '--';
    secs = Math.max(0, Math.floor(secs));
    var mm = Math.floor(secs / 60), ss = secs % 60;
    return mm + ':' + (ss < 10 ? '0' : '') + ss + (isOT ? ' OT' : '');
  }
  function arenaNice(s) {
    if (!s) return '';
    return s.replace(/_p$/, '').replace(/_/g, ' ')
      .replace(/\b\w/g, function(c) { return c.toUpperCase(); });
  }
  function escapeHtml(s) {
    if (!s) return '';
    return s.replace(/[&<>"']/g, function(c) {
      return { '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c];
    });
  }

  function buildRosterCard(teamNum, name, players, score) {
    var color = teamNum === 0 ? 'team-blue' : 'team-orng';
    var totals = { goals: 0, assists: 0, saves: 0, shots: 0, demos: 0, score: 0, touches: 0 };
    var rows = players.map(function(p) {
      totals.goals   += p.goals   || 0;
      totals.assists += p.assists || 0;
      totals.saves   += p.saves   || 0;
      totals.shots   += p.shots   || 0;
      totals.demos   += p.demos   || 0;
      totals.score   += p.score   || 0;
      totals.touches += p.touches || 0;
      var isSelf = selfName && p.name === selfName;
      var bot = p.is_bot ? ' <span class="chip bot">BOT</span>' : '';
      var you = isSelf ? ' <span class="you-marker">YOU</span>' : '';
      var boostPct = p.boost == null ? '--' : Math.round(p.boost);
      var boostBarWidth = Math.max(0, Math.min(100, p.boost || 0));
      var speed = p.speed == null ? '' : Math.round(p.speed);
      var posTag = p.has_car
        ? (p.on_wall ? 'wall' : p.on_ground ? 'ground' : 'air')
        : 'no car';
      var sup = p.supersonic ? '<span class="live-supersonic">SUPERSONIC</span>' : '';
      var nameHtml = p.is_bot
        ? '<span class="player-link" style="cursor:default;color:var(--text-faint)">' + escapeHtml(p.name) + '</span>'
        : '<a class="player-link' + (isSelf ? ' self' : '') + '" href="/player/' + encodeURIComponent(p.name) + '">' + escapeHtml(p.name) + '</a>';
      return ''
        + '<tr>'
        +   '<td class="player-cell">' + nameHtml + you + bot + sup + '</td>'
        +   '<td class="num tnum"><b>' + (p.score || 0) + '</b></td>'
        +   '<td class="num tnum">' + (p.goals || 0) + '</td>'
        +   '<td class="num tnum">' + (p.assists || 0) + '</td>'
        +   '<td class="num tnum">' + (p.saves || 0) + '</td>'
        +   '<td class="num tnum">' + (p.shots || 0) + '</td>'
        +   '<td class="num tnum">' + (p.demos || 0) + '</td>'
        +   '<td class="num tnum">' + (p.touches || 0) + '</td>'
        +   '<td class="live-boost-cell">'
        +     '<div class="live-boost-bar"><div class="live-boost-fill" style="width:' + boostBarWidth + '%"></div></div>'
        +     '<span class="tnum">' + boostPct + '</span>'
        +   '</td>'
        +   '<td class="num tnum dim">' + speed + '</td>'
        +   '<td class="dim live-pos-cell">' + posTag + '</td>'
        + '</tr>';
    }).join('');
    return ''
      + '<div class="roster-card ' + color + '">'
      +   '<div class="roster-head">'
      +     '<div class="roster-team">'
      +       '<span class="roster-stripe"></span>'
      +       '<span class="team-name-truncate" title="' + escapeHtml(name) + '">' + escapeHtml(name) + '</span>'
      +     '</div>'
      +     '<span class="roster-score tnum">' + (score == null ? 0 : score) + '</span>'
      +   '</div>'
      +   '<table>'
      +     '<thead><tr>'
      +       '<th>Player</th>'
      +       '<th class="num">Score</th>'
      +       '<th class="num" title="Goals">G</th>'
      +       '<th class="num" title="Assists">A</th>'
      +       '<th class="num" title="Saves">Sv</th>'
      +       '<th class="num" title="Shots">Sh</th>'
      +       '<th class="num" title="Demos">D</th>'
      +       '<th class="num" title="Touches">T</th>'
      +       '<th>Boost</th>'
      +       '<th class="num">Speed</th>'
      +       '<th>Position</th>'
      +     '</tr></thead>'
      +     '<tbody>' + rows
      +       '<tr class="total-row">'
      +         '<td>Team total</td>'
      +         '<td class="num tnum">' + totals.score + '</td>'
      +         '<td class="num tnum">' + totals.goals + '</td>'
      +         '<td class="num tnum">' + totals.assists + '</td>'
      +         '<td class="num tnum">' + totals.saves + '</td>'
      +         '<td class="num tnum">' + totals.shots + '</td>'
      +         '<td class="num tnum">' + totals.demos + '</td>'
      +         '<td class="num tnum">' + totals.touches + '</td>'
      +         '<td></td><td></td><td></td>'
      +       '</tr>'
      +     '</tbody>'
      +   '</table>'
      + '</div>';
  }

  function renderTick(data) {
    inMatch = true;
    idle.style.display = 'none';
    hero.style.display = '';
    rostersEl.style.display = '';
    setPipState('on', 'LIVE');

    t0Name.textContent = data.team0_name || 'Blue';
    t0Name.setAttribute('title', data.team0_name || 'Blue');
    t1Name.textContent = data.team1_name || 'Orange';
    t1Name.setAttribute('title', data.team1_name || 'Orange');
    t0Score.textContent = data.team0_score || 0;
    t1Score.textContent = data.team1_score || 0;
    clockEl.textContent = fmtClock(data.time_seconds, false);
    if (data.is_overtime) {
      periodEl.textContent = 'Overtime';
      otPill.style.display = '';
      otPill.textContent = 'OT';
    } else {
      periodEl.textContent = 'Regulation';
      otPill.style.display = 'none';
    }
    arenaEl.textContent = data.arena_nice || arenaNice(data.arena);
    ballSpeedEl.textContent = Math.round(data.ball_speed || 0);

    var players = data.players || [];
    var blue = players.filter(function(p) { return p.team_num === 0; })
                      .sort(function(a, b) { return (b.score || 0) - (a.score || 0); });
    var orng = players.filter(function(p) { return p.team_num === 1; })
                      .sort(function(a, b) { return (b.score || 0) - (a.score || 0); });

    rostersEl.innerHTML = ''
      + buildRosterCard(0, data.team0_name || 'Blue', blue, data.team0_score)
      + buildRosterCard(1, data.team1_name || 'Orange', orng, data.team1_score);

    lastTick = data;
  }

  function addEvent(label, body, kindCls, iconKey) {
    if (eventCount === 0) eventsCard.style.display = '';
    var ts = new Date();
    var hh = ts.getHours(), mm = ts.getMinutes();
    var when = (hh % 12 || 12) + ':' + (mm < 10 ? '0' : '') + mm + (hh >= 12 ? 'pm' : 'am');
    var iconMap = {
      'goal':       '/static/icons/Goal_points_icon.png',
      'crossbar':   '/static/icons/Shot_on_Goal_points_icon.png',
      'aerial':     '/static/icons/Aerial_Goal_points_icon.png',
      'epic-save':  '/static/icons/Epic_Save_points_icon.png',
      'demo':       '/static/icons/Demolition_points_icon.png',
    };
    var icon = iconMap[iconKey || kindCls] || '';
    var iconHtml = icon ? '<img class="rl-icon" src="' + icon + '" width="16" height="16" alt="" />' : '';
    var li = document.createElement('li');
    li.className = 'pb-event pb-event-' + kindCls;
    li.innerHTML = ''
      + '<span class="pb-event-time tnum">' + when + '</span>'
      + '<span class="pb-event-tag">' + iconHtml + label + '</span>'
      + '<span class="pb-event-body">' + body + '</span>';
    eventsEl.insertBefore(li, eventsEl.firstChild);
    eventCount++;
    while (eventsEl.children.length > maxEvents) {
      eventsEl.removeChild(eventsEl.lastChild);
    }
  }

  function handleGoal(raw) {
    var scorer = (raw.Scorer || {}).Name || 'Own goal';
    var team = (raw.Scorer || {}).TeamNum;
    var assister = (raw.Assister || {}).Name || '';
    var speed = Math.round(raw.GoalSpeed || 0);
    var body = '<b>' + escapeHtml(scorer) + '</b> scored'
             + ' <span class="pb-event-meta">' + speed + ' kph</span>'
             + (assister ? ' <span class="pb-event-meta">assist: <b>' + escapeHtml(assister) + '</b></span>' : '');
    addEvent('Goal', body, 'goal ' + (team === 0 ? 'team-blue' : 'team-orng'), 'goal');
  }
  function handleCrossbar(raw) {
    var blt = ((raw.BallLastTouch || {}).Player || {});
    var who = blt.Name || 'Unknown';
    addEvent('Crossbar', '<b>' + escapeHtml(who) + '</b> hit the crossbar', 'crossbar', 'crossbar');
  }
  function handleMatchStart(data) {
    eventCount = 0;
    eventsEl.innerHTML = '';
    eventsCard.style.display = 'none';
    inMatch = true;
    idle.style.display = 'none';
    setPipState('on', 'LIVE');
    meta.textContent = 'New match starting on ' + (data.arena_nice || arenaNice(data.arena || ''));
    // Pre-match scouting card: fetch each player's recent form + career
    // averages from local DB and render above the hero.
    var matchPlayers = (data.players || []).filter(function(p) { return p && p.name; });
    if (!matchPlayers.length) return;
    var qs = matchPlayers.map(function(p) { return 'names=' + encodeURIComponent(p.name); }).join('&');
    fetch('/api/player-form?' + qs).then(function(r) { return r.json(); }).then(function(j) {
      renderScoutingCard(matchPlayers, j.players || {});
    }).catch(function() {});
  }
  function renderScoutingCard(matchPlayers, formMap) {
    var existing = document.getElementById('live-scouting-card');
    if (existing) existing.remove();
    var card = document.createElement('div');
    card.id = 'live-scouting-card';
    card.className = 'card scouting-card';
    var headerHtml = '<div class="section-title">'
      + '<span>Pre-match scouting</span>'
      + '<span class="dim" style="text-transform:none;letter-spacing:0">'
      + 'Last 10 form + career averages. From local DB only.'
      + '</span></div>';
    var teamRows = [0, 1].map(function(tn) {
      var teamPlayers = matchPlayers.filter(function(p) { return p.team_num === tn; });
      if (!teamPlayers.length) return '';
      var rows = teamPlayers.map(function(p) {
        var f = formMap[p.name] || {};
        var dots = (f.form || '').split('').map(function(c) {
          return '<span class="form-dot form-' + (c === 'W' ? 'w' : 'l') + '"></span>';
        }).join('');
        var n = f.matches || 0;
        var wr = (f.win_pct == null) ? '--' : f.win_pct.toFixed(0) + '%';
        var ag = (f.avg_goals == null) ? '--' : f.avg_goals;
        var asv = (f.avg_saves == null) ? '--' : f.avg_saves;
        var bot = p.is_bot ? ' <span class="chip bot">BOT</span>' : '';
        return ''
          + '<div class="scout-row">'
          +   '<div class="scout-name">' + escapeHtml(p.name) + bot + '</div>'
          +   '<div class="scout-form">' + (dots || '<span class="dim">no history</span>') + '</div>'
          +   '<div class="scout-meta dim">'
          +     '<b class="tnum">' + n + '</b> matches'
          +     ' &middot; <b class="tnum">' + wr + '</b> WR'
          +     ' &middot; <b class="tnum">' + ag + '</b> g/match'
          +     ' &middot; <b class="tnum">' + asv + '</b> sv/match'
          +   '</div>'
          + '</div>';
      }).join('');
      var teamCls = tn === 0 ? 'team-blue' : 'team-orng';
      return '<div class="scout-team ' + teamCls + '">' + rows + '</div>';
    }).join('');
    card.innerHTML = headerHtml + '<div class="scout-grid">' + teamRows + '</div>';
    var hero = document.getElementById('live-hero');
    if (hero && hero.parentNode) hero.parentNode.insertBefore(card, hero);
  }
  function handleMatchEnd(data) {
    inMatch = false;
    setPipState('off', 'idle');
    var t0 = data.team0_name || 'Blue', t1 = data.team1_name || 'Orange';
    var s0 = data.team0_score || 0, s1 = data.team1_score || 0;
    var who = s0 > s1 ? t0 : s1 > s0 ? t1 : 'Tie';
    var matchId = data.match_id || '';
    var detailLink = matchId
      ? ' <a href="/match/' + encodeURIComponent(matchId) + '" class="player-link">view detail</a>'
      : '';
    meta.innerHTML = 'Final: <b>' + escapeHtml(t0) + ' ' + s0 + '</b> - <b>'
                   + s1 + ' ' + escapeHtml(t1) + '</b>'
                   + ' &middot; winner: <b>' + escapeHtml(who) + '</b>' + detailLink;
  }

  // ---- Live page placeholder mode -------------------------------------
  // Show the UI scaffolded with mock players when no live tick yet.
  // Params: ?placeholder=N (1..4 per side) &spectator=1 (show both teams)
  var seenLiveTick = false;
  var livePlaceholderTimer = null;
  function urlParamLive(k) {
    try { return new URLSearchParams(location.search).get(k); } catch (e) { return null; }
  }
  function livePlaceholderTick() {
    var n = parseInt(urlParamLive('placeholder') || '3', 10);
    if (!(n >= 1 && n <= 4)) n = 3;
    var spectator = urlParamLive('spectator') === '1';
    function mkPlayer(i, team) {
      return {
        name: 'Player ' + (i + 1),
        primary_id: 'pl|' + team + '|' + i,
        team_num: team,
        is_bot: false,
        goals: Math.floor(Math.random() * 4),
        assists: Math.floor(Math.random() * 3),
        saves: Math.floor(Math.random() * 4),
        shots: Math.floor(Math.random() * 8),
        demos: Math.floor(Math.random() * 3),
        score: 100 + Math.floor(Math.random() * 800),
        touches: 10 + Math.floor(Math.random() * 60),
        car_touches: 0,
        boost: 20 + Math.floor(Math.random() * 80),
        speed: Math.floor(Math.random() * 2200),
        on_ground: Math.random() < 0.7,
        on_wall: Math.random() < 0.1,
        has_car: true,
        boosting: Math.random() < 0.3,
        supersonic: Math.random() < 0.08,
      };
    }
    var players = [];
    for (var i = 0; i < n; i++) players.push(mkPlayer(i, 0));
    for (var j = 0; j < n; j++) players.push(mkPlayer(j, 1));
    return {
      team0_name: 'Blue',
      team1_name: 'Orange',
      team0_score: Math.floor(Math.random() * 5),
      team1_score: Math.floor(Math.random() * 5),
      time_seconds: 240 + Math.floor(Math.random() * 60),
      is_overtime: false,
      arena: 'placeholder_p',
      ball_speed: Math.floor(Math.random() * 1800),
      players: players,
    };
  }
  function startLivePlaceholder() {
    renderTick(livePlaceholderTick());
    setPipState('off', 'placeholder');
    meta.textContent = 'Preview - waiting for live match. URL: ?placeholder=N&spectator=1';
    livePlaceholderTimer = setInterval(function() {
      if (seenLiveTick) { clearInterval(livePlaceholderTimer); return; }
      renderTick(livePlaceholderTick());
      setPipState('off', 'placeholder');
      meta.textContent = 'Preview - waiting for live match. URL: ?placeholder=N&spectator=1';
    }, 1500);
  }

  var ws = null;
  var backoff = 500;
  function connect() {
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    setPipState('off', 'connecting...');
    try {
      ws = new WebSocket(proto + '//' + location.host + '/ws');
    } catch (e) {
      setPipState('off', 'socket error');
      setTimeout(connect, backoff = Math.min(backoff * 2, 8000));
      return;
    }
    ws.onopen = function() {
      backoff = 500;
      setPipState('off', 'connected · waiting for match');
    };
    ws.onmessage = function(evt) {
      var msg = null;
      try { msg = JSON.parse(evt.data); } catch (e) { return; }
      if (msg.type === 'tick') {
        seenLiveTick = true;
        if (livePlaceholderTimer) { clearInterval(livePlaceholderTimer); livePlaceholderTimer = null; }
        renderTick(msg.data);
      }
      else if (msg.type === 'match_start') handleMatchStart(msg.data);
      else if (msg.type === 'match_end')   handleMatchEnd(msg.data);
      else if (msg.type === 'goal')        handleGoal(msg.data);
      else if (msg.type === 'crossbar')    handleCrossbar(msg.data);
    };
    ws.onclose = function() {
      setPipState('off', 'disconnected · retrying...');
      setTimeout(connect, backoff = Math.min(backoff * 2, 8000));
    };
    ws.onerror = function() { /* onclose handles reconnect */ };
  }
  startLivePlaceholder();
  connect();
})();
"""


_BOOST_JS = r"""
(function() {
  var root = document.getElementById('boost-root');
  if (!root) return;
  var selfName = root.dataset.selfName || '';

  var pip = document.getElementById('boost-pip');
  var pipText = document.getElementById('boost-pip-text');
  var meta = document.getElementById('boost-meta');
  var stage = document.getElementById('boost-stage');
  var idle = document.getElementById('boost-idle');
  var bodyEl = document.getElementById('boost-myteam-body');

  function setPipState(state, text) {
    if (!pip) return;
    pip.className = 'live-pip ' + state;
    pipText.textContent = text;
  }
  function escapeHtml(s) {
    if (!s) return '';
    return s.replace(/[&<>"']/g, function(c) {
      return { '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c];
    });
  }
  function cardKey(p) { return (p.primary_id || p.name || '') + '|' + p.team_num; }

  function buildCard(p) {
    // Slot = header (label OUTSIDE the box) + the box itself.
    var el = document.createElement('div');
    el.className = 'boost-hud-slot';
    el.dataset.key = cardKey(p);
    el.innerHTML = ''
      + '<div class="boost-hud-header">'
      +   '<span class="boost-hud-name"></span>'
      +   '<span class="boost-hud-flags">'
      +     '<span class="boost-hud-chip boosting">BOOSTING</span>'
      +     '<span class="boost-hud-chip super">SUPERSONIC</span>'
      +   '</span>'
      + '</div>'
      + '<div class="boost-hud-card">'
      +   '<div class="boost-hud-meter">'
      +     '<div class="boost-hud-meter-fill"></div>'
      +   '</div>'
      +   '<div class="boost-hud-state-icon" aria-hidden="true">'
      +     '<img class="ic-aerial" src="/static/icons/Aerial_Hit_points_icon.png" alt="" />'
      +     '<img class="ic-wall"   src="/static/icons/Bicycle_Hit_points_icon.png" alt="" />'
      +   '</div>'
      +   '<div class="boost-hud-pct">'
      +     '<span class="boost-hud-num tnum">0</span>'
      +     '<span class="boost-hud-percent">%</span>'
      +   '</div>'
      + '</div>';
    return el;
  }

  // Color buckets: red <25, orange <60, yellow <85, green >=85.
  function meterTier(pct) {
    if (pct == null) return 'unknown';
    if (pct >= 85)   return 'full';
    if (pct >= 60)   return 'high';
    if (pct >= 25)   return 'mid';
    return 'low';
  }

  function updateCard(el, p, isSelf, teamCls) {
    // `el` is the .boost-hud-slot. Inner `.boost-hud-card` holds the bar +
    // state classes so existing CSS selectors keep working.
    var card = el.querySelector('.boost-hud-card');
    var unknown = p.boost == null;
    var pct = Math.max(0, Math.min(100, unknown ? 0 : p.boost));
    var name = p.name || '?';
    var nameEl = el.querySelector('.boost-hud-name');
    var numEl  = el.querySelector('.boost-hud-num');
    var fill   = el.querySelector('.boost-hud-meter-fill');

    nameEl.textContent = name + (isSelf ? '  (YOU)' : '');
    nameEl.title = name;
    numEl.textContent = unknown ? '--' : Math.round(pct);
    fill.style.width = (unknown ? 0 : pct) + '%';

    [el, card].forEach(function(node) {
      node.classList.remove('team-blue', 'team-orng');
      if (teamCls) node.classList.add(teamCls);
      node.classList.remove('tier-full', 'tier-high', 'tier-mid', 'tier-low', 'tier-unknown');
      node.classList.add('tier-' + meterTier(unknown ? null : pct));
    });

    var inAir = !!p.has_car && !p.on_ground && !p.on_wall;
    var onWall = !!p.has_car && !!p.on_wall;
    [el, card].forEach(function(node) {
      node.classList.toggle('is-self',     !!isSelf);
      node.classList.toggle('is-boosting', !!p.boosting);
      node.classList.toggle('is-super',    !!p.supersonic);
      node.classList.toggle('is-aerial',   inAir);
      node.classList.toggle('is-onwall',   onWall);
      node.classList.toggle('no-data',     unknown);
    });
  }

  function renderCards(players, teamCls) {
    var seen = {};
    var existing = {};
    Array.prototype.forEach.call(bodyEl.children, function(c) {
      existing[c.dataset.key] = c;
    });

    // Show YOU first, then by boost descending (so the user's card has the most
    // stable position on the second monitor).
    players.sort(function(a, b) {
      var aSelf = selfName && a.name === selfName ? 1 : 0;
      var bSelf = selfName && b.name === selfName ? 1 : 0;
      if (aSelf !== bSelf) return bSelf - aSelf;
      var av = a.boost == null ? -1 : a.boost;
      var bv = b.boost == null ? -1 : b.boost;
      return bv - av;
    });

    players.forEach(function(p, idx) {
      var k = cardKey(p);
      seen[k] = true;
      var el = existing[k];
      if (!el) {
        el = buildCard(p);
        bodyEl.appendChild(el);
      } else if (bodyEl.children[idx] !== el) {
        bodyEl.insertBefore(el, bodyEl.children[idx]);
      }
      updateCard(el, p, selfName && p.name === selfName, teamCls);
    });

    // BUG FIX: dataset.key lives on .boost-hud-SLOT (the outer wrapper),
    // not on .boost-hud-card. Previously this cleanup queried for cards,
    // every card had key=undefined, seen[undefined] was always false, so
    // every card got removed on every tick - leaving slots with just the
    // header text and nothing else. That's why blank boost view looked
    // empty in the corner. Query SLOTS now.
    Array.prototype.forEach.call(bodyEl.querySelectorAll('.boost-hud-slot'), function(c) {
      if (!seen[c.dataset.key]) c.remove();
    });

    bodyEl.dataset.count = String(players.length);
    // Detect both-teams (spectator) layout and switch to 2-column grid.
    var hasT0 = players.some(function(p) { return p.team_num === 0; });
    var hasT1 = players.some(function(p) { return p.team_num === 1; });
    var bothTeams = hasT0 && hasT1;
    bodyEl.dataset.teams = bothTeams ? '2' : '1';
    if (bothTeams) {
      // Assign each card to a grid column based on team
      Array.prototype.forEach.call(bodyEl.querySelectorAll('.boost-hud-slot'), function(s) {
        var card = s.querySelector('.boost-hud-card');
        var isT0 = card && card.classList.contains('team-blue');
        s.style.gridColumn = isT0 ? '1' : '2';
      });
    } else {
      Array.prototype.forEach.call(bodyEl.querySelectorAll('.boost-hud-slot'), function(s) {
        s.style.gridColumn = '';
      });
    }
  }

  function renderTick(data) {
    idle.style.display = 'none';
    stage.style.display = '';
    setPipState('on', 'LIVE');

    var players = data.players || [];
    var me = selfName ? players.find(function(p) { return p.name === selfName; }) : null;
    var myTeam = me ? me.team_num : null;

    var teamPlayers = (myTeam == null)
      ? players
      : players.filter(function(p) { return p.team_num === myTeam; });

    var teamCls = myTeam === 0 ? 'team-blue' : myTeam === 1 ? 'team-orng' : '';
    renderCards(teamPlayers, teamCls);

    var known = teamPlayers.filter(function(p) { return p.boost != null; });
    if (meta) {
      meta.textContent = known.length
        ? ''
        : 'Tick received but no boost values yet for your team.';
    }
  }

  function handleMatchEnd() {
    setPipState('off', 'idle');
    if (meta) meta.textContent = 'Match ended. Waiting for the next match...';
  }

  // ---- Placeholder mode -------------------------------------------------
  // Always render a layout even when no live match. Lets the user position
  // their second monitor & verify the HUD works. URL params:
  //   ?placeholder=N  to set number of slots (1..4, default 3)
  //   &spectator=1    to show BOTH teams (split view, 3v3 = 6 cards)
  var seenRealTick = false;
  var placeholderTimer = null;

  function urlParam(k) {
    try { return new URLSearchParams(location.search).get(k); } catch (e) { return null; }
  }
  function placeholderTick() {
    var n = parseInt(urlParam('placeholder') || '3', 10);
    if (!(n >= 1 && n <= 4)) n = 3;
    var spectator = urlParam('spectator') === '1';
    var rng = Math.random;
    // Blank placeholder: empty meters, no flags. Just the skeleton.
    function mkPlayer(i, team) {
      return {
        name: 'Player ' + (i + 1),
        primary_id: 'placeholder|' + team + '|' + i,
        team_num: team,
        is_bot: false,
        boost: null,
        speed: null,
        has_car: false,
        on_ground: false,
        on_wall: false,
        boosting: false,
        supersonic: false,
      };
    }
    var players = [];
    for (var i = 0; i < n; i++) players.push(mkPlayer(i, 0));
    if (spectator) for (var j = 0; j < n; j++) players.push(mkPlayer(j, 1));
    return {
      team0_name: 'Blue', team1_name: 'Orange',
      team0_score: 0, team1_score: 0,
      time_seconds: 300, is_overtime: false,
      arena: 'placeholder_p',
      ball_speed: 0,
      players: players,
    };
  }
  function startPlaceholder() {
    // Blank skeleton: render once with empty data and leave it. When a real
    // tick arrives we swap to live data.
    renderTick(placeholderTick());
    setPipState('off', 'waiting for match');
    if (meta) meta.textContent = '';
  }

  var ws = null;
  var backoff = 500;
  function connect() {
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    setPipState('off', 'connecting...');
    try {
      ws = new WebSocket(proto + '//' + location.host + '/ws');
    } catch (e) {
      setPipState('off', 'socket error');
      setTimeout(connect, backoff = Math.min(backoff * 2, 8000));
      return;
    }
    ws.onopen = function() {
      backoff = 500;
      setPipState('off', 'connected · waiting for match');
    };
    ws.onmessage = function(evt) {
      var msg = null;
      try { msg = JSON.parse(evt.data); } catch (e) { return; }
      if (msg.type === 'tick') {
        if (!seenRealTick) {
          // First real tick: nuke any placeholder cards so they don't sit
          // alongside the real players. Cards are keyed by primary_id and
          // placeholder keys ('placeholder|...') never collide with real
          // primary_ids, so renderCards would otherwise leave them stuck.
          Array.prototype.forEach.call(
            bodyEl.querySelectorAll('.boost-hud-slot, .boost-hud-card'),
            function(el) {
              if ((el.dataset.key || '').indexOf('placeholder|') === 0) el.remove();
            }
          );
        }
        seenRealTick = true;
        if (placeholderTimer) { clearInterval(placeholderTimer); placeholderTimer = null; }
        renderTick(msg.data);
      } else if (msg.type === 'match_end') handleMatchEnd(msg.data);
    };
    ws.onclose = function() {
      setPipState('off', 'disconnected · retrying...');
      setTimeout(connect, backoff = Math.min(backoff * 2, 8000));
    };
    ws.onerror = function() { /* onclose handles reconnect */ };
  }
  startPlaceholder();
  connect();
})();
"""


_LIVE_BOOST_TOGGLE_JS = r"""
(function() {
  var btn = document.getElementById('live-view-toggle');
  var defView = document.getElementById('live-default-view');
  var boostView = document.getElementById('boost-root');
  var label = document.getElementById('live-view-toggle-label');
  if (!btn || !defView || !boostView) return;

  function setMode(mode) {
    btn.dataset.mode = mode;
    if (mode === 'boost') {
      defView.style.display = 'none';
      boostView.style.display = '';
      label.textContent = 'LIVE VIEW';
      btn.classList.add('is-active');
      document.body.classList.add('live-mode-boost');
    } else {
      defView.style.display = '';
      boostView.style.display = 'none';
      label.textContent = 'BOOST VIEW';
      btn.classList.remove('is-active');
      document.body.classList.remove('live-mode-boost');
    }
    try { localStorage.setItem('chumstats-live-mode', mode); } catch (e) {}
  }

  btn.addEventListener('click', function() {
    setMode(btn.dataset.mode === 'boost' ? 'live' : 'boost');
  });

  // Restore prior choice so refresh / new-tab stays in BOOST VIEW if that's
  // what the user had open.
  var saved = null;
  try { saved = localStorage.getItem('chumstats-live-mode'); } catch (e) {}
  if (saved === 'boost') setMode('boost');

  // "HIDE ME" / "SHOW ME" toggle — only meaningful in boost mode. We just flip
  // a body class; the .is-self card is hidden via CSS.
  var selfBtn = document.getElementById('boost-exclude-self-toggle');
  var selfLbl = document.getElementById('boost-exclude-self-label');
  if (selfBtn && selfLbl) {
    function setExcludeSelf(exclude) {
      if (exclude) {
        document.body.classList.add('boost-exclude-self');
        selfLbl.textContent = 'SHOW ME';
        selfBtn.classList.add('is-active');
      } else {
        document.body.classList.remove('boost-exclude-self');
        selfLbl.textContent = 'HIDE ME';
        selfBtn.classList.remove('is-active');
      }
      try { localStorage.setItem('chumstats-boost-exclude-self', exclude ? '1' : '0'); } catch (e) {}
    }
    selfBtn.addEventListener('click', function() {
      setExcludeSelf(!document.body.classList.contains('boost-exclude-self'));
    });
    var savedExclude = null;
    try { savedExclude = localStorage.getItem('chumstats-boost-exclude-self'); } catch (e) {}
    if (savedExclude === '1') setExcludeSelf(true);
  }
})();
"""


# Kickoff first-touches always land at dead centre (the ball spawns at 0,0), so
# ~15% of all touches pile into one cell and saturate the density map. Drop
# touches inside this centre box from touch heatmaps so they read true.
_KICKOFF_CENTER_UU = 256


def _ball_heatmap_svg(playback: dict, player_filter: str | None = None,
                     compact: bool = False, key: str = "", orient: bool = True,
                     exclude_center: bool = True) -> str:
    """Top-down ball-touch *density* heatmap on ONE pitch.

    Every BallHit is splatted as a soft point, Gaussian-blurred into a
    continuous density field, then recoloured through a thermal palette LUT
    (cool/sparse -> hot/dense) via feComponentTransfer. Density is the signal —
    where the player contacts the ball most.

    With `orient=True` (the default, for lifetime / multi-match maps) team-1
    touches are rotated 180° about the pitch centre so every touch reads
    "attacking ->". A player is on both teams across matches, so the old
    blue/orange split drew two incoherent half-maps; normalising to one
    attacking direction gives a single readable pitch. Per-match minis pass
    `orient=False` to keep the literal pitch orientation (matches the playback).

    `player_filter` restricts to one player's touches. `key` must be unique per
    heatmap on the page so the <filter>/<gradient> ids don't collide (inline
    SVG id resolution isn't reliably scoped per-svg across browsers)."""
    ball = playback.get("ball_track") or []
    if player_filter:
        ball = [bh for bh in ball if bh["player"] == player_filter]
    # Drop kickoff first-touches. Per-match tracks are sequence-tagged
    # (is_kickoff) so we drop them precisely; the lifetime aggregate has no
    # sequence, so it falls back to the dead-centre box below.
    ball = [bh for bh in ball if not bh.get("is_kickoff")]
    if exclude_center:
        R = _KICKOFF_CENTER_UU
        ball = [bh for bh in ball
                if abs(bh.get("x", 0)) > R or abs(bh.get("y", 0)) > R]
    if not ball:
        return ""

    svg = playback["svg"]
    if orient:
        cx = svg["pad_x"] + svg["pitch_w"] / 2
        cy = svg["pad_y"] + svg["pitch_h"] / 2
        ball = [
            ({**bh, "sx": 2 * cx - bh["sx"], "sy": 2 * cy - bh["sy"]}
             if bh.get("team") == 1 else bh)
            for bh in ball
        ]
    return _heat_pitch_svg(ball, svg, compact, key)


def _touch_spots_svg(playback: dict, player_filter: str | None = None,
                     compact: bool = True, key: str = "", orient: bool = False) -> str:
    """Per-match touch *spot* map: one marker per ball touch on a pitch, with
    kickoff first-touches dropped. A density heatmap is misleading on a single
    match's handful of touches — discrete spots show exactly where contact
    happened (and overlap naturally darkens busy areas). Lifetime/career views
    still use the density heatmap (`_ball_heatmap_svg`)."""
    ball = playback.get("ball_track") or []
    if player_filter:
        ball = [bh for bh in ball if bh["player"] == player_filter]
    ball = [bh for bh in ball if not bh.get("is_kickoff")]
    if not ball:
        return ""
    svg = playback["svg"]
    vb_w, vb_h = svg["vb_w"], svg["vb_h"]
    pad_x, pad_y = svg["pad_x"], svg["pad_y"]
    pitch_w, pitch_h = svg["pitch_w"], svg["pitch_h"]
    if orient:
        cx = pad_x + pitch_w / 2
        cy = pad_y + pitch_h / 2
        ball = [
            ({**bh, "sx": 2 * cx - bh["sx"], "sy": 2 * cy - bh["sy"]}
             if bh.get("team") == 1 else bh)
            for bh in ball
        ]
    r = 3.2 if compact else 4.5
    dots = "".join(
        f'<circle class="tspot" cx="{bh["sx"]:.1f}" cy="{bh["sy"]:.1f}" r="{r}"/>'
        for bh in ball
    )
    pitch_cls = "hm-pitch hm-pitch-compact" if compact else "hm-pitch"
    return (
        f'<svg viewBox="0 0 {vb_w} {vb_h}" class="{pitch_cls}" '
        f'xmlns="http://www.w3.org/2000/svg">'
        f'<rect class="pb-field" x="{pad_x:.1f}" y="{pad_y:.1f}" '
        f'width="{pitch_w}" height="{pitch_h}" />'
        f'<line class="pb-midline" x1="{vb_w/2:.1f}" y1="{pad_y:.1f}" '
        f'x2="{vb_w/2:.1f}" y2="{pad_y + pitch_h:.1f}" />'
        f'<circle class="pb-midcircle" cx="{vb_w/2:.1f}" cy="{vb_h/2:.1f}" r="48" fill="none" />'
        f'<rect class="pb-net pb-net-blue" x="{pad_x - 10:.1f}" '
        f'y="{pad_y + pitch_h/2 - 60:.1f}" width="10" height="120" />'
        f'<rect class="pb-net pb-net-orng" x="{pad_x + pitch_w:.1f}" '
        f'y="{pad_y + pitch_h/2 - 60:.1f}" width="10" height="120" />'
        f'<g class="tspots">{dots}</g>'
        f'</svg>'
    )


def _heat_pitch_svg(ball: list, svg: dict, compact: bool, key: str,
                    team: int | None = None) -> str:
    """Render ONE density pitch for a list of touches (one team's, or all of
    them when only one team is present). `team` picks the colour ramp: 0=blue,
    1=orange, None=legacy thermal (ball-only maps)."""
    if not ball:
        return ""
    vb_w, vb_h = svg["vb_w"], svg["vb_h"]
    pad_x, pad_y = svg["pad_x"], svg["pad_y"]
    pitch_w, pitch_h = svg["pitch_w"], svg["pitch_h"]
    # Per-point opacity adapts to touch count so a ~40-touch mini and a dense
    # lifetime map both land in a readable density band. Sparse data can't be
    # both smooth AND warm, so minis stay tight (warm marks) while dense maps use
    # a wider blur + lower opacity (hot zones emerge instead of saturating).
    n = len(ball)
    pt_opacity = max(0.06, min(0.60, 2.4 / (n ** 0.5)))
    pt_r  = 5 if compact else 6
    blur  = 4 if compact else 13
    sfx = f"-{key}" if key else ""
    heat_id = f"heat{sfx}"
    pts = "".join(
        f'<circle cx="{bh["sx"]:.1f}" cy="{bh["sy"]:.1f}" r="{pt_r}" '
        f'fill="#000" fill-opacity="{pt_opacity:.3f}"/>'
        for bh in ball
    )
    # Blur the splats into a density field, copy density into every channel, then
    # recolour sparse->dense through a SINGLE-HUE ramp keyed to the team, so a
    # "Blue" pitch reads blue and an "Orange" pitch reads orange (peaks go
    # white-hot for legibility). team=None keeps the legacy thermal rainbow.
    alpha = "0 0.42 0.66 0.82 0.90 0.95 0.98 1"
    if team == 0:        # blue: deep blue -> cyan -> white-hot
        fr = "0.05 0.06 0.10 0.18 0.34 0.58 0.82 1"
        fg = "0.10 0.28 0.46 0.62 0.76 0.88 0.95 1"
        fb = "0.30 0.55 0.78 0.92 0.99 1 1 1"
    elif team == 1:      # orange: deep orange -> amber -> white-hot
        fr = "0.25 0.50 0.74 0.92 1 1 1 1"
        fg = "0.08 0.20 0.36 0.52 0.68 0.82 0.92 1"
        fb = "0.05 0.06 0.09 0.16 0.28 0.48 0.74 1"
    else:                # legacy thermal: indigo -> blue -> teal -> green -> yellow -> red
        fr = "0.13 0.10 0.12 0.34 0.74 0.97 1 1"
        fg = "0.09 0.36 0.66 0.86 0.89 0.70 0.40 0.18"
        fb = "0.28 0.69 0.73 0.43 0.17 0.06 0.05 0.12"
    defs = (
        f'<defs>'
        f'<filter id="{heat_id}" x="-15%" y="-15%" width="130%" height="130%" '
        f'color-interpolation-filters="sRGB">'
        f'<feGaussianBlur in="SourceGraphic" stdDeviation="{blur}" result="b"/>'
        f'<feColorMatrix in="b" type="matrix" '
        f'values="0 0 0 2.6 0  0 0 0 2.6 0  0 0 0 2.6 0  0 0 0 2.6 0" result="d"/>'
        f'<feComponentTransfer in="d">'
        f'<feFuncR type="table" tableValues="{fr}"/>'
        f'<feFuncG type="table" tableValues="{fg}"/>'
        f'<feFuncB type="table" tableValues="{fb}"/>'
        f'<feFuncA type="table" tableValues="{alpha}"/>'
        f'</feComponentTransfer>'
        f'</filter>'
        f'</defs>'
    )
    legend = "" if compact else _heat_legend_svg(vb_w, vb_h, sfx, team)
    pitch_cls = "hm-pitch hm-pitch-compact" if compact else "hm-pitch"
    return (
        f'<svg viewBox="0 0 {vb_w} {vb_h}" class="{pitch_cls}" '
        f'xmlns="http://www.w3.org/2000/svg">'
        f'{defs}'
        f'<rect class="pb-field" x="{pad_x:.1f}" y="{pad_y:.1f}" '
        f'width="{pitch_w}" height="{pitch_h}" />'
        f'<g filter="url(#{heat_id})">{pts}</g>'
        f'<line class="pb-midline" x1="{vb_w/2:.1f}" y1="{pad_y:.1f}" '
        f'x2="{vb_w/2:.1f}" y2="{pad_y + pitch_h:.1f}" />'
        f'<circle class="pb-midcircle" cx="{vb_w/2:.1f}" cy="{vb_h/2:.1f}" r="48" fill="none" />'
        f'<rect class="pb-net pb-net-blue" x="{pad_x - 10:.1f}" '
        f'y="{pad_y + pitch_h/2 - 60:.1f}" width="10" height="120" />'
        f'<rect class="pb-net pb-net-orng" x="{pad_x + pitch_w:.1f}" '
        f'y="{pad_y + pitch_h/2 - 60:.1f}" width="10" height="120" />'
        f'{legend}'
        f'</svg>'
    )


def _heat_legend_svg(vb_w: float, vb_h: float, sfx: str, team: int | None = None) -> str:
    """Small 'fewer -> more touches' key, bottom-right of a heatmap. Tinted to the
    team hue so the legend matches the pitch (blue / orange); thermal otherwise."""
    lw, lh = 96, 6
    lx, ly = vb_w - lw - 14, vb_h - 16
    gid = f"heatleg{sfx}"
    if team == 0:
        stops = (
            '<stop offset="0%" stop-color="rgb(18,34,78)"/>'
            '<stop offset="45%" stop-color="rgb(38,110,200)"/>'
            '<stop offset="78%" stop-color="rgb(120,200,255)"/>'
            '<stop offset="100%" stop-color="rgb(235,248,255)"/>'
        )
    elif team == 1:
        stops = (
            '<stop offset="0%" stop-color="rgb(74,32,12)"/>'
            '<stop offset="45%" stop-color="rgb(214,108,36)"/>'
            '<stop offset="78%" stop-color="rgb(255,196,110)"/>'
            '<stop offset="100%" stop-color="rgb(255,246,232)"/>'
        )
    else:
        stops = (
            '<stop offset="0%" stop-color="rgb(33,26,82)"/>'
            '<stop offset="26%" stop-color="rgb(26,92,176)"/>'
            '<stop offset="50%" stop-color="rgb(40,176,150)"/>'
            '<stop offset="70%" stop-color="rgb(214,209,60)"/>'
            '<stop offset="86%" stop-color="rgb(240,120,40)"/>'
            '<stop offset="100%" stop-color="rgb(220,40,40)"/>'
        )
    tstyle = "font:600 8px system-ui,sans-serif;fill:currentColor;opacity:.7"
    return (
        f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="1" y2="0">{stops}</linearGradient></defs>'
        f'<rect x="{lx:.1f}" y="{ly:.1f}" width="{lw}" height="{lh}" rx="3" '
        f'fill="url(#{gid})"/>'
        f'<text x="{lx:.1f}" y="{ly - 3:.1f}" style="{tstyle}">fewer</text>'
        f'<text x="{lx + lw:.1f}" y="{ly - 3:.1f}" style="{tstyle}" '
        f'text-anchor="end">more touches</text>'
    )


def _kickoff_card_html(playback: dict, players, viewer_pid: str | None,
                         viewer_name: str | None) -> str:
    """Detect kickoff outcomes from BallHit data. A kickoff is the first ball
    touch after match start, or the first touch after a goal. We classify the
    kickoff winner as whichever team touched first."""
    ball = playback.get("ball_track") or []
    # Kickoffs are sequence-tagged (is_kickoff = the first touch after the match
    # start and after each goal), so detection works even when uploaded-match
    # timestamps collapse to one value — the old time-window approach found
    # nothing then and rendered an empty card.
    kickoffs = [bh for bh in ball if bh.get("is_kickoff")]
    if not kickoffs:
        return ""
    # Neutral Blue-vs-Orange framing (no viewer "you" / "my wins").
    total = len(kickoffs)
    t0_wins = sum(1 for k in kickoffs if k.get("team") == 0)
    t1_wins = sum(1 for k in kickoffs if k.get("team") == 1)
    row_html = []
    for i, k in enumerate(kickoffs):
        team_cls = "team-blue" if k.get("team") == 0 else "team-orng" if k.get("team") == 1 else ""
        kind = "Opening" if i == 0 else f"Restart {i}"
        row_html.append(
            f'<tr class="row"><td class="dim">{kind}</td>'
            f'<td class="{team_cls}"><b>{html.escape(k.get("player") or "?")}</b></td></tr>'
        )
    return f"""
      <div class="card kickoff-card" style="margin-top:0">
        <div class="section-title">
          <span>Kickoffs</span>
          <span class="dim" style="text-transform:none;letter-spacing:0">
            Who won each kickoff (first touch) &middot; {total} this match
          </span>
        </div>
        <div class="kickoff-summary">
          <b class="tnum team-blue">{t0_wins}</b> <span class="dim">Blue</span>
          &nbsp;&middot;&nbsp;
          <b class="tnum team-orng">{t1_wins}</b> <span class="dim">Orange</span>
          <span class="dim">&mdash; kickoffs won</span>
        </div>
        <table class="history" style="margin-top:8px">
          <thead><tr>
            <th>Kickoff</th>
            <th>Won by (first touch)</th>
          </tr></thead>
          <tbody>{"".join(row_html)}</tbody>
        </table>
      </div>
    """


def _match_insights_html(playback: dict, t0_name: str, t1_name: str) -> str:
    """Insights card: ball touch heatmap + possession + ball-half pressure.

    All three are derived from BallHit positions. Possession and pressure are
    estimates rather than ground-truth (RL doesn't expose continuous ball
    state) but the inter-hit intervals give a reasonable proxy."""
    ball = playback.get("ball_track") or []
    if not ball:
        return ""

    # ---- Touch share (possession proxy) ------------------------------------
    # Per-touch timing isn't reliable: uploaded matches arrive batched with a
    # single received_at, so inter-touch intervals collapse to zero (this was
    # the "possession 0% / pressure 50-50" bug). Use touch COUNTS instead — a
    # robust proxy that works for every match. Each team's share of contacts.
    b_touch = sum(1 for bh in ball if bh.get("team") == 0)
    o_touch = sum(1 for bh in ball if bh.get("team") == 1)
    total_touch = b_touch + o_touch or 1
    b_pct = b_touch / total_touch * 100
    o_pct = o_touch / total_touch * 100

    # ---- Field tilt (pressure proxy) ---------------------------------------
    # Blue attacks Y < 0 (Orange's half); Orange attacks Y > 0. Count contacts
    # in each attacking half — a position-based proxy for territorial pressure
    # (count, not time, so it survives the batched-timestamp upload).
    b_attack = sum(1 for bh in ball if (bh.get("y") or 0) < -300)
    o_attack = sum(1 for bh in ball if (bh.get("y") or 0) > 300)
    pres_total = b_attack + o_attack or 1
    b_pres = b_attack / pres_total * 100
    o_pres = o_attack / pres_total * 100

    # ---- Touches per player ------------------------------------------------
    by_player: dict[str, dict] = {}
    for bh in ball:
        name = bh["player"]
        if not name:
            continue
        team = bh["team"]
        d = by_player.setdefault(name, {"team": team, "n": 0})
        d["n"] += 1

    # Sort: Blue first (by descending touches), then Orange.
    blue_touch = sorted(((n, v) for n, v in by_player.items() if v["team"] == 0),
                        key=lambda kv: -kv[1]["n"])
    orng_touch = sorted(((n, v) for n, v in by_player.items() if v["team"] == 1),
                        key=lambda kv: -kv[1]["n"])
    total_blue_touch = sum(v["n"] for _, v in blue_touch) or 1
    total_orng_touch = sum(v["n"] for _, v in orng_touch) or 1

    def touch_rows(team_list, total, tcls):
        out = []
        for name, v in team_list:
            pct = v["n"] / total * 100
            out.append(
                f'<li class="touch-row {tcls}">'
                f'<span class="touch-name">{html.escape(name)}</span>'
                f'<span class="touch-bar"><span class="touch-bar-fill" '
                f'style="width:{pct:.1f}%"></span></span>'
                f'<span class="touch-num tnum">{v["n"]} <span class="dim">({pct:.0f}%)</span></span>'
                f'</li>'
            )
        return "".join(out)

    return f"""
      <div class="card insights-card">
        <div class="section-title">
          <span>Match insights</span>
          <span class="dim" style="text-transform:none;letter-spacing:0">
            Derived from {len(ball)} ball contacts. Touch share and field tilt
            are from contact counts and positions.
          </span>
        </div>

        <div class="insights-row">
          <div class="insights-bars">
            <div class="insights-subtitle">Touch share (share of ball contacts)</div>
            <div class="dual-bar">
              <div class="dual-bar-blue" style="width:{b_pct:.1f}%">
                <span class="dual-bar-label">{b_pct:.0f}%</span>
              </div>
              <div class="dual-bar-orng" style="width:{o_pct:.1f}%">
                <span class="dual-bar-label">{o_pct:.0f}%</span>
              </div>
            </div>
            <div class="dual-bar-foot">
              <span class="team-blue team-name-truncate" title="{html.escape(t0_name)}">{html.escape(t0_name)}</span>
              <span class="team-orng team-name-truncate" title="{html.escape(t1_name)}">{html.escape(t1_name)}</span>
            </div>

            <div class="insights-subtitle" style="margin-top:14px">
              Field tilt (contacts in opponent's half)
            </div>
            <div class="dual-bar">
              <div class="dual-bar-blue" style="width:{b_pres:.1f}%">
                <span class="dual-bar-label">{b_pres:.0f}%</span>
              </div>
              <div class="dual-bar-orng" style="width:{o_pres:.1f}%">
                <span class="dual-bar-label">{o_pres:.0f}%</span>
              </div>
            </div>
            <div class="dual-bar-foot">
              <span class="team-blue team-name-truncate" title="{html.escape(t0_name)}">{html.escape(t0_name)} attacking</span>
              <span class="team-orng team-name-truncate" title="{html.escape(t1_name)}">{html.escape(t1_name)} attacking</span>
            </div>
          </div>

          <div class="insights-touches">
            <div class="insights-subtitle">Touches by player</div>
            <ul class="touch-list">
              {touch_rows(blue_touch, total_blue_touch, "team-blue")}
              {touch_rows(orng_touch, total_orng_touch, "team-orng")}
            </ul>
          </div>
        </div>
      </div>
    """


def _about_html() -> str:
    body = """
      <div class="prose prose-section">
      <h1>How Chumstats works</h1>
      <p class="caption">A short explainer of where the numbers come from, what's possible,
        and what's intentionally out of reach.</p>

      <section>
        <h2>Where the data comes from</h2>
        <p>Rocket League ships a built-in <b>Stats API</b>. When you set <code>PacketSendRate</code>
          to a non-zero value in <code>DefaultStatsAPI.ini</code>, the game opens a local TCP
          socket on <code>127.0.0.1:49123</code> and streams JSON events while you play.
          Chumstats connects to that socket, persists everything to a local SQLite database
          (<code>data/chumstats.db</code>), and turns the events into match summaries, lifetime
          stats, and the OBS overlay.</p>
        <p>No remote services are involved. No third-party APIs (no ballchasing, no tracker.gg).
          The only network call out is your Discord bot posting embeds to your channel.</p>
        <pre class="codeblock">[StatsAPI]
PacketSendRate=30
HostName=127.0.0.1
PortNumber=49123</pre>
      </section>

      <section>
        <h2>What we capture for every player</h2>
        <p>These are emitted for everyone in every match - you, teammates, opponents, bots:</p>
        <ul>
          <li>Score, Goals, Assists, Saves, Shots, Demos, Touches</li>
          <li>Team affiliation, platform (Steam / Epic / Switch), MVP designation</li>
          <li>Match-level: final score, arena, winner, duration, crossbar hits, ball touches with XYZ</li>
        </ul>
        <pre class="codeblock">{
  "Event": "PlayerInfo",
  "Data": {
    "Name": "Octane_Ace",
    "Goals": 2, "Assists": 1,
    "Saves": 3, "Shots": 5,
    "Demos": 0, "Score": 540
  }
}</pre>
      </section>

      <section>
        <h2>Advanced stats: who gets them, and why some players don't</h2>
        <p>Several fields are marked <code>SPECTATOR</code> in the API spec:</p>
        <ul>
          <li>Current boost (0-100)</li>
          <li>Car speed</li>
          <li>On-wall / on-ground / has-car / is-boosting booleans</li>
        </ul>
        <p>The game emits these for whoever the spectator camera is locked on. For <b>you and
          your teammates</b>, that's the whole match. For <b>opponents and bots</b>, that's only
          the brief moments when the camera cuts to them during goal celebrations, usually
          5 to 30 seconds per match, not enough to draw conclusions from.</p>
        <p>The match detail page hides the advanced row when coverage is below 70%. So you'll
          see Supersonic / Air / Wall / Ground / Avg speed / Boost used for yourself and your
          teammates, and a "partial data" note for everyone else.</p>
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
          console output when Chumstats starts - it prints something like
          <code>http://192.168.1.42:5050/dashboard</code>. Lock it back down to loopback by
          setting <code>CHUMSTATS_SERVER_HOST=127.0.0.1</code> in <code>.env</code>.</p>
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

      <section>
        <h2>Sharing match links (chumstats.local alias)</h2>
        <p>Discord match posts include a clickable link to the match detail
          page. By default the link points at <code>http://chumstats.local:5050</code>
          so the URL stays the same when you switch laptops / network. To make
          the alias resolve, add one line to your Windows hosts file:</p>
        <pre style="background:var(--bg);border:1px solid var(--border);padding:10px 14px;font-family:'JetBrains Mono',monospace;font-size:12.5px;overflow-x:auto">127.0.0.1   chumstats.local</pre>
        <p>Edit the hosts file at
          <code>C:\\Windows\\System32\\drivers\\etc\\hosts</code> as Administrator
          (right-click Notepad &rarr; Run as administrator &rarr; open the file).
          Append the line above and save. Test by opening
          <code>http://chumstats.local:5050/live</code> in a browser.</p>
        <p>To hand-pick a different host (e.g., when you buy a real domain and
          host the server publicly), set the env var
          <code>CHUMSTATS_PUBLIC_URL=https://stats.yourdomain.com</code> and
          restart chumstats.</p>
      </section>

      <section>
        <h2>What is a "session"?</h2>
        <p>The <em>Session</em> stats shown in Discord and on the dashboard
          track everything since chumstats started running. Quit and relaunch
          the tracker and the counter resets. It does NOT roll on a 24-hour
          window or reset at midnight - it's tied to the chumstats process
          lifetime. Tip: launch the tracker once at the start of your gaming
          block and let it run; the session line will track that block.</p>
      </section>

      <section>
        <h2>Pro-tier benchmarks</h2>
        <p>The Compare page now shows a <em>Pro tier</em> reference column. Those
          numbers are rough averages from Champion-through-Grand-Champion-tier
          competitive play; treat them as orientation, not targets. A 30% shooting
          percentage looks pedestrian until you realize that's the GC range and you
          rarely see better even in RLCS broadcasts.</p>
      </section>

      <section>
        <h2>Icons + attribution</h2>
        <p>The point icons (Goal, Aerial Goal, Epic Save, Hat Trick, MVP, etc.) are
          from the
          <a href="https://rocketleague.fandom.com/wiki/Category:Point_icons">
            Rocket League Wiki on Fandom
          </a>,
          available under the
          <a href="https://creativecommons.org/licenses/by-sa/3.0/">
            CC-BY-SA 3.0
          </a> license.
          Psyonix / Epic Games own the underlying game assets.</p>
        <p>The on-pitch ball graphic during playback and the car silhouettes used
          for ball touches are drawn locally as inline SVG.</p>
      </section>
      </div>
    """
    return _page_wrap("How it works", body, active="about")


def _compare_page_html(store, slots: list[str], *, self_name: str | None = None,
                       include_bots: bool = False,
                       mode_filter: int | None = None,
                       window_days: int | None = None,
                       last_n: int | None = 20) -> str:
    """Side-by-side compare for up to 3 players (slot 0 defaults to self).
    Pulls stats from match_player_stats and derives Combat/Highlights/Goal-
    quality stats from raw_events on the fly.

    By default each player is scoped to their most recent `last_n` games (20) so
    a high-volume player's lifetime totals don't dominate the comparison; set
    last_n falsy to compare full history (then the time window applies)."""
    from .analytics import _lifetime_row

    bot_filter = "" if include_bots else "WHERE COALESCE(max_bot, 0) = 0"
    with store._conn() as con:
        all_players = con.execute(f"""
            SELECT name, MAX(is_bot) AS max_bot, COUNT(*) AS n
            FROM match_player_stats
            GROUP BY name
        """).fetchall()
        all_players = sorted([dict(r) for r in all_players if include_bots or not r["max_bot"]],
                             key=lambda r: -r["n"])

        def _scope(slot_name: str) -> set | None:
            """The player's most-recent `last_n` match ids (equal-sample window).
            None means 'no games window' (use full history / time filter)."""
            if not slot_name or not last_n or last_n <= 0:
                return None
            return {r["match_id"] for r in con.execute(
                """SELECT mps.match_id FROM match_player_stats mps
                   JOIN matches m ON m.id = mps.match_id
                   WHERE mps.name = ? ORDER BY m.started_at DESC LIMIT ?""",
                (slot_name, last_n))}
        scopes = [_scope(s) for s in slots]

        rows = []
        derived_rows: list[dict] = []
        for i, slot_name in enumerate(slots):
            row = _lifetime_row(con, None, slot_name,
                                mode_filter=mode_filter,
                                window_days=window_days,
                                match_ids=scopes[i]) if slot_name else {}
            rows.append(row)
            derived_rows.append(_lifetime_derived(store, slot_name, scopes[i]) if slot_name else _empty_derived() | {"goal_participation_num": 0, "goal_participation_den": 0})

    # Lifetime touch data (heatmap + position thirds) is computed outside the
    # connection block since the helper manages its own connection.
    touch_data = [
        _lifetime_touch_data(store, slot_name, scopes[i]) if slot_name else None
        for i, slot_name in enumerate(slots)
    ]
    # Shot maps (where each player's goals were struck from) — selectable
    # alongside touch maps via the heatmap-type dropdown.
    shot_data = [
        _lifetime_shot_data(store, slot_name) if slot_name else None
        for slot_name in slots
    ]

    def option_list(selected: str) -> str:
        opts = ['<option value="">(select a player)</option>']
        for r in all_players:
            sel = " selected" if r["name"] == selected else ""
            nm = html.escape(r["name"] or "")  # names are attacker-controllable
            opts.append(f'<option value="{nm}"{sel}>{nm} ({r["n"]})</option>')
        return "".join(opts)

    # Equal-sample window: default to each player's most recent 20 games so a
    # high-volume player's totals don't dominate. The control lives in the
    # sidebar "Sample" filter; the player form carries it via a hidden field.
    _cur_last = last_n or 0

    # Compose the row of selectors.
    selectors = []
    for i, slot in enumerate(slots):
        label = "Slot " + str(i + 1)  # neutral all-players view — no "(you)"
        selectors.append(f"""
          <label class="compare-slot">
            <span class="compare-slot-label">{label}</span>
            <select name="names">{option_list(slot)}</select>
          </label>
        """)

    def safe_div(a, b):
        return (a or 0) / b if b else 0.0

    def pct_if(ticks, num):
        """Only return % when we have enough ticks for the value to be
        representative. Below 1000 ticks (~33s of play), opponents' goal-cam
        tail data leads to misleading splits."""
        if not ticks or ticks < 1000:
            return None
        return (num or 0) / ticks

    # Pre-compute per-match averages for derived stats too.
    def avg_per_match(field):
        return [safe_div(d.get(field, 0), r.get("matches")) for d, r in zip(derived_rows, rows)]

    demos_given_avg = avg_per_match("demos_given")
    demos_received_avg = avg_per_match("demos_received")
    # K/D ratio with sensible behaviour when denominator is 0:
    def kd_pair(d):
        given = d.get("demos_given", 0)
        recv = d.get("demos_received", 0)
        if recv == 0 and given == 0:
            return None
        if recv == 0:
            return float('inf')  # avoid divide by zero; render as "∞"
        return given / recv
    demo_kd = [kd_pair(d) for d in derived_rows]
    # Goal participation = sum(player_g+a) / sum(team_g)
    goal_part = [(d.get("goal_participation_num", 0) / d.get("goal_participation_den") * 100)
                 if d.get("goal_participation_den") else None
                 for d in derived_rows]
    # Average goal speed
    avg_goal_speed = [(d.get("goal_speed_sum", 0) / d.get("goal_count"))
                      if d.get("goal_count") else None
                      for d in derived_rows]

    # Lifetime BPM = boost_used / minutes_of_coverage (only meaningful when we
    # have enough ticks; falls back to None otherwise).
    def bpm_of(row):
        ticks = row.get("ticks") or 0
        if ticks < 1000:
            return None
        minutes = ticks / 30 / 60
        return (row.get("boost_used") or 0) / minutes if minutes else None
    bpm_vals = [bpm_of(r) for r in rows]

    # Position thirds (from lifetime touch heatmap data) as percentages.
    def third_pct(td, key):
        if not td:
            return None
        t = sum(td["thirds"].values())
        return (td["thirds"][key] / t * 100) if t else None
    def_pct_vals = [third_pct(td, "def") for td in touch_data]
    neu_pct_vals = [third_pct(td, "neu") for td in touch_data]
    off_pct_vals = [third_pct(td, "off") for td in touch_data]

    # Each row in a "totavg" section shows the total (big) AND the per-match
    # average (small) in the same cell. "single" sections show one value.
    # Peak speed dropped - it's 2300 for every player who boosted; no signal.
    sections: list = [
        ("Volume", "single", [
            ("Matches", lambda v: f"{v:.0f}",
             [r.get("matches") or 0 for r in rows], True),
            ("Wins",    lambda v: f"{v:.0f}",
             [r.get("wins") or 0 for r in rows], True),
            ("Losses",  lambda v: f"{v:.0f}",
             [(r.get("matches") or 0) - (r.get("wins") or 0) for r in rows], False),
            ("MVPs",    lambda v: f"{v:.0f}",
             [r.get("mvp") or 0 for r in rows], True),
        ]),
        ("Efficiency", "single", [
            ("Win rate",            lambda v: f"{v*100:.1f}%",
             [safe_div(r.get("wins"), r.get("matches"))   for r in rows], True),
            ("MVP rate",            lambda v: f"{v*100:.1f}%",
             [safe_div(r.get("mvp"),  r.get("matches"))   for r in rows], True),
            ("Shooting %",          lambda v: f"{v*100:.1f}%",
             [safe_div(r.get("goals"), r.get("shots"))    for r in rows], True),
            ("Score / touch",       lambda v: f"{v:.2f}",
             [safe_div(r.get("score"), r.get("touches"))  for r in rows], True),
            ("Goal participation",  lambda v: f"{v:.1f}%",
             goal_part, True),
            ("Avg goal speed (kph)", lambda v: f"{v:.0f}",
             avg_goal_speed, True),
        ]),
        ("Combat", "single", [
            ("Demos delivered (total)", lambda v: f"{v:.0f}",
             [d.get("demos_given", 0)   for d in derived_rows], True),
            ("Demos received (total)",  lambda v: f"{v:.0f}",
             [d.get("demos_received", 0) for d in derived_rows], False),
            ("Demos delivered / match",  lambda v: f"{v:.2f}",
             demos_given_avg, True),
            ("Demos received / match",   lambda v: f"{v:.2f}",
             demos_received_avg, False),
            ("Demo K/D",                lambda v: ("∞" if v == float('inf') else f"{v:.2f}"),
             demo_kd, True),
            ("Crossbar hits",           lambda v: f"{v:.0f}",
             [d.get("crossbar_hits", 0) for d in derived_rows], True),
        ]),
        ("Highlights (special moments)", "single", [
            ("Epic saves",      lambda v: f"{v:.0f}", [d.get("n_epicsave", 0)      for d in derived_rows], True),
            ("Aerial goals",    lambda v: f"{v:.0f}", [d.get("n_aerialgoal", 0)    for d in derived_rows], True),
            ("Bicycle hits",    lambda v: f"{v:.0f}", [d.get("n_bicyclehit", 0)    for d in derived_rows], True),
            ("Flip resets",     lambda v: f"{v:.0f}", [d.get("n_flipreset", 0)     for d in derived_rows], True),
            ("Hat tricks",      lambda v: f"{v:.0f}", [d.get("n_hattrick", 0)      for d in derived_rows], True),
            ("Long goals",      lambda v: f"{v:.0f}", [d.get("n_longgoal", 0)      for d in derived_rows], True),
            ("Backwards goals", lambda v: f"{v:.0f}", [d.get("n_backwardsgoal", 0) for d in derived_rows], True),
            ("Saviors",         lambda v: f"{v:.0f}", [d.get("n_savior", 0)        for d in derived_rows], True),
            ("Low fives",       lambda v: f"{v:.0f}", [d.get("n_lowfive", 0)       for d in derived_rows], True),
            ("Total highlights",lambda v: f"{v:.0f}", [d.get("highlights", 0)      for d in derived_rows], True),
        ]),
        ("Per-stat output (total · per match)", "totavg", [
            ("Goals",   lambda v: f"{v:.0f}", lambda v: f"{v:.2f}",
             [r.get("goals")   or 0 for r in rows],
             [safe_div(r.get("goals"),   r.get("matches")) for r in rows], True),
            ("Assists", lambda v: f"{v:.0f}", lambda v: f"{v:.2f}",
             [r.get("assists") or 0 for r in rows],
             [safe_div(r.get("assists"), r.get("matches")) for r in rows], True),
            ("Saves",   lambda v: f"{v:.0f}", lambda v: f"{v:.2f}",
             [r.get("saves")   or 0 for r in rows],
             [safe_div(r.get("saves"),   r.get("matches")) for r in rows], True),
            ("Shots",   lambda v: f"{v:.0f}", lambda v: f"{v:.2f}",
             [r.get("shots")   or 0 for r in rows],
             [safe_div(r.get("shots"),   r.get("matches")) for r in rows], True),
            ("Demos",   lambda v: f"{v:.0f}", lambda v: f"{v:.2f}",
             [r.get("demos")   or 0 for r in rows],
             [safe_div(r.get("demos"),   r.get("matches")) for r in rows], True),
            ("Score",   lambda v: f"{v:.0f}", lambda v: f"{v:.0f}",
             [r.get("score")   or 0 for r in rows],
             [safe_div(r.get("score"),   r.get("matches")) for r in rows], True),
            ("Touches", lambda v: f"{v:.0f}", lambda v: f"{v:.1f}",
             [r.get("touches") or 0 for r in rows],
             [safe_div(r.get("touches"), r.get("matches")) for r in rows], True),
        ]),
        ("Movement (where they are on the field)", "single", [
            ("Supersonic %",     lambda v: f"{v*100:.1f}%",
             [pct_if(r.get("ticks"), r.get("ticks_super"))  for r in rows], True),
            ("Time in air %",    lambda v: f"{v*100:.1f}%",
             [pct_if(r.get("ticks"), r.get("ticks_air"))    for r in rows], True),
            ("Time on wall %",   lambda v: f"{v*100:.1f}%",
             [pct_if(r.get("ticks"), r.get("ticks_wall"))   for r in rows], True),
            ("Time on ground %", lambda v: f"{v*100:.1f}%",
             [pct_if(r.get("ticks"), r.get("ticks_ground")) for r in rows], False),
            ("Avg speed",        lambda v: f"{v:.1f}",
             [safe_div(r.get("speed_sum"), r.get("ticks")) if (r.get("ticks") or 0) >= 1000 else None for r in rows], True),
        ]),
        ("Boost (total · per match)", "totavg", [
            ("Boost used", lambda v: f"{v:.0f}", lambda v: f"{v:.0f}",
             [r.get("boost_used") if (r.get("ticks") or 0) >= 1000 else None for r in rows],
             [safe_div(r.get("boost_used"), r.get("matches")) if (r.get("ticks") or 0) >= 1000 else None for r in rows], True),
        ]),
        ("Boost timing", "single", [
            ("BPM (boost used per minute)", lambda v: f"{v:.0f}",
             bpm_vals, True),
            # "Time near-empty %" removed — boost<=1 almost never fires (0% for
            # everyone), so the stat was invalid. See session.py threshold note.
            ("Time at 100 boost %", lambda v: f"{v*100:.1f}%",
             [pct_if(r.get("ticks"), r.get("ticks_full")) for r in rows], True),
        ]),
        ("Ball positioning (across matches)", "single", [
            ("Ball touches",      lambda v: f"{v:,.0f}",
             [td.get("touches") if td else None for td in touch_data], True),
            ("Defensive third %", lambda v: f"{v:.1f}%", def_pct_vals, False),
            ("Neutral third %",   lambda v: f"{v:.1f}%", neu_pct_vals, True),
            ("Offensive third %", lambda v: f"{v:.1f}%", off_pct_vals, True),
        ]),
    ]

    # Equal-width slot columns + a "Pro tier" benchmark column at the end.
    n_slots = len(slots)
    n_cols = 1 + n_slots + 1  # metric + slots + benchmark
    colgroup = ('<colgroup><col class="compare-col-metric"/>'
                + ''.join('<col class="compare-col-slot"/>' for _ in slots)
                + '<col class="compare-col-pro"/>'
                + '</colgroup>')
    header_cells = ['<th class="compare-metric-col">Metric</th>']
    for i, slot in enumerate(slots):
        display_name = slot or f"(empty slot {i+1})"
        header_cells.append(f'<th class="compare-col">{display_name}</th>')
    header_cells.append(
        '<th class="compare-pro-col" '
        'title="Soft benchmarks from competitive RL play (Champion/GC tier). '
        'Treat as references, not hard targets.">Pro tier</th>'
    )

    def winner_classes(values, higher_better):
        clean = [v for v in values if isinstance(v, (int, float))]
        if len(clean) >= 2 and len(set(clean)) > 1:
            target = max(clean) if higher_better else min(clean)
            return ["best" if v == target else "" for v in values]
        return ["" for _ in values]

    def pro_cell(label: str) -> str:
        v = _PRO_BENCHMARKS.get(label)
        if not v:
            return '<td class="compare-pro-val dim">--</td>'
        return f'<td class="compare-pro-val tnum">{v}</td>'

    rows_html: list[str] = []
    for section_entry in sections:
        section_name, kind, section_metrics = section_entry
        rows_html.append(
            f'<tr class="compare-section-row"><td colspan="{n_cols}">{section_name}</td></tr>'
        )
        if kind == "single":
            for label, formatter, values, higher_better in section_metrics:
                formatted = [(formatter(v) if isinstance(v, (int, float)) else "n/a") for v in values]
                classes = winner_classes(values, higher_better)
                cells = [f'<td class="compare-metric">{_stat_icon_html(label)}{label}</td>']
                for v, c in zip(formatted, classes):
                    cls = ("compare-val " + c).strip()
                    cells.append(f'<td class="{cls}">{v}</td>')
                cells.append(pro_cell(label))
                rows_html.append(f'<tr>{"".join(cells)}</tr>')
        else:  # totavg
            for label, f_tot, f_avg, totals_vals, avg_vals, higher_better in section_metrics:
                # Highlight on per-match average so players with different match
                # counts compare fairly (raw totals would just reward whoever has
                # played more games).
                classes = winner_classes(avg_vals, higher_better)
                cells = [f'<td class="compare-metric">{_stat_icon_html(label)}{label}</td>']
                for tot, avg, c in zip(totals_vals, avg_vals, classes):
                    tot_s = f_tot(tot) if isinstance(tot, (int, float)) else "n/a"
                    avg_s = (f_avg(avg) + " / match") if isinstance(avg, (int, float)) else ""
                    cls = ("compare-val compare-totavg " + c).strip()
                    cells.append(
                        f'<td class="{cls}">'
                        f'<div class="compare-tot">{tot_s}</div>'
                        f'<div class="compare-avg">{avg_s}</div>'
                        f'</td>'
                    )
                cells.append(pro_cell(label))
                rows_html.append(f'<tr>{"".join(cells)}</tr>')

    body = f"""
      <div class="page-head">
        <div>
          <h1>Compare players</h1>
          <div class="sub">Side-by-side lifetime stats. Pick up to 3 players —
            defaults to the most-played. Best value in each row is highlighted by per-match average.</div>
        </div>
      </div>

      <form class="compare-form" method="get" action="/compare">
        <div class="compare-slots">
          {"".join(selectors)}
        </div>
        <input type="hidden" name="last" value="{_cur_last}">
        <button type="submit" class="copy-btn" style="padding:8px 18px;font-size:12px">Compare</button>
      </form>

      {_compare_heatmap_row(slots, touch_data, shot_data)}

      <div class="card" style="padding:0;overflow:hidden;margin-top:16px">
        <table class="compare-table">
          {colgroup}
          <thead><tr>{"".join(header_cells)}</tr></thead>
          <tbody>{"".join(rows_html)}</tbody>
        </table>
      </div>
    """
    return _page_wrap("Compare players", body, active="compare")


def _compare_heatmap_row(slots: list, touch_data: list, shot_data: list | None = None) -> str:
    """Per-slot lifetime heatmaps with a TYPE dropdown (touch map / shot map).
    Both maps render per card; the dropdown toggles which is visible (JS). Skipped
    entirely if no slot has any touch data."""
    if not any(td and td.get("touches") for td in touch_data):
        return ""
    shot_data = shot_data or [None] * len(slots)
    any_shots = any(sd and sd.get("shots") for sd in shot_data)
    cards = []
    for i, slot in enumerate(slots):
        td = touch_data[i] if i < len(touch_data) else None
        sd = shot_data[i] if i < len(shot_data) else None
        label = slot or f"(empty slot {i+1})"
        if not td or not td.get("touches"):
            cards.append(
                f'<div class="compare-hm-card"><div class="compare-hm-head">{label}</div>'
                f'<div class="compare-hm-empty">No data yet.</div></div>'
            )
            continue
        name_slug = "".join(ch if ch.isalnum() else "_" for ch in slot)
        touch_hm = _ball_heatmap_svg(td, compact=True, key=f"cmp-t-{name_slug}")
        shot_hm = (_ball_heatmap_svg(sd, compact=True, key=f"cmp-s-{name_slug}", exclude_center=False)
                   if sd and sd.get("shots") else '<div class="compare-hm-empty">No goals yet.</div>')
        cards.append(
            f'<div class="compare-hm-card">'
            f'<div class="compare-hm-head" title="{label}">{label}</div>'
            f'<div class="compare-hm-body cmp-hm cmp-hm-touch">{touch_hm}</div>'
            f'<div class="compare-hm-body cmp-hm cmp-hm-shot" style="display:none">{shot_hm}</div>'
            f'</div>'
        )
    selector = (
        '<select id="cmp-hm-select" class="cmp-hm-select">'
        '<option value="touch">Touch map &mdash; where they touch the ball</option>'
        '<option value="shot">Shot map &mdash; where they score from</option>'
        '</select>') if any_shots else ""
    js = ("<script>(function(){var s=document.getElementById('cmp-hm-select');if(!s)return;"
          "s.addEventListener('change',function(){var v=s.value;"
          "document.querySelectorAll('.cmp-hm-touch').forEach(function(e){e.style.display=v==='touch'?'':'none';});"
          "document.querySelectorAll('.cmp-hm-shot').forEach(function(e){e.style.display=v==='shot'?'':'none';});});})();</script>"
          ) if any_shots else ""
    return f"""
      <div class="card" style="margin-top:16px">
        <div class="section-title">
          <span>Heatmaps (lifetime)</span>
          {selector}
        </div>
        <div class="section-sub dim" style="margin:-4px 0 10px;text-transform:none;letter-spacing:0">
          Rotated so each player attacks &#8594; (right). Brighter = more.
        </div>
        <div class="compare-hm-row">{"".join(cards)}</div>
        {js}
      </div>
    """


def _live_page_html(*, self_name: str | None = None, friend_mode: bool = False) -> str:
    """Real-time match view. Subscribes to /ws and renders a match-detail
    style scoreboard + roster table that updates on every tick. When idle
    (no live socket / no active match), shows a placeholder + link to the
    most recent finished match."""
    self_attr = f'data-self-name="{self_name}"' if self_name else ""
    # The friend's local server doesn't serve the analytical pages, so skip the
    # idle-state links that would 404 there.
    idle_links = "" if friend_mode else (
        '<p class="caption dim" style="margin:0">'
        '<a href="/history">View match history</a>'
        ' &middot; <a href="/about">How it works</a>'
        '</p>'
    )
    body = f"""
      <div id="live-root" class="live-page" {self_attr}>
        <div class="live-status">
          <span class="live-pip" id="live-pip"><span class="dot"></span><span id="live-pip-text">connecting...</span></span>
          <span class="dim" id="live-meta"></span>
          <button type="button" id="live-view-toggle" class="live-view-toggle" data-mode="live">
            <span class="boost-fs-glyph">&#x26A1;</span>
            <span id="live-view-toggle-label">BOOST VIEW</span>
          </button>
          <button type="button" id="boost-exclude-self-toggle" class="live-view-toggle boost-self-toggle">
            <span id="boost-exclude-self-label">HIDE ME</span>
          </button>
        </div>

        <div class="live-default-view" id="live-default-view">
          <header class="match-hero" id="live-hero" style="display:none">
            <div class="side left">
              <div class="team-stripe"></div>
              <div class="team-meta">
                <div class="team-tag">Blue &middot; Team 0</div>
                <div class="team-name" id="live-t0-name">Blue</div>
                <div class="live-team-badge" id="live-t0-badge"></div>
              </div>
              <div class="score-display tnum" id="live-t0-score" style="margin-left:auto">0</div>
            </div>
            <div class="middle">
              <div class="hero-duration tnum" id="live-clock">5:00</div>
              <div class="hero-context">
                <span class="hero-ctx-final" id="live-period">Regulation</span>
                <span class="hero-ctx-pill" id="live-ot-pill" style="display:none">Overtime</span>
              </div>
              <div class="hero-meta">
                <span id="live-arena">--</span>
                <span class="hero-meta-sep">&middot;</span>
                <span>Ball <b class="tnum" id="live-ball-speed">0</b> kph</span>
              </div>
            </div>
            <div class="side right">
              <div class="team-stripe"></div>
              <div class="team-meta">
                <div class="team-tag">Orange &middot; Team 1</div>
                <div class="team-name" id="live-t1-name">Orange</div>
                <div class="live-team-badge" id="live-t1-badge"></div>
              </div>
              <div class="score-display tnum" id="live-t1-score" style="margin-right:auto">0</div>
            </div>
          </header>

          <div id="live-rosters" style="display:none"></div>

          <div class="card" id="live-events-card" style="display:none">
            <div class="section-title">
              <span>Live events</span>
              <span class="dim" style="text-transform:none;letter-spacing:0">
                Goals + crossbar hits as they happen.
              </span>
            </div>
            <ol class="pb-events" id="live-events"></ol>
          </div>

          <div class="card live-idle" id="live-idle">
            <div class="empty">
              <h2 style="margin:0 0 8px">No active match</h2>
              <p class="caption" style="margin:0 0 14px">
                Start a match in Rocket League and the tracker will fill this
                in tick by tick. Make sure <code>PacketSendRate</code> is set
                in <code>DefaultStatsAPI.ini</code>.
              </p>
              {idle_links}
            </div>
          </div>
        </div>

        <div id="boost-root" class="boost-page live-boost-view" style="display:none" {self_attr}>
          {_boost_view_markup()}
        </div>
      </div>

      <script>{_LIVE_JS}</script>
      <script>{_BOOST_JS}</script>
      <script>{_LIVE_BOOST_TOGGLE_JS}</script>
    """
    return _page_wrap("Live", body, active="live", friend_mode=friend_mode)


def _boost_view_markup() -> str:
    """A HUD-style boost display for a second monitor: one giant card per
    teammate with the player name, a massive percentage, and a colored
    meter. No tables, no scoreboard, no extra stats. Readable from across
    the room."""
    return """
      <div class="boost-hud" id="boost-stage" style="display:none">
        <div class="boost-hud-grid" id="boost-myteam-body"></div>
      </div>

      <div class="card boost-idle" id="boost-idle">
        <div class="empty">
          <h2 style="margin:0 0 8px">No active match</h2>
          <p class="caption" style="margin:0 0 14px">
            Start a match (or jump into free play) in Rocket League and
            live boost values for you and your teammates will fill this
            screen.
          </p>
        </div>
      </div>
    """


def _opponents_page_html(store, self_primary_id, self_name, *,
                          include_bots: bool = False,
                          mode_filter: int | None = None,
                          platform_filter: str | None = None,
                          limit: int = 50, is_self: bool = True) -> str:
    """Head-to-head opponent records. Lists every player you've faced on the
    OTHER team at least twice, with your W/L vs them, total goals for/against,
    and the last time you played them."""
    if not store or (not self_primary_id and not self_name):
        return _page_wrap("Opponents", '<div class="empty">No player configured.</div>', active="opponents")

    self_clause = "x.primary_id = ?" if self_primary_id else "x.name = ?"
    self_clause_me = "me.primary_id = ?" if self_primary_id else "me.name = ?"
    self_arg = self_primary_id or self_name
    bots_clause = "" if include_bots else " AND opp.is_bot = 0"
    mode_clause = ""
    mode_args: list = []
    if mode_filter is not None:
        mode_clause = """ AND (
            SELECT MAX(c) FROM (
                SELECT team_num, COUNT(*) AS c
                FROM match_player_stats
                WHERE match_id = m.id
                GROUP BY team_num
            )
        ) = ?"""
        mode_args = [mode_filter]
    # Opponent-platform filter: matches where the opposing team (relative to me)
    # fielded a player on this platform. Each of the 3 queries below has me + m
    # aliases, so the same clause + arg plug into all of them.
    platform_clause = ""
    platform_args: list = []
    if platform_filter:
        platform_clause = (" AND EXISTS (SELECT 1 FROM match_player_stats op2 "
                           "WHERE op2.match_id = m.id AND op2.team_num != me.team_num "
                           "AND op2.platform LIKE '%' || ? || '%')")
        platform_args = [platform_filter]

    with store._conn() as con:
        rows = con.execute(f"""
            SELECT
                opp.name                          AS opp_name,
                opp.primary_id                    AS opp_pid,
                opp.is_bot                        AS opp_bot,
                MIN(opp.platform)                 AS opp_platform,
                COUNT(*)                          AS matches,
                SUM(CASE WHEN me.team_num = m.winner_team_num THEN 1 ELSE 0 END) AS wins,
                SUM(opp.goals)                    AS opp_goals,
                SUM(me.goals)                     AS me_goals,
                SUM(opp.saves)                    AS opp_saves,
                SUM(opp.demos)                    AS opp_demos,
                MAX(m.started_at)                 AS last_played
            FROM match_player_stats me
            JOIN matches m              ON m.id = me.match_id
            JOIN match_player_stats opp ON opp.match_id = m.id
                                        AND opp.team_num != me.team_num
            JOIN match_player_stats x   ON x.match_id = me.match_id
                                        AND x.primary_id = me.primary_id
            WHERE {self_clause}{bots_clause}{mode_clause}{platform_clause}
            GROUP BY opp.name, opp.primary_id
            HAVING matches >= 1
            ORDER BY matches DESC, last_played DESC
            LIMIT ?
        """, (self_arg, *mode_args, *platform_args, limit)).fetchall()

        # Opposing TEAMS: aggregate per MATCH (one row per match, via me's single
        # row) so the match count isn't multiplied by the opponent roster size.
        # goals_for/against are the team scoreline -> real GA / goal difference.
        team_rows = con.execute(f"""
            SELECT
                opp_team,
                COUNT(*)         AS matches,
                SUM(won)         AS wins,
                SUM(gf)          AS goals_for,
                SUM(ga)          AS goals_against,
                MAX(last_played) AS last_played
            FROM (
                SELECT
                    CASE WHEN me.team_num = 0 THEN m.team1_name ELSE m.team0_name END AS opp_team,
                    CASE WHEN me.team_num = m.winner_team_num THEN 1 ELSE 0 END AS won,
                    CASE WHEN me.team_num = 0 THEN m.team0_score ELSE m.team1_score END AS gf,
                    CASE WHEN me.team_num = 0 THEN m.team1_score ELSE m.team0_score END AS ga,
                    m.started_at AS last_played
                FROM match_player_stats me
                JOIN matches m ON m.id = me.match_id
                WHERE {self_clause_me}{mode_clause}{platform_clause}
            )
            WHERE opp_team NOT IN ('Blue', 'Orange', 'Home', 'Away', '')
            GROUP BY opp_team
            HAVING matches >= 1
            ORDER BY matches DESC, last_played DESC
            LIMIT ?
        """, (self_arg, *mode_args, *platform_args, limit)).fetchall()

        # Rosters they fielded, fetched separately (the opponent join would
        # otherwise re-inflate the per-match aggregation above).
        roster_map: dict[str, str] = {}
        if team_rows:
            for rr in con.execute(f"""
                SELECT
                    CASE WHEN me.team_num = 0 THEN m.team1_name ELSE m.team0_name END AS opp_team,
                    GROUP_CONCAT(DISTINCT opp.name) AS roster
                FROM match_player_stats me
                JOIN matches m ON m.id = me.match_id
                JOIN match_player_stats opp ON opp.match_id = m.id
                                            AND opp.team_num != me.team_num
                WHERE {self_clause_me}{bots_clause}{mode_clause}{platform_clause}
                  AND CASE WHEN me.team_num = 0 THEN m.team1_name ELSE m.team0_name END NOT IN ('Blue', 'Orange', 'Home', 'Away', '')
                GROUP BY opp_team
            """, (self_arg, *mode_args, *platform_args)).fetchall():
                roster_map[rr["opp_team"]] = rr["roster"] or ""

    if not rows:
        body_rows_html = '<div class="empty">No opponents yet.</div>'
        rows_html = ""
    else:
        cells: list[str] = []
        for r in rows:
            wins = r["wins"] or 0
            losses = (r["matches"] or 0) - wins
            wr = (wins / r["matches"] * 100) if r["matches"] else 0.0
            last_iso = datetime.fromtimestamp(r["last_played"]).isoformat()
            last_fallback = datetime.fromtimestamp(r["last_played"]).strftime("%b %d")
            opp_name = r["opp_name"] or "?"
            bot_chip = ' <span class="chip bot">BOT</span>' if r["opp_bot"] else ""
            href = f'/player/{quote(opp_name, safe="")}' if not r["opp_bot"] else "#"
            link_open = f'<a href="{href}" class="player-link">' if not r["opp_bot"] else "<span>"
            link_close = "</a>" if not r["opp_bot"] else "</span>"
            plat_icon = _platform_icon_html(r["opp_platform"], size=16)
            plat_cell = (
                f'<span class="plat-cell" title="{r["opp_platform"] or "unknown"}">'
                f'{plat_icon}</span>'
                if plat_icon else
                f'<span class="dim tnum">{r["opp_platform"] or "—"}</span>'
            )
            cells.append(f"""
              <tr class="row">
                <td>{link_open}<b>{html.escape(opp_name)}</b>{link_close}{bot_chip}</td>
                <td>{plat_cell}</td>
                <td class="num tnum"><b>{r['matches']}</b></td>
                <td class="num tnum">
                  <span class="{'good' if wins > losses else 'bad' if losses > wins else 'dim'}">
                    <b>{wins}</b>-<b>{losses}</b>
                  </span>
                </td>
                <td class="num tnum">{wr:.0f}%</td>
                <td class="num tnum">{r['me_goals']}</td>
                <td class="num tnum">{r['opp_goals']}</td>
                <td class="num tnum dim">{r['opp_saves']}</td>
                <td class="num tnum dim">{r['opp_demos']}</td>
                <td class="dim tnum"><time datetime="{last_iso}">{last_fallback}</time></td>
              </tr>
            """)
        rows_html = f"""
          <table class="history">
            <thead><tr>
              <th>Opponent</th>
              <th>Platform</th>
              <th class="num">Matches</th>
              <th class="num">W-L</th>
              <th class="num">Win rate</th>
              <th class="num">Goals for</th>
              <th class="num">Goals against</th>
              <th class="num">Their saves</th>
              <th class="num">Their demos</th>
              <th>Last played</th>
            </tr></thead>
            <tbody>{"".join(cells)}</tbody>
          </table>
        """
        body_rows_html = f'<div class="card" style="padding:0;overflow:hidden">{rows_html}</div>'

    # Opposing teams (clans) section
    if team_rows:
        team_cells = []
        for r in team_rows:
            wins = r["wins"] or 0
            losses = (r["matches"] or 0) - wins
            wr = (wins / r["matches"] * 100) if r["matches"] else 0.0
            roster = [n for n in (roster_map.get(r["opp_team"]) or "").split(",") if n]
            gf = r["goals_for"] or 0
            ga = r["goals_against"] or 0
            gd = gf - ga
            gd_str = f"+{gd}" if gd > 0 else str(gd)
            gd_cls = "good" if gd > 0 else "bad" if gd < 0 else "dim"
            roster_chips = " ".join(
                f'<span class="chip" style="font-size:10.5px">{html.escape(n)}</span>'
                for n in roster[:6]
            )
            if len(roster) > 6:
                roster_chips += f' <span class="dim">+{len(roster)-6} more</span>'
            last_iso = datetime.fromtimestamp(r["last_played"]).isoformat()
            last_fallback = datetime.fromtimestamp(r["last_played"]).strftime("%b %d")
            team_cells.append(f"""
              <tr class="row click" onclick="window.location='/club/{quote(r['opp_team'], safe='')}'">
                <td><a class="player-link" href="/club/{quote(r['opp_team'], safe='')}"><span class="club-name" title="{html.escape(r['opp_team'])}"><b>{html.escape(r['opp_team'])}</b></span></a></td>
                <td class="num tnum"><b>{r['matches']}</b></td>
                <td class="num tnum">
                  <span class="{'good' if wins > losses else 'bad' if losses > wins else 'dim'}">
                    <b>{wins}</b>-<b>{losses}</b>
                  </span>
                </td>
                <td class="num tnum">{wr:.0f}%</td>
                <td class="num tnum">{gf}</td>
                <td class="num tnum">{ga}</td>
                <td class="num tnum"><span class="{gd_cls}">{gd_str}</span></td>
                <td>{roster_chips}</td>
                <td class="dim tnum"><time datetime="{last_iso}">{last_fallback}</time></td>
              </tr>
            """)
        teams_html = f"""
          <div class="section-title" style="margin-top:24px">
            <span>Opposing clubs</span>
            <span class="dim" style="text-transform:none;letter-spacing:0">
              Grouped by their club name. Only named clubs are shown.
            </span>
          </div>
          <div class="card" style="padding:0;overflow:hidden">
            <table class="history">
              <thead><tr>
                <th>Club</th>
                <th class="num">Matches</th>
                <th class="num">W-L</th>
                <th class="num">Win rate</th>
                <th class="num" title="Goals for">GF</th>
                <th class="num" title="Goals against">GA</th>
                <th class="num" title="Goal difference">GD</th>
                <th>Roster faced</th>
                <th>Last played</th>
              </tr></thead>
              <tbody>{"".join(team_cells)}</tbody>
            </table>
          </div>
        """
    else:
        teams_html = ""

    # Filter toolbar (mode + bots)
    def _url(mode_=..., bots_=...) -> str:
        m = mode_filter if mode_ is ... else mode_
        b = include_bots if bots_ is ... else bots_
        parts = []
        if m is not None: parts.append(f"mode={m}")
        if b: parts.append("include_bots=1")
        return "/opponents" + ("?" + "&".join(parts) if parts else "")
    def mode_chip(label: str, m: int | None) -> str:
        active = " active" if mode_filter == m else ""
        return f'<a class="{active.strip()}" href="{_url(mode_=m)}">{label}</a>'
    bots_chip_state = "active" if not include_bots else ""

    body = f"""
      <div class="page-head">
        <div>
          <h1>{"Opponents" if is_self else html.escape(self_name or "") + " — opponents"}</h1>
          <div class="sub">{"Every player faced." if is_self else "Every player " + html.escape(self_name or "") + " faced."} Head-to-head records, last meeting,
          total goals exchanged. Repeats sit at the top.</div>
        </div>
      </div>

      <div class="toolbar">
        <div style="margin-left:auto;font-size:12px;color:var(--text-dim)">
          {len(rows)} opponent{'s' if len(rows) != 1 else ''}
        </div>
      </div>

      {body_rows_html}

      {teams_html}
    """
    return _page_wrap("Opponents", body, active="opponents")


def _club_detail_html(store, club_name: str,
                       self_primary_id: str | None, self_name: str | None,
                       *, include_bots: bool = False,
                       mode_filter: int | None = None,
                       window_days: int | None = None) -> str:
    """Drill-down view for a single opposing club: every match we have played
    them, plus their roster aggregated across those matches."""
    if not store or not club_name:
        return _page_wrap(club_name or "Club", "<div class='empty'>No club specified.</div>", active="clan")

    # Build extra clauses (mode + window + bots)
    extra: list[str] = []
    if not include_bots:
        extra.append(
            "NOT EXISTS (SELECT 1 FROM match_player_stats x WHERE x.match_id = m.id AND x.is_bot = 1)"
        )
    if mode_filter is not None:
        extra.append(f"""(SELECT MAX(c) FROM (
            SELECT team_num, COUNT(*) AS c FROM match_player_stats
            WHERE match_id = m.id GROUP BY team_num
        )) = {int(mode_filter)}""")
    if window_days and window_days > 0:
        import time as _time
        extra.append(f"m.started_at >= {_time.time() - window_days * 86400}")
    extra_sql = " AND " + " AND ".join(extra) if extra else ""

    with store._conn() as con:
        # Matches where this club's name was the OPPOSING team to "me" (or any
        # configured self). Identify "me" by primary_id or name fallback.
        if self_primary_id:
            me_where = "me.primary_id = ?"
            me_arg = self_primary_id
        elif self_name:
            me_where = "me.name = ?"
            me_arg = self_name
        else:
            return _page_wrap("Club", "<div class='empty'>No player configured.</div>", active="clan")

        rows = con.execute(f"""
            SELECT m.id, m.started_at, m.team0_name, m.team1_name,
                   m.team0_score, m.team1_score, m.winner_team_num, m.arena,
                   me.team_num AS my_team
            FROM matches m
            JOIN match_player_stats me ON me.match_id = m.id
            WHERE {me_where}
              AND CASE WHEN me.team_num = 0 THEN m.team1_name ELSE m.team0_name END = ?
              {extra_sql}
            ORDER BY m.started_at DESC
        """, (me_arg, club_name)).fetchall()

        if not rows:
            body = f"""
              <div class="page-head">
                <div>
                  <h1>{html.escape(club_name)}</h1>
                  <div class="sub">No matches found vs <b>{html.escape(club_name)}</b>.</div>
                </div>
              </div>
              <div class="empty">Nothing to show.</div>
            """
            return _page_wrap(club_name, body, active="clan")

        wins = sum(1 for r in rows if r["my_team"] == r["winner_team_num"])
        losses = len(rows) - wins
        win_pct = wins / len(rows) * 100

        # Roster: every player on the OPPOSING side in those matches
        match_ids = [r["id"] for r in rows]
        ph = ",".join("?" * len(match_ids))
        roster = con.execute(f"""
            SELECT mps.name, mps.primary_id, mps.is_bot, mps.platform,
                   COUNT(*) AS n,
                   SUM(mps.goals)   AS goals,
                   SUM(mps.assists) AS assists,
                   SUM(mps.saves)   AS saves,
                   SUM(mps.shots)   AS shots,
                   SUM(mps.demos)   AS demos,
                   SUM(mps.score)   AS score,
                   SUM(mps.is_mvp)  AS mvps
            FROM match_player_stats mps
            JOIN matches m ON m.id = mps.match_id
            JOIN match_player_stats me ON me.match_id = m.id AND {me_where}
            WHERE m.id IN ({ph})
              AND mps.team_num != me.team_num
            GROUP BY mps.name, mps.primary_id
            ORDER BY n DESC, score DESC
        """, (me_arg, *match_ids)).fetchall()

    # Render
    roster_rows = []
    for rank, r in enumerate(roster, 1):
        bot = ' <span class="chip bot">BOT</span>' if r["is_bot"] else ""
        href = f"/player/{quote(r['name'], safe='')}" if not r["is_bot"] else "#"
        link_open = f'<a href="{href}" class="player-link">' if not r["is_bot"] else "<span>"
        link_close = "</a>" if not r["is_bot"] else "</span>"
        roster_rows.append(f"""
          <tr class="row">
            <td class="num tnum rank">{rank}</td>
            <td>{link_open}<b>{html.escape(r['name'])}</b>{link_close}{bot}</td>
            <td class="dim">{r['platform'] or 'n/a'}</td>
            <td class="num tnum"><b>{r['n']}</b></td>
            <td class="num tnum">{r['score'] or 0}</td>
            <td class="num tnum">{r['goals'] or 0}</td>
            <td class="num tnum">{r['assists'] or 0}</td>
            <td class="num tnum">{r['saves'] or 0}</td>
            <td class="num tnum">{r['shots'] or 0}</td>
            <td class="num tnum">{r['demos'] or 0}</td>
            <td class="num tnum">{r['mvps'] or 0}</td>
          </tr>
        """)
    match_rows = []
    for r in rows:
        won = r["my_team"] == r["winner_team_num"]
        ts_iso = datetime.fromtimestamp(r["started_at"]).isoformat()
        ts_fall = datetime.fromtimestamp(r["started_at"]).strftime("%b %d, %Y")
        my_score = r["team0_score"] if r["my_team"] == 0 else r["team1_score"]
        their_score = r["team1_score"] if r["my_team"] == 0 else r["team0_score"]
        my_team_name = (r["team0_name"] if r["my_team"] == 0 else r["team1_name"]) or "us"
        match_rows.append(f"""
          <tr class="row click match-row {'win' if won else 'loss'}" onclick="window.location='/match/{quote(r['id'], safe='')}'">
            <td><span class="badge {'win' if won else 'loss'}">{'W' if won else 'L'}</span></td>
            <td class="dim tnum"><time datetime="{ts_iso}">{ts_fall}</time></td>
            <td class="score-cell"><b class="tnum">{my_score}</b> <span class="dim">vs</span> <b class="tnum">{their_score}</b></td>
            <td class="dim"><span class="club-name" title="{html.escape(my_team_name)}">{html.escape(my_team_name)}</span></td>
          </tr>
        """)

    body = f"""
      <div class="page-head">
        <div>
          <h1><span class="club-name" title="{html.escape(club_name)}">{html.escape(club_name)}</span></h1>
          <div class="sub">Head-to-head record across {len(rows)} recorded match{'es' if len(rows) != 1 else ''}.</div>
        </div>
      </div>

      <div class="kpi-row">
        <div class="kpi primary">
          <div class="kpi-label">Head-to-head</div>
          <div class="kpi-value tnum">{wins}-{losses}</div>
          <div class="kpi-foot">{win_pct:.0f}% win rate</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">Matches</div>
          <div class="kpi-value tnum">{len(rows)}</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">Roster size</div>
          <div class="kpi-value tnum">{len(roster)}</div>
        </div>
      </div>

      <div class="card" style="margin-top:14px;padding:0;overflow:hidden">
        <div class="section-title" style="padding:14px 18px 6px">
          <span>Their roster</span>
          <span class="dim" style="text-transform:none;letter-spacing:0">
            Every player they fielded across the matches above.
          </span>
        </div>
        <table class="history">
          <thead><tr>
            <th class="num rank">#</th>
            <th>Player</th>
            <th>Platform</th>
            <th class="num">Matches</th>
            <th class="num">Score</th>
            <th class="num">Goals</th>
            <th class="num">Assists</th>
            <th class="num">Saves</th>
            <th class="num">Shots</th>
            <th class="num">Demos</th>
            <th class="num">MVPs</th>
          </tr></thead>
          <tbody>{"".join(roster_rows)}</tbody>
        </table>
      </div>

      <div class="card" style="margin-top:14px;padding:0;overflow:hidden">
        <div class="section-title" style="padding:14px 18px 6px">
          <span>Recorded matches</span>
        </div>
        <table class="history">
          <thead><tr>
            <th></th>
            <th>When</th>
            <th>Score</th>
            <th>Our side name</th>
          </tr></thead>
          <tbody>{"".join(match_rows)}</tbody>
        </table>
      </div>
    """
    return _page_wrap(club_name, body, active="clan")


def _clan_page_html(store, members: list[str], *, self_name: str | None = None,
                    include_bots: bool = False,
                    mode_filter: int | None = None,
                    window_days: int | None = None) -> str:
    """Aggregated stats for a designated clan of player names. A match counts
    as a 'clan match' when 2+ members are on the same team. Renders combined
    W-L, top contributors, lifetime touch heatmap, and recent clan matches."""
    from urllib.parse import quote
    from .analytics import _lifetime_row

    bot_filter = "" if include_bots else "WHERE COALESCE(max_bot, 0) = 0"
    with store._conn() as con:
        all_players_rows = con.execute(f"""
            SELECT name, MAX(is_bot) AS max_bot, COUNT(*) AS n
            FROM match_player_stats
            GROUP BY name
        """).fetchall()
        all_players = sorted(
            [dict(r) for r in all_players_rows if include_bots or not r["max_bot"]],
            key=lambda r: -r["n"],
        )

    members = [m for m in members if m]
    if not members:
        body = """
          <div class="page-head">
            <div>
              <h1>Opposing clubs</h1>
              <div class="sub">No player configured. Set RL_PLAYER_NAME in .env to populate this page.</div>
            </div>
          </div>
          <div class="empty">Nothing to show yet.</div>
        """
        return _page_wrap("Clubs", body, active="clan")

    # ---- Find "clan matches" -----------------------------------------------
    # A match counts if 2+ members are on the same team. Pull all matches that
    # any member appeared in, then filter to ones with sufficient overlap.
    placeholders = ",".join("?" * len(members))
    # Optional mode + window filters from sidebar
    mode_clause = ""
    if mode_filter is not None:
        mode_clause = f"""
            AND (SELECT MAX(c) FROM (
                SELECT team_num, COUNT(*) AS c FROM match_player_stats
                WHERE match_id = m.id GROUP BY team_num
            )) = {int(mode_filter)}
        """
    window_clause = ""
    if window_days and window_days > 0:
        import time as _time
        cutoff = _time.time() - window_days * 86400
        window_clause = f" AND m.started_at >= {cutoff}"
    with store._conn() as con:
        rows = con.execute(f"""
            SELECT mps.match_id, mps.name, mps.team_num,
                   mps.goals, mps.assists, mps.saves, mps.shots,
                   mps.demos, mps.score, mps.is_mvp, mps.is_bot,
                   mps.touches, mps.ticks_total, mps.boost_used,
                   m.started_at, m.arena, m.is_online,
                   m.team0_score, m.team1_score,
                   m.team0_name, m.team1_name, m.winner_team_num
            FROM match_player_stats mps
            JOIN matches m ON m.id = mps.match_id
            WHERE mps.name IN ({placeholders})
              AND ({"NOT EXISTS (SELECT 1 FROM match_player_stats x WHERE x.match_id = m.id AND x.is_bot = 1)" if not include_bots else "1=1"})
              {mode_clause}{window_clause}
            ORDER BY m.started_at DESC
        """, tuple(members)).fetchall()

    # Group by (match_id, team_num): which clan members were on each team?
    from collections import defaultdict
    by_match: dict[str, dict] = {}
    for r in rows:
        mid = r["match_id"]
        by_match.setdefault(mid, {"teams": defaultdict(list), "meta": r})
        by_match[mid]["teams"][r["team_num"]].append(r)

    # Threshold: with 1 member configured, every match counts (so we get a
    # full opposing-clubs view from a single player's perspective). With 2+
    # members specified, require 2+ on the same team (real "club match").
    min_overlap = 1 if len(members) == 1 else 2
    clan_matches: list[dict] = []
    for mid, d in by_match.items():
        team_counts = {t: len(rs) for t, rs in d["teams"].items()}
        clan_team_num, count = max(team_counts.items(), key=lambda kv: kv[1])
        if count < min_overlap:
            continue
        meta = d["meta"]
        clan_team_won = (clan_team_num == meta["winner_team_num"])
        clan_members_in_match = d["teams"][clan_team_num]
        clan_matches.append({
            "id": mid,
            "started_at": meta["started_at"],
            "arena": meta["arena"],
            "is_online": meta["is_online"],
            "team_num": clan_team_num,
            "team0_name": meta["team0_name"],
            "team1_name": meta["team1_name"],
            "team0_score": meta["team0_score"],
            "team1_score": meta["team1_score"],
            "winner_team_num": meta["winner_team_num"],
            "won": clan_team_won,
            "members": clan_members_in_match,
        })
    clan_matches.sort(key=lambda m: -m["started_at"])

    n_matches = len(clan_matches)
    wins = sum(1 for m in clan_matches if m["won"])
    losses = n_matches - wins
    win_pct = (wins / n_matches * 100) if n_matches else 0.0

    # Per-member rollup of stats across CLAN matches only.
    member_totals: dict[str, dict] = {
        m: {"matches": 0, "wins": 0, "goals": 0, "assists": 0, "saves": 0,
            "shots": 0, "demos": 0, "score": 0, "mvps": 0,
            "touches": 0, "ticks": 0, "boost_used": 0}
        for m in members
    }
    for cm in clan_matches:
        for r in cm["members"]:
            n = r["name"]
            if n not in member_totals:
                continue
            t = member_totals[n]
            t["matches"] += 1
            if cm["won"]:
                t["wins"] += 1
            t["goals"]      += r["goals"]      or 0
            t["assists"]    += r["assists"]    or 0
            t["saves"]      += r["saves"]      or 0
            t["shots"]      += r["shots"]      or 0
            t["demos"]      += r["demos"]      or 0
            t["score"]      += r["score"]      or 0
            t["mvps"]       += 1 if r["is_mvp"] else 0
            t["touches"]    += r["touches"]    or 0
            t["ticks"]      += r["ticks_total"] or 0
            t["boost_used"] += r["boost_used"] or 0

    # Aggregate goals / assists / saves / etc. for the clan.
    clan_total_g = sum(t["goals"]   for t in member_totals.values())
    clan_total_a = sum(t["assists"] for t in member_totals.values())
    clan_total_s = sum(t["saves"]   for t in member_totals.values())
    clan_total_sh = sum(t["shots"]  for t in member_totals.values())
    clan_total_d = sum(t["demos"]   for t in member_totals.values())
    clan_total_mvp = sum(t["mvps"]  for t in member_totals.values())

    # Per-member breakdown table — each member's stats in club matches, so the
    # club page shows "everyone's stats all in one" (sorted by score).
    _mrows = []
    for m in sorted(members, key=lambda nm: -member_totals.get(nm, {}).get("score", 0)):
        t = member_totals.get(m, {})
        mt = t.get("matches", 0)
        if not mt:
            continue
        w = t["wins"]
        _mrows.append(
            f'<tr class="row"><td><a class="player-link" href="/player/{quote(m, safe="")}">'
            f'{html.escape(m)}</a></td>'
            f'<td class="num tnum">{mt}</td>'
            f'<td class="num tnum"><b>{w}</b>-{mt - w}</td>'
            f'<td class="num tnum">{t["goals"]}</td><td class="num tnum">{t["assists"]}</td>'
            f'<td class="num tnum">{t["saves"]}</td><td class="num tnum">{t["shots"]}</td>'
            f'<td class="num tnum">{t["demos"]}</td><td class="num tnum">{t["mvps"]}</td></tr>'
        )
    members_table = (
        '<div class="card" style="margin-top:14px;padding:0;overflow:hidden">'
        '<div class="section-title" style="padding:14px 18px 6px"><span>Club members</span>'
        '<span class="dim" style="text-transform:none;letter-spacing:0">'
        'Each member&rsquo;s stats in club matches.</span></div>'
        '<table class="history"><thead><tr><th>Member</th>'
        f'<th class="num">Matches</th><th class="num">W-L</th>'
        f'<th class="num">{_stat_icon_html("Goals")}Goals</th>'
        f'<th class="num">{_stat_icon_html("Assists")}Assists</th>'
        f'<th class="num">{_stat_icon_html("Saves")}Saves</th>'
        f'<th class="num">{_stat_icon_html("Shots")}Shots</th>'
        f'<th class="num">{_stat_icon_html("Demos")}Demos</th>'
        f'<th class="num">{_stat_icon_html("MVP")}MVPs</th></tr></thead>'
        f'<tbody>{"".join(_mrows)}</tbody></table></div>'
    ) if _mrows else ""

    # ---- Rivalries: us vs other teams --------------------------------------
    # For each clan-match, find our team's name and the opposing team's name.
    # Group by opposing team name. Skip default "Blue"/"Orange" names — only
    # named clans count as rivals.
    our_name_counter: dict[str, int] = {}
    rivals: dict[str, dict] = {}
    for cm in clan_matches:
        if cm["team_num"] == 0:
            our_n, their_n = cm["team0_name"], cm["team1_name"]
        else:
            our_n, their_n = cm["team1_name"], cm["team0_name"]
        if our_n:
            our_name_counter[our_n] = our_name_counter.get(our_n, 0) + 1
        if their_n in ("", "Blue", "Orange", "Home", "Away"):
            continue
        if their_n not in rivals:
            rivals[their_n] = {
                "name": their_n, "matches": 0, "wins": 0,
                "last": 0, "their_goals": 0, "our_goals": 0,
            }
        r = rivals[their_n]
        r["matches"] += 1
        if cm["won"]:
            r["wins"] += 1
        r["last"] = max(r["last"], cm["started_at"])
        # Team scores in clan_matches are full team scores
        if cm["team_num"] == 0:
            r["our_goals"]   += cm["team0_score"] or 0
            r["their_goals"] += cm["team1_score"] or 0
        else:
            r["our_goals"]   += cm["team1_score"] or 0
            r["their_goals"] += cm["team0_score"] or 0
    our_team_name = (max(our_name_counter.items(), key=lambda kv: kv[1])[0]
                      if our_name_counter else "")
    rival_rows = sorted(rivals.values(), key=lambda r: (-r["matches"], -r["last"]))

    # Member leaderboard rows sorted by score.
    leader_rows = sorted(member_totals.items(),
                         key=lambda kv: -kv[1]["score"])
    leader_html = []
    for nm, t in leader_rows:
        if t["matches"] == 0:
            continue
        bpm = ((t["boost_used"] * 1800.0 / t["ticks"])
               if t["ticks"] >= 1000 else None)
        bpm_str = f"{bpm:.0f}" if bpm is not None else "--"
        href = f"/player/{quote(nm, safe='')}"
        win_str = f"{t['wins']}-{t['matches'] - t['wins']}"
        leader_html.append(f"""
          <tr>
            <td><a href="{href}" class="player-link">{nm}</a></td>
            <td class="num tnum">{t['matches']}</td>
            <td class="num tnum">{win_str}</td>
            <td class="num tnum"><b>{t['score']}</b></td>
            <td class="num tnum">{t['goals']}</td>
            <td class="num tnum">{t['assists']}</td>
            <td class="num tnum">{t['saves']}</td>
            <td class="num tnum">{t['shots']}</td>
            <td class="num tnum">{t['demos']}</td>
            <td class="num tnum">{t['mvps']}</td>
            <td class="num tnum">{bpm_str}</td>
          </tr>
        """)

    # Combined SHOT heatmap: union of every member's goal-shot locations (where
    # the club scores from). The merged TOUCH heatmap was a useless dense blob;
    # shots are sparse + meaningful.
    merged_shots = []
    for nm in members:
        sd = _lifetime_shot_data(store, nm)
        merged_shots.extend(sd.get("ball_track") or [])
    combined = {
        "ball_track": merged_shots,
        "svg": {"vb_w": 880, "vb_h": 380, "pitch_w": 800, "pitch_h": 320,
                "pad_x": 40.0, "pad_y": 30.0},
    }
    heatmap_svg = _ball_heatmap_svg(combined, key="clan", exclude_center=False) if merged_shots else ""
    heatmap_html = (
        f"""
          <div class="card" style="margin-top:14px">
            <div class="section-title">
              <span>Where the club scores from</span>
              <span class="dim" style="text-transform:none;letter-spacing:0">
                Shot location of {len(merged_shots)} goal{'s' if len(merged_shots) != 1 else ''}
                across every match these {len(members)} players appeared in,
                rotated so the club attacks &#8594; (right).
              </span>
            </div>
            <div class="hm-wrap">{heatmap_svg}</div>
          </div>
        """ if heatmap_svg else ""
    )

    # Recent clan matches list (last 20).
    mvps_lookup = _match_mvp_lookup(store, [cm["id"] for cm in clan_matches[:20]])
    match_rows = []
    for cm in clan_matches[:20]:
        ts_iso = datetime.fromtimestamp(cm["started_at"]).isoformat()
        ts_fallback = datetime.fromtimestamp(cm["started_at"]).strftime("%b %d, %Y")
        arena = _arena_nice(cm["arena"] or "")
        won = cm["won"]
        member_chips = " ".join(
            f'<span class="clan-member-chip">{html.escape(r["name"])}</span>'
            for r in cm["members"]
        )
        match_rows.append(f"""
          <tr class="row click match-row {'win' if won else 'loss'}"
              onclick="window.location='/match/{quote(cm['id'], safe='')}'">
            <td><span class="badge {'win' if won else 'loss'}">{'W' if won else 'L'}</span></td>
            <td class="dim tnum"><time datetime="{ts_iso}">{ts_fallback}</time></td>
            <td class="score-cell">
              <span class="score-team team-blue" title="{html.escape(cm['team0_name'])}">{html.escape(cm['team0_name'])}</span>
              <b class="tnum">{cm['team0_score']}</b>
              <span class="dim">-</span>
              <b class="tnum">{cm['team1_score']}</b>
              <span class="score-team team-orng" title="{html.escape(cm['team1_name'])}">{html.escape(cm['team1_name'])}</span>
            </td>
            <td class="dim">{arena}</td>
            <td class="clan-members-cell">{member_chips}</td>
            <td>{_mvp_cell_html(mvps_lookup.get(cm['id']))}</td>
          </tr>
        """)
    if not match_rows:
        recent_matches_html = '<div class="empty">No club matches found yet.</div>'
    else:
        recent_matches_html = f"""
          <table class="history">
            <thead><tr>
              <th></th>
              <th>When</th>
              <th>Score</th>
              <th>Arena</th>
              <th>Club members</th>
              <th>MVP</th>
            </tr></thead>
            <tbody>{"".join(match_rows)}</tbody>
          </table>
        """

    # Rivalries block
    rivals_cells = []
    for rank, r in enumerate(rival_rows[:30], 1):
        wn = r["wins"]; ls = r["matches"] - wn
        wr = (wn / r["matches"] * 100) if r["matches"] else 0
        diff = (r["our_goals"] or 0) - (r["their_goals"] or 0)
        diff_cls = "good" if diff > 0 else ("bad" if diff < 0 else "dim")
        diff_lbl = f"+{diff}" if diff > 0 else str(diff)
        last_iso = datetime.fromtimestamp(r["last"]).isoformat() if r["last"] else ""
        last_lbl = datetime.fromtimestamp(r["last"]).strftime("%b %d") if r["last"] else "—"
        from urllib.parse import quote as _q
        rivals_cells.append(f"""
          <tr class="row click" onclick="window.location='/club/{_q(r['name'], safe='')}'">
            <td class="num tnum rank">{rank}</td>
            <td><a class="player-link" href="/club/{_q(r['name'], safe='')}"><span class="club-name" title="{html.escape(r['name'])}"><b>{html.escape(r['name'])}</b></span></a></td>
            <td class="num tnum"><b>{r['matches']}</b></td>
            <td class="num tnum">
              <span class="{'good' if wn > ls else 'bad' if ls > wn else 'dim'}">
                <b>{wn}</b>-<b>{ls}</b>
              </span>
            </td>
            <td class="num tnum">{wr:.0f}%</td>
            <td class="num tnum">{r['our_goals']}</td>
            <td class="num tnum">{r['their_goals']}</td>
            <td class="num tnum {diff_cls}"><b>{diff_lbl}</b></td>
            <td class="dim tnum"><time datetime="{last_iso}">{last_lbl}</time></td>
          </tr>
        """)
    if rival_rows:
        rivals_html = f"""
          <div class="card" style="margin-top:14px;padding:0;overflow:hidden">
            <div class="section-title" style="padding:14px 18px 6px">
              <span>Rivalries</span>
              <span class="dim" style="text-transform:none;letter-spacing:0">
                Other named clubs matched up against.
              </span>
            </div>
            <table class="history">
              <thead><tr>
                <th class="num rank">#</th>
                <th>Club</th>
                <th class="num">Matches</th>
                <th class="num">W-L</th>
                <th class="num">Win rate</th>
                <th class="num">Goals for</th>
                <th class="num">Goals against</th>
                <th class="num">+/−</th>
                <th>Last played</th>
              </tr></thead>
              <tbody>{"".join(rivals_cells)}</tbody>
            </table>
          </div>
        """
    else:
        rivals_html = ""

    _mnames = ", ".join(html.escape(m) for m in members[:4])
    if len(members) > 4:
        _mnames += f" +{len(members) - 4}"
    body = f"""
      <div class="page-head">
        <div>
          <h1>Our club</h1>
          <div class="sub">Combined record for <b>{_mnames}</b> when 2+ play together &mdash;
            and how we stack up against rival clubs below.</div>
        </div>
      </div>

      <div class="section-eyebrow">Our club</div>
      <div class="kpi-row">
        <div class="kpi primary">
          <div class="kpi-label">Record</div>
          <div class="kpi-value tnum">{wins}-{losses}</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">Win rate</div>
          <div class="kpi-value tnum">{win_pct:.0f}%</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">Matches</div>
          <div class="kpi-value tnum">{n_matches}</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">{_stat_icon_html("Goals")}Goals</div>
          <div class="kpi-value tnum">{clan_total_g}</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">{_stat_icon_html("Saves")}Saves</div>
          <div class="kpi-value tnum">{clan_total_s}</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">{_stat_icon_html("Shots")}Shots</div>
          <div class="kpi-value tnum">{clan_total_sh}</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">{_stat_icon_html("MVP")}MVPs</div>
          <div class="kpi-value tnum">{clan_total_mvp}</div>
        </div>
      </div>

      {members_table}

      {heatmap_html}

      <div class="card" style="margin-top:14px;padding:0;overflow:hidden">
        <div class="section-title" style="padding:14px 18px 6px">
          <span>Recent club matches</span>
          <span class="dim" style="text-transform:none;letter-spacing:0">
            Last 20 matches where 2+ members were on the same team.
          </span>
        </div>
        {recent_matches_html}
      </div>

      <div class="section-eyebrow" style="margin-top:24px">Opposing clubs</div>
      {rivals_html}
    """
    return _page_wrap("Clubs", body, active="clan")


_NO_SIDEBAR_PAGES = {"overlay", "about"}

# Per-page filter suppression: filters that don't make sense on that page.
# E.g. /clan (Team) is a multi-platform aggregate, so platform/bots don't help.
_PAGE_FILTER_SUPPRESS = {
    "clan":    {"platform", "include_bots"},   # team is multi-platform; bots irrelevant in team play
    "live":    {"platform", "window"},          # current match's context, time-fixed
    "history": set(),
    "players": set(),
    "compare": {"platform"},                    # platform meaningless across a comparison
    "opponents": set(),
    "dashboard": set(),
}

# Pages that DO show the sidebar (filter visibility controlled by _PAGE_FILTER_SUPPRESS):
#   /dashboard /players /history /compare /clan(/Team) /opponents /live
# Pages that DON'T:
#   /about (docs), /overlay (OBS embed)
#   /match/<id> opts out by passing `with_sidebar=False` to _page_wrap directly


def _filter_sidebar(active: str, force_hidden: bool = False) -> str:
    """Persistent left sidebar with global filters. Sections shown depend on
    the active page (see _PAGE_FILTER_SUPPRESS to hide filters that don't
    make sense, e.g. platform on team view since teams are multi-platform).

    Returns empty string when sidebar shouldn't render at all."""
    if force_hidden or active in _NO_SIDEBAR_PAGES:
        return ""
    suppress = _PAGE_FILTER_SUPPRESS.get(active, set())

    def section(filter_key: str, html: str) -> str:
        return "" if filter_key in suppress else html

    mode = section("mode", """
        <div class="sf-section">
          <div class="sf-title">Mode</div>
          <div class="sf-group" data-filter="mode">
            <a class="sf-chip" data-val="">All</a>
            <a class="sf-chip" data-val="1">1v1</a>
            <a class="sf-chip" data-val="2">2v2</a>
            <a class="sf-chip" data-val="3">3v3</a>
            <a class="sf-chip" data-val="4">4v4</a>
          </div>
        </div>
    """)
    # Platform marks: monochrome single-path brand glyphs (simple-icons),
    # vendored under overlay/icons/platforms/ and inlined so they inherit
    # currentColor and tint to the accent on hover/active. See SOURCES.md.
    ic_steam = _PLATFORM_ICONS["steam"]
    ic_epic = _PLATFORM_ICONS["epic"]
    ic_ps = _PLATFORM_ICONS["playstation"]
    ic_xbox = _PLATFORM_ICONS["xbox"]
    ic_switch = _PLATFORM_ICONS["switch"]
    platform = section("platform", f"""
        <div class="sf-section">
          <div class="sf-title">Platform</div>
          <div class="sf-tip">Matches where the other team had a player on this platform.</div>
          <div class="sf-group sf-platform" data-filter="platform">
            <a class="sf-chip" data-val="" title="All platforms">All</a>
            <a class="sf-chip sf-chip-ic" data-val="Steam" title="Steam">{ic_steam}</a>
            <a class="sf-chip sf-chip-ic" data-val="Epic" title="Epic">{ic_epic}</a>
            <a class="sf-chip sf-chip-ic" data-val="PS4" title="PlayStation">{ic_ps}</a>
            <a class="sf-chip sf-chip-ic" data-val="XboxOne" title="Xbox">{ic_xbox}</a>
            <a class="sf-chip sf-chip-ic" data-val="Switch" title="Switch">{ic_switch}</a>
          </div>
        </div>
    """)
    window = section("window", """
        <div class="sf-section">
          <div class="sf-title">Date</div>
          <div class="sf-group" data-filter="window">
            <a class="sf-chip" data-val="">All time</a>
            <a class="sf-chip" data-val="today">Today</a>
            <a class="sf-chip" data-val="7d">7 days</a>
            <a class="sf-chip" data-val="30d">30 days</a>
          </div>
        </div>
    """)
    bots = section("include_bots", """
        <div class="sf-section sf-bots-section">
          <div class="sf-title">Bots</div>
          <div class="sf-group" data-filter="include_bots">
            <a class="sf-chip" data-val="0">Hide</a>
            <a class="sf-chip" data-val="1">Show</a>
          </div>
        </div>
    """)
    # Sample size is only meaningful on the head-to-head compare, where it keeps
    # a high-volume player from skewing the result. Lives with the other filters.
    sample = """
        <div class="sf-section">
          <div class="sf-title">Sample</div>
          <div class="sf-tip">Most recent N games per player, so volume doesn't skew the compare.</div>
          <div class="sf-group" data-filter="last">
            <a class="sf-chip" data-val="20">Last 20</a>
            <a class="sf-chip" data-val="50">Last 50</a>
            <a class="sf-chip" data-val="100">Last 100</a>
            <a class="sf-chip" data-val="0">All</a>
          </div>
        </div>
    """ if active == "compare" else ""
    return f"""
      <aside class="side-filters" id="side-filters">
        {mode}{platform}{window}{sample}{bots}
        <div class="sf-foot">
          <a class="sf-clear" id="sf-clear">Clear all</a>
        </div>
      </aside>
    """


_SIDEBAR_FILTER_JS = """<script>
(function() {
  var sb = document.getElementById('side-filters');
  if (!sb) return;
  var KEYS = ['mode','include_bots','platform','window','last'];

  function getUrlParam(k) {
    try { return new URLSearchParams(location.search).get(k); } catch (e) { return null; }
  }
  function getStored(k) {
    try { return localStorage.getItem('chumstats-flt-' + k); } catch (e) { return null; }
  }
  function setStored(k, v) {
    try {
      if (v === null || v === '') localStorage.removeItem('chumstats-flt-' + k);
      else                        localStorage.setItem('chumstats-flt-' + k, v);
    } catch (e) {}
  }
  function effective(k) {
    var u = getUrlParam(k);
    if (u !== null) return u;
    return getStored(k) || '';
  }

  // Mark active chips based on effective state.
  KEYS.forEach(function(k) {
    var group = sb.querySelector('[data-filter="' + k + '"]');
    if (!group) return;
    var cur = effective(k);
    var defaultMap = { include_bots: '0', last: '20' };
    if (cur === '' && defaultMap[k] !== undefined) cur = defaultMap[k];
    Array.prototype.forEach.call(group.querySelectorAll('.sf-chip'), function(chip) {
      chip.classList.toggle('active', chip.dataset.val === cur);
      // Build the URL we'd navigate to if clicked: preserve OTHER params,
      // override this one (or remove if empty).
      var p = new URLSearchParams(location.search);
      if (chip.dataset.val === '') p.delete(k);
      else p.set(k, chip.dataset.val);
      // Drop "_path" cleanup
      var qs = p.toString();
      chip.href = location.pathname + (qs ? '?' + qs : '');
      chip.addEventListener('click', function(ev) {
        // 'last' is compare-only; don't persist it so it can't leak onto other
        // pages' URLs via the stored-filter redirect.
        if (k !== 'last') setStored(k, chip.dataset.val);
      });
    });
  });

  // Clear button: blow away localStorage filters + go to current path with no params.
  var clr = document.getElementById('sf-clear');
  if (clr) clr.addEventListener('click', function(ev) {
    ev.preventDefault();
    KEYS.forEach(function(k) { setStored(k, null); });
    location.href = location.pathname;
  });

  // Carry the active filters onto the main nav links so moving page-to-page
  // KEEPS and APPLIES them with no redirect round-trip (the common case).
  var carry = new URLSearchParams();
  KEYS.forEach(function(k) {
    if (k === 'last') return;            // compare-only; don't leak elsewhere
    var v = effective(k);
    if (v) carry.set(k, v);
  });
  Array.prototype.forEach.call(document.querySelectorAll('.topnav .navlink'), function(a) {
    try {
      var u = new URL(a.getAttribute('href'), location.origin);
      carry.forEach(function(val, key) { u.searchParams.set(key, val); });
      a.setAttribute('href', u.pathname + (u.search || ''));
    } catch (e) {}
  });

  // Fallback for non-nav entries (direct URL, player/match/club links): if the
  // URL lacks a filter the user has stored, apply it. The redirect writes the
  // param into the URL, so `added` is false on the reload -> self-guarding, no
  // session flag (the old flag fired ONCE per session, so filters never applied
  // after the first page — that was the "saves but doesn't apply" bug).
  var url = new URL(location.href);
  var added = false;
  KEYS.forEach(function(k) {
    if (k === 'last') return;
    if (!url.searchParams.get(k)) {
      var v = getStored(k);
      if (v) { url.searchParams.set(k, v); added = true; }
    }
  });
  if (added) { location.replace(url.toString()); }
})();
</script>"""


def _page_wrap(title: str, body_html: str, *, status: int = 200, active: str = "",
               with_sidebar: bool = True, friend_mode: bool = False) -> str:
    """Common HTML chrome. Pages can pass with_sidebar=False to opt out of the
    global filter rail (used by /match/<id> which is a single-match snapshot)."""
    sidebar = _filter_sidebar(active, force_hidden=not with_sidebar)
    body_cls = "with-sidebar" if sidebar else "no-sidebar"
    main_html = f'<main class="page-main">{body_html}</main>' if sidebar else body_html
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="color-scheme" content="dark light">
<title>Chumstats - {html.escape(title)}</title>
<link rel="icon" type="image/png" href="/static/brand/chum-logo.png">
<script>(function(){{try{{var t=localStorage.getItem('chumstats-theme')||((window.matchMedia&&window.matchMedia('(prefers-color-scheme: light)').matches)?'light':'dark');document.documentElement.setAttribute('data-theme',t);}}catch(e){{document.documentElement.setAttribute('data-theme','dark');}}}})();</script>
{_STYLE_TAG}
</head><body class="{body_cls}">
<div class="wrapper">
  {_nav(active, friend_mode=friend_mode)}
  <div class="page-layout">
    {sidebar}
    {main_html}
  </div>
</div>
{_THEME_SCRIPT}
{_LOCAL_TIME_SCRIPT}
{_SIDEBAR_FILTER_JS}
</body></html>"""


# Rewrite every <time datetime="ISO"> tag to a 12hr local-tz format. Each tag
# can opt out of date / time / tz with data-* attributes if it only wants
# part of the format. Falls back silently if Intl isn't available.
_LOCAL_TIME_SCRIPT = """<script>
(function() {
  function fmt(iso, mode) {
    try {
      var d = new Date(iso);
      if (isNaN(d.getTime())) return null;
      if (mode === 'date') {
        return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
      }
      if (mode === 'time') {
        return d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit', hour12: true });
      }
      // Default: date + 12hr local time + timezone short name.
      var date = d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
      var time = d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit', hour12: true, timeZoneName: 'short' });
      return date + ' at ' + time;
    } catch (e) { return null; }
  }
  document.querySelectorAll('time[datetime]').forEach(function(el) {
    var iso = el.getAttribute('datetime');
    var mode = el.dataset.mode || 'full';
    var pretty = fmt(iso, mode);
    if (pretty) el.textContent = pretty;
  });
})();
</script>"""


_LOGO_SVG = '''<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
  <!-- Compact RL mark: soccer ball on left, stylized car silhouette right -->
  <circle cx="7" cy="13" r="5" fill="#fff" stroke="#0a0d14" stroke-width="1.1"/>
  <polygon points="7,10 9.6,11.7 8.6,14.7 5.4,14.7 4.4,11.7" fill="#0a0d14"/>
  <g stroke="#0a0d14" stroke-width="0.9" stroke-linecap="round">
    <line x1="7" y1="10" x2="7" y2="7.5"/>
    <line x1="9.6" y1="11.7" x2="11.6" y2="10.5"/>
    <line x1="8.6" y1="14.7" x2="9.8" y2="17"/>
    <line x1="5.4" y1="14.7" x2="4.2" y2="17"/>
    <line x1="4.4" y1="11.7" x2="2.4" y2="10.5"/>
  </g>
  <path d="M11 17 L 12 14 L 15 13 L 18 11 L 21.5 11.5 L 22 14 L 22 17 Z"
        fill="#0a0d14" stroke="#0a0d14" stroke-width="0.6" stroke-linejoin="round"/>
  <circle cx="14" cy="17.4" r="1.6" fill="#0a0d14"/>
  <circle cx="14" cy="17.4" r="0.7" fill="#fff" opacity="0.6"/>
  <circle cx="20" cy="17.4" r="1.6" fill="#0a0d14"/>
  <circle cx="20" cy="17.4" r="0.7" fill="#fff" opacity="0.6"/>
</svg>'''

def _nav(active: str = "", friend_mode: bool = False) -> str:
    # Stat pages live in the main nav row. Utility links (OBS overlay, How it
    # works) move to the right-hand aside as separate buttons so they don't read
    # like another stat page.
    stat_items = [
        ("live",      "/live",          "Live"),
        ("history",   "/history",       "Matches"),
        ("players",   "/players",       "Players"),
        ("compare",   "/compare",       "Compare"),
        ("clan",      "/clan",          "Clubs"),
    ]
    # OBS overlay is a local/live-host tool, not a central-site page — it lives as
    # a button on the Live view, not the nav. Opponents is folded into Players
    # (same data), so it's no longer a top-level page.
    util_items = [("about", "/about", "How it works")]
    if friend_mode:
        # The friend's local server only serves the live view + OBS overlay.
        stat_items = [it for it in stat_items if it[0] == "live"]
        util_items = [("overlay", "/overlay", "OBS overlay")]
    if not _LIVE_AVAILABLE:
        # The central `serve` host has no RL ingest -> no live feed; drop the
        # dead 'Live' link (the /live route still exists for direct hits).
        stat_items = [it for it in stat_items if it[0] != "live"]
    # All-matches tracker (no "Me" home): the brand goes to the neutral splash.
    # Friend mode only serves /live, so it points there instead.
    brand_href = "/live" if friend_mode else "/"

    live_pip = ('<span class="live-pip off" id="live-pip" title="No active match">'
                '<span class="dot"></span><span id="live-pip-label">idle</span></span>'
                if _LIVE_AVAILABLE else "")

    def _navlink(key, href, label, base="navlink"):
        klass = f"{base} active" if key == active else base
        return f'<a class="{klass}" href="{href}">{label}</a>'

    parts = [_navlink(*it) for it in stat_items]
    util_parts = [_navlink(k, h, l, "navlink nav-util") for k, h, l in util_items]
    return f'''
<nav class="topnav">
  <a class="brand" href="{brand_href}">
    <span class="brand-logo"><img src="/static/brand/chum-logo.png" alt="Chumstats" /></span>
    <span>
      <div class="brand-name">Chumstats</div>
    </span>
  </a>
  <div class="navlinks">{"".join(parts)}</div>
  <div class="nav-aside">
    {"".join(util_parts)}
    {live_pip}
    <button id="theme-toggle" type="button" aria-label="Toggle theme">
      <span class="theme-icon" id="theme-icon"></span>
      <span id="theme-label">Dark</span>
    </button>
  </div>
</nav>
'''

_THEME_SCRIPT = """
<script>
(function () {
  var SUN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>';
  var MOON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
  function paint(theme) {
    var icon = document.getElementById('theme-icon');
    var label = document.getElementById('theme-label');
    if (icon)  icon.innerHTML = theme === 'dark' ? SUN : MOON;
    if (label) label.textContent = theme === 'dark' ? 'Light' : 'Dark';
  }
  function set(t) {
    document.documentElement.setAttribute('data-theme', t);
    try { localStorage.setItem('chumstats-theme', t); } catch (e) {}
    paint(t);
  }
  var saved = null;
  try { saved = localStorage.getItem('chumstats-theme'); } catch (e) {}
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
@import url('https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,600;12..96,700;12..96,800&family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;700&display=swap');

:root {
  --accent:        #ff7a18;
  --accent-2:      #ff4d2d;
  --accent-soft:   rgba(255, 122, 24, 0.12);
  --accent-line:   rgba(255, 122, 24, 0.32);

  --team-blue:     #2d7dff;
  --team-blue-soft:rgba(45, 125, 255, 0.14);
  --team-orng:     #ff7a18;
  --team-orng-soft:rgba(255, 122, 24, 0.14);

  --good:          #34d399;
  --bad:           #f87171;
  --warn:          #fbbf24;

  --bg:            #0a0d14;
  --bg-elev:       #0f131c;
  --card:          #131826;
  --card-2:        #1a2030;
  --card-hover:    #1c2336;
  --border:        rgba(255, 255, 255, 0.08);
  --border-strong: rgba(255, 255, 255, 0.14);
  --text:          #e8edf3;
  --text-dim:      #a5adba;
  --text-faint:    #8a93a0;

  /* Aliases kept for backwards-compatible inline styles in render code */
  --bg-card:   var(--card);
  --bg-hover:  var(--card-hover);
  --accent-bg: var(--accent-soft);
}

[data-theme="light"] {
  --bg:            #f1f3f7;
  --bg-elev:       #ffffff;
  --card:          #ffffff;
  --card-2:        #f7f9fc;
  --card-hover:    #f0f3f9;
  --border:        rgba(15, 23, 42, 0.08);
  --border-strong: rgba(15, 23, 42, 0.16);
  --text:          #0c1426;
  --text-dim:      #5a6577;
  --text-faint:    #95a0b3;
  --accent:        #ea580c;
  --accent-soft:   rgba(234, 88, 12, 0.10);
  --accent-line:   rgba(234, 88, 12, 0.30);
  --good:          #16a34a;
  --bad:           #dc2626;
  --team-blue-soft:rgba(45, 125, 255, 0.12);
  --team-orng-soft:rgba(234, 88, 12, 0.14);
}

* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0;
  background: var(--bg);
  color: var(--text);
  font-family: "Bricolage Grotesque", ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: 14.5px;
  line-height: 1.55;
  letter-spacing: -0.005em;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  transition: background 200ms ease, color 200ms ease;
  font-optical-sizing: auto;
  font-variation-settings: "wdth" 96;
}
body { min-height: 100vh; }

.mono { font-family: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace; }
.tnum {
  font-family: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace;
  font-variant-numeric: tabular-nums;
  font-feature-settings: "tnum";
  letter-spacing: -0.02em;
}
.num, table td.num, table th.num {
  font-family: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace;
  font-feature-settings: "tnum";
  letter-spacing: -0.02em;
}
.dim  { color: var(--text-dim); }
.faint{ color: var(--text-faint); }

/* Display headlines get Bricolage with a slightly narrower width axis
   for a sturdier, more sport-tracker feel. */
.display, h1, .page-head h1, .brand-name,
.match-hero .team-name,
.roster-head .roster-team,
.section-title span:first-child {
  font-family: "Bricolage Grotesque", ui-sans-serif, sans-serif;
  font-optical-sizing: auto;
  font-variation-settings: "wdth" 92;
}

/* Numeric chrome (scores, KPI values, team scores) lives in JetBrains Mono.
   Mono creates a second-font presence that reads "scoreboard" and breaks the
   single-font monotony. */
.match-hero .score-display,
.roster-head .roster-score,
.kpi .kpi-value,
.quick-stat .v,
.bh-center .sc,
.sess-wl,
.badge {
  font-family: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace;
  font-feature-settings: "tnum";
  letter-spacing: -0.04em;
}

/* App shell + nav */
.wrapper, .app-shell { max-width: 1680px; margin: 0 auto; padding: 0 24px 56px; }

/* Page layout with optional left filter sidebar */
.page-layout { display: block; }
.page-main { min-width: 0; }

/* Filters: a compact horizontal bar at the top of the content (ballchasing
   style). No left rail, so nothing can overlap content on the left. */
.side-filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 11px;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 7px 12px;
  margin: 0 0 16px;
}
.side-filters .sf-section { display: flex; align-items: center; gap: 5px; margin: 0; }
.side-filters .sf-title {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--text-faint);
  margin: 0;
  white-space: nowrap;
}
.side-filters .sf-tip { display: none; }  /* too verbose for the compact bar */
.side-filters .sf-group { display: flex; gap: 3px; }

/* Every chip gets the same height + same vertical/horizontal centering. */
.side-filters .sf-chip {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: 30px;
  padding: 0 6px;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text-dim);
  background: var(--bg);
  border: 1px solid var(--border);
  cursor: pointer;
  text-decoration: none;
  transition: color 120ms ease, border-color 120ms ease, background 120ms ease;
  line-height: 1;
  white-space: nowrap;
}
.side-filters .sf-chip:hover {
  color: var(--text);
  border-color: var(--border-strong);
}
.side-filters .sf-chip.active {
  color: var(--accent);
  background: var(--accent-soft);
  border-color: var(--accent-line);
}
/* Icon-only chips: SVG centered, same height as text chips */
.side-filters .sf-chip-ic { padding: 0; }
/* The platform icon files (steam.svg etc.) ship with a 512 viewBox, NO
   width/height, and NO class -- so without a hard CSS size on the SVG itself
   they balloon to ~512px (the "huge console icons" bug, worst when the sidebar
   goes full-width on narrow screens). Constrain the SVG directly. */
svg.plat-ic { width: 15px; height: 15px; flex: 0 0 auto; }
/* Platform icon chips: smaller logo, square box (same shape as the column). */
.side-filters .sf-chip-ic { display: inline-flex; align-items: center; justify-content: center;
  width: 30px; height: 30px; padding: 0; }
.side-filters .sf-chip-ic svg { display: block; width: 15px; height: 15px; }
.side-filters .sf-chip-ic:hover svg.plat-ic { color: var(--accent); }
.side-filters .sf-chip-ic.active svg.plat-ic { color: var(--accent); }
.side-filters .sf-platform .sf-chip[data-val=""] {
  font-size: 10.5px;
  letter-spacing: 0.04em;
}
.side-filters .sf-foot {
  margin: 0;
  padding: 0;
  border: none;
}
.side-filters .sf-clear {
  font-size: 11px;
  color: var(--text-dim);
  cursor: pointer;
  text-decoration: none;
}
.side-filters .sf-clear:hover { color: var(--accent); }
.topnav {
  display: grid;
  grid-template-columns: auto 1fr auto;
  align-items: center;
  gap: 16px;
  padding: 10px 0 8px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 18px;
}
.brand {
  display: flex; align-items: center; gap: 10px;
  text-decoration: none; color: var(--text);
  cursor: pointer;
}
.brand-logo {
  width: 34px; height: 34px;
  background: transparent;
  border-radius: 0;
  display: grid; place-items: center;
  position: relative;
  overflow: hidden;
}
.brand-logo img {
  width: 100%; height: 100%;
  object-fit: contain;
  position: relative; z-index: 1;
  filter: drop-shadow(0 1px 2px rgba(0,0,0,.35));
}
.brand-name {
  font-weight: 800; font-size: 14px; letter-spacing: -0.02em;
  line-height: 1;
}
.brand-sub  {
  font-size: 9px; font-weight: 600; letter-spacing: 0.12em;
  color: var(--text-faint); text-transform: uppercase;
  margin-top: 2px;
}

.navlinks, .nav-links {
  display: flex; align-items: center; gap: 1px; justify-content: center;
  font-size: 12.5px;
  flex-wrap: wrap;
}
.navlink, .nav-link {
  color: var(--text-dim);
  text-decoration: none;
  padding: 5px 10px;
  border-radius: 0;
  font-weight: 500;
  letter-spacing: -0.005em;
  cursor: pointer;
  transition: color 140ms ease, background 140ms ease;
  display: inline-flex; align-items: center; gap: 6px;
}
@media (max-width: 1200px) {
  .navlink, .nav-link { padding: 5px 8px; font-size: 12px; }
}
/* Narrow screens: stop the stat links from wrapping into a pile over the brand.
   Brand + controls on top; the links become one horizontally-scrollable row. */
@media (max-width: 760px) {
  .topnav {
    grid-template-columns: 1fr auto;
    grid-template-areas: "brand aside" "nav nav";
    gap: 8px 12px;
  }
  .topnav > .brand { grid-area: brand; }
  .topnav > .nav-aside { grid-area: aside; flex-wrap: wrap; justify-content: flex-end; }
  .topnav > .navlinks {
    grid-area: nav;
    flex-wrap: nowrap;
    overflow-x: auto;
    justify-content: flex-start;
    -webkit-overflow-scrolling: touch;
    scrollbar-width: none;
  }
  .topnav > .navlinks::-webkit-scrollbar { height: 0; }
  .topnav > .navlinks .navlink { flex: 0 0 auto; }
}
.navlink:hover, .nav-link:hover { color: var(--text); background: var(--card); }
.navlink.active, .nav-link.active {
  color: var(--accent);
  background: var(--accent-soft);
  font-weight: 600;
}

.nav-aside { display: flex; align-items: center; gap: 10px; }
#theme-toggle, .theme-toggle {
  display: inline-flex; align-items: center; gap: 8px;
  background: var(--card); color: var(--text-dim);
  border: 1px solid var(--border); border-radius: 0;
  padding: 7px 14px 7px 12px; cursor: pointer;
  font-family: inherit; font-size: 12px; font-weight: 600;
  letter-spacing: -0.005em;
  transition: all 150ms ease;
}
#theme-toggle:hover, .theme-toggle:hover { color: var(--accent); border-color: var(--accent-line); }
#theme-toggle svg, .theme-toggle svg { width: 14px; height: 14px; }
#theme-toggle .theme-icon { display: inline-flex; }

.live-pip {
  display: inline-flex; align-items: center; gap: 8px;
  background: var(--card); border: 1px solid var(--border);
  border-radius: 0;
  padding: 7px 14px 7px 12px;
  font-size: 11px; font-weight: 700; letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--text-dim);
}
.live-pip .dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--good);
  box-shadow: 0 0 0 0 rgba(52, 211, 153, 0.7);
  animation: pulse 1.6s ease-out infinite;
}
@keyframes pulse {
  0%   { box-shadow: 0 0 0 0    rgba(52, 211, 153, 0.55); }
  70%  { box-shadow: 0 0 0 8px  rgba(52, 211, 153, 0.0);  }
  100% { box-shadow: 0 0 0 0    rgba(52, 211, 153, 0.0);  }
}
.live-pip.off .dot { background: var(--text-faint); animation: none; }

/* Page chrome */
.page-head {
  display: flex; align-items: flex-end; justify-content: space-between;
  gap: 20px;
  margin: 8px 0 22px;
}
.page-head h1 {
  margin: 0;
  font-size: 30px; font-weight: 800; letter-spacing: -0.02em;
}
.page-head .sub {
  color: var(--text-dim); font-size: 13px; margin-top: 4px;
  max-width: 60ch;
}
.page-head .right { display: flex; gap: 8px; align-items: center; }

h1 { margin: 4px 0 8px 0; font-weight: 800; font-size: 28px; letter-spacing: -0.02em; }
h2 {
  margin: 0 0 12px 0;
  font-size: 11px; font-weight: 700;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.10em;
}
.caption { color: var(--text-dim); font-size: 13px; max-width: 65ch; }
.who { color: var(--text-dim); font-size: 13px; margin: -4px 0 18px 0; }

.eyebrow {
  font-size: 10px; font-weight: 700; letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--text-dim);
  margin: 0 0 14px;
}

/* Cards */
section, .card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 0;
  padding: 18px 20px;
  margin: 0 0 14px 0;
}
.card-pad-lg { padding: 24px; }
.section-title {
  display: flex; align-items: baseline; justify-content: space-between;
  margin: 0 0 14px;
  font-size: 12px; font-weight: 700; letter-spacing: 0.10em;
  text-transform: uppercase; color: var(--text-dim);
}
.section-title .extras { display: flex; gap: 10px; align-items: baseline; }
.see-all {
  font-size: 11px; font-weight: 600; letter-spacing: -0.005em;
  text-transform: none; color: var(--accent); cursor: pointer;
  text-decoration: none;
}
.see-all:hover { text-decoration: underline; }

/* Chips */
.chip {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 11px; font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.06em;
  padding: 3px 8px;
  border-radius: 0;
  border: 1px solid var(--border-strong);
  color: var(--text-dim);
  background: transparent;
}
.chip.win,  .chip.good { color: var(--good); border-color: rgba(52, 211, 153, 0.35); background: rgba(52, 211, 153, 0.10); }
.chip.loss, .chip.bad  { color: var(--bad);  border-color: rgba(248, 113, 113, 0.32); background: rgba(248, 113, 113, 0.08); }
.chip.mvp,  .mvp       { color: var(--accent); border-color: var(--accent-line); background: var(--accent-soft); display: inline-flex; padding: 3px 8px; border-radius: 0; font-size: 10px; font-weight: 800; letter-spacing: 0.06em; }
.chip.bot,  .tag       { color: var(--text-faint); border: 1px solid var(--border-strong); padding: 2px 6px; border-radius: 0; font-size: 9px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; }
.chip.blue { color: var(--team-blue); border-color: rgba(45,125,255,0.35); background: var(--team-blue-soft); }
.chip.orng { color: var(--team-orng); border-color: var(--accent-line);    background: var(--team-orng-soft); }

/* Badges (W/L blocks beside score) */
.badge {
  display: inline-grid; place-items: center;
  width: 28px; height: 24px;
  border-radius: 0;
  font-size: 12px; font-weight: 800; letter-spacing: 0.04em;
}
.badge.win  { background: rgba(52, 211, 153, 0.18); color: var(--good); }
.badge.loss { background: rgba(248, 113, 113, 0.15); color: var(--bad); }

/* Inline links */
.player-link {
  color: var(--text); text-decoration: none;
  font-weight: 600; letter-spacing: -0.005em;
  border-bottom: 1px dashed transparent;
  transition: border-color 120ms ease, color 120ms ease;
  cursor: pointer;
}
.player-link:hover { color: var(--accent); border-bottom-color: var(--accent-line); }
.player-link.self  { color: var(--accent); }

/* KPI tiles */
.section-eyebrow {
  font-size: 11px; font-weight: 800; letter-spacing: 0.1em; text-transform: uppercase;
  color: var(--text-faint); margin: 0 0 8px;
}
.kpi-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(168px, 1fr));
  gap: 10px;
  margin: 0 0 18px;
}
.kpi {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 0;
  padding: 14px 16px;
  position: relative;
  overflow: hidden;
  display: flex; flex-direction: column;
  justify-content: center; align-items: center;
  text-align: center;
  min-height: 80px;
}
.kpi .kpi-label {
  font-size: 10px; font-weight: 700; letter-spacing: 0.12em;
  text-transform: uppercase; color: var(--text-dim);
  margin-bottom: 4px;
}
.kpi .kpi-value {
  font-size: 26px; font-weight: 800; letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
  line-height: 1.1;
}
.kpi .kpi-foot {
  margin-top: 6px;
  font-size: 12px; color: var(--text-dim);
  font-variant-numeric: tabular-nums;
  display: flex; gap: 6px; align-items: center; justify-content: center;
}
.kpi.primary { border-color: var(--accent-line); background: linear-gradient(180deg, var(--accent-soft), transparent 60%), var(--card); }
.kpi.primary .kpi-label { color: var(--accent); }

.delta { font-weight: 700; }
.delta.up   { color: var(--good); }
.delta.down { color: var(--bad); }

/* Tables */
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { padding: 10px 12px; text-align: left; vertical-align: middle; }
th {
  color: var(--text-dim); font-weight: 600; font-size: 10px;
  letter-spacing: 0.10em; text-transform: uppercase;
  border-bottom: 1px solid var(--border);
}
tr { border-bottom: 1px solid var(--border); }
tr:last-child { border-bottom: none; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
th.num { text-align: right; }

table.tight th { padding: 6px 10px; font-size: 9px; }
table.tight td { padding: 7px 10px; font-size: 12.5px; }

table.history tr.row.click,
table.history tr.match-row { cursor: pointer; transition: background 100ms ease; }
table.history tr.row.click:hover,
table.history tr.match-row:hover { background: var(--card-hover); }
.row td.score-cell, .match-row td.score-cell {
  font-weight: 500;
  min-width: 280px;
  max-width: 420px;
  width: 420px;
}
.row .score-cell b, .match-row .score-cell b { font-weight: 800; }
.row.win  td.score-cell .winner, .match-row.win  td.score-cell .winner { color: var(--good); }
.row.loss td.score-cell .winner, .match-row.loss td.score-cell .winner { color: var(--bad); }
.row td.dim, .match-row td.dim { color: var(--text-dim); font-size: 12px; }

/* Kickoff outcomes card on /match/<id> */
.kickoff-card .kickoff-summary {
  font-size: 14px;
  margin: 6px 0 4px;
}
.kickoff-card td.team-blue { color: var(--team-blue); }
.kickoff-card td.team-orng { color: var(--team-orng); }

/* Long club / team names: truncate with ellipsis, allow horizontal scroll
   on focus/hover so the full name is readable. Apply to opposing-team cells
   in the history table and rivalries / opponents tables. */
.club-name {
  display: inline-block;
  max-width: 200px;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  vertical-align: middle;
}
.club-name:hover, .club-name:focus {
  overflow-x: auto;
  text-overflow: clip;
}
.row .vs-team {
  display: inline-block;
  max-width: 180px;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  vertical-align: middle;
}
.row .vs-team:hover { overflow-x: auto; text-overflow: clip; }

/* Pre-match scouting card on /live (renders on match_start) */
.scouting-card { margin-bottom: 14px; }
.scouting-card .scout-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  margin-top: 8px;
}
.scouting-card .scout-team {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  background: var(--card-2);
  border-left-width: 4px;
}
.scouting-card .scout-team.team-blue { border-left-color: var(--team-blue); }
.scouting-card .scout-team.team-orng { border-left-color: var(--team-orng); }
.scouting-card .scout-row {
  display: grid;
  grid-template-columns: minmax(120px, 1fr) auto 1fr;
  align-items: center;
  gap: 10px;
}
.scouting-card .scout-name {
  font-weight: 700;
  font-size: 13px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.scouting-card .scout-form { display: inline-flex; gap: 3px; align-items: center; }
.scouting-card .form-dot {
  width: 9px;
  height: 9px;
  display: inline-block;
}
.scouting-card .form-dot.form-w { background: var(--good); }
.scouting-card .form-dot.form-l { background: var(--bad); }
.scouting-card .scout-meta { font-size: 11.5px; text-align: right; }

@media (max-width: 900px) {
  .scouting-card .scout-grid { grid-template-columns: 1fr; }
  .scouting-card .scout-row { grid-template-columns: 1fr; gap: 4px; }
  .scouting-card .scout-meta { text-align: left; }
}

/* Personal insights card on /match/<id> */
.insights-card .insights-list {
  list-style: none;
  margin: 8px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.insights-card .insight-row {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 10px 12px;
  background: var(--card-2);
  border: 1px solid var(--border);
}
.insights-card .insight-icon {
  flex: 0 0 22px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.insights-card .insight-icon img.rl-icon { width: 22px; height: 22px; }
.insights-card .insight-head {
  font-weight: 700;
  font-size: 14px;
  color: var(--text);
}
.insights-card .insight-sub {
  font-size: 12.5px;
  margin-top: 2px;
}
.insights-card .insight-sub .good { color: var(--good); font-weight: 700; }
.insights-card .insight-sub .bad  { color: var(--bad);  font-weight: 700; }

/* Stacked horizontal bar for playstyle breakdowns (position / speed / boost) */
.stacked-bar {
  display: flex;
  width: 100%;
  height: var(--bar-h, 10px);
  background: var(--bg);
  border: 1px solid var(--border);
  overflow: hidden;
  margin: 6px 0 4px;
}
.stacked-bar .seg {
  display: block;
  height: 100%;
  transition: width 220ms ease;
}
.stacked-bar .seg:first-child { border-right: 1px solid rgba(0,0,0,0.2); }

/* Balanced possession row sits under team-vs-team line in /history rows.
   Blue% on left, bar in middle, Orange% on right, alignment tag at the end. */
.poss-row {
  display: grid;
  grid-template-columns: 28px 1fr 28px auto;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
  font-size: 10.5px;
}
.poss-row .poss-pct.blue { color: var(--team-blue); font-weight: 700; text-align: right; }
.poss-row .poss-pct.orng { color: var(--team-orng); font-weight: 700; text-align: left; }
.poss-bar {
  display: flex;
  height: 4px;
  background: var(--bg);
  border: 1px solid var(--border);
  overflow: hidden;
}
.poss-fill { height: 100%; transition: width 200ms ease; }
.poss-fill.blue { background: var(--team-blue); }
.poss-fill.orng { background: var(--team-orng); }
.poss-tag {
  font-size: 9.5px;
  font-weight: 800;
  letter-spacing: 0.08em;
  padding: 1px 5px;
  border: 1px solid;
  white-space: nowrap;
}
.poss-tag.even    { color: var(--text-dim); background: var(--card-2);   border-color: var(--border); }
.poss-tag.aligned { color: var(--good);     background: rgba(52,211,153,0.10); border-color: rgba(52,211,153,0.30); }
.poss-tag.upset   { color: var(--accent);   background: var(--accent-soft); border-color: var(--accent-line); }

/* Per-row platform icon cell (used on /opponents, possibly others) */
.plat-cell {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  color: var(--text-dim);
}
.plat-cell svg.plat-ic { display: block; }

/* Mode / offline chips that sit below the team-vs-team line */
.row-chips {
  margin-top: 4px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
.row-chips .chip { font-size: 10.5px; }

/* Team-vs-team line: blue-name | score | "vs" | score | orange-name.
   Cap the whole line width and let the team-name columns truncate so the
   right-side stat columns (G/A/Sv/Sh/MVP) stay close. */
.vs-line {
  display: grid;
  grid-template-columns: minmax(0, 140px) 28px 22px 28px minmax(0, 140px);
  gap: 8px;
  align-items: center;
  font-variant-numeric: tabular-nums;
  max-width: 400px;
}
.vs-line .vs-team {
  font-weight: 700;
  font-size: 13px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
}
.vs-line .vs-team:hover { overflow-x: auto; text-overflow: clip; }
.vs-line .vs-team.blue { color: var(--team-blue); text-align: right; }
.vs-line .vs-team.orng { color: var(--team-orng); text-align: left; }
.vs-line .vs-score {
  font-family: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace;
  font-weight: 800;
  font-size: 16px;
  letter-spacing: -0.02em;
  text-align: center;
  color: var(--text-dim);
}
.vs-line .vs-score.winner { color: var(--text); }
.vs-line .vs-sep {
  color: var(--text-faint);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  text-align: center;
}

/* Stat-grid (two-column key/value lists inside cards) */
.statgrid { display: grid; grid-template-columns: 1fr auto auto; row-gap: 6px; column-gap: 14px; }
.statgrid .k { color: var(--text-dim); font-size: 12.5px; }
.statgrid .v { font-weight: 700; font-variant-numeric: tabular-nums; }
.statgrid .c { font-size: 11.5px; color: var(--text-faint); font-variant-numeric: tabular-nums; }
.statgrid .row-div { grid-column: 1 / -1; height: 1px; background: var(--border); margin: 6px 0; }

/* Form dots & sparklines */
.form-section h2 { margin-bottom: 8px; }
.form-strip {
  display: flex;
  align-items: center;
  gap: 24px;
  padding: 12px 16px;
  background: var(--card);
  border: 1px solid var(--border);
  flex-wrap: wrap;
}
.form-avgs { margin: 0; }
.form-dots { display: flex; gap: 5px; align-items: center; flex-shrink: 0; }
.form-dots .d {
  width: 18px; height: 18px; border-radius: 0;
  background: transparent;
  border: 2px solid var(--border-strong);
  font-size: 11px;
  font-weight: 800;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--bg);
  font-family: "JetBrains Mono", ui-monospace, monospace;
}
.form-dots .d.win::before  { content: "W"; }
.form-dots .d.loss::before { content: "L"; }
.form-dots .d.win  { background: var(--good); border-color: var(--good); }
.form-dots .d.loss { background: var(--bad);  border-color: var(--bad); }
.form-dots .d.tbd  { opacity: 0.4; }

.sparkbar { display: flex; align-items: flex-end; gap: 2px; height: 28px; }
.sparkbar .b {
  flex: 1; min-width: 4px;
  border-radius: 0;
  background: var(--text-faint);
  opacity: 0.5;
}
.sparkbar .b.hi { background: var(--accent); opacity: 1; }

/* Match hero scoreboard */
.match-hero {
  display: grid;
  grid-template-columns: 1fr auto 1fr;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 0;
  overflow: hidden;
  margin-bottom: 18px;
  position: relative;
}
.match-hero .side {
  padding: 24px 28px;
  display: flex; align-items: center; gap: 22px;
  position: relative;
}
.match-hero .side.left  {
  background: linear-gradient(90deg, var(--team-blue-soft) 0%, var(--card) 60%);
  border-left: 5px solid var(--team-blue);
}
.match-hero .side.right {
  background: linear-gradient(270deg, var(--team-orng-soft) 0%, var(--card) 60%);
  border-right: 5px solid var(--team-orng);
  justify-content: flex-end; flex-direction: row-reverse;
}
.match-hero .side .team-meta { display: flex; flex-direction: column; gap: 4px; min-width: 0; flex: 1; }
.match-hero .side.right .team-meta { align-items: flex-end; }
.match-hero .team-stripe { width: 8px; height: 72px; border-radius: 0; flex-shrink: 0; }
.match-hero .side.left  .team-stripe  { background: var(--team-blue); }
.match-hero .side.right .team-stripe { background: var(--team-orng); }
.match-hero .team-name {
  font-size: clamp(15px, 1.6vw, 22px);
  font-weight: 800;
  letter-spacing: -0.02em;
  color: var(--text);
  /* Single-line ellipsis so the giant score never gets overlapped. Hover the
     name (title attr) to read the full long club tag. */
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  line-height: 1.15;
  max-width: 100%;
  min-width: 0;
}
.match-hero .side { container-type: inline-size; min-width: 0; }
.match-hero .side .team-meta { min-width: 0; overflow: hidden; }
.match-hero .team-tag { font-size: 10px; font-weight: 700; letter-spacing: 0.14em; text-transform: uppercase; color: var(--text-dim); }
.match-hero .side.left  .team-tag { color: var(--team-blue); }
.match-hero .side.right .team-tag { color: var(--team-orng); }
.match-hero .result-pill {
  display: inline-block; margin-top: 6px;
  font-size: 11px; font-weight: 800; letter-spacing: 0.14em;
  text-transform: uppercase;
  padding: 3px 8px; border-radius: 0;
}
.match-hero .result-pill.win  { background: rgba(52,211,153,0.15); color: var(--good); }
.match-hero .result-pill.loss { background: rgba(248,113,113,0.12); color: var(--bad); }
.match-hero .score-display {
  font-size: 88px; font-weight: 900; letter-spacing: -0.06em;
  line-height: 1;
  font-variant-numeric: tabular-nums;
  color: var(--text);
}
.match-hero .side.left  .score-display { color: var(--team-blue); }
.match-hero .side.right .score-display { color: var(--team-orng); }
.match-hero .score-display.loss { opacity: 0.55; }
.match-hero .middle {
  background: var(--bg-elev);
  border-left: 1px solid var(--border);
  border-right: 1px solid var(--border);
  padding: 22px 28px;
  display: grid; place-items: center; gap: 4px;
  min-width: 220px;
  text-align: center;
}
/* Big game-clock duration is the centerpiece of the hero middle column. */
.match-hero .middle .hero-duration {
  font-size: 40px; font-weight: 900;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  letter-spacing: -0.04em;
  color: var(--text);
  line-height: 1;
}
.match-hero .middle .hero-context {
  display: inline-flex; align-items: center; gap: 8px;
  margin-top: 4px;
}
.match-hero .middle .hero-ctx-final {
  font-size: 11px; font-weight: 800; letter-spacing: 0.16em;
  text-transform: uppercase; color: var(--text-dim);
}
.match-hero .middle .hero-ctx-pill {
  font-size: 9px; font-weight: 800; letter-spacing: 0.14em;
  text-transform: uppercase;
  padding: 2px 8px;
  background: var(--accent-soft); color: var(--accent);
  border: 1px solid var(--accent-line);
  border-radius: 0;
}
.match-hero .middle .hero-ctx-pill.ot { background: rgba(251,191,36,0.16); color: var(--warn); border-color: rgba(251,191,36,0.40); }
.match-hero .middle .hero-ctx-pill.ff { background: rgba(248,113,113,0.16); color: var(--bad); border-color: rgba(248,113,113,0.40); }
.match-hero .middle .hero-meta {
  display: flex; align-items: center; justify-content: center;
  gap: 6px; flex-wrap: wrap;
  margin-top: 6px;
  font-size: 12px; color: var(--text-dim);
}
.match-hero .middle .hero-meta-sep { color: var(--text-faint); }

/* MVP callout in the hero middle column */
.match-hero .middle .hero-mvp {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
  padding: 4px 10px;
  background: var(--accent-soft);
  border: 1px solid var(--accent-line);
  font-size: 12px;
  max-width: 100%;
}
.match-hero .middle .hero-mvp-tag {
  font-size: 9px;
  font-weight: 800;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--accent);
  padding-right: 6px;
  border-right: 1px solid var(--accent-line);
}
.match-hero .middle .hero-mvp-entry {
  display: inline-flex;
  align-items: baseline;
  gap: 4px;
  font-weight: 700;
  min-width: 0;
}
.match-hero .middle .hero-mvp-entry.team-blue { color: var(--team-blue); }
.match-hero .middle .hero-mvp-entry.team-orng { color: var(--team-orng); }
.match-hero .middle .hero-mvp-name {
  color: inherit;
  text-decoration: none;
  font-weight: 800;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 160px;
}
.match-hero .middle .hero-mvp-name:hover { text-decoration: underline; }
/* RL point icons: small inline images shown next to event labels and chips.
   The icons themselves are light/white silhouettes on transparent so they
   need a tinted backdrop to read on the light theme. */
.rl-icon {
  display: inline-block;
  vertical-align: middle;
  width: 1em;
  height: 1em;
  object-fit: contain;
  filter: drop-shadow(0 0 1px rgba(0, 0, 0, 0.6));
}
[data-theme="light"] .rl-icon {
  filter: invert(0.85) drop-shadow(0 0 1px rgba(255, 255, 255, 0.7));
}
.pb-event-tag .rl-icon {
  margin-right: 4px;
  width: 14px; height: 14px;
}
.rc-stat-line li .rl-icon {
  margin-right: 4px;
  width: 14px; height: 14px;
}
.hero-mvp .rl-icon {
  width: 16px; height: 16px;
  margin-right: -2px;
}

.match-hero .middle .hero-mvp-you {
  font-size: 9px;
  font-weight: 900;
  letter-spacing: 0.14em;
  padding: 1px 5px;
  background: var(--text);
  color: var(--bg);
  border-radius: 0;
}
@media (max-width: 760px) {
  .match-hero { grid-template-columns: 1fr; }
  .match-hero .middle { border-left: 0; border-right: 0; border-top: 1px solid var(--border); border-bottom: 1px solid var(--border); }
  .match-hero .score-display { font-size: 54px; }
}

/* Team roster cards */
.roster-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 0;
  overflow: hidden;
  margin-bottom: 12px;
}
/* Team identity carried via the inline roster-stripe + team-color score
   in the header. No outer accent border. */
.roster-head {
  display: flex; align-items: center; gap: 14px;
  padding: 14px 18px;
  background: var(--card-2);
  border-bottom: 1px solid var(--border);
}
.roster-head .roster-team {
  font-size: 15px; font-weight: 800; letter-spacing: -0.01em;
  display: flex; align-items: center; gap: 10px;
}
.roster-head .roster-stripe { width: 4px; height: 18px; border-radius: 0; }
.team-blue .roster-stripe { background: var(--team-blue); }
.team-orng .roster-stripe { background: var(--team-orng); }
.roster-head .roster-score {
  font-size: 22px; font-weight: 900; letter-spacing: -0.02em;
  margin-left: auto;
  font-variant-numeric: tabular-nums;
}
.team-blue .roster-score { color: var(--team-blue); }
.team-orng .roster-score { color: var(--team-orng); }

/* When the live roster table can't fit, scroll horizontally instead of
   overlapping. The .roster-card already has overflow:hidden which clips
   contents - switch to auto so the table can scroll inside the card. */
.roster-card { overflow-x: auto; }
.roster-card table { width: 100%; table-layout: auto; }
.roster-card td.player-cell {
  font-weight: 600;
  min-width: 120px;
  max-width: 180px;
}
.roster-card td.player-cell .player-link {
  display: inline-block;
  max-width: 110px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: middle;
}
/* Live roster has 11 cols — keep cells from collapsing into one another */
.roster-card td, .roster-card th {
  white-space: nowrap;
}
/* Live boost bar gets a tighter footprint inside the cell */
.roster-card .live-boost-cell {
  min-width: 110px;
  max-width: 140px;
  gap: 6px;
}
.roster-card .live-boost-cell .live-boost-bar { min-width: 56px; }
.roster-card td.player-cell .meta-line {
  font-size: 12.5px; color: var(--text-faint); margin-top: 4px;
  display: flex; gap: 6px; align-items: center; font-weight: 500; flex-wrap: wrap;
}
/* Tighten table cells in roster so 11-column live view fits within the sidebar-narrowed page */
.roster-card td, .roster-card th { padding: 6px 8px; }
.roster-card td.num, .roster-card th.num { padding: 6px 6px; }
.roster-card td.player-cell .meta-line .super { color: var(--accent); font-variant-numeric: tabular-nums; }
.roster-card .total-row td {
  border-top: 1px solid var(--border);
  background: var(--card-2);
  font-weight: 700; color: var(--text-dim);
  font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase;
}
.roster-card .total-row td.num { color: var(--text); font-size: 13px; letter-spacing: 0; text-transform: none; }
.you-marker, .you-tag {
  display: inline-flex; align-items: center;
  margin-left: 8px;
  font-size: 9px; font-weight: 800; letter-spacing: 0.14em;
  padding: 2px 6px; border-radius: 0;
  background: var(--accent); color: #0a0d14;
}

/* Per-player section - vertical full-width stack, one card per player.
   Each card has: header (name + result counts) and body (radar + adv grid). */
.radar-grid {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.radar-card {
  background: var(--card-2);
  border: 1px solid var(--border);
  border-radius: 0;
  padding: 18px 22px;
  width: 100%;
}
/* Team-color shows up inside the radar polygon itself + via the player link
   in the rc-head. No outer accent border. */
.radar-card .rc-head {
  display: flex; align-items: center; justify-content: space-between;
  gap: 16px; flex-wrap: wrap;
  padding-bottom: 14px;
  margin-bottom: 14px;
  border-bottom: 1px solid var(--border);
  font-size: 14px;
}
.radar-card .rc-head-name { font-weight: 700; font-size: 15px; display: flex; align-items: center; gap: 4px; flex-wrap: wrap; }
.radar-card .rc-head-counts {
  display: flex; gap: 14px; flex-wrap: wrap;
  font-size: 12.5px; color: var(--text-dim); align-items: center;
}
.radar-card .rc-count b {
  color: var(--text); font-weight: 700;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  margin-right: 4px;
}
.radar-card .rc-count.rc-score-pill b {
  color: var(--accent); font-size: 14px;
}

/* Body grid: radar on the left, advanced stats on the right */
.radar-card .rc-body {
  display: block;
  padding-top: 14px;
}
.radar-card > summary.rc-head {
  cursor: pointer; list-style: none; -webkit-user-select: none; user-select: none;
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
}
.radar-card > summary.rc-head::-webkit-details-marker { display: none; }
.radar-card > summary.rc-head::after {
  content: '\25B8'; color: var(--text-faint); font-size: 13px;
  transition: transform .15s ease; flex: 0 0 auto;
}
.radar-card[open] > summary.rc-head::after { transform: rotate(90deg); }

/* Sticky in-page match nav (jump chips). CSS-only, no JS. */
.match-nav {
  position: sticky; top: 0; z-index: 20;
  display: flex; gap: 6px; flex-wrap: wrap; align-items: center;
  padding: 8px 2px; margin: 2px 0 14px;
  background: var(--bg);
  border-bottom: 1px solid var(--accent-line);
}
.match-nav .mn-chip {
  font-size: 12px; font-weight: 600; padding: 4px 10px; border-radius: 999px;
  color: var(--text-faint); text-decoration: none; white-space: nowrap;
  border: 1px solid transparent; background: none; cursor: pointer;
  font-family: inherit; line-height: 1.4;
}
.match-nav .mn-chip:hover { color: var(--text); background: var(--card); }
.match-nav .mn-chip.active {
  color: var(--text); background: var(--card); border-color: var(--accent-line);
}
.match-nav .mn-sep { width: 1px; align-self: stretch; background: var(--accent-line); margin: 2px 4px; }
.match-nav .mn-player .mn-swatch {
  display: inline-block; width: 7px; height: 7px; border-radius: 50%;
  margin-right: 5px; vertical-align: middle;
}
.match-nav .mn-player.team-blue .mn-swatch { background: var(--team-blue); }
.match-nav .mn-player.team-orng .mn-swatch { background: var(--team-orng); }
/* Swappable section panes — only the active pane shows (a real SPA, not scroll). */
.md-pane { display: none; }
.md-pane.active { display: block; }
.md-rosters { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; align-items: start; }
@media (max-width: 760px) { .md-rosters { grid-template-columns: 1fr; } }
.mn-target, #timeline, #goalmap, .radar-card[id] { scroll-margin-top: 64px; }
/* ESPN / ballchasing-style box scores (Players pane): full-width team-grouped
   tables, all players in rows, stats in columns. */
.bs-card { background: var(--card); border: 1px solid var(--accent-line); border-radius: 10px;
  padding: 12px 14px; margin-bottom: 14px; overflow-x: auto; }
.bs-title { font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--text); margin-bottom: 8px; display: flex; align-items: baseline; gap: 8px; }
.bs-title .dim { font-size: 10px; font-weight: 500; text-transform: none; letter-spacing: 0;
  color: var(--text-faint); }
.bs-table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
.bs-table thead th { font-size: 10px; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.04em; color: var(--text-faint); padding: 4px 8px; text-align: right;
  border-bottom: 1px solid var(--accent-line); white-space: nowrap; }
.bs-table thead th:first-child { text-align: left; }
.bs-table tbody td { padding: 6px 8px; font-size: 13px; border-bottom: 1px solid var(--accent-line); }
.bs-table tbody tr:last-child td { border-bottom: none; }
.bs-table td.player-cell { text-align: left; white-space: nowrap; font-weight: 600; }
.bs-table td.num { text-align: right; }
.bs-table .bs-sw { display: inline-block; width: 7px; height: 7px; border-radius: 50%;
  margin-right: 7px; vertical-align: middle; }
.bs-table tr.team-blue .bs-sw { background: var(--team-blue); }
.bs-table tr.team-orng .bs-sw { background: var(--team-orng); }
.bs-table tr.team-blue td.player-cell { box-shadow: inset 3px 0 0 var(--team-blue); }
.bs-table tr.team-orng td.player-cell { box-shadow: inset 3px 0 0 var(--team-orng); }
.bs-tmap-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 12px; }
.bs-tmap { border: 1px solid var(--accent-line); border-radius: 8px; padding: 6px; }
.bs-tmap-name { font-size: 11px; font-weight: 600; margin-bottom: 4px; }
.bs-tmap.team-blue .bs-tmap-name { color: var(--team-blue); }
.bs-tmap.team-orng .bs-tmap-name { color: var(--team-orng); }
/* Player-profile skill bars: per-match avg vs the field (replaces weak radar). */
.skill-card { margin-top: 14px; }
.skill-row { display: grid; grid-template-columns: 84px 1fr 116px; align-items: center;
  gap: 12px; padding: 5px 0; }
.skill-label { font-size: 13px; font-weight: 600; color: var(--text); }
.skill-track { position: relative; height: 10px; background: var(--accent-line); border-radius: 6px; }
.skill-fill { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 6px; }
.skill-fill.good { background: #34d399; }
.skill-fill.bad { background: var(--text-faint); }
.skill-avg { position: absolute; top: -3px; bottom: -3px; width: 2px; background: var(--text); opacity: 0.55; }
.skill-val { font-size: 13px; font-weight: 700; text-align: right; font-variant-numeric: tabular-nums; }
.skill-favg { display: block; font-size: 10px; font-weight: 500; color: var(--text-faint); }
/* Leaderboard rank column (players directory, club rivalries, …). */
td.rank, th.rank { color: var(--text-faint); font-weight: 600;
  width: 34px; text-align: right; padding-right: 10px; }
/* History results table: keep the score tight + a venue (Arena) column so the
   row doesn't have a dead gap between the score and the stat columns. */
.history .score-cell { width: 168px; }
.history .arena-cell { color: var(--text-faint); white-space: nowrap; font-size: 12.5px; }
/* Player-profile quick nav to subject-parameterized pages. */
.profile-links { display: flex; gap: 6px; margin: 4px 0 12px; flex-wrap: wrap; }
.profile-links a { font-size: 12px; font-weight: 600; padding: 4px 12px; border-radius: 999px;
  border: 1px solid var(--accent-line); color: var(--text); text-decoration: none; }
.profile-links a:hover { background: var(--card); }

/* Per-player SPA: scrollable tab strip + one visible panel (replaces the
   tall stack of collapsible cards). */
.pp-selector {
  display: flex; gap: 6px; overflow-x: auto; padding: 2px 0 12px;
  scrollbar-width: thin;
}
.pp-tab {
  flex: 0 0 auto; display: inline-flex; align-items: center; gap: 5px;
  padding: 6px 12px; border-radius: 999px; cursor: pointer; white-space: nowrap;
  font: inherit; font-size: 12px; font-weight: 600;
  color: var(--text-dim); background: var(--card-2); border: 1px solid var(--border);
  transition: color 120ms ease, border-color 120ms ease, background 120ms ease;
}
.pp-tab:hover { color: var(--text); border-color: var(--border-strong); }
.pp-tab.active { color: var(--text); background: var(--accent-soft); border-color: var(--accent-line); }
.pp-tab .mn-swatch { width: 8px; height: 8px; border-radius: 50%; }
.pp-tab.team-blue .mn-swatch { background: var(--team-blue); }
.pp-tab.team-orng .mn-swatch { background: var(--team-orng); }
.pp-tab .mn-you { font-size: 9px; font-weight: 800; color: var(--accent); }
.player-panel { display: none; }
.player-panel.active { display: block; }
.pp-head { font-weight: 700; font-size: 15px; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-bottom: 12px; }

/* Us-vs-them compare block */
.cmp-table { width: 100%; max-width: 420px; border-collapse: collapse; }
.cmp-table th, .cmp-table td { padding: 6px 10px; }
.cmp-table thead th { font-size: 11px; text-transform: uppercase; letter-spacing: .06em; }
.cmp-table thead th.team-blue { color: var(--team-blue); }
.cmp-table thead th.team-orng { color: var(--team-orng); }
.cmp-table .cmp-label { color: var(--text-faint); font-size: 13px; }
.cmp-table tbody tr + tr td { border-top: 1px solid var(--accent-line); }
.cmp-touch { margin-top: 12px; }
.cmp-touch .cmp-pp-title { font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--text-faint); margin-bottom: 6px; }
.cmp-pp-row { display: flex; flex-wrap: wrap; gap: 6px 12px; margin-bottom: 4px; }
.cmp-pp-row.team-blue .cmp-pp { color: var(--team-blue); }
.cmp-pp-row.team-orng .cmp-pp { color: var(--team-orng); }
.cmp-pp { font-size: 12px; font-variant-numeric: tabular-nums; }
.radar-card .rc-adv { display: flex; flex-direction: column; gap: 16px; }
.radar-card .rc-adv-section { display: flex; flex-direction: column; gap: 10px; }
.radar-card .rc-adv-title {
  font-size: 10px; font-weight: 700; letter-spacing: 0.12em;
  color: var(--text-dim); text-transform: uppercase;
}
.radar-card .rc-adv-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(100px, 1fr));
  gap: 10px;
}
/* Dense single-row stat line (replaces the multi-tile grid for readability) */
.rc-stat-line {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-wrap: wrap;
  column-gap: 16px;
  row-gap: 4px;
  font-size: 13px;
}
.rc-stat-line li {
  display: inline-flex;
  align-items: baseline;
  gap: 5px;
  white-space: nowrap;
}
.rc-stat-line li b {
  font-weight: 800;
  color: var(--text);
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.02em;
}
.rc-stat-line li span {
  color: var(--text-dim);
  font-size: 11px;
}
.rc-stat-line-highlight b { color: var(--accent); }
.rc-stat-line-highlight span { color: var(--accent); opacity: 0.85; }
.radar-card .rc-stat {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 0;
  padding: 10px 12px;
  text-align: center;
}
.radar-card .rc-stat-v {
  font-size: 18px; font-weight: 800; color: var(--text);
  font-family: "JetBrains Mono", ui-monospace, monospace;
  letter-spacing: -0.03em;
  font-variant-numeric: tabular-nums;
  line-height: 1.1;
}
.radar-card .rc-stat-l {
  font-size: 10px; font-weight: 700; color: var(--text-dim);
  margin-top: 4px; text-transform: uppercase; letter-spacing: 0.08em;
}
.radar-card .rc-adv-empty {
  color: var(--text-dim); font-size: 13px;
  background: var(--card);
  border: 1px dashed var(--border);
  padding: 16px 18px;
  line-height: 1.55;
}
.radar-card .rc-adv-empty a { color: var(--accent); text-decoration: none; border-bottom: 1px dotted var(--accent-line); }

/* Player profile head */
.player-head {
  display: grid;
  grid-template-columns: auto 1fr auto;
  align-items: center;
  gap: 24px;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 0;
  padding: 22px 24px;
  margin-bottom: 16px;
}
.avatar {
  width: 64px; height: 64px; border-radius: 0;
  background: var(--card-2);
  display: grid; place-items: center;
  font-size: 22px; font-weight: 800; letter-spacing: -0.02em;
  color: var(--accent);
  border: 1px solid var(--border);
  position: relative;
}
.avatar.self { background: var(--accent-soft); border-color: var(--accent-line); }
.player-head .name-line h1 { margin: 0; font-size: 28px; font-weight: 800; letter-spacing: -0.02em; }
.player-head .name-line .meta {
  margin-top: 4px; font-size: 12px; color: var(--text-dim);
  display: flex; gap: 12px; align-items: center;
}
.player-head .quick { display: flex; gap: 8px; }
.quick-stat {
  background: var(--card-2);
  border: 1px solid var(--border);
  border-radius: 0;
  padding: 10px 16px;
  text-align: center;
  min-width: 76px;
}
.quick-stat .v { font-size: 18px; font-weight: 800; letter-spacing: -0.02em; font-variant-numeric: tabular-nums; }
.quick-stat .l { font-size: 10px; font-weight: 700; letter-spacing: 0.10em; text-transform: uppercase; color: var(--text-dim); margin-top: 2px; }
.profile-header { display: flex; align-items: center; gap: 12px; margin-bottom: 4px; }
.profile-bot-badge {
  display: inline-block;
  background: var(--accent-soft); color: var(--accent);
  border: 1px solid var(--accent-line);
  padding: 3px 12px;
  border-radius: 0;
  font-size: 11px; font-weight: 800; letter-spacing: 0.10em;
}

/* Dashboard layout */
.dash-grid {
  display: grid;
  grid-template-columns: 1.4fr 1fr;
  gap: 14px;
}
@media (max-width: 980px) { .dash-grid { grid-template-columns: 1fr; } }

.radar-block {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 18px;
  align-items: center;
}
@media (max-width: 700px) { .radar-block { grid-template-columns: 1fr; } }
.radar-block .legend { display: flex; flex-direction: column; gap: 6px; font-size: 12px; }
.radar-block .legend .row {
  display: grid; grid-template-columns: 1fr auto auto;
  gap: 14px; align-items: baseline;
  padding: 6px 0;
  border-bottom: 1px dashed var(--border);
}
.radar-block .legend .row:last-child { border-bottom: none; }
.radar-block .legend .lbl { color: var(--text-dim); }
.radar-block .legend .val { font-weight: 700; font-variant-numeric: tabular-nums; }
.radar-block .legend .max { color: var(--text-faint); font-variant-numeric: tabular-nums; font-size: 11px; }

.radar-section { display: flex; flex-direction: column; align-items: center; padding: 8px 0; }
.radar-section svg { margin: 4px 0 8px 0; }

/* Stacked bar */
.stack {
  display: flex; width: 100%; height: 10px;
  border-radius: 0; overflow: hidden;
  background: var(--bg-elev);
  border: 1px solid var(--border);
}
.stack span { display: block; height: 100%; }
.stack-legend { display: flex; gap: 14px; flex-wrap: wrap; margin-top: 8px; font-size: 11px; color: var(--text-dim); }
.stack-legend .dot { display: inline-block; width: 8px; height: 8px; border-radius: 0; margin-right: 6px; vertical-align: 1px; }

/* Toolbar + filter chips */
.toolbar {
  display: flex; align-items: center; gap: 8px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
/* Page-toolbar seg: matches sidebar chip dimensions so filter buttons feel
   uniform whether they're in the sidebar or in a page toolbar. */
.seg {
  display: inline-flex;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 0;
  padding: 0;
}
.seg button, .seg a {
  background: transparent; border: 0; cursor: pointer;
  font: inherit; font-size: 11.5px; font-weight: 600;
  color: var(--text-dim);
  height: 30px;
  padding: 0 12px; border-radius: 0;
  letter-spacing: -0.005em;
  text-decoration: none;
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  border-right: 1px solid var(--border);
  white-space: nowrap;
}
.seg button:last-child, .seg a:last-child { border-right: 0; }
.seg button:hover, .seg a:hover { color: var(--text); }
.seg button.active, .seg a.active {
  background: var(--accent-soft); color: var(--accent);
  border-color: var(--accent-line);
}
.search-box {
  display: inline-flex; align-items: center; gap: 8px;
  background: var(--card); border: 1px solid var(--border);
  border-radius: 0; padding: 6px 12px;
  flex: 1; max-width: 320px;
}
.search-box input {
  flex: 1; background: transparent; border: 0; outline: 0;
  font: inherit; font-size: 13px; color: var(--text);
}
.search-box input::placeholder { color: var(--text-faint); }
.search-box svg { width: 14px; height: 14px; color: var(--text-faint); }

.filter-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin: 0 0 14px 0; }
.filter-chip {
  display: inline-flex; align-items: center; gap: 6px;
  background: var(--card);
  border: 1px solid var(--border);
  color: var(--text-dim);
  padding: 6px 14px;
  border-radius: 0;
  font-size: 12px; font-weight: 600;
  cursor: pointer; text-decoration: none;
  transition: all 150ms ease;
  font-family: inherit;
}
.filter-chip:hover { color: var(--text); background: var(--card-hover); }
.filter-chip.active { background: var(--accent-soft); color: var(--accent); border-color: var(--accent-line); }
.chip-mark { display: inline-block; width: 12px; text-align: center; opacity: 0.85; }

/* Summary above tables */
.summary-row {
  display: flex; gap: 14px; align-items: center; flex-wrap: wrap;
  padding: 10px 14px;
  background: var(--card-2);
  border: 1px dashed var(--border);
  border-radius: 0;
  margin-bottom: 12px;
  font-size: 12px;
  color: var(--text-dim);
}
.summary-row b { color: var(--text); font-weight: 700; }

/* Misc */
.divider { height: 1px; background: var(--border); margin: 16px 0; }
.breadcrumb { font-size: 12px; color: var(--text-dim); margin-bottom: 12px; }
.breadcrumb a {
  color: var(--text-dim); text-decoration: none; cursor: pointer;
  padding: 4px 10px; border-radius: 0;
  background: var(--card); border: 1px solid var(--border);
  font-weight: 600;
}
.breadcrumb a:hover { color: var(--accent); border-color: var(--accent-line); }
.empty {
  padding: 40px 20px;
  text-align: center;
  color: var(--text-dim);
  font-size: 13px;
}
.note {
  background: var(--card-2);
  border: 1px solid var(--border);
  border-radius: 0;
  padding: 12px 16px 12px 38px;
  position: relative;
  font-size: 13px;
  color: var(--text-dim);
  margin-bottom: 12px;
  line-height: 1.55;
}
.note::before {
  content: "i"; position: absolute; left: 14px; top: 14px;
  width: 14px; height: 14px; border-radius: 50%;
  background: var(--accent); color: #0a0d14;
  display: grid; place-items: center;
  font-family: "Bricolage Grotesque", serif; font-weight: 800; font-size: 10px;
  font-style: italic;
}
.note b { color: var(--text); font-weight: 700; }
.note code { background: var(--bg); padding: 1px 5px; border-radius: 0; margin: 0 4px; color: var(--accent); font-family: "JetBrains Mono", monospace; font-size: 12px; }
.hint {
  background: var(--accent-soft);
  color: var(--text);
  padding: 10px 14px;
  border-radius: 0;
  margin: 0 0 14px 0;
  font-size: 13px;
}

/* Radar SVG theme-aware classes */
.radar-svg .radar-grid  { stroke: var(--border); }
.radar-svg .radar-spoke { stroke: var(--border); }
.radar-svg .radar-label { fill: var(--text); }
.radar-svg .radar-value { fill: var(--text-dim); }

/* Players directory cards */
.player-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 12px;
  margin-top: 14px;
}
.player-card {
  display: flex; gap: 12px; align-items: center;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 0;
  padding: 14px;
  text-decoration: none;
  color: var(--text);
  transition: all 150ms ease;
}
.player-card:hover { background: var(--card-hover); transform: translateY(-1px); border-color: var(--accent-line); }
.pc-body { flex: 1; min-width: 0; }
.pc-name { font-weight: 600; white-space: nowrap; text-overflow: ellipsis; overflow: hidden; }
.pc-meta { color: var(--text-dim); font-size: 11px; margin-top: 2px; }
.pc-stats { font-size: 12px; margin-top: 4px; color: var(--text-dim); display: flex; gap: 10px; }
.pc-stats b { color: var(--text); font-weight: 600; }
.players-table td:nth-child(1) { font-weight: 600; }
.players-table .player-link { color: var(--text); text-decoration: none; border-bottom: 1px dotted var(--border); }
.players-table .player-link:hover { color: var(--accent); border-color: var(--accent); }

/* Overlay picker - single-column 4-row stack, each card spans the page */
.overlay-grid {
  display: flex; flex-direction: column;
  gap: 18px; margin: 18px 0;
}
.overlay-card {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 0; padding: 0;
  display: grid;
  grid-template-columns: minmax(260px, 320px) 1fr;
  gap: 0;
  align-items: stretch;
}
.overlay-card-info {
  display: flex; flex-direction: column; gap: 12px;
  padding: 22px 24px;
  border-right: 1px solid var(--border);
  min-width: 0;
}
.overlay-card h2.ov-title {
  margin: 0; font-size: 20px; font-weight: 800;
  color: var(--text); letter-spacing: -0.015em; text-transform: none;
}
.overlay-card .ov-desc {
  color: var(--text-dim); font-size: 13.5px; margin: 0;
  max-width: 56ch; line-height: 1.55;
}
.overlay-preview {
  background:
    radial-gradient(ellipse at center, rgba(255,122,24,0.04) 0%, transparent 60%),
    linear-gradient(135deg, #161a22 0%, #0b0f17 100%);
  border: 0; border-radius: 0;
  overflow: hidden; margin: 0;
  display: flex; align-items: center; justify-content: center;
  position: relative;
  padding: 16px;
  min-height: 160px;
}
.overlay-iframe {
  width: 100%; height: 100%;
  border: 0; background: transparent;
  pointer-events: none;
  display: block;
}
@media (max-width: 820px) {
  .overlay-card { grid-template-columns: 1fr; }
  .overlay-card-info { border-right: 0; border-bottom: 1px solid var(--border); }
}
.setup-list { color: var(--text-dim); font-size: 13px; line-height: 1.8; padding-left: 20px; }
.setup-list li { margin-bottom: 4px; }
.setup-list code { background: var(--card-2); padding: 1px 6px; border-radius: 0; font-size: 12px; color: var(--text); font-family: "JetBrains Mono", monospace; }
.setup-list b { color: var(--text); }
.copy-row {
  display: flex; gap: 8px; align-items: center;
  background: var(--bg-elev); border: 1px solid var(--border);
  border-radius: 0; padding: 4px 4px 4px 12px;
  font-size: 12px; font-family: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace;
}
.copy-row code { color: var(--text-dim); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.copy-btn {
  background: var(--accent); color: #0a0d14; border: none;
  border-radius: 0; padding: 6px 12px; cursor: pointer;
  font-size: 11px; font-weight: 700; letter-spacing: 0.04em;
}
.copy-btn:hover { filter: brightness(1.1); }
.copy-btn.copied { background: var(--good); color: #0a0d14; }
.overlay-meta { margin-top: 12px; color: var(--text-dim); font-size: 11px; display: flex; gap: 12px; }
.open-link { color: var(--accent); text-decoration: none; font-weight: 600; }
.open-link:hover { text-decoration: underline; }

/* Prose width cap - long-form text reads better at ~62ch */
.prose { max-width: 680px; }
.prose section { background: var(--card); border: 1px solid var(--border); border-radius: 0; padding: 20px 24px; margin: 0 0 14px 0; max-width: 680px; }
.prose p, .prose ul, .prose ol, .prose li { max-width: 60ch; }
.prose section h2 { color: var(--text-dim); }
.prose code, code {
  background: var(--card-2); border: 1px solid var(--border);
  padding: 2px 7px; border-radius: 0;
  font-family: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace !important;
  font-size: 12.5px;
  color: var(--accent);
  font-feature-settings: "tnum";
}
/* Belt-and-braces fallback for any uncapped paragraph */
p, li { max-width: 72ch; }
.who, .sub, .caption, .ov-desc, .note { max-width: 56ch; }
.codeblock {
  background: var(--card-2);
  border: 1px solid var(--border);
  border-radius: 0;
  padding: 14px 18px;
  font-family: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace;
  font-size: 13px;
  color: var(--text);
  overflow-x: auto;
  margin: 16px 0 0 0;
  line-height: 1.65;
  max-width: 56ch;
  white-space: pre;
}

/* Compare page */
.compare-form {
  display: flex;
  align-items: flex-end;
  gap: 12px;
  flex-wrap: wrap;
  padding: 16px 18px;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 0;
  margin-bottom: 14px;
}
.compare-slots {
  display: grid;
  grid-template-columns: repeat(3, minmax(180px, 1fr));
  gap: 12px;
  flex: 1;
}
.compare-slot { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.compare-slot-label {
  font-size: 10px; font-weight: 700; letter-spacing: 0.10em;
  color: var(--text-dim); text-transform: uppercase;
}
.compare-slot select {
  font: inherit; font-size: 13px;
  padding: 8px 12px;
  background: var(--card-2);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: 0;
}
.compare-slot select:focus { outline: 0; border-color: var(--accent-line); }
.compare-table { width: 100%; border-collapse: collapse; table-layout: fixed; }
.compare-table col.compare-col-metric { width: 220px; }
.compare-table col.compare-col-slot   { width: auto; }
.compare-table col.compare-col-pro    { width: 110px; }

/* Pro benchmark column: text-only, italic, lower contrast — reads as a
   reference, not a participating stat. */
.compare-table th.compare-pro-col {
  color: var(--text-dim);
  font-style: italic;
  font-size: 11px;
  letter-spacing: 0;
  text-transform: none;
  border-left: 1px dashed var(--border-strong);
  text-align: center;
}
.compare-table td.compare-pro-val {
  text-align: center;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 12px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  color: var(--text-dim);
  font-style: italic;
  border-left: 1px dashed var(--border-strong);
}
.compare-table td.compare-pro-val.dim { color: var(--text-faint); }
.compare-table td.compare-totavg .compare-tot {
  font-size: 17px; font-weight: 800; line-height: 1.1;
}
.compare-table td.compare-totavg .compare-avg {
  font-size: 11px; font-weight: 500; color: var(--text-dim);
  letter-spacing: -0.005em;
  margin-top: 2px;
}
.compare-table td.compare-totavg.best .compare-tot { color: var(--good); }
.compare-table td.compare-totavg.best .compare-avg { color: var(--good); opacity: 0.85; }
.compare-table td.compare-val { word-break: break-word; }
.compare-table thead th {
  text-align: left;
  padding: 12px 16px;
  background: var(--card-2);
  border-bottom: 1px solid var(--border);
  font-size: 13px;
  font-weight: 700;
  color: var(--text);
  text-transform: none;
  letter-spacing: -0.005em;
}
.compare-table thead th.compare-metric-col {
  width: 200px;
  color: var(--text-dim);
  font-weight: 600;
  font-size: 10px;
  letter-spacing: 0.10em;
  text-transform: uppercase;
}
.compare-table tbody td {
  padding: 11px 16px;
  border-bottom: 1px solid var(--border);
  font-size: 13.5px;
}
.compare-table tbody tr:last-child td { border-bottom: none; }
.compare-table td.compare-metric {
  color: var(--text-dim);
  font-weight: 500;
}
.compare-table td.compare-val {
  font-family: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.02em;
  font-weight: 600;
  color: var(--text);
}
.compare-table td.compare-val.best {
  color: var(--good);
  font-weight: 800;
  background: rgba(52, 211, 153, 0.12);
  border-left: 0; border-right: 0;
}
.compare-table tr.compare-section-row td {
  background: var(--card-2);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--text-dim);
  padding: 14px 16px 8px 16px;
  border-bottom: 1px solid var(--border);
  border-top: 1px solid var(--border);
}
.compare-table tr.compare-section-row:first-child td { border-top: 0; }

/* Compact mode (optional, body.compact) */
body.compact section, body.compact .card { padding: 14px 16px; }
body.compact .page-head { margin-bottom: 16px; }
body.compact .page-head h1 { font-size: 24px; }
body.compact .kpi-row { gap: 8px; }
body.compact .kpi { padding: 12px 14px; }
body.compact .kpi .kpi-value { font-size: 22px; }
body.compact .match-hero .score-display { font-size: 52px; }
body.compact .match-hero .side { padding: 18px 22px; }
body.compact th, body.compact td { padding: 7px 10px; }

/* ============================================================
   Highlight chip (radar-card header: "N HL" total)
   ============================================================ */
.chip.highlight-chip {
  color: var(--accent);
  border-color: var(--accent-line);
  background: var(--accent-soft);
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-weight: 800;
  letter-spacing: 0.06em;
  font-variant-numeric: tabular-nums;
  margin-left: 6px;
}

/* ============================================================
   Highlight stat tile: accent-tinted variant of rc-stat for
   special-moment counts (Epic save, Aerial goal, Flip reset)
   ============================================================ */
.radar-card .rc-stat.rc-highlight {
  background: var(--accent-soft);
  border-color: var(--accent-line);
}
.radar-card .rc-stat.rc-highlight .rc-stat-v {
  color: var(--accent);
}
.radar-card .rc-stat.rc-highlight .rc-stat-l {
  color: var(--accent);
  opacity: 0.85;
}

/* ============================================================
   Long clan-name handling + stronger team-color eyebrow
   ============================================================ */
.team-eyebrow {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  padding: 8px 14px 8px 10px;
  margin-bottom: 8px;
  border-left: 6px solid var(--border);
  font-weight: 800;
  font-size: 13px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  max-width: 100%;
  min-width: 0;
}
.team-eyebrow.team-blue { border-left-color: var(--team-blue); background: var(--team-blue-soft); color: var(--team-blue); }
.team-eyebrow.team-orng { border-left-color: var(--team-orng); background: var(--team-orng-soft); color: var(--team-orng); }
.team-eyebrow .team-swatch {
  width: 12px; height: 12px;
  background: currentColor;
  flex-shrink: 0;
}
/* Note appended to a team eyebrow (e.g. spectator-data disclaimer) */
.eyebrow-note {
  margin-left: auto;
  padding-left: 14px;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-dim);
  text-transform: none;
  letter-spacing: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.eyebrow-note a {
  color: var(--text-dim);
  text-decoration: underline;
  text-decoration-style: dotted;
}
.eyebrow-note a:hover { color: var(--text); }
.team-name-truncate {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
  max-width: 100%;
  display: inline-block;
  vertical-align: middle;
}
/* Inline team-name used in the score cell of the dense history list */
.score-team {
  display: inline-block;
  max-width: 140px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 700;
  vertical-align: middle;
}
.score-team.team-blue { color: var(--team-blue); }
.score-team.team-orng { color: var(--team-orng); }

/* MVP cell on history tables: icon + clickable name + YOU badge when viewer
   was the MVP. Color follows the MVP's team. */
.mvp-cell {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  font-weight: 700;
  max-width: 100%;
  min-width: 0;
}
.mvp-cell.team-blue { color: var(--team-blue); }
.mvp-cell.team-orng { color: var(--team-orng); }
.mvp-cell .mvp-name {
  color: inherit;
  text-decoration: none;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 140px;
  font-weight: 800;
}
.mvp-cell .mvp-name:hover { text-decoration: underline; }
.mvp-cell .mvp-you {
  font-size: 9px;
  font-weight: 900;
  letter-spacing: 0.14em;
  padding: 1px 4px;
  background: var(--text);
  color: var(--bg);
  border-radius: 0;
}
.mvp-cell .rl-icon {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
}

/* Clan tracker chips for member lists in match rows */
.clan-members-cell {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}
.clan-member-chip {
  display: inline-block;
  padding: 2px 7px;
  font-size: 11px;
  font-weight: 600;
  background: var(--card-2);
  border: 1px solid var(--border-strong);
  border-radius: 0;
  color: var(--text);
  max-width: 130px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Roster card: thicker team-color border to reinforce who's who */
.roster-card.team-blue { border-left: 5px solid var(--team-blue); }
.roster-card.team-orng { border-left: 5px solid var(--team-orng); }
.roster-card .roster-stripe { width: 8px; height: 24px; flex-shrink: 0; }
.roster-card.team-blue .roster-stripe { background: var(--team-blue); }
.roster-card.team-orng .roster-stripe { background: var(--team-orng); }
.roster-card .roster-team {
  display: inline-flex; align-items: center; gap: 10px;
  min-width: 0;
}
.roster-card .roster-team > span:not(.roster-stripe):not(.chip) {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 320px;
}

/* Radar-card team accents: left border in team color */
.radar-card.team-blue { border-left: 4px solid var(--team-blue); }
.radar-card.team-orng { border-left: 4px solid var(--team-orng); }

/* ============================================================
   Match playback widget: pitch + event list + transport bar
   ============================================================ */
.pb-card { margin-top: 14px; margin-bottom: 14px; }

.pb-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.6fr) minmax(280px, 1fr);
  gap: 18px;
  align-items: stretch;
}
@media (max-width: 980px) {
  .pb-grid { grid-template-columns: 1fr; }
}

.pb-left { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.pb-right { display: flex; flex-direction: column; min-width: 0; }

.pb-pitch-wrap {
  width: 100%;
  background: var(--card-2);
  border: 1px solid var(--border);
}
.pb-pitch { width: 100%; height: auto; display: block; }

/* Pitch graphics */
.pb-field { fill: var(--card); stroke: var(--border-strong); stroke-width: 2; }
.pb-midline { stroke: var(--border-strong); stroke-width: 1.5; opacity: 0.6; }
.pb-midcircle { fill: none; stroke: var(--border-strong); stroke-width: 1.5; opacity: 0.6; }
.pb-net { opacity: 0.85; }
.pb-net-blue { fill: var(--team-blue); }
.pb-net-orng { fill: var(--team-orng); }
.pb-net-label {
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}
.pb-net-label-blue { fill: var(--team-blue); }
.pb-net-label-orng { fill: var(--team-orng); }

/* Ball trail: each contact is a tiny car icon (RL #rl-car symbol) coloured
   by the team that touched it. <g> wraps the <use> so we can transform. */
.pb-touch.team-blue { color: var(--team-blue); }
.pb-touch.team-orng { color: var(--team-orng); }
.pb-touch.team-neutral { color: var(--text-faint); }
.pb-touch-aerial { filter: drop-shadow(0 0 4px rgba(255, 122, 24, 0.55)); }
.pb-touch-altitude.team-blue { stroke: var(--team-blue); }
.pb-touch-altitude.team-orng { stroke: var(--team-orng); }
.pb-touch-altitude { stroke-dasharray: 2 2; }
.pb-touch-name {
  fill: var(--text);
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.01em;
  dominant-baseline: middle;
}
.pb-touch.team-blue .pb-touch-name { fill: #ffffff; }
.pb-touch.team-orng .pb-touch-name { fill: #ffffff; }
.pb-touch-name-bg {
  fill: rgba(15, 19, 28, 0.92);
  stroke: var(--border-strong);
  stroke-width: 1;
}
.pb-touch-name-bg.team-blue { fill: var(--team-blue); stroke: var(--team-blue); }
.pb-touch-name-bg.team-orng { fill: var(--team-orng); stroke: var(--team-orng); }
/* Halo behind icon-touches so the icon reads against any background */
.pb-touch-halo {
  fill: rgba(15, 19, 28, 0.75);
  stroke: var(--border-strong);
  stroke-width: 0.8;
}
.pb-touch.team-blue .pb-touch-halo { stroke: var(--team-blue); }
.pb-touch.team-orng .pb-touch-halo { stroke: var(--team-orng); }
/* Big drop-in event icon (Goal! / Save! / Demo!) on the pitch */
.pb-pulse-icon {
  animation: pb-pulse-icon 2s ease-out forwards;
  pointer-events: none;
  filter: drop-shadow(0 2px 6px rgba(0, 0, 0, 0.6));
}
@keyframes pb-pulse-icon {
  0%   { opacity: 0; transform: translateY(8px) scale(0.7); }
  18%  { opacity: 1; transform: translateY(0) scale(1.15); }
  35%  { transform: translateY(0) scale(1); }
  100% { opacity: 0; transform: translateY(-14px) scale(1); }
}

/* Current-touch player label that floats above the ball */
.pb-label-bg {
  fill: var(--card-2);
  stroke: var(--border-strong);
  stroke-width: 1;
}
.pb-current-label.team-blue .pb-label-bg { stroke: var(--team-blue); }
.pb-current-label.team-orng .pb-label-bg { stroke: var(--team-orng); }
.pb-label-text {
  fill: var(--text);
  font-size: 11px;
  font-weight: 700;
  text-anchor: middle;
  dominant-baseline: middle;
}
.pb-current-label.team-blue .pb-label-text { fill: var(--team-blue); }
.pb-current-label.team-orng .pb-label-text { fill: var(--team-orng); }

/* Pre-goal chains (static overlays that appear at goal time) */
.pb-chain { transition: opacity 220ms ease; }
.pb-chain-dot { opacity: 0.85; }
.pb-chain-impact { opacity: 1; }

/* Static event-icon overlay on the playback pitch. Always rendered so the
   map tells the story at a glance; faded down until their moment passes. */
.pb-static-icon { transition: opacity 240ms ease; }
.pb-static-icon image { pointer-events: none; }

/* Table header icons sit inline with the label text and shrink to fit. */
table.history th .rl-icon,
.kpi-label .rl-icon,
.rc-count .rl-icon,
.compare-metric .rl-icon {
  width: 14px;
  height: 14px;
  margin-right: 4px;
  vertical-align: middle;
}
.compare-table td.compare-metric .rl-icon { margin-right: 6px; }
.rc-count .rl-icon { width: 12px; height: 12px; margin-right: 3px; }

/* The animated ball is a soccer-ball symbol (#rl-ball) inside a group. */
#playback-ball-group {
  filter: drop-shadow(0 1px 3px rgba(0, 0, 0, 0.6));
}

/* Mode toggle: Playback vs Heatmap. The .pb-grid carries the current mode
   via [data-mode], which we use to switch visibility of the SVG layers. */
.pb-mode-toggle {
  display: inline-flex;
  border: 1px solid var(--border);
  align-self: flex-start;
}
.pb-mode-toggle button {
  background: var(--card-2);
  color: var(--text-dim);
  border: none;
  padding: 6px 14px;
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  cursor: pointer;
}
.pb-mode-toggle button:hover { color: var(--text); }
.pb-mode-toggle button.active {
  background: var(--accent-soft);
  color: var(--accent);
}

/* Layer visibility driven by mode. All three layers are rendered in the SVG;
   we just toggle which one paints. */
.pb-grid[data-mode="playback"] .layer-heatmap,
.pb-grid[data-mode="playback"] .layer-goals { display: none; }
.pb-grid[data-mode="heatmap"]  .layer-playback,
.pb-grid[data-mode="heatmap"]  .layer-goals { display: none; }
.pb-grid[data-mode="goals"]    .layer-playback,
.pb-grid[data-mode="goals"]    .layer-heatmap { display: none; }
.pb-grid[data-mode="heatmap"]  .pb-play,
.pb-grid[data-mode="heatmap"]  .pb-scrub,
.pb-grid[data-mode="heatmap"]  .pb-time,
.pb-grid[data-mode="heatmap"]  .pb-speed,
.pb-grid[data-mode="goals"]    .pb-play,
.pb-grid[data-mode="goals"]    .pb-scrub,
.pb-grid[data-mode="goals"]    .pb-time,
.pb-grid[data-mode="goals"]    .pb-speed {
  opacity: 0.35;
  pointer-events: none;
}
.gm-goal { cursor: default; }

/* Event pulse (spawned on event fire, CSS-animated then removed) */
.pb-pulse {
  fill: none;
  stroke-width: 3;
  animation: pb-pulse-ring 1.4s ease-out forwards;
  pointer-events: none;
}
.pb-pulse.team-blue { stroke: var(--team-blue); }
.pb-pulse.team-orng { stroke: var(--team-orng); }
.pb-pulse-goal { stroke-width: 4; }
.pb-pulse-crossbar { stroke: var(--warn); }
.pb-pulse-demo { stroke: var(--bad); }
.pb-pulse-epic-save { stroke: var(--good); }
.pb-pulse-aerial,
.pb-pulse-flip-reset,
.pb-pulse-bicycle,
.pb-pulse-hat-trick,
.pb-pulse-celebrate { stroke: var(--accent); }

@keyframes pb-pulse-ring {
  0%   { r: 6;  opacity: 0.95; }
  100% { r: 40; opacity: 0; }
}

/* Transport controls */
.pb-controls {
  display: grid;
  grid-template-columns: auto auto 1fr auto auto;
  gap: 12px;
  align-items: center;
  padding: 10px 12px;
  background: var(--card-2);
  border: 1px solid var(--border);
}
@media (max-width: 720px) {
  .pb-controls { grid-template-columns: auto 1fr; row-gap: 8px; }
  .pb-controls .pb-scrub { grid-column: 1 / -1; }
  .pb-controls .pb-speed,
  .pb-controls .pb-clock { grid-column: 1 / -1; }
}

.pb-play {
  width: 38px; height: 38px;
  display: inline-flex; align-items: center; justify-content: center;
  background: var(--accent);
  color: var(--bg);
  border: none; border-radius: 0;
  font-size: 16px;
  cursor: pointer;
}
.pb-play:hover { background: var(--accent-hover, var(--accent)); filter: brightness(1.1); }

.pb-time {
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 13px; font-weight: 700;
  font-variant-numeric: tabular-nums;
  color: var(--text);
  display: inline-flex; align-items: baseline; gap: 6px;
}
.pb-time-sep { color: var(--text-dim); }
.pb-time .pb-time-sep + .tnum { color: var(--text-dim); }

.pb-scrub {
  -webkit-appearance: none;
  appearance: none;
  width: 100%; height: 6px;
  background: var(--bg);
  border: 1px solid var(--border);
  cursor: pointer;
}
.pb-scrub::-webkit-slider-thumb {
  -webkit-appearance: none;
  appearance: none;
  width: 16px; height: 16px;
  background: var(--accent);
  border-radius: 0;
  cursor: pointer;
}
.pb-scrub::-moz-range-thumb {
  width: 16px; height: 16px;
  background: var(--accent);
  border: none;
  border-radius: 0;
  cursor: pointer;
}

.pb-speed {
  display: inline-flex; gap: 2px;
  border: 1px solid var(--border);
}
.pb-speed button {
  background: var(--card-2);
  color: var(--text-dim);
  border: none;
  padding: 6px 9px;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 11px;
  font-weight: 700;
  cursor: pointer;
  font-variant-numeric: tabular-nums;
}
.pb-speed button:hover { color: var(--text); }
.pb-speed button.active {
  background: var(--accent-soft);
  color: var(--accent);
}

.pb-clock {
  display: inline-flex; align-items: baseline; gap: 6px;
  font-size: 12px;
  color: var(--text-dim);
}
.pb-clock-label {
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.1em;
  text-transform: uppercase;
}
.pb-clock #playback-clock,
.pb-clock #playback-score {
  color: var(--text);
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-weight: 800;
  font-variant-numeric: tabular-nums;
}
.pb-clock-sep { color: var(--text-faint); }

/* Event list (right rail) */
.pb-events {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 460px;
  overflow-y: auto;
  border: 1px solid var(--border);
  background: var(--card-2);
}
.pb-event {
  display: grid;
  grid-template-columns: 48px 78px 1fr;
  gap: 8px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--border);
  cursor: pointer;
  transition: background 120ms ease;
  font-size: 12px;
}
.pb-event:hover { background: var(--card); }
.pb-event.pb-current {
  background: var(--accent-soft);
  border-left: 3px solid var(--accent);
  padding-left: 7px;
}
.pb-event.pb-past { opacity: 0.6; }
.pb-event-time {
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-weight: 700;
  font-size: 11px;
  color: var(--text-dim);
  font-variant-numeric: tabular-nums;
}
.pb-event-tag {
  font-size: 9px;
  font-weight: 800;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--text-dim);
  padding: 2px 6px;
  border: 1px solid var(--border-strong);
  align-self: center;
  text-align: center;
}
.pb-event-body { color: var(--text); align-self: center; }
.pb-event-body b { font-weight: 700; }
.pb-event-meta { color: var(--text-dim); font-size: 11px; margin-left: 4px; }

/* Per-kind colors on the event row */
.pb-event-goal .pb-event-tag,
.pb-event-goal.team-blue .pb-event-tag { color: var(--team-blue); border-color: rgba(45,125,255,0.45); background: var(--team-blue-soft); }
.pb-event-goal.team-orng .pb-event-tag { color: var(--team-orng); border-color: var(--accent-line); background: var(--team-orng-soft); }
.pb-event-crossbar .pb-event-tag { color: var(--warn); border-color: rgba(251,191,36,0.4); background: rgba(251,191,36,0.10); }
.pb-event-demo .pb-event-tag { color: var(--bad); border-color: rgba(248,113,113,0.4); background: rgba(248,113,113,0.10); }
.pb-event-epic-save .pb-event-tag,
.pb-event-save .pb-event-tag { color: var(--good); border-color: rgba(52,211,153,0.4); background: rgba(52,211,153,0.10); }
.pb-event-shot .pb-event-tag,
.pb-event-assist .pb-event-tag { color: var(--text); border-color: var(--border-strong); background: var(--bg); }
.pb-event-aerial .pb-event-tag,
.pb-event-flip-reset .pb-event-tag,
.pb-event-bicycle .pb-event-tag,
.pb-event-hat-trick .pb-event-tag,
.pb-event-celebrate .pb-event-tag {
  color: var(--accent); border-color: var(--accent-line); background: var(--accent-soft);
}

.pb-event-empty {
  padding: 16px;
  color: var(--text-dim);
  text-align: center;
  font-size: 12px;
}

/* ============================================================
   Insights card: heatmap + possession + touch breakdown
   ============================================================ */
.insights-card { margin-top: 14px; }
.insights-heatmap { margin-bottom: 18px; min-width: 0; }
.insights-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 24px;
  align-items: start;
}
@media (max-width: 820px) {
  .insights-row { grid-template-columns: 1fr; }
}
.insights-subtitle {
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--text-dim);
  margin-bottom: 8px;
}
.insights-bars, .insights-touches { min-width: 0; }

/* Heatmap pitch */
.hm-wrap {
  width: 100%;
  background: var(--card-2);
  border: 1px solid var(--border);
}
.hm-pitch {
  width: 100%;
  height: auto;
  display: block;
}
/* Per-match touch spots: one marker per touch (overlap darkens, so density
   still reads) — clearer than a density heatmap on a single match's ~40 touches. */
.tspot { fill: var(--accent); fill-opacity: 0.5; stroke: var(--bg-elev); stroke-width: 0.6; }
.hm-layer { mix-blend-mode: screen; }
[data-theme="light"] .hm-layer { mix-blend-mode: multiply; }
.hm-legend {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  margin-top: 8px;
  font-size: 11px;
  color: var(--text-dim);
}
.hm-legend-item { display: inline-flex; align-items: center; gap: 6px; }
.hm-legend-item.team-blue, .hm-legend .team-blue { color: var(--team-blue); }
.hm-legend-item.team-orng, .hm-legend .team-orng { color: var(--team-orng); }
.hm-swatch { width: 12px; height: 12px; display: inline-block; flex-shrink: 0; }
.hm-swatch.team-blue { background: var(--team-blue); }
.hm-swatch.team-orng { background: var(--team-orng); }

/* Possession + pressure dual bars */
.dual-bar {
  display: flex;
  width: 100%;
  height: 26px;
  background: var(--card-2);
  border: 1px solid var(--border);
  overflow: hidden;
}
.dual-bar-blue,
.dual-bar-orng {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 12px;
  font-weight: 800;
  font-variant-numeric: tabular-nums;
  color: var(--card);
  transition: width 320ms ease;
}
.dual-bar-blue { background: var(--team-blue); }
.dual-bar-orng { background: var(--team-orng); }
.dual-bar-label { padding: 0 8px; white-space: nowrap; }
.dual-bar-foot {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  margin-top: 4px;
}
.dual-bar-foot > span {
  min-width: 0;
  flex: 0 1 auto;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 50%;
}
.dual-bar-foot > span:last-child { text-align: right; }

/* Touch breakdown list */
.touch-list {
  list-style: none;
  padding: 0;
  margin: 0;
}
.touch-row {
  display: grid;
  grid-template-columns: 140px 1fr 80px;
  gap: 8px;
  align-items: center;
  padding: 4px 0;
  font-size: 12px;
  border-bottom: 1px dotted var(--border);
}
.touch-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 600;
}
.touch-row.team-blue .touch-name { color: var(--team-blue); }
.touch-row.team-orng .touch-name { color: var(--team-orng); }
.touch-bar {
  position: relative;
  height: 8px;
  background: var(--card-2);
  border: 1px solid var(--border);
  display: block;
}
.touch-bar-fill {
  display: block;
  height: 100%;
  background: var(--text-faint);
  transition: width 200ms ease;
}
.touch-row.team-blue .touch-bar-fill { background: var(--team-blue); }
.touch-row.team-orng .touch-bar-fill { background: var(--team-orng); }
.touch-num {
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 11px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  text-align: right;
}

/* Mini touch heatmap inside radar cards */
.rc-mini-heatmap {
  width: 100%;
  background: var(--card-2);
  border: 1px solid var(--border);
  padding: 4px;
}
.rc-mini-heatmap .hm-pitch-compact {
  width: 100%;
  height: auto;
  display: block;
}

/* Compare-page lifetime heatmap row */
.compare-hm-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 16px;
  margin-top: 4px;
}
.cmp-hm-select {
  font-size: 12px; font-weight: 600; padding: 4px 10px; border-radius: 6px;
  border: 1px solid var(--border); background: var(--card); color: var(--text);
  cursor: pointer; margin-left: auto;
}
.compare-hm-card {
  border: 1px solid var(--border);
  background: var(--card-2);
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.compare-hm-head {
  padding: 8px 12px;
  font-size: 13px;
  font-weight: 800;
  letter-spacing: -0.01em;
  border-bottom: 1px solid var(--border);
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.compare-hm-body {
  padding: 4px;
}
.compare-hm-foot {
  padding: 6px 12px 8px;
  font-size: 11px;
  border-top: 1px solid var(--border);
}
.compare-hm-empty {
  padding: 24px 12px;
  text-align: center;
  font-size: 12px;
  color: var(--text-dim);
}

/* ---- Live match view ---- */

.live-page {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.live-status {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  margin-bottom: 2px;
}
.live-status #live-meta {
  font-size: 12.5px;
  color: var(--text-dim);
}
.live-status #live-meta b { color: var(--text); }

.live-team-badge {
  font-size: 11px;
  color: var(--text-dim);
  letter-spacing: 0.04em;
  text-transform: uppercase;
  margin-top: 4px;
}
.live-team-badge:empty { display: none; }

/* Boost bar cell inside the live roster table */
.live-boost-cell {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 90px;
  padding-top: 6px;
  padding-bottom: 6px;
}
.live-boost-cell .tnum {
  min-width: 28px;
  text-align: right;
  font-size: 12px;
  color: var(--text);
}
.live-boost-bar {
  flex: 1;
  height: 6px;
  background: var(--bg);
  border: 1px solid var(--border);
  overflow: hidden;
  min-width: 48px;
  position: relative;
}
.live-boost-fill {
  height: 100%;
  background: linear-gradient(90deg, var(--accent), var(--accent-2));
  transition: width 220ms ease;
}
[data-theme="light"] .live-boost-bar {
  background: var(--card-2);
}

/* Supersonic flag pill */
.live-supersonic {
  display: inline-block;
  margin-left: 6px;
  padding: 1px 6px;
  font-size: 9.5px;
  font-weight: 800;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--accent);
  background: var(--accent-soft);
  border: 1px solid var(--accent-line);
  vertical-align: middle;
  animation: live-super-flicker 900ms ease-in-out infinite;
}
@keyframes live-super-flicker {
  0%, 100% { opacity: 1; }
  50%      { opacity: 0.55; }
}

/* Position cell (wall / ground / air / no car) */
.live-pos-cell {
  font-size: 11.5px;
  font-weight: 600;
  letter-spacing: 0.02em;
  text-transform: uppercase;
  color: var(--text-dim);
}

/* Idle placeholder card */
.live-idle {
  margin-top: 8px;
}
.live-idle .empty {
  padding: 28px 18px;
}
.live-idle code {
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 12px;
  background: var(--bg);
  border: 1px solid var(--border);
  padding: 1px 5px;
}

/* Team name in roster header — clip very long names */
.team-name-truncate {
  display: inline-block;
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: middle;
}

/* BOOST VIEW toggle inside /live — matches .live-pip / #theme-toggle chrome */
.live-view-toggle {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
  background: var(--card);
  color: var(--text-dim);
  border: 1px solid var(--border);
  border-radius: 0;
  padding: 7px 14px 7px 12px;
  font-family: inherit;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  cursor: pointer;
  transition: color 150ms ease, border-color 150ms ease, background 150ms ease;
}
.live-view-toggle:hover {
  color: var(--accent);
  border-color: var(--accent-line);
}
.live-view-toggle.is-active {
  color: var(--accent);
  background: var(--accent-soft);
  border-color: var(--accent-line);
}
.live-view-toggle .boost-fs-glyph {
  font-size: 13px;
  line-height: 1;
  transform: translateY(-0.5px);
}

/* When the page is in boost mode, hide all chrome but the toggle so the second
   monitor shows only the boost cards. */
body.live-mode-boost #live-meta,
body.live-mode-boost #live-pip {
  display: none;
}
body.live-mode-boost .live-status {
  justify-content: flex-end;
}

/* HIDE ME / SHOW ME toggle — only visible in boost mode */
.boost-self-toggle { display: none; }
body.live-mode-boost .boost-self-toggle { display: inline-flex; }

/* When excluding self, drop the user's own card entirely from the HUD */
body.boost-exclude-self .boost-hud-card.is-self { display: none; }

/* ---- Boost HUD (second-screen, BIG TEXT, fills the viewport) ---- */

.boost-hud {
  margin-top: 4px;
}
/* Default: slots stack vertically — one row per teammate, full width */
.boost-hud-grid,
.boost-hud-grid[data-count="2"],
.boost-hud-grid[data-count="3"],
.boost-hud-grid[data-count="4"] {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
/* Spectator view: both teams. Switch to two columns side by side. */
.boost-hud-grid[data-teams="2"] {
  display: grid;
  grid-template-columns: 1fr 1fr;
  grid-auto-rows: 1fr;
  gap: 10px;
  align-items: stretch;
}

/* Slot = header (label outside) + card (the bar box) */
.boost-hud-slot {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-height: 0;
}
.boost-hud-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  padding: 0 4px;
  flex: 0 0 auto;
}

.boost-hud-card {
  display: flex;
  flex-direction: row;
  align-items: center;
  justify-content: flex-end;
  gap: 18px;
  padding: 0 28px;
  background: var(--card);
  border: 4px solid #000;
  position: relative;
  overflow: hidden;
  isolation: isolate;
  flex: 1 1 auto;
  min-height: 0;
  transition: border-color 200ms ease, border-width 200ms ease, background 200ms ease;
}
.boost-hud-card.team-blue { border-color: #000; box-shadow: inset 4px 0 0 var(--team-blue); }
.boost-hud-card.team-orng { border-color: #000; box-shadow: inset 4px 0 0 var(--team-orng); }
.boost-hud-card.is-self   { box-shadow: inset 4px 0 0 var(--accent); }
[data-theme="light"] .boost-hud-card { border-color: #0c1426; }

/* Subtle background bar — fills left to right, sits behind the text */
.boost-hud-meter {
  position: absolute;
  inset: 0;
  background: transparent;
  border: 0;
  overflow: hidden;
  z-index: 0;
  pointer-events: none;
}
.boost-hud-meter-fill {
  height: 100%;
  width: 0;
  background: var(--text-faint);
  opacity: 0.28;
  transition: width 240ms ease, background 240ms ease, opacity 200ms ease;
}

/* Name and number float above the bar */
.boost-hud-name,
.boost-hud-pct {
  position: relative;
  z-index: 1;
}

/* Player name label — OUTSIDE the boost box (header above) */
.boost-hud-name {
  font-size: clamp(16px, 2.6vw, 32px);
  font-weight: 800;
  letter-spacing: 0.04em;
  line-height: 1.05;
  color: var(--text);
  text-transform: uppercase;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex: 1 1 auto;
  min-width: 0;
}

/* Number sits on the RIGHT of the bar box, right-aligned */
.boost-hud-pct {
  display: flex;
  align-items: baseline;
  gap: 8px;
  line-height: 0.9;
  position: relative;
  z-index: 1;
  flex: 0 0 auto;
  margin-left: auto;
}

/* State icon overlay — sits on the LEFT inside the bar, fades in by state */
.boost-hud-state-icon {
  position: relative;
  z-index: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  width: clamp(36px, 6vh, 84px);
  height: clamp(36px, 6vh, 84px);
}
.boost-hud-state-icon img {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: contain;
  filter: drop-shadow(0 2px 6px rgba(0,0,0,0.6));
  opacity: 0;
  transition: opacity 180ms ease;
  pointer-events: none;
}
.boost-hud-card.is-aerial .boost-hud-state-icon .ic-aerial { opacity: 1; }
.boost-hud-card.is-onwall .boost-hud-state-icon .ic-wall   { opacity: 1; }
.boost-hud-num {
  font-family: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace;
  font-variant-numeric: tabular-nums;
  font-feature-settings: "tnum";
  font-size: clamp(140px, 22vw, 420px);
  font-weight: 700;
  letter-spacing: -0.05em;
  line-height: 0.85;
  color: var(--text);
  transition: color 220ms ease;
  text-shadow: 0 4px 24px rgba(0, 0, 0, 0.4);
}
.boost-hud-percent {
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: clamp(46px, 7vw, 140px);
  font-weight: 600;
  color: var(--text-dim);
  letter-spacing: -0.04em;
}

/* Faint tier tint on the whole card so empty cards still read as low/red */
.boost-hud-card.tier-full    { background: rgba(52, 211, 153, 0.08); }
.boost-hud-card.tier-high    { background: rgba(250, 204, 21, 0.08); }
.boost-hud-card.tier-mid     { background: rgba(251, 146, 60, 0.10); }
.boost-hud-card.tier-low     { background: rgba(239, 68, 68, 0.14); }
.boost-hud-card.tier-unknown { background: var(--card); }

/* The bar fill colors — vivid, ~55% opacity so text stays readable */
.boost-hud-card.tier-full    .boost-hud-meter-fill { background: rgba(52, 211, 153, 0.55); }
.boost-hud-card.tier-high    .boost-hud-meter-fill { background: rgba(250, 204, 21, 0.55); }
.boost-hud-card.tier-mid     .boost-hud-meter-fill { background: rgba(251, 146, 60, 0.60); }
.boost-hud-card.tier-low     .boost-hud-meter-fill { background: rgba(239, 68, 68, 0.65); }
.boost-hud-card.tier-unknown .boost-hud-meter-fill { background: var(--text-faint); }

[data-theme="light"] .boost-hud-card.tier-full    { background: rgba(22, 163, 74, 0.10); }
[data-theme="light"] .boost-hud-card.tier-high    { background: rgba(202, 138, 4, 0.10); }
[data-theme="light"] .boost-hud-card.tier-mid     { background: rgba(234, 88, 12, 0.12); }
[data-theme="light"] .boost-hud-card.tier-low     { background: rgba(220, 38, 38, 0.14); }

/* When totally empty, paint the WHOLE card as the warning red bar */
.boost-hud-card.tier-low.is-empty .boost-hud-meter-fill { width: 100% !important; opacity: 0.45; }
/* When totally full, paint the WHOLE card green */
.boost-hud-card.tier-full.is-full .boost-hud-meter-fill { width: 100% !important; opacity: 0.45; }

.boost-hud-card.tier-full .boost-hud-num { color: #34d399; }
.boost-hud-card.tier-low  .boost-hud-num { color: #ef4444; }
/* Blank/placeholder state - use the dim color (not faint) so the "--"
   is clearly readable even at the giant placeholder size. Add a soft
   "WAITING" watermark in the meter area so the user knows the card is
   alive and waiting for data, not broken. */
.boost-hud-card.no-data .boost-hud-num,
.boost-hud-card.no-data .boost-hud-percent {
  color: var(--text-dim);
  opacity: 0.7;
}
.boost-hud-card.no-data::after {
  content: "WAITING FOR LIVE DATA";
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: clamp(11px, 1.4vh, 18px);
  font-weight: 700;
  letter-spacing: 0.22em;
  color: var(--text-faint);
  pointer-events: none;
  z-index: 1;
}

/* Flag chips — absolute, top-right corner of the card */
.boost-hud-flags {
  position: absolute;
  top: 14px;
  right: 18px;
  z-index: 2;
  display: flex;
  flex-direction: row;
  align-items: center;
  gap: 6px;
}
.boost-hud-chip {
  display: none;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: clamp(10px, 1.2vh, 18px);
  font-weight: 800;
  letter-spacing: 0.16em;
  padding: 4px 9px;
  border: 1px solid transparent;
  line-height: 1;
  white-space: nowrap;
}
.boost-hud-card.is-aerial   .boost-hud-chip.aerial   { display: inline-block; }
.boost-hud-card.is-boosting .boost-hud-chip.boosting { display: inline-block; }
.boost-hud-card.is-super    .boost-hud-chip.super    { display: inline-block; }

.boost-hud-chip.aerial {
  color: #60a5fa;
  background: rgba(96, 165, 250, 0.12);
  border-color: rgba(96, 165, 250, 0.45);
}

/* Big THICK BLACK square around the card when the player is airborne. */
.boost-hud-card.is-aerial {
  border-width: 14px;
  border-color: #000;
  /* Keep the team-color inset stripe visible — increase its width to match
     the thicker frame. */
}
.boost-hud-card.team-blue.is-aerial { box-shadow: inset 4px 0 0 var(--team-blue); }
.boost-hud-card.team-orng.is-aerial { box-shadow: inset 4px 0 0 var(--team-orng); }
.boost-hud-card.is-self.is-aerial   { box-shadow: inset 4px 0 0 var(--accent); }
.boost-hud-chip.boosting {
  color: var(--accent);
  background: var(--accent-soft);
  border-color: var(--accent-line);
  animation: boost-hud-pulse 700ms ease-in-out infinite;
}
.boost-hud-chip.super {
  color: var(--bg);
  background: var(--accent);
}

@keyframes boost-hud-pulse {
  0%, 100% { opacity: 1; }
  50%      { opacity: 0.45; }
}

/* ---- Fullscreen scale: when boost mode is on, the HUD fills the window ---- */

/* Suppress any document-level scrollbar in boost mode. The HUD owns the
   whole viewport. */
body.live-mode-boost,
html:has(body.live-mode-boost) { overflow: hidden; }

body.live-mode-boost main,
body.live-mode-boost .container,
body.live-mode-boost .page,
body.live-mode-boost .wrapper,
body.live-mode-boost .app-shell {
  max-width: none;
  padding-left: 10px;
  padding-right: 10px;
  padding-top: 0;
  padding-bottom: 8px;
  height: 100vh;
  display: flex;
  flex-direction: column;
  min-height: 0;
}
/* In BOOST mode the sidebar collapses so the HUD owns the viewport.
   The whole vertical chain (wrapper → page-layout → page-main → live-page →
   boost-root → boost-hud → boost-hud-grid → slots) must be flex-column with
   `flex: 1 1 auto; min-height: 0` so the slots can stretch to 100vh.
   Previously page-layout and page-main were `display: block`, which stopped
   the flex chain cold and the HUD collapsed to 32px tall. */
body.live-mode-boost .side-filters { display: none !important; }
body.live-mode-boost .page-layout {
  display: flex !important;
  flex-direction: column !important;
  grid-template-columns: 1fr !important;
  gap: 0 !important;
  flex: 1 1 auto !important;
  min-height: 0 !important;
  width: 100% !important;
}
body.live-mode-boost .page-main {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-height: 0;
  width: 100%;
}
/* Topnav is ~58px on default; flex-shrink lets it stay its natural size and
   leaves the rest for .live-page. */
body.live-mode-boost .topnav { flex: 0 0 auto; }
body.live-mode-boost .live-page {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
  overflow: hidden;
}
body.live-mode-boost .live-status {
  flex: 0 0 auto;
}
body.live-mode-boost #boost-root {
  flex: 1 1 auto;
  display: flex;
  min-height: 0;
}
body.live-mode-boost .boost-hud {
  flex: 1 1 auto;
  display: flex;
  min-height: 0;
  width: 100%;
  margin-top: 0;
}
body.live-mode-boost .boost-hud-grid {
  flex: 1 1 auto;
  height: 100%;
  width: 100%;
  gap: 8px;
}
body.live-mode-boost .boost-hud-slot {
  flex: 1 1 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
body.live-mode-boost .boost-hud-header {
  flex: 0 0 auto;
  padding: 0 6px;
}
body.live-mode-boost .boost-hud-card {
  flex: 1 1 auto;
  min-height: 0;
  padding: 0 clamp(20px, 3vw, 56px);
}
/* Critical: each slot gets a fraction of the available viewport, and the
   number font scales to that fraction so it always fits.

   Per-slot height = (100vh - nav - status - paddings) / N
   We use 100cqh inside the slot via container-queries-style logic — but
   without container queries support everywhere, we instead clamp to small
   absolute viewport-h fractions that work for up to 4 cards.
*/
body.live-mode-boost .boost-hud-name {
  font-size: clamp(14px, 2.6vh, 26px);
  letter-spacing: 0.06em;
}
/* Boost number scales with the smaller of vh-per-card or vw. For 1 card we
   get the full viewport height; for 4 cards each gets a quarter. */
body.live-mode-boost .boost-hud-grid[data-count="1"] .boost-hud-num { font-size: clamp(80px, min(58vh, 22vw), 480px); }
body.live-mode-boost .boost-hud-grid[data-count="2"] .boost-hud-num { font-size: clamp(70px, min(32vh, 20vw), 320px); }
body.live-mode-boost .boost-hud-grid[data-count="3"] .boost-hud-num { font-size: clamp(60px, min(22vh, 18vw), 230px); }
body.live-mode-boost .boost-hud-grid[data-count="4"] .boost-hud-num { font-size: clamp(50px, min(17vh, 15vw), 180px); }
body.live-mode-boost .boost-hud-grid:not([data-count="1"]):not([data-count="2"]):not([data-count="3"]):not([data-count="4"]) .boost-hud-num {
  font-size: clamp(40px, min(12vh, 12vw), 140px);
}
body.live-mode-boost .boost-hud-percent {
  font-size: 0.42em;
  color: var(--text-dim);
}

@media (max-width: 800px) {
  .boost-hud-grid,
  .boost-hud-grid[data-count="2"],
  .boost-hud-grid[data-count="3"],
  .boost-hud-grid[data-count="4"] { grid-template-columns: 1fr; }
  .boost-hud-card { padding: 22px 24px; }
}

</style>
"""


def _featured_players(store, *, limit: int = 8) -> list[dict]:
    """Most-active real players, for the splash quick-jump chips. Auto-derived
    from match counts so any deployment surfaces its own crew; override with the
    CHUMSTATS_FEATURED_PIDS env var (comma-separated primary_ids) for a curated
    list. No account ids are baked into the code."""
    if store is None:
        return []
    import os
    override = [p.strip() for p in os.environ.get("CHUMSTATS_FEATURED_PIDS", "").split(",") if p.strip()]
    try:
        with store._conn() as con:
            rows = con.execute("""
                SELECT mps.primary_id AS pid, MAX(mps.name) AS name,
                       COUNT(DISTINCT mps.match_id) AS n, MIN(mps.platform) AS platform
                FROM match_player_stats mps
                WHERE COALESCE(mps.is_bot, 0) = 0
                  AND mps.primary_id NOT LIKE 'Unknown%'
                GROUP BY mps.primary_id
                HAVING COUNT(DISTINCT mps.match_id) > 20
                ORDER BY n DESC
            """).fetchall()
    except Exception:
        return []
    by_pid = {r["pid"]: dict(r) for r in rows}
    if override:
        return [by_pid[p] for p in override if p in by_pid]
    # Prefer players with real activity so the featured list reads as a crew, not
    # a list of one-off opponents; fall back to the top-N on a fresh deployment.
    meaningful = [dict(r) for r in rows if (r["n"] or 0) >= 3]
    return (meaningful or [dict(r) for r in rows])[:limit]


_SPLASH_STYLE = """<style>
.splash { max-width: 860px; margin: 0 auto; padding: 28px 20px 64px; }
.splash-hero { text-align: center; padding: 20px 0 30px; }
.splash-logo { width: 76px; height: 76px; }
.splash-hero h1 { font-size: 40px; margin: 12px 0 6px; letter-spacing: -0.02em; }
.splash-tagline { color: var(--muted, #9aa3b2); max-width: 560px; margin: 0 auto 22px; font-size: 15px; line-height: 1.5; }
.splash-cta { display: flex; gap: 10px; justify-content: center; flex-wrap: wrap; }
.splash-cta a { padding: 9px 17px; border-radius: 10px; font-weight: 600; text-decoration: none; font-size: 14px; }
.splash-cta .btn-primary { background: var(--accent, #ff7a18); color: #0a0d14; }
.splash-cta .btn-ghost { border: 1px solid var(--accent-line, rgba(255,122,24,.32)); color: var(--text, #e8edf6); }
.splash-friends { margin-top: 30px; }
.splash-friends h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .09em; color: var(--muted, #9aa3b2); margin: 0 0 12px; }
.splash-chips { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 10px; }
.splash-chip { display: flex; flex-direction: column; gap: 3px; padding: 12px 15px; border-radius: 12px; background: var(--card, #131826); border: 1px solid rgba(255,255,255,.06); text-decoration: none; color: var(--text, #e8edf6); transition: border-color .15s, transform .15s; }
.splash-chip:hover { border-color: var(--accent, #ff7a18); transform: translateY(-1px); }
.splash-chip.self { border-color: var(--accent-line, rgba(255,122,24,.45)); }
.splash-chip-name { font-weight: 700; }
.splash-chip-meta { font-size: 12px; color: var(--muted, #9aa3b2); }
</style>"""


def _splash_html(store, self_primary_id: str | None = None) -> str:
    """Neutral chumstats landing page: brand, one-line pitch, get-started CTA,
    and quick-jump chips for the most-active players (the owner is just one of
    them). Every interpolated name is escaped (public, multi-user page)."""
    chips = []
    for p in _featured_players(store):
        name = p.get("name") or "Player"
        # Neutral all-players landing: the owner is just one of the chips — no
        # "· you" tag, no self-highlight.
        meta = f"{p.get('n', 0)} matches"
        chips.append(
            f'<a class="splash-chip" '
            f'href="/player/{quote(name, safe="")}">'
            f'<span class="splash-chip-name">{html.escape(name)}</span>'
            f'<span class="splash-chip-meta">{html.escape(meta)}</span></a>'
        )
    chips_html = ("".join(chips)
                  or '<p class="dim">No players yet &mdash; upload a match to get started.</p>')
    body = f"""
      {_SPLASH_STYLE}
      <section class="splash">
        <div class="splash-hero">
          <img class="splash-logo" src="/static/brand/chum-logo.png" alt="Chumstats" />
          <h1>Chumstats</h1>
          <p class="splash-tagline">Rocket League match stats for Chum and his friends &mdash;
            goals, boost, positioning, and the story of every game, from everyone who plays.</p>
          <div class="splash-cta">
            <a class="btn-primary" href="/players">Browse all players</a>
            <a class="btn-ghost" href="/about">How it works</a>
          </div>
        </div>
        <div class="splash-friends">
          <h2>Jump to a player</h2>
          <div class="splash-chips">{chips_html}</div>
        </div>
      </section>
    """
    return _page_wrap("Home", body, active="", with_sidebar=False)


def _overlay_picker_html(host: str, friend_mode: bool = False) -> str:
    """Picker page with LIVE iframe previews + copy URLs for each overlay mode."""
    modes = [
        ("live",    "Live HUD",
         "Full BARL-style scoreboard during the match. Team names, scores, clock, and per-player stats, all live.",
         "640 x 230", "top-center", 230),
        ("last",    "Last Match Card",
         "Final scoreline + per-player stats from the most recent finished match. Persists between matches.",
         "640 x 260", "any corner", 260),
        ("session", "Session Tracker",
         "Running W-L, current streak, last-10 form (✓/✗), session totals. Tiny corner companion.",
         "340 x 110", "bottom-left", 110),
        ("me",      "My Stats Mini",
         "Just your Goals / Assists / Saves / Shots line. Smallest footprint, fits beside the RL boost gauge.",
         "280 x 50",  "any corner", 60),
    ]
    cards = []
    for mode, title, desc, size, place, prev_h in modes:
        url = f"http://{host}/overlay/{mode}"
        card_height = max(prev_h + 60, 220)
        cards.append(f"""
          <div class="overlay-card" style="min-height:{card_height}px">
            <div class="overlay-card-info">
              <h2 class="ov-title">{title}</h2>
              <p class="ov-desc">{desc}</p>
              <div class="copy-row">
                <code id="url-{mode}">{url}</code>
                <button class="copy-btn" type="button" data-target="url-{mode}">Copy</button>
              </div>
              <div class="overlay-meta">
                <a class="open-link" href="/overlay/{mode}" target="_blank">Open fullscreen &rarr;</a>
                <span class="dim">{size} &middot; {place}</span>
              </div>
            </div>
            <div class="overlay-preview">
              <iframe class="overlay-iframe" src="/overlay/{mode}"
                title="Preview of {title}"></iframe>
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
          <li>Chumstats must be running for the URL to respond.</li>
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
    return _page_wrap("Browser overlay", body, active="overlay", friend_mode=friend_mode)


def _dashboard_html(d, store=None, primary_id: str | None = None,
                    name: str | None = None, is_self: bool = False,
                    include_bots: bool = False) -> str:
    """Render the Dashboard dataclass into a single-file HTML page."""
    kpis = _kpi_tiles_from_dashboard(d)
    radar = _radar_block_for_player(store, primary_id, name, include_bots=include_bots)
    history = _match_history_html(store, primary_id, name, limit=8, include_bots=include_bots)
    ball_section = _player_ball_section_html(store, name) if (store and name) else ""
    form_section = _recent_form_html(store, primary_id, name, include_bots=include_bots)

    detail_sections: list[str] = []
    teammate_sections: list[str] = []
    # Online-vs-offline removed per feedback; teammates get their own tab; the
    # "Recent form (last 10)" group renders as the form-dot strip instead.
    skip_titles = {"Overview", "Per-match averages", "Recent form (last 10)",
                   "Online vs offline"}
    for g in d.all_groups():
        if not g.lines or g.title in skip_titles:
            continue
        rows = "\n".join(
            f'<tr><td>{ml.label}</td><td><b>{ml.value}</b></td><td class="cmp">{ml.comparison}</td></tr>'
            for ml in g.lines
        )
        sec = f'<section><h2>{g.title}</h2><table>{rows}</table></section>'
        (teammate_sections if "teammate" in g.title.lower() else detail_sections).append(sec)

    page_title = "Career dashboard" if is_self else f"{name or d.player_label}"
    active = "dashboard" if is_self else ""

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

    # Bot-filter chip - default is "Filter Bot matches" ON (include_bots=False).
    base_path = "/dashboard" if is_self else f"/player/{quote(name, safe='')}"
    filter_html = _filter_chip_html(base_path, "Filter Bot matches", include_bots)

    # Subtitle only when it adds info — otherwise it's just the name twice.
    who_html = (f'<div class="who">{html.escape(d.player_label)}</div>'
                if d.player_label and d.player_label != page_title else "")
    # Quick nav to this player's (subject-parameterized) neutral pages, so any
    # player's matches/opponents/comparison are reachable — not just the owner's.
    _subjq = (f"?pid={quote(primary_id, safe='')}" if primary_id
              else (f"?name={quote(name, safe='')}" if name else ""))
    profile_links = (
        f'<div class="profile-links">'
        f'<a href="/history{_subjq}">Matches</a>'
        f'<a href="/compare?names={quote(name or "", safe="")}">Compare</a>'
        f'</div>'
    ) if (primary_id or name) else ""
    # Tabbed SPA (mirror the match page): one pane visible at a time, no long
    # scroll. Overview is the landing; Matches sits last.
    panes = [("overview", "Overview", f"{kpis}{form_section}{radar}")]
    if detail_sections:
        panes.append(("breakdown", "Breakdown",
                      f'<div class="detail-grid">{"".join(detail_sections)}</div>'))
    if teammate_sections:
        panes.append(("teammates", "Teammates",
                      f'<div class="detail-grid">{"".join(teammate_sections)}</div>'))
    if ball_section:
        panes.append(("heatmap", "Heatmap", ball_section))
    panes.append(("matches", "Matches", history))
    nav = ('<nav class="match-nav" id="profile-nav">'
           + "".join(f'<button type="button" class="mn-chip{" active" if i == 0 else ""}" '
                     f'data-target="{k}">{lbl}</button>'
                     for i, (k, lbl, _) in enumerate(panes))
           + '</nav>')
    panes_html = "".join(
        f'<section class="md-pane{" active" if i == 0 else ""}" data-pane="{k}">{c}</section>'
        for i, (k, lbl, c) in enumerate(panes))
    pane_js = ("<script>(function(){var nav=document.getElementById('profile-nav');"
               "if(!nav)return;function show(n){document.querySelectorAll('.md-pane')"
               ".forEach(function(p){p.classList.toggle('active',p.dataset.pane===n);});"
               "nav.querySelectorAll('.mn-chip').forEach(function(c){"
               "c.classList.toggle('active',c.dataset.target===n);});}"
               "nav.querySelectorAll('.mn-chip').forEach(function(el){"
               "el.addEventListener('click',function(){show(el.dataset.target);});});})();</script>")
    body = f"""
  <div class="profile-header">
    <h1>{html.escape(page_title)} {bot_badge}</h1>
  </div>
  {who_html}
  {profile_links}
  {filter_html}
  <style>
    .detail-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;align-items:start}}
    @media(max-width:760px){{.detail-grid{{grid-template-columns:1fr}}}}
    #profile-nav{{position:static}}
    .kpi-row{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}}
  </style>
  {nav}
  {panes_html}
  {pane_js}
"""
    # Use the shared chrome so the global filter bar shows up on the profile too.
    return _page_wrap(d.player_label, body, active=active)


async def serve(app: FastAPI, host: str = "127.0.0.1", port: int = 5050) -> None:
    import uvicorn  # local import keeps cli imports cheap
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    await server.serve()
