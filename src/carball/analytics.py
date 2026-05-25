"""Match analytics for a competitive Discord group.

This module computes structured, comparable metrics about a finished match:
your line vs your own baseline, your line vs opponents and teammates in
the same match, and head-to-head splits against any other player whose
matches are in the DB.

The output is a tree of `MetricLine` records, each a `label / value /
comparison` triple. Bot / overlay / CLI all render the same tree, so we
keep one definition of "what's interesting" and reuse it everywhere.

No emoji vibes, no narrative. The intent is "what someone in a competitive
clan would want to see between games."
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Iterable

from .session import MatchSummary, PlayerLine


# ----- value objects --------------------------------------------------------

@dataclass(frozen=True)
class MetricLine:
    label: str
    value: str
    comparison: str = ""


@dataclass
class MetricGroup:
    title: str
    lines: list[MetricLine] = field(default_factory=list)


@dataclass
class MatchAnalytics:
    summary_block: list[MetricLine] = field(default_factory=list)
    you_block: MetricGroup = field(default_factory=lambda: MetricGroup("Your line"))
    movement_block: MetricGroup = field(default_factory=lambda: MetricGroup("Movement"))
    boost_block: MetricGroup = field(default_factory=lambda: MetricGroup("Boost"))
    opponents_block: MetricGroup = field(default_factory=lambda: MetricGroup("Opponents"))
    teammates_block: MetricGroup = field(default_factory=lambda: MetricGroup("Teammates"))
    h2h_block: MetricGroup = field(default_factory=lambda: MetricGroup("Head-to-head"))
    session_block: MetricGroup = field(default_factory=lambda: MetricGroup("Session"))

    def all_groups(self) -> Iterable[MetricGroup]:
        return (
            self.you_block, self.movement_block, self.boost_block,
            self.opponents_block, self.teammates_block,
            self.h2h_block, self.session_block,
        )


# ----- formatting helpers ---------------------------------------------------

def _pct(num: float, den: float, digits: int = 0) -> str:
    if not den:
        return "-"
    return f"{(num / den) * 100:.{digits}f}%"


def _per(num: float, den: float, digits: int = 2) -> str:
    if not den:
        return "-"
    return f"{(num / den):.{digits}f}"


def _vs(now: float, lifetime: float, *, kind: str = "abs", digits: int = 2) -> str:
    """Format a 'vs your lifetime' delta. `kind='abs'` for raw, `'pct'` for percentages."""
    if lifetime is None:
        return ""
    if kind == "pct":
        delta = now - lifetime
        sign = "+" if delta >= 0 else ""
        return f"lifetime {lifetime:.0f}% ({sign}{delta:.0f})"
    delta = now - lifetime
    sign = "+" if delta >= 0 else ""
    return f"lifetime {lifetime:.{digits}f} ({sign}{delta:.{digits}f})"


def _fmt_duration(seconds: float) -> str:
    mm = int(seconds // 60)
    ss = int(seconds % 60)
    return f"{mm}:{ss:02d}"


# ----- lifetime + H2H queries ----------------------------------------------

def _lifetime_avgs(con: sqlite3.Connection, primary_id: str) -> dict[str, float]:
    """Per-match averages for a player across ALL matches in the DB."""
    row = con.execute("""
        SELECT
            COUNT(*) AS matches,
            AVG(goals)   AS avg_goals,
            AVG(assists) AS avg_assists,
            AVG(saves)   AS avg_saves,
            AVG(shots)   AS avg_shots,
            AVG(demos)   AS avg_demos,
            AVG(score)   AS avg_score,
            AVG(CASE WHEN shots > 0 THEN (goals * 1.0 / shots) ELSE NULL END) AS avg_shot_pct,
            AVG(ticks_supersonic * 1.0 / NULLIF(ticks_total, 0)) AS avg_super_pct,
            AVG(ticks_in_air * 1.0 / NULLIF(ticks_total, 0))     AS avg_air_pct,
            AVG(ticks_on_wall * 1.0 / NULLIF(ticks_total, 0))    AS avg_wall_pct,
            AVG(boost_used) AS avg_boost_used,
            AVG(speed_sum * 1.0 / NULLIF(ticks_total, 0)) AS avg_speed
        FROM match_player_stats
        WHERE primary_id = ?
    """, (primary_id,)).fetchone()
    return dict(row) if row else {}


def _h2h_record(con: sqlite3.Connection, my_pid: str, other_pid: str | None,
                other_name: str | None) -> dict[str, int]:
    """Wins/losses/ties when `my_pid` and `other_player` were in the same match,
    on opposing teams. Falls back to name match for bots (Unknown|0|0)."""
    if other_pid and other_pid != "Unknown|0|0":
        rows = con.execute("""
            SELECT m.id, mps_me.team_num AS my_team, mps_other.team_num AS other_team, m.winner_team_num
            FROM matches m
            JOIN match_player_stats mps_me    ON mps_me.match_id    = m.id AND mps_me.primary_id    = ?
            JOIN match_player_stats mps_other ON mps_other.match_id = m.id AND mps_other.primary_id = ?
            WHERE mps_me.team_num != mps_other.team_num
        """, (my_pid, other_pid)).fetchall()
    elif other_name:
        rows = con.execute("""
            SELECT m.id, mps_me.team_num AS my_team, mps_other.team_num AS other_team, m.winner_team_num
            FROM matches m
            JOIN match_player_stats mps_me    ON mps_me.match_id    = m.id AND mps_me.primary_id    = ?
            JOIN match_player_stats mps_other ON mps_other.match_id = m.id AND mps_other.name       = ?
            WHERE mps_me.team_num != mps_other.team_num
        """, (my_pid, other_name)).fetchall()
    else:
        rows = []
    wins = losses = 0
    for r in rows:
        if r["my_team"] == r["winner_team_num"]:
            wins += 1
        else:
            losses += 1
    return {"matches": wins + losses, "wins": wins, "losses": losses}


def _coplay_record(con: sqlite3.Connection, my_pid: str, other_pid: str | None,
                   other_name: str | None) -> dict[str, int]:
    """Wins/losses when on the SAME team as the other player."""
    if other_pid and other_pid != "Unknown|0|0":
        rows = con.execute("""
            SELECT m.winner_team_num, mps_me.team_num AS my_team
            FROM matches m
            JOIN match_player_stats mps_me    ON mps_me.match_id    = m.id AND mps_me.primary_id    = ?
            JOIN match_player_stats mps_other ON mps_other.match_id = m.id AND mps_other.primary_id = ?
            WHERE mps_me.team_num = mps_other.team_num
              AND (mps_me.name != mps_other.name OR mps_me.primary_id != mps_other.primary_id)
        """, (my_pid, other_pid)).fetchall()
    elif other_name:
        rows = con.execute("""
            SELECT m.winner_team_num, mps_me.team_num AS my_team
            FROM matches m
            JOIN match_player_stats mps_me    ON mps_me.match_id    = m.id AND mps_me.primary_id    = ?
            JOIN match_player_stats mps_other ON mps_other.match_id = m.id AND mps_other.name       = ?
            WHERE mps_me.team_num = mps_other.team_num
        """, (my_pid, other_name)).fetchall()
    else:
        rows = []
    wins = losses = 0
    for r in rows:
        if r["my_team"] == r["winner_team_num"]:
            wins += 1
        else:
            losses += 1
    return {"matches": wins + losses, "wins": wins, "losses": losses}


# ----- builders -------------------------------------------------------------

def build_analytics(s: MatchSummary, *,
                    self_primary_id: str | None = None,
                    self_name: str | None = None,
                    store=None,
                    session_totals=None) -> MatchAnalytics:
    """Compute the full analytics tree for a finished match."""
    a = MatchAnalytics()
    me = s.me(self_primary_id, self_name)

    # ---- summary line(s) ----
    duration = _fmt_duration(s.duration_seconds) if s.duration_seconds else "?"
    mode = "online" if s.is_online else "offline"
    a.summary_block.append(MetricLine(
        "Result",
        f"{s.team0_name} {s.team0_score} - {s.team1_score} {s.team1_name}",
        f"{duration} on {s.arena} ({mode})",
    ))

    if not me:
        return a

    # ---- your line ----
    shot_pct_now = (me.goals / me.shots) * 100 if me.shots else 0.0
    score_per_touch = (me.score / me.touches) if me.touches else 0.0

    a.you_block.lines.extend([
        MetricLine("Result", "Win" if me.team_num == s.winner_team_num else "Loss",
                   "MVP" if s.is_mvp.get(me.primary_id) else ""),
        MetricLine("Line", f"Goals {me.goals}  ·  Assists {me.assists}  ·  Saves {me.saves}  ·  Shots {me.shots}  ·  Demos {me.demos}",
                   f"score {me.score}"),
        MetricLine("Shooting", f"{me.goals}/{me.shots} ({shot_pct_now:.0f}%)" if me.shots else f"{me.goals}/0 (-)", ""),
        MetricLine("Touches", f"{me.touches}", f"{score_per_touch:.1f} pts/touch" if me.touches else ""),
        MetricLine("Demos delivered", str(me.demos), ""),
    ])

    # ---- movement (only meaningful if we have tick state) ----
    if me.ticks_total >= 200:
        a.movement_block.lines.extend([
            MetricLine("Avg speed",   f"{me.avg_speed:.1f}",           f"max {me.speed_max:.1f}"),
            MetricLine("Supersonic",  f"{me.pct_supersonic * 100:.0f}%", ""),
            MetricLine("Time in air", f"{me.pct_in_air * 100:.0f}%", ""),
            MetricLine("Time on wall", f"{me.pct_on_wall * 100:.0f}%", ""),
            MetricLine("Time on ground", f"{me.pct_on_ground * 100:.0f}%", ""),
        ])
        a.boost_block.lines.extend([
            MetricLine("Total used", f"{me.boost_used:.0f}",
                       f"{me.boost_used / max(s.duration_seconds, 1):.1f}/sec"),
            MetricLine("Time at 0",   f"{me.ticks_zero_boost // 30}s",
                       f"({me.ticks_zero_boost / me.ticks_total * 100:.0f}% of match)"),
            MetricLine("Time at 100", f"{me.ticks_full_boost // 30}s",
                       f"({me.ticks_full_boost / me.ticks_total * 100:.0f}% of match)"),
        ])

    # ---- opponents + teammates ----
    teammates = [p for p in s.players if p.team_num == me.team_num
                 and (p.primary_id != me.primary_id or p.name != me.name)]
    opponents = [p for p in s.players if p.team_num != me.team_num]

    for p in teammates:
        a.teammates_block.lines.append(MetricLine(
            p.name,
            f"Goals {p.goals}  ·  Assists {p.assists}  ·  Saves {p.saves}  ·  Shots {p.shots}  ·  Demos {p.demos}",
            f"score {p.score}",
        ))
    for p in opponents:
        a.opponents_block.lines.append(MetricLine(
            p.name,
            f"Goals {p.goals}  ·  Assists {p.assists}  ·  Saves {p.saves}  ·  Shots {p.shots}  ·  Demos {p.demos}",
            f"score {p.score}",
        ))

    # ---- comparison against your own baseline ----
    if store and me.primary_id:
        try:
            with store._conn() as con:
                avgs = _lifetime_avgs(con, me.primary_id)
                # Don't compare against just this match - look up after writing
                # would include current match. Subtract one for a stable feel:
                matches = (avgs.get("matches") or 0)
                if matches >= 2:
                    a.you_block.lines.append(MetricLine(
                        "vs your average",
                        "",
                        (f"Goals {avgs.get('avg_goals') or 0:.1f}  ·  "
                         f"Assists {avgs.get('avg_assists') or 0:.1f}  ·  "
                         f"Saves {avgs.get('avg_saves') or 0:.1f}  ·  "
                         f"Shots {avgs.get('avg_shots') or 0:.1f}  ·  "
                         f"shot% {(avgs.get('avg_shot_pct') or 0) * 100:.0f}% "
                         f"over {matches} matches"),
                    ))

                # ---- H2H ----
                for opp in opponents:
                    h2h = _h2h_record(con, me.primary_id, opp.primary_id, opp.name)
                    if h2h["matches"] >= 1:
                        a.h2h_block.lines.append(MetricLine(
                            f"vs {opp.name}",
                            f"{h2h['wins']}-{h2h['losses']}",
                            f"{h2h['matches']} match{'es' if h2h['matches'] != 1 else ''}",
                        ))
                for mate in teammates:
                    co = _coplay_record(con, me.primary_id, mate.primary_id, mate.name)
                    if co["matches"] >= 1:
                        a.h2h_block.lines.append(MetricLine(
                            f"with {mate.name}",
                            f"{co['wins']}-{co['losses']}",
                            f"{co['matches']} match{'es' if co['matches'] != 1 else ''}",
                        ))
        except Exception:
            pass

    # ---- session ----
    if session_totals and session_totals.matches_played >= 1:
        st = session_totals
        a.session_block.lines.extend([
            MetricLine("Session",
                       f"{st.wins}-{st.losses}",
                       f"win% {st.win_rate * 100:.0f}, streak {st.streak_label}"),
            MetricLine("Session totals",
                       f"Goals {st.goals}  ·  Assists {st.assists}  ·  Saves {st.saves}  ·  Shots {st.shots}  ·  Demos {st.demos}",
                       ""),
        ])

    return a


# ----- lifetime / career dashboard -----------------------------------------

@dataclass
class Dashboard:
    player_label: str = ""
    overview: MetricGroup = field(default_factory=lambda: MetricGroup("Overview"))
    averages: MetricGroup = field(default_factory=lambda: MetricGroup("Per-match averages"))
    movement: MetricGroup = field(default_factory=lambda: MetricGroup("Movement (lifetime)"))
    boost: MetricGroup = field(default_factory=lambda: MetricGroup("Boost (lifetime)"))
    records: MetricGroup = field(default_factory=lambda: MetricGroup("Single-match records"))
    arenas: MetricGroup = field(default_factory=lambda: MetricGroup("By arena"))
    modes: MetricGroup = field(default_factory=lambda: MetricGroup("By mode"))
    recent_form: MetricGroup = field(default_factory=lambda: MetricGroup("Recent form (last 10)"))
    teammates: MetricGroup = field(default_factory=lambda: MetricGroup("Best teammates"))
    opponents: MetricGroup = field(default_factory=lambda: MetricGroup("Toughest opponents"))

    def all_groups(self) -> Iterable[MetricGroup]:
        return (
            self.overview, self.averages, self.movement, self.boost,
            self.records, self.recent_form, self.modes, self.arenas,
            self.teammates, self.opponents,
        )


def build_dashboard(store, *, primary_id: str | None = None,
                    name: str | None = None,
                    include_bots: bool = True,
                    mode_filter: int | None = None,
                    platform_filter: str | None = None,
                    window_days: int | None = None) -> Dashboard:
    """Aggregate everything we know about one player from all stored matches."""
    d = Dashboard()
    d.player_label = primary_id or name or "(unknown player)"
    if not store or (not primary_id and not name):
        return d

    where = "primary_id = ?" if primary_id else "name = ?"
    arg = primary_id or name
    bot_filter = "" if include_bots else (
        " AND NOT EXISTS (SELECT 1 FROM match_player_stats x "
        "WHERE x.match_id = m.id AND x.is_bot = 1)"
    )
    # Mode filter: count team rosters and require team_size matches
    mode_filter_sql = ""
    if mode_filter is not None:
        mode_filter_sql = """
            AND (SELECT MAX(c) FROM (
                SELECT team_num, COUNT(*) AS c
                FROM match_player_stats
                WHERE match_id = m.id
                GROUP BY team_num
            )) = """ + str(int(mode_filter))
    # Platform filter targets the OPPONENT team (matches where the other
    # team had a player on that platform). Filtering by the user's own
    # platform isn't useful since they're always on the same one.
    platform_sql = ""
    if platform_filter:
        platform_sql = (
            " AND EXISTS (SELECT 1 FROM match_player_stats opp_p "
            "WHERE opp_p.match_id = m.id AND opp_p.team_num != mps.team_num "
            "AND opp_p.platform LIKE '%' || " + repr(platform_filter) + " || '%')"
        )
    # Window filter (last N days)
    window_sql = ""
    if window_days and window_days > 0:
        import time as _time
        cutoff = _time.time() - window_days * 86400
        window_sql = f" AND m.started_at >= {cutoff}"
    # Concatenate all filters
    bot_filter = bot_filter + mode_filter_sql + platform_sql + window_sql

    with store._conn() as con:
        # ---- overview ----
        row = con.execute(f"""
            SELECT
                COUNT(*)                                        AS matches,
                SUM(CASE WHEN team_num = m.winner_team_num THEN 1 ELSE 0 END) AS wins,
                SUM(is_mvp)                                     AS mvp,
                SUM(goals)                                      AS goals,
                SUM(assists)                                    AS assists,
                SUM(saves)                                      AS saves,
                SUM(shots)                                      AS shots,
                SUM(demos)                                      AS demos,
                SUM(score)                                      AS score,
                SUM(touches)                                    AS touches,
                SUM(ticks_total)                                AS ticks,
                SUM(ticks_on_wall)                              AS ticks_wall,
                SUM(ticks_in_air)                               AS ticks_air,
                SUM(ticks_on_ground)                            AS ticks_ground,
                SUM(ticks_supersonic)                           AS ticks_super,
                SUM(ticks_zero_boost)                           AS ticks_zero,
                SUM(ticks_full_boost)                           AS ticks_full,
                SUM(speed_sum)                                  AS speed_sum,
                MAX(speed_max)                                  AS speed_max,
                SUM(boost_used)                                 AS boost_used
            FROM match_player_stats mps
            JOIN matches m ON m.id = mps.match_id
            WHERE mps.{where}{bot_filter}
        """, (arg,)).fetchone()
        if not row or not row["matches"]:
            return d
        matches = row["matches"] or 0
        wins = row["wins"] or 0
        losses = matches - wins
        ticks = row["ticks"] or 0

        d.overview.lines.extend([
            MetricLine("Matches", str(matches), ""),
            MetricLine("Win-loss", f"{wins}-{losses}", f"win% {(wins / matches) * 100:.1f}"),
            MetricLine("MVP count", str(row["mvp"] or 0), f"rate {(row['mvp'] or 0) / matches * 100:.0f}%"),
            MetricLine("Total goals", str(row["goals"] or 0), ""),
            MetricLine("Total assists", str(row["assists"] or 0), ""),
            MetricLine("Total saves", str(row["saves"] or 0), ""),
            MetricLine("Total shots", str(row["shots"] or 0), ""),
            MetricLine("Total demos delivered", str(row["demos"] or 0), ""),
        ])

        # ---- per-match averages ----
        shooting_pct = ((row["goals"] or 0) / (row["shots"] or 1)) * 100
        d.averages.lines.extend([
            MetricLine("Goals/match",   f"{(row['goals'] or 0) / matches:.2f}", ""),
            MetricLine("Assists/match", f"{(row['assists'] or 0) / matches:.2f}", ""),
            MetricLine("Saves/match",   f"{(row['saves'] or 0) / matches:.2f}", ""),
            MetricLine("Shots/match",   f"{(row['shots'] or 0) / matches:.2f}", ""),
            MetricLine("Demos/match",   f"{(row['demos'] or 0) / matches:.2f}", ""),
            MetricLine("Score/match",   f"{(row['score'] or 0) / matches:.0f}", ""),
            MetricLine("Touches/match", f"{(row['touches'] or 0) / matches:.0f}", ""),
            MetricLine("Shooting %",    f"{shooting_pct:.1f}%", "career"),
        ])

        # ---- movement / boost (lifetime, weighted by tick count) ----
        if ticks >= 1000:
            d.movement.lines.extend([
                MetricLine("Avg speed", f"{(row['speed_sum'] or 0) / ticks:.2f}", f"all-time max {row['speed_max'] or 0:.1f}"),
                MetricLine("Supersonic", _pct(row["ticks_super"] or 0, ticks, 1), ""),
                MetricLine("In air",     _pct(row["ticks_air"] or 0, ticks, 1), ""),
                MetricLine("On wall",    _pct(row["ticks_wall"] or 0, ticks, 1), ""),
                MetricLine("On ground",  _pct(row["ticks_ground"] or 0, ticks, 1), ""),
            ])
            boost_per_match = (row["boost_used"] or 0) / matches
            d.boost.lines.extend([
                MetricLine("Avg used/match",   f"{boost_per_match:.0f}", f"~{boost_per_match / 100:.1f} full tanks"),
                MetricLine("Lifetime used",    f"{row['boost_used'] or 0:.0f}", ""),
                MetricLine("Time at 0 boost",  _pct(row["ticks_zero"] or 0, ticks, 1), "of all play"),
                MetricLine("Time at 100 boost",_pct(row["ticks_full"] or 0, ticks, 1), "of all play"),
            ])

        # ---- records ----
        records_filter = "" if include_bots else (
            " AND match_id IN (SELECT m.id FROM matches m WHERE NOT EXISTS "
            "(SELECT 1 FROM match_player_stats x WHERE x.match_id = m.id AND x.is_bot = 1))"
        )
        rec = con.execute(f"""
            SELECT
                MAX(goals)   AS max_g,
                MAX(assists) AS max_a,
                MAX(saves)   AS max_sv,
                MAX(shots)   AS max_sh,
                MAX(demos)   AS max_d,
                MAX(score)   AS max_score
            FROM match_player_stats
            WHERE {where}{records_filter}
        """, (arg,)).fetchone()
        if rec:
            d.records.lines.extend([
                MetricLine("Goals in a match",   str(rec["max_g"] or 0), ""),
                MetricLine("Assists in a match", str(rec["max_a"] or 0), ""),
                MetricLine("Saves in a match",   str(rec["max_sv"] or 0), ""),
                MetricLine("Shots in a match",   str(rec["max_sh"] or 0), ""),
                MetricLine("Demos in a match",   str(rec["max_d"] or 0), ""),
                MetricLine("Score in a match",   str(rec["max_score"] or 0), ""),
            ])

        # ---- by arena ----
        arena_rows = con.execute(f"""
            SELECT m.arena AS arena,
                   COUNT(*) AS n,
                   SUM(CASE WHEN mps.team_num = m.winner_team_num THEN 1 ELSE 0 END) AS w
            FROM match_player_stats mps
            JOIN matches m ON m.id = mps.match_id
            WHERE mps.{where}{bot_filter}
            GROUP BY m.arena
            ORDER BY n DESC
        """, (arg,)).fetchall()
        for r in arena_rows:
            if not r["arena"]:
                continue
            n = r["n"]; w = r["w"]
            d.arenas.lines.append(MetricLine(
                r["arena"], f"{w}-{n - w}", f"win% {(w / n) * 100:.0f}",
            ))

        # ---- by mode ----
        mode_rows = con.execute(f"""
            SELECT m.is_online AS is_online,
                   COUNT(*) AS n,
                   SUM(CASE WHEN mps.team_num = m.winner_team_num THEN 1 ELSE 0 END) AS w
            FROM match_player_stats mps
            JOIN matches m ON m.id = mps.match_id
            WHERE mps.{where}{bot_filter}
            GROUP BY m.is_online
        """, (arg,)).fetchall()
        for r in mode_rows:
            label = "Online" if r["is_online"] else "Offline"
            n = r["n"]; w = r["w"]
            d.modes.lines.append(MetricLine(label, f"{w}-{n - w}", f"win% {(w / n) * 100:.0f}"))

        # ---- recent form ----
        recent_rows = con.execute(f"""
            SELECT m.started_at, mps.team_num, m.winner_team_num,
                   mps.goals, mps.assists, mps.saves
            FROM match_player_stats mps
            JOIN matches m ON m.id = mps.match_id
            WHERE mps.{where}{bot_filter}
            ORDER BY m.started_at DESC
            LIMIT 10
        """, (arg,)).fetchall()
        if recent_rows:
            # Spaced check/cross instead of "WWLWW" - easier to read at a glance.
            pattern = " ".join(
                "✓" if r["team_num"] == r["winner_team_num"] else "✗"
                for r in reversed(recent_rows)
            )
            recent_g = sum((r["goals"] or 0) for r in recent_rows) / len(recent_rows)
            recent_a = sum((r["assists"] or 0) for r in recent_rows) / len(recent_rows)
            recent_sv = sum((r["saves"] or 0) for r in recent_rows) / len(recent_rows)
            d.recent_form.lines.extend([
                MetricLine(f"Last {len(recent_rows)}",     pattern, ""),
                MetricLine("Avg goals",   f"{recent_g:.2f}", ""),
                MetricLine("Avg assists", f"{recent_a:.2f}", ""),
                MetricLine("Avg saves",   f"{recent_sv:.2f}", ""),
            ])

        # ---- best teammates (min 2 shared matches) ----
        if primary_id and primary_id != "Unknown|0|0":
            tm_rows = con.execute("""
                SELECT mps_t.name AS name,
                       COUNT(*) AS n,
                       SUM(CASE WHEN mps_me.team_num = m.winner_team_num THEN 1 ELSE 0 END) AS w
                FROM match_player_stats mps_me
                JOIN matches m ON m.id = mps_me.match_id
                JOIN match_player_stats mps_t ON mps_t.match_id = m.id
                                              AND mps_t.team_num = mps_me.team_num
                                              AND NOT (mps_t.primary_id = mps_me.primary_id AND mps_t.name = mps_me.name)
                WHERE mps_me.primary_id = ?
                GROUP BY mps_t.name
                HAVING n >= 2
                ORDER BY (w * 1.0 / n) DESC, n DESC
                LIMIT 10
            """, (primary_id,)).fetchall()
            for r in tm_rows:
                d.teammates.lines.append(MetricLine(
                    r["name"], f"{r['w']}-{r['n'] - r['w']}",
                    f"win% {(r['w'] / r['n']) * 100:.0f} over {r['n']} matches",
                ))

            # ---- toughest opponents ----
            opp_rows = con.execute("""
                SELECT mps_o.name AS name,
                       COUNT(*) AS n,
                       SUM(CASE WHEN mps_me.team_num = m.winner_team_num THEN 1 ELSE 0 END) AS w
                FROM match_player_stats mps_me
                JOIN matches m ON m.id = mps_me.match_id
                JOIN match_player_stats mps_o ON mps_o.match_id = m.id
                                              AND mps_o.team_num != mps_me.team_num
                WHERE mps_me.primary_id = ?
                GROUP BY mps_o.name
                HAVING n >= 2
                ORDER BY (w * 1.0 / n) ASC, n DESC
                LIMIT 10
            """, (primary_id,)).fetchall()
            for r in opp_rows:
                d.opponents.lines.append(MetricLine(
                    r["name"], f"{r['w']}-{r['n'] - r['w']}",
                    f"win% {(r['w'] / r['n']) * 100:.0f} over {r['n']} matches",
                ))

    return d


# ----- text rendering -------------------------------------------------------

def render_text(a: MatchAnalytics) -> str:
    """Render the analytics tree as multi-line text. Used by CLI."""
    lines: list[str] = []
    for ml in a.summary_block:
        lines.append(f"{ml.label}: {ml.value}" + (f"   ({ml.comparison})" if ml.comparison else ""))
    for g in a.all_groups():
        if not g.lines:
            continue
        lines.append("")
        lines.append(f"-- {g.title} --")
        for ml in g.lines:
            row = f"  {ml.label:<22} {ml.value}"
            if ml.comparison:
                row += f"   ({ml.comparison})"
            lines.append(row)
    return "\n".join(lines)


@dataclass
class CompareRow:
    label: str
    a_value: str
    b_value: str
    delta: str = ""   # "+1.2", "+5%", etc.
    limited: bool = False  # opponent has spectator-limited adv fields


@dataclass
class ComparisonResult:
    a_label: str
    b_label: str
    rows: list[CompareRow] = field(default_factory=list)


def _lifetime_row(con, primary_id: str | None, name: str | None,
                  *, mode_filter: int | None = None,
                  window_days: int | None = None) -> dict:
    """Aggregate stats for either primary_id (preferred) or name. Optional
    filters narrow the result to matches that match playlist size / recency."""
    if primary_id and primary_id != "Unknown|0|0":
        where, arg = "primary_id = ?", primary_id
    elif name:
        where, arg = "name = ?", name
    else:
        return {}
    extras = ""
    if mode_filter is not None:
        extras += f"""
            AND (SELECT MAX(c) FROM (
                SELECT team_num, COUNT(*) AS c FROM match_player_stats
                WHERE match_id = m.id GROUP BY team_num
            )) = {int(mode_filter)}
        """
    if window_days and window_days > 0:
        import time as _time
        cutoff = _time.time() - window_days * 86400
        extras += f" AND m.started_at >= {cutoff}"
    row = con.execute(f"""
        SELECT
            COUNT(*) AS matches,
            SUM(CASE WHEN team_num = m.winner_team_num THEN 1 ELSE 0 END) AS wins,
            SUM(is_mvp)   AS mvp,
            SUM(goals)    AS goals,
            SUM(assists)  AS assists,
            SUM(saves)    AS saves,
            SUM(shots)    AS shots,
            SUM(demos)    AS demos,
            SUM(score)    AS score,
            SUM(touches)  AS touches,
            SUM(ticks_total)       AS ticks,
            SUM(ticks_on_wall)     AS ticks_wall,
            SUM(ticks_in_air)      AS ticks_air,
            SUM(ticks_on_ground)   AS ticks_ground,
            SUM(ticks_supersonic)  AS ticks_super,
            SUM(ticks_zero_boost)  AS ticks_zero,
            SUM(ticks_full_boost)  AS ticks_full,
            SUM(speed_sum)         AS speed_sum,
            MAX(speed_max)         AS speed_max,
            SUM(boost_used)        AS boost_used
        FROM match_player_stats mps
        JOIN matches m ON m.id = mps.match_id
        WHERE mps.{where}{extras}
    """, (arg,)).fetchone()
    return dict(row) if row else {}


def build_comparison(store, a_primary_id: str | None = None, a_name: str | None = None,
                     b_primary_id: str | None = None, b_name: str | None = None) -> ComparisonResult:
    """Build a side-by-side comparison of two players' lifetime stats from
    the DB. Either id or name works for each player."""
    res = ComparisonResult(
        a_label=a_name or a_primary_id or "(A)",
        b_label=b_name or b_primary_id or "(B)",
    )
    with store._conn() as con:
        a = _lifetime_row(con, a_primary_id, a_name)
        b = _lifetime_row(con, b_primary_id, b_name)
    if not a.get("matches") or not b.get("matches"):
        return res

    def _f(v, *, kind="i"):
        if v is None:
            return "—"
        if kind == "i":   return f"{int(v)}"
        if kind == "f1":  return f"{float(v):.1f}"
        if kind == "f2":  return f"{float(v):.2f}"
        if kind == "pct": return f"{float(v) * 100:.1f}%"
        return str(v)

    def _delta(av, bv, *, kind="i"):
        if av is None or bv is None:
            return ""
        d = float(av) - float(bv)
        sign = "+" if d >= 0 else ""
        if kind == "i":   return f"{sign}{int(d)}"
        if kind == "f1":  return f"{sign}{d:.1f}"
        if kind == "f2":  return f"{sign}{d:.2f}"
        if kind == "pct": return f"{sign}{d * 100:.1f}%"
        return str(d)

    def add(label, a_v, b_v, *, kind="i", limited=False):
        res.rows.append(CompareRow(
            label,
            _f(a_v, kind=kind), _f(b_v, kind=kind),
            _delta(a_v, b_v, kind=kind), limited,
        ))

    am = a["matches"]; bm = b["matches"]
    a_wins = a.get("wins") or 0; b_wins = b.get("wins") or 0

    add("Matches",     am, bm)
    add("Wins",        a_wins, b_wins)
    add("Losses",      am - a_wins, bm - b_wins)
    add("Win rate",    (a_wins / am) if am else 0, (b_wins / bm) if bm else 0, kind="pct")
    add("MVP count",   a.get("mvp"), b.get("mvp"))

    add("Goals total", a.get("goals"), b.get("goals"))
    add("Assists total", a.get("assists"), b.get("assists"))
    add("Saves total",   a.get("saves"), b.get("saves"))
    add("Shots total",   a.get("shots"), b.get("shots"))
    add("Demos total",   a.get("demos"), b.get("demos"))
    add("Score total",   a.get("score"), b.get("score"))
    add("Touches total", a.get("touches"), b.get("touches"))

    add("Goals/match",   (a.get("goals") or 0) / am, (b.get("goals") or 0) / bm, kind="f2")
    add("Assists/match", (a.get("assists") or 0) / am, (b.get("assists") or 0) / bm, kind="f2")
    add("Saves/match",   (a.get("saves") or 0) / am, (b.get("saves") or 0) / bm, kind="f2")
    add("Shots/match",   (a.get("shots") or 0) / am, (b.get("shots") or 0) / bm, kind="f2")
    add("Demos/match",   (a.get("demos") or 0) / am, (b.get("demos") or 0) / bm, kind="f2")
    add("Score/match",   (a.get("score") or 0) / am, (b.get("score") or 0) / bm, kind="f1")

    a_shot_pct = (a.get("goals") or 0) / a.get("shots") if a.get("shots") else 0
    b_shot_pct = (b.get("goals") or 0) / b.get("shots") if b.get("shots") else 0
    add("Shooting %", a_shot_pct, b_shot_pct, kind="pct")

    # Advanced - we only get full spectator-visible fields for you + your
    # teammates. A player who was usually your opponent will have a low
    # tick count and unreliable adv stats. Heuristic: if ticks-per-match
    # is below ~3000 (vs the ~15000 a normal 5-minute match emits), flag
    # it as "limited" so the user knows the number isn't comparable.
    a_ticks = a.get("ticks") or 0; b_ticks = b.get("ticks") or 0
    a_tpm = a_ticks / max(am, 1)
    b_tpm = b_ticks / max(bm, 1)
    LIMITED_THRESHOLD = 3000  # ticks/match below this means mostly-opponent

    def add_adv(label, a_val, b_val, *, kind="pct"):
        a_show = a_val if a_ticks > 0 else None
        b_show = b_val if b_ticks > 0 else None
        a_str = _f(a_show, kind=kind) if a_show is not None else "—"
        b_str = _f(b_show, kind=kind) if b_show is not None else "—"
        delta = _delta(a_show, b_show, kind=kind) if (a_show is not None and b_show is not None) else ""
        limited = (a_ticks == 0) or (b_ticks == 0) or (a_tpm < LIMITED_THRESHOLD) or (b_tpm < LIMITED_THRESHOLD)
        res.rows.append(CompareRow(label, a_str, b_str, delta, limited))

    if a_ticks > 0 or b_ticks > 0:
        add_adv("Avg speed",
            (a.get("speed_sum") or 0) / a_ticks if a_ticks else None,
            (b.get("speed_sum") or 0) / b_ticks if b_ticks else None,
            kind="f1")
        add_adv("Top speed", a.get("speed_max") if a_ticks else None,
                            b.get("speed_max") if b_ticks else None, kind="f1")
        add_adv("Supersonic %",
            (a.get("ticks_super") or 0) / a_ticks if a_ticks else None,
            (b.get("ticks_super") or 0) / b_ticks if b_ticks else None, kind="pct")
        add_adv("In air %",
            (a.get("ticks_air") or 0) / a_ticks if a_ticks else None,
            (b.get("ticks_air") or 0) / b_ticks if b_ticks else None, kind="pct")
        add_adv("On wall %",
            (a.get("ticks_wall") or 0) / a_ticks if a_ticks else None,
            (b.get("ticks_wall") or 0) / b_ticks if b_ticks else None, kind="pct")
        add_adv("On ground %",
            (a.get("ticks_ground") or 0) / a_ticks if a_ticks else None,
            (b.get("ticks_ground") or 0) / b_ticks if b_ticks else None, kind="pct")
        add_adv("Boost used/match",
            (a.get("boost_used") or 0) / am if a_ticks else None,
            (b.get("boost_used") or 0) / bm if b_ticks else None, kind="f1")
        add_adv("At 0 boost %",
            (a.get("ticks_zero") or 0) / a_ticks if a_ticks else None,
            (b.get("ticks_zero") or 0) / b_ticks if b_ticks else None, kind="pct")
        add_adv("At 100 boost %",
            (a.get("ticks_full") or 0) / a_ticks if a_ticks else None,
            (b.get("ticks_full") or 0) / b_ticks if b_ticks else None, kind="pct")
    return res


def render_comparison_text(c: ComparisonResult) -> str:
    lines = [
        f"=== compare ===",
        f"  {'':<22} {c.a_label:>16}   {c.b_label:>16}   {'delta':>10}",
        "  " + "-" * 70,
    ]
    if not c.rows:
        lines.append("  (no overlap - one or both players have no recorded matches)")
        return "\n".join(lines)
    has_limited = any(r.limited for r in c.rows)
    for r in c.rows:
        tail = " *" if r.limited else ""
        lines.append(f"  {r.label:<22} {r.a_value:>16}   {r.b_value:>16}   {r.delta:>10}{tail}")
    if has_limited:
        lines.append("")
        lines.append("  * Stats API marks boost / position / speed as SPECTATOR-only")
        lines.append("    fields, so adv-stats are unreliable for anyone you've mostly")
        lines.append("    played against (we only see them during goal replays).")
    return "\n".join(lines)


def render_dashboard_text(d: Dashboard) -> str:
    lines = [f"=== Career dashboard: {d.player_label} ==="]
    for g in d.all_groups():
        if not g.lines:
            continue
        lines.append("")
        lines.append(f"-- {g.title} --")
        for ml in g.lines:
            row = f"  {ml.label:<24} {ml.value}"
            if ml.comparison:
                row += f"   ({ml.comparison})"
            lines.append(row)
    return "\n".join(lines)
