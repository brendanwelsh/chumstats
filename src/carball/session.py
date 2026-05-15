"""Match-level aggregator and session tracker.

The aggregator consumes a stream of parsed events and emits exactly one
MatchSummary per finished match (i.e. when MatchEnded fires AFTER at least
one goal-or-stat event - aborted matches that never got a MatchEnded are
discarded). The session tracker keeps running W/L + streak + totals across
finished matches in the current process run.

Design notes:
- Match boundary detection: MatchCreated -> ... -> (MatchEnded?) -> MatchDestroyed.
  We start a new match aggregator on MatchCreated and finalize on MatchEnded.
  MatchDestroyed without a preceding MatchEnded = aborted; we discard.
- "You" identification: by default we pick the player whose primary_id is
  not "Unknown|0|0" (bot) and matches a configured self_name OR a configured
  self_primary_id. If neither is given we guess: the first non-bot player.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Iterable

from .models import (
    BallHit,
    CrossbarHit,
    GoalScored,
    MatchEnded,
    MatchLifecycle,
    PlayerState,
    StatfeedEvent,
    UpdateState,
)

# Speed units in this API appear normalized to ~0-100+ where 100 is car
# top-speed-without-boost. Supersonic in real RL is at ~2200 uu/s; in our
# fixture captures players topped at ~82-110 in normal play. Using 80 as a
# "moving fast" threshold approximates supersonic-time in this metric.
# Recalibrate once we collect more matches.
SUPERSONIC_THRESHOLD = 80.0


# --- value objects ----------------------------------------------------------

@dataclass
class PlayerLine:
    name: str
    primary_id: str
    team_num: int
    goals: int = 0
    shots: int = 0
    assists: int = 0
    saves: int = 0
    demos: int = 0
    touches: int = 0
    score: int = 0
    is_bot: bool = False
    platform: str = "Unknown"

    # Derived from tick state across the match.
    ticks_on_wall: int = 0
    ticks_on_ground: int = 0
    ticks_in_air: int = 0
    ticks_boosting: int = 0
    ticks_total: int = 0
    speed_sum: float = 0.0
    speed_max: float = 0.0
    ticks_supersonic: int = 0
    boost_used: float = 0.0           # sum of negative boost deltas
    ticks_zero_boost: int = 0
    ticks_full_boost: int = 0

    @property
    def avg_speed(self) -> float:
        return (self.speed_sum / self.ticks_total) if self.ticks_total else 0.0

    @property
    def pct_on_wall(self) -> float:
        return (self.ticks_on_wall / self.ticks_total) if self.ticks_total else 0.0

    @property
    def pct_on_ground(self) -> float:
        return (self.ticks_on_ground / self.ticks_total) if self.ticks_total else 0.0

    @property
    def pct_in_air(self) -> float:
        return (self.ticks_in_air / self.ticks_total) if self.ticks_total else 0.0

    @property
    def pct_supersonic(self) -> float:
        return (self.ticks_supersonic / self.ticks_total) if self.ticks_total else 0.0

    @classmethod
    def from_player_state(cls, p: PlayerState) -> "PlayerLine":
        return cls(
            name=p.name,
            primary_id=p.primary_id,
            team_num=p.team_num,
            goals=p.goals,
            shots=p.shots,
            assists=p.assists,
            saves=p.saves,
            demos=p.demos,
            touches=p.touches,
            score=p.score,
            is_bot=p.is_bot,
            platform=p.platform,
        )


@dataclass
class BallTouch:
    """A single BallHit captured for heatmap / shot-map rendering."""
    t: float  # seconds since match start
    x: float
    y: float
    z: float
    player: str
    team_num: int
    pre_speed: float
    post_speed: float


@dataclass
class MatchSummary:
    match_id: str  # MatchGuid if non-empty, else a synthesized uuid
    started_at: float
    ended_at: float
    arena: str
    team0_score: int
    team1_score: int
    team0_name: str
    team1_name: str
    winner_team_num: int  # 0 or 1
    players: list[PlayerLine] = field(default_factory=list)
    is_mvp: dict[str, bool] = field(default_factory=dict)  # primary_id -> True
    crossbar_hits: int = 0
    is_online: bool = False  # MatchGuid present => online/LAN
    color_primary: dict[int, str] = field(default_factory=dict)  # team_num -> hex color
    ball_touches: list[BallTouch] = field(default_factory=list)
    duration_seconds: float = 0.0
    goal_events: list[dict] = field(default_factory=list)  # deduped goal records

    def team_name(self, team_num: int) -> str:
        return self.team0_name if team_num == 0 else self.team1_name

    def team_score(self, team_num: int) -> int:
        return self.team0_score if team_num == 0 else self.team1_score

    @property
    def match_type(self) -> str:
        """Heuristic match-type detection.

        The Stats API doesn't emit playlist/mode, so we infer from what's
        observable in the stream:
          - No MatchGuid       -> Exhibition (offline)
          - Custom team name   -> Private / Tournament (one or both teams
                                  has a non-default name; matchmaking always
                                  uses "Blue" / "Orange")
          - Bots present       -> Casual vs Bots (matchmaking has no bots)
          - Otherwise          -> Public matchmaking
        """
        if not self.is_online:
            return "Exhibition"
        DEFAULT_TEAM_NAMES = {"blue", "orange", ""}
        custom = (self.team0_name.lower() not in DEFAULT_TEAM_NAMES
                  or self.team1_name.lower() not in DEFAULT_TEAM_NAMES)
        has_bot = any(p.is_bot for p in self.players)
        if custom:
            return "Private / Tournament"
        if has_bot:
            return "Casual vs Bots"
        return "Online Matchmaking"

    def me(self, self_primary_id: str | None = None, self_name: str | None = None) -> PlayerLine | None:
        if self_primary_id:
            for p in self.players:
                if p.primary_id == self_primary_id:
                    return p
        if self_name:
            for p in self.players:
                if p.name == self_name:
                    return p
        for p in self.players:
            if not p.is_bot:
                return p
        return None

    def result_for(self, line: PlayerLine) -> str:
        if line.team_num == self.winner_team_num:
            return "W"
        return "L"


# --- aggregator -------------------------------------------------------------

class MatchAggregator:
    """One per match. Consumes events; produces a MatchSummary on finalize().

    Use:
        agg = MatchAggregator()
        for event_name, parsed in stream:
            agg.handle(event_name, parsed)
        if agg.ended:
            summary = agg.finalize()
    """

    def __init__(self) -> None:
        self.match_guid: str = ""
        self.started_at: float = time.time()
        self.ended_at: float | None = None
        self.last_update: UpdateState | None = None
        self.winner_team_num: int | None = None
        self.crossbar_hits: int = 0
        self._mvp_ids: set[str] = set()  # primary_ids of MVP recipients

        # Derived per-player accumulators keyed by primary_id (fall back to name).
        self._lines: dict[str, PlayerLine] = {}
        self._last_boost: dict[str, int] = {}

        # Ball touch events for heatmap.
        self._ball_touches: list[BallTouch] = []

        # Goal log (deduped by (goal_time, scorer.name)).
        self._goals: list[dict] = []
        self._seen_goal_keys: set[tuple] = set()

    @property
    def ended(self) -> bool:
        return self.winner_team_num is not None

    @property
    def has_meaningful_play(self) -> bool:
        """An aborted match has no UpdateState with any goal yet."""
        if not self.last_update:
            return False
        return any(p.goals > 0 for p in self.last_update.players) or self.ended

    @staticmethod
    def _player_key(primary_id: str, name: str) -> str:
        """All bots share PrimaryId 'Unknown|0|0', so we must include name
        to distinguish them. Real players are uniquely keyed by primary_id."""
        if not primary_id or primary_id == "Unknown|0|0":
            return f"name:{name}"
        return primary_id

    def handle(self, event_name: str, parsed, raw: dict | None = None) -> None:
        if event_name == "MatchCreated" and isinstance(parsed, MatchLifecycle):
            self.match_guid = parsed.match_guid
            self.started_at = time.time()

        elif event_name == "UpdateState" and isinstance(parsed, UpdateState):
            self.last_update = parsed
            if parsed.match_guid:
                self.match_guid = parsed.match_guid
            self._accumulate_tick(parsed)

        elif event_name == "MatchEnded" and isinstance(parsed, MatchEnded):
            self.winner_team_num = parsed.winner_team_num
            self.ended_at = time.time()
            if parsed.match_guid:
                self.match_guid = parsed.match_guid

        elif event_name == "CrossbarHit":
            self.crossbar_hits += 1

        elif event_name == "GoalScored" and isinstance(parsed, GoalScored):
            if parsed.is_replay_echo:
                return
            key = (parsed.goal_time, parsed.scorer.name, parsed.scorer.team_num)
            if key in self._seen_goal_keys:
                return
            self._seen_goal_keys.add(key)
            self._goals.append({
                "goal_time": parsed.goal_time,
                "scorer": parsed.scorer.name,
                "scorer_team": parsed.scorer.team_num,
                "assister": parsed.assister.name if parsed.assister else None,
                "goal_speed": parsed.goal_speed,
                "impact_location": (
                    [parsed.impact_location.x, parsed.impact_location.y, parsed.impact_location.z]
                    if parsed.impact_location else None
                ),
            })

        elif event_name == "BallHit" and raw:
            self._record_ball_hit(raw)

        elif event_name == "StatfeedEvent" and isinstance(parsed, StatfeedEvent):
            if parsed.event_name == "MVP":
                # We don't have primary_id here, just name/team. Resolve to
                # primary_id on the LAST update state we saw.
                if self.last_update:
                    for p in self.last_update.players:
                        if p.name == parsed.main_target.name and p.team_num == parsed.main_target.team_num:
                            self._mvp_ids.add(p.primary_id)

    def _accumulate_tick(self, update: UpdateState) -> None:
        for p in update.players:
            key = self._player_key(p.primary_id, p.name)
            line = self._lines.get(key)
            if line is None:
                line = PlayerLine.from_player_state(p)
                self._lines[key] = line
            else:
                # Always refresh visible counters from the latest tick - these
                # ARE captured for every player (basic stats).
                line.goals = p.goals
                line.shots = p.shots
                line.assists = p.assists
                line.saves = p.saves
                line.demos = p.demos
                line.touches = p.touches
                line.score = p.score

            # Movement / boost are SPECTATOR-only - emitted by Psyonix for
            # you + your teammates, omitted for opponents. We detect that
            # absence and skip the per-tick accumulation entirely so we
            # don't accidentally report "89% in air" for an opponent whose
            # boolean fields were never sent.
            has_adv = (
                p.speed is not None or p.boost is not None
                or p.is_on_wall is not None or p.is_on_ground is not None
                or p.has_car is not None or p.is_boosting is not None
            )
            if not has_adv:
                continue

            line.ticks_total += 1
            if p.is_on_wall:
                line.ticks_on_wall += 1
            elif p.is_on_ground:
                line.ticks_on_ground += 1
            else:
                # In-air is the residual, only meaningful when we KNOW the
                # other two are false rather than just unset.
                line.ticks_in_air += 1

            if p.speed is not None:
                line.speed_sum += p.speed
                if p.speed > line.speed_max:
                    line.speed_max = p.speed
                if p.speed >= SUPERSONIC_THRESHOLD:
                    line.ticks_supersonic += 1

            if p.boost is not None:
                last = self._last_boost.get(key)
                if last is not None and p.boost < last:
                    line.boost_used += (last - p.boost)
                self._last_boost[key] = p.boost
                if p.boost <= 0:
                    line.ticks_zero_boost += 1
                if p.boost >= 100:
                    line.ticks_full_boost += 1

            if p.is_boosting:
                line.ticks_boosting += 1

    def _record_ball_hit(self, raw: dict) -> None:
        players = raw.get("Players") or []
        ball = raw.get("Ball") or {}
        loc = ball.get("Location") or {}
        t = 0.0
        if self.last_update is not None and self.last_update.game and self.last_update.game.time_seconds is not None:
            t = self.last_update.game.time_seconds
        for p in players:
            self._ball_touches.append(BallTouch(
                t=t,
                x=float(loc.get("X", 0.0)),
                y=float(loc.get("Y", 0.0)),
                z=float(loc.get("Z", 0.0)),
                player=p.get("Name", ""),
                team_num=int(p.get("TeamNum", 0)),
                pre_speed=float(ball.get("PreHitSpeed", 0.0)),
                post_speed=float(ball.get("PostHitSpeed", 0.0)),
            ))

    def finalize(self) -> MatchSummary | None:
        """Return a MatchSummary if the match actually ended; else None."""
        if self.winner_team_num is None or self.last_update is None:
            return None

        update = self.last_update
        teams_by_num = {t.team_num: t for t in update.game.teams}
        team0 = teams_by_num.get(0)
        team1 = teams_by_num.get(1)
        is_online = bool(self.match_guid)
        match_id = self.match_guid or f"local-{uuid.uuid4().hex[:12]}"

        # Merge: the accumulator has derived fields; the final tick has
        # the latest counters. Key by player_key so distinct bots (all with
        # primary_id "Unknown|0|0") don't collapse.
        seen: dict[str, PlayerLine] = {}
        for line in self._lines.values():
            seen[self._player_key(line.primary_id, line.name)] = line
        for p in update.players:
            key = self._player_key(p.primary_id, p.name)
            if key not in seen:
                seen[key] = PlayerLine.from_player_state(p)

        players = list(seen.values())
        is_mvp = {pid: True for pid in self._mvp_ids}

        color_primary: dict[int, str] = {}
        if team0 and team0.color_primary:
            color_primary[0] = team0.color_primary
        if team1 and team1.color_primary:
            color_primary[1] = team1.color_primary

        ended_at = self.ended_at or time.time()
        # Best effort: wall clock if it's meaningful (live mode), otherwise
        # estimate from tick count / 30Hz (replay mode).
        wall_dur = max(0.0, ended_at - self.started_at)
        max_ticks = max((p.ticks_total for p in self._lines.values()), default=0)
        tick_dur = max_ticks / 30.0
        duration = wall_dur if wall_dur > tick_dur * 0.5 else tick_dur

        return MatchSummary(
            match_id=match_id,
            started_at=self.started_at,
            ended_at=ended_at,
            arena=update.game.arena or "",
            team0_score=team0.score if team0 else 0,
            team1_score=team1.score if team1 else 0,
            team0_name=(team0.name if team0 else "Blue") or "Blue",
            team1_name=(team1.name if team1 else "Orange") or "Orange",
            winner_team_num=self.winner_team_num,
            players=players,
            is_mvp=is_mvp,
            crossbar_hits=self.crossbar_hits,
            is_online=is_online,
            color_primary=color_primary,
            ball_touches=list(self._ball_touches),
            duration_seconds=duration,
            goal_events=list(self._goals),
        )


# --- session tracker --------------------------------------------------------

@dataclass
class SessionTotals:
    matches_played: int = 0
    wins: int = 0
    losses: int = 0
    goals: int = 0
    assists: int = 0
    saves: int = 0
    shots: int = 0
    demos: int = 0
    current_streak: int = 0  # negative for L-streak
    crossbar_hits: int = 0

    @property
    def win_rate(self) -> float:
        return self.wins / self.matches_played if self.matches_played else 0.0

    @property
    def streak_label(self) -> str:
        if self.current_streak == 0:
            return "—"
        return f"{abs(self.current_streak)}{'W' if self.current_streak > 0 else 'L'}"


class SessionTracker:
    """In-memory running totals for matches finalized this run."""

    def __init__(self, self_primary_id: str | None = None, self_name: str | None = None) -> None:
        self.self_primary_id = self_primary_id
        self.self_name = self_name
        self.totals = SessionTotals()
        self.match_log: list[MatchSummary] = []

    def add(self, summary: MatchSummary) -> None:
        self.match_log.append(summary)
        me = summary.me(self.self_primary_id, self.self_name)
        if not me:
            return

        won = me.team_num == summary.winner_team_num
        t = self.totals
        t.matches_played += 1
        if won:
            t.wins += 1
            t.current_streak = t.current_streak + 1 if t.current_streak >= 0 else 1
        else:
            t.losses += 1
            t.current_streak = t.current_streak - 1 if t.current_streak <= 0 else -1

        t.goals += me.goals
        t.assists += me.assists
        t.saves += me.saves
        t.shots += me.shots
        t.demos += me.demos
        t.crossbar_hits += summary.crossbar_hits


# --- convenience runner over an iterable of (event_name, parsed) -----------

def run_aggregation(events: Iterable[tuple[str, dict, object]]) -> list[MatchSummary]:
    """Run a full event stream through aggregation, returning every
    finalized match. Aborted matches (no MatchEnded) are dropped.

    Accepts (event_name, raw_dict, parsed) triples.
    """
    summaries: list[MatchSummary] = []
    agg: MatchAggregator | None = None

    for event_name, raw, parsed in events:
        if event_name == "MatchCreated":
            if agg is not None and agg.ended:
                s = agg.finalize()
                if s:
                    summaries.append(s)
            agg = MatchAggregator()

        if agg is None:
            continue

        agg.handle(event_name, parsed, raw=raw)

        if event_name == "MatchDestroyed":
            if agg.ended:
                s = agg.finalize()
                if s:
                    summaries.append(s)
            agg = None

    if agg is not None and agg.ended:
        s = agg.finalize()
        if s:
            summaries.append(s)

    return summaries
