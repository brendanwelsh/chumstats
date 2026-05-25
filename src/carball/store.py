"""SQLite persistence layer.

Design:
- We keep EVERYTHING. Three tables:
    matches              one row per finalized match
    match_player_stats   one row per player per match
    raw_events           one row per envelope, full payload JSON
                         (lets us reprocess captures later when we add metrics)

- `raw_events.match_id` is the same id used in `matches.id` for events that
  fell inside a finalized match. For events outside any match (between matches,
  before MatchCreated, etc.) it's NULL.
- We don't store binary blobs - everything is text JSON. Disk is cheap and it
  makes inspection trivial (just `sqlite3 carball.db` and read).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

from .session import MatchSummary

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    id              TEXT PRIMARY KEY,            -- MatchGuid or local-<uuid>
    started_at      REAL NOT NULL,               -- unix seconds
    ended_at        REAL NOT NULL,
    arena           TEXT NOT NULL,
    team0_score     INTEGER NOT NULL,
    team1_score     INTEGER NOT NULL,
    team0_name      TEXT NOT NULL DEFAULT 'Blue',
    team1_name      TEXT NOT NULL DEFAULT 'Orange',
    team0_color     TEXT NOT NULL DEFAULT '',
    team1_color     TEXT NOT NULL DEFAULT '',
    winner_team_num INTEGER NOT NULL,
    is_online       INTEGER NOT NULL,            -- 0/1
    crossbar_hits   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_matches_started ON matches(started_at);

CREATE TABLE IF NOT EXISTS match_player_stats (
    match_id    TEXT NOT NULL,
    primary_id  TEXT NOT NULL,                   -- e.g. "Steam|76561...|0" or "Unknown|0|0"
    name        TEXT NOT NULL,
    team_num    INTEGER NOT NULL,
    goals       INTEGER NOT NULL DEFAULT 0,
    shots       INTEGER NOT NULL DEFAULT 0,
    assists     INTEGER NOT NULL DEFAULT 0,
    saves       INTEGER NOT NULL DEFAULT 0,
    demos       INTEGER NOT NULL DEFAULT 0,
    touches     INTEGER NOT NULL DEFAULT 0,
    score       INTEGER NOT NULL DEFAULT 0,
    is_bot      INTEGER NOT NULL DEFAULT 0,
    is_mvp      INTEGER NOT NULL DEFAULT 0,
    platform    TEXT NOT NULL DEFAULT 'Unknown',

    -- Derived from tick state.
    ticks_total       INTEGER NOT NULL DEFAULT 0,
    ticks_on_wall     INTEGER NOT NULL DEFAULT 0,
    ticks_on_ground   INTEGER NOT NULL DEFAULT 0,
    ticks_in_air      INTEGER NOT NULL DEFAULT 0,
    ticks_boosting    INTEGER NOT NULL DEFAULT 0,
    ticks_supersonic  INTEGER NOT NULL DEFAULT 0,
    ticks_zero_boost  INTEGER NOT NULL DEFAULT 0,
    ticks_full_boost  INTEGER NOT NULL DEFAULT 0,
    speed_sum         REAL    NOT NULL DEFAULT 0,
    speed_max         REAL    NOT NULL DEFAULT 0,
    boost_used        REAL    NOT NULL DEFAULT 0,

    PRIMARY KEY (match_id, name, team_num),
    FOREIGN KEY (match_id) REFERENCES matches(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_mps_primary ON match_player_stats(primary_id);

CREATE TABLE IF NOT EXISTS raw_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at REAL NOT NULL,
    match_id    TEXT,
    event       TEXT NOT NULL,
    payload     TEXT NOT NULL                    -- raw inner JSON string
);

CREATE TABLE IF NOT EXISTS match_extras (
    match_id        TEXT PRIMARY KEY,
    duration_seconds REAL NOT NULL DEFAULT 0,
    ball_touches    TEXT NOT NULL DEFAULT '[]',  -- JSON array of BallTouch dicts
    goal_events     TEXT NOT NULL DEFAULT '[]',  -- JSON array of goal records
    FOREIGN KEY (match_id) REFERENCES matches(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_raw_match ON raw_events(match_id);
CREATE INDEX IF NOT EXISTS idx_raw_event ON raw_events(event);
"""


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            con.execute("PRAGMA foreign_keys=ON")
            yield con
            con.commit()
        finally:
            con.close()

    # --- writes -------------------------------------------------------------

    def save_match(self, s: MatchSummary) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO matches
                (id, started_at, ended_at, arena, team0_score, team1_score,
                 team0_name, team1_name, team0_color, team1_color,
                 winner_team_num, is_online, crossbar_hits)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    s.match_id, s.started_at, s.ended_at, s.arena,
                    s.team0_score, s.team1_score,
                    s.team0_name, s.team1_name,
                    s.color_primary.get(0, ""), s.color_primary.get(1, ""),
                    s.winner_team_num,
                    1 if s.is_online else 0, s.crossbar_hits,
                ),
            )
            c.execute("DELETE FROM match_player_stats WHERE match_id = ?", (s.match_id,))
            c.executemany(
                """
                INSERT INTO match_player_stats
                (match_id, primary_id, name, team_num, goals, shots, assists, saves,
                 demos, touches, score, is_bot, is_mvp, platform,
                 ticks_total, ticks_on_wall, ticks_on_ground, ticks_in_air,
                 ticks_boosting, ticks_supersonic, ticks_zero_boost, ticks_full_boost,
                 speed_sum, speed_max, boost_used)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        s.match_id, p.primary_id, p.name, p.team_num,
                        p.goals, p.shots, p.assists, p.saves, p.demos, p.touches, p.score,
                        1 if p.is_bot else 0,
                        1 if s.is_mvp.get(p.primary_id) else 0,
                        p.platform,
                        p.ticks_total, p.ticks_on_wall, p.ticks_on_ground, p.ticks_in_air,
                        p.ticks_boosting, p.ticks_supersonic, p.ticks_zero_boost, p.ticks_full_boost,
                        p.speed_sum, p.speed_max, p.boost_used,
                    )
                    for p in s.players
                ],
            )
            from dataclasses import asdict
            c.execute(
                """
                INSERT OR REPLACE INTO match_extras
                (match_id, duration_seconds, ball_touches, goal_events)
                VALUES (?, ?, ?, ?)
                """,
                (
                    s.match_id,
                    s.duration_seconds,
                    json.dumps([asdict(b) for b in s.ball_touches]),
                    json.dumps(s.goal_events),
                ),
            )

    def save_raw_event(self, received_at: float, match_id: str | None, event: str, payload: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO raw_events (received_at, match_id, event, payload) VALUES (?, ?, ?, ?)",
                (received_at, match_id, event, payload),
            )

    def save_raw_events_bulk(self, rows: Iterable[tuple[float, str | None, str, str]]) -> None:
        with self._conn() as c:
            c.executemany(
                "INSERT INTO raw_events (received_at, match_id, event, payload) VALUES (?, ?, ?, ?)",
                rows,
            )

    # --- maintenance --------------------------------------------------------

    def backfill_from_raw_events(self, since_ts: float | None = None) -> int:
        """Replay raw_events through MatchAggregator and save any matches that
        aren't already in `matches`. Returns the number of new matches saved.

        If the live ingest is restarted mid-match (or misses MatchCreated for a
        match that's already in progress), the events still land in raw_events
        but no `matches` row is produced. This method recovers them.
        """
        from .models import EVENT_MODEL
        from .session import run_aggregation

        with self._conn() as c:
            existing_ids = {r[0] for r in c.execute("SELECT id FROM matches")}
            sql = "SELECT event, payload FROM raw_events"
            params: tuple = ()
            if since_ts is not None:
                sql += " WHERE received_at >= ?"
                params = (since_ts,)
            sql += " ORDER BY received_at ASC, id ASC"
            cur = c.execute(sql, params)

            def _iter():
                for event_name, payload_str in cur:
                    if not event_name:
                        continue
                    try:
                        raw = json.loads(payload_str) if payload_str else {}
                    except Exception:
                        continue
                    model = EVENT_MODEL.get(event_name)
                    parsed = None
                    if model is not None:
                        try:
                            parsed = model.model_validate(raw)
                        except Exception:
                            parsed = None
                    yield event_name, raw, parsed

            summaries = run_aggregation(_iter())

        saved = 0
        for s in summaries:
            if not s.match_id or s.match_id in existing_ids:
                continue
            try:
                self.save_match(s)
                saved += 1
            except Exception:
                pass
        return saved

    def prune_raw_events(self, keep_days: int = 7, vacuum: bool = True) -> dict:
        """Drop the bulk high-rate event types (UpdateState, BallHit,
        ClockUpdatedSeconds) for matches that have already been aggregated
        into `matches`. Once a match has a summary row, the 30 Hz tick data
        is redundant: we keep MatchCreated/Initialized/Ended/Destroyed/
        GoalScored/CrossbarHit/StatfeedEvent so the match can still be
        replayed/re-aggregated, but the ticks are dropped immediately.

        For events with NULL match_id (orphans from missed-MatchCreated
        before the fix), prune by `keep_days` since they're not recoverable.

        Returns {'deleted': N, 'bytes_before': X, 'bytes_after': Y}.
        """
        import os as _os
        import time as _time

        path = self.db_path
        bytes_before = _os.path.getsize(path) if path and _os.path.exists(path) else None

        # Keep BallHit events forever - they're sparse (a few per second, not
        # 30/sec like UpdateState) and we need them for kickoff tracking and
        # touch heatmaps on historical matches.
        bulk_events = ("UpdateState", "ClockUpdatedSeconds")
        placeholders = ",".join(["?"] * len(bulk_events))
        cutoff = _time.time() - keep_days * 86400

        with self._conn() as c:
            # 1) Aggregated matches: drop ticks regardless of age.
            cur = c.execute(
                f"""
                DELETE FROM raw_events
                WHERE event IN ({placeholders})
                  AND match_id IN (SELECT id FROM matches)
                """,
                bulk_events,
            )
            deleted_aggregated = cur.rowcount or 0

            # 2) NULL-match-id orphans older than the cutoff (likely never
            #    going to become matches). Recent NULL events stay because the
            #    backfill on next startup may still associate them.
            cur = c.execute(
                f"""
                DELETE FROM raw_events
                WHERE event IN ({placeholders})
                  AND match_id IS NULL
                  AND received_at < ?
                """,
                (*bulk_events, cutoff),
            )
            deleted_orphan = cur.rowcount or 0

        deleted = deleted_aggregated + deleted_orphan
        if deleted and vacuum:
            try:
                with self._conn() as c:
                    c.execute("VACUUM")
            except Exception:
                pass
        bytes_after = _os.path.getsize(path) if path and _os.path.exists(path) else None
        return {
            "deleted": deleted,
            "deleted_aggregated": deleted_aggregated,
            "deleted_orphan": deleted_orphan,
            "bytes_before": bytes_before,
            "bytes_after": bytes_after,
        }

    # --- reads --------------------------------------------------------------

    def lifetime_for(self, primary_id: str | None = None, name: str | None = None) -> dict:
        """Return aggregate lifetime stats for a player, keyed by primary_id
        if given, else falling back to name."""
        if not primary_id and not name:
            return {}

        where = "primary_id = ?" if primary_id else "name = ?"
        arg = primary_id or name

        with self._conn() as c:
            row = c.execute(
                f"""
                SELECT
                    COUNT(DISTINCT mps.match_id) AS matches,
                    SUM(mps.goals)               AS goals,
                    SUM(mps.assists)             AS assists,
                    SUM(mps.saves)               AS saves,
                    SUM(mps.shots)               AS shots,
                    SUM(mps.demos)               AS demos,
                    SUM(mps.is_mvp)              AS mvp_count,
                    SUM(CASE WHEN mps.team_num = m.winner_team_num THEN 1 ELSE 0 END) AS wins
                FROM match_player_stats mps
                JOIN matches m ON m.id = mps.match_id
                WHERE mps.{where}
                """,
                (arg,),
            ).fetchone()
        if not row:
            return {}
        d = dict(row)
        d["losses"] = (d.get("matches") or 0) - (d.get("wins") or 0)
        return d

    def recent_matches(self, primary_id: str | None = None, limit: int = 10) -> list[dict]:
        with self._conn() as c:
            if primary_id:
                rows = c.execute(
                    """
                    SELECT m.*, mps.goals AS my_goals, mps.assists AS my_assists,
                           mps.saves AS my_saves, mps.shots AS my_shots,
                           mps.team_num AS my_team, mps.is_mvp AS my_mvp
                    FROM matches m
                    JOIN match_player_stats mps ON mps.match_id = m.id
                    WHERE mps.primary_id = ?
                    ORDER BY m.started_at DESC
                    LIMIT ?
                    """,
                    (primary_id, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM matches ORDER BY started_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]
