"""Pydantic models for the Rocket League Stats API event stream.

Schema observed from real captures (May 2026):

The wire format is concatenated JSON envelopes:
    {"Event": "<EventName>", "Data": "<json-encoded string>"}

`Data` is a JSON string and must be parsed a second time. EnvelopeRaw
captures the outer shape; specific event payload models parse the inner.

Known events seen in fixture data:
    MatchCreated, MatchInitialized, MatchDestroyed, MatchEnded,
    MatchPaused, MatchUnpaused, CountdownBegin, RoundStarted,
    UpdateState, ClockUpdatedSeconds, BallHit, GoalScored,
    CrossbarHit, StatfeedEvent, ReplayPlaybackStart, ReplayPlaybackEnd.

Documented but not yet observed: PodiumStart, ReplayCreated,
ReplayWillEnd (one capture had ReplayWillEnd; treat as a goal-replay marker).

Fields marked Optional are because some events only carry them in certain
contexts (e.g. MatchGuid is empty for offline / exhibition matches).
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


# --- shared sub-objects ------------------------------------------------------

class PlayerRef(_Model):
    """Lightweight player reference used in event payloads."""
    name: str = Field(alias="Name")
    shortcut: int | None = Field(default=None, alias="Shortcut")
    team_num: int = Field(alias="TeamNum")


class BallLastTouch(_Model):
    player: PlayerRef = Field(alias="Player")
    speed: float = Field(alias="Speed")


class Vec3(_Model):
    x: float = Field(alias="X")
    y: float = Field(alias="Y")
    z: float = Field(alias="Z")


class Team(_Model):
    name: str = Field(alias="Name")
    team_num: int = Field(alias="TeamNum")
    score: int = Field(alias="Score")
    color_primary: str | None = Field(default=None, alias="ColorPrimary")
    color_secondary: str | None = Field(default=None, alias="ColorSecondary")


class Ball(_Model):
    speed: float = Field(default=0.0, alias="Speed")
    team_num: int | None = Field(default=None, alias="TeamNum")


class PlayerState(_Model):
    """A player as it appears inside UpdateState.Players."""
    name: str = Field(alias="Name")
    primary_id: str = Field(alias="PrimaryId")  # "Steam|<id>|0" / "Epic|<guid>|0" / "Switch|<id>|0" / "Unknown|0|0" for bots
    shortcut: int | None = Field(default=None, alias="Shortcut")
    team_num: int = Field(alias="TeamNum")
    score: int = Field(default=0, alias="Score")
    goals: int = Field(default=0, alias="Goals")
    shots: int = Field(default=0, alias="Shots")
    assists: int = Field(default=0, alias="Assists")
    saves: int = Field(default=0, alias="Saves")
    touches: int = Field(default=0, alias="Touches")
    car_touches: int = Field(default=0, alias="CarTouches")
    demos: int = Field(default=0, alias="Demos")
    boost: int | None = Field(default=None, alias="Boost")
    speed: float | None = Field(default=None, alias="Speed")
    is_boosting: bool | None = Field(default=None, alias="bBoosting")
    is_on_ground: bool | None = Field(default=None, alias="bOnGround")
    is_on_wall: bool | None = Field(default=None, alias="bOnWall")
    has_car: bool | None = Field(default=None, alias="bHasCar")

    @property
    def is_bot(self) -> bool:
        return self.primary_id == "Unknown|0|0"

    @property
    def platform(self) -> str:
        # "Steam|76561197985273611|0" -> "Steam"
        return self.primary_id.split("|", 1)[0] if self.primary_id else "Unknown"


class GameState(_Model):
    teams: list[Team] = Field(default_factory=list, alias="Teams")
    time_seconds: float = Field(default=0.0, alias="TimeSeconds")
    is_overtime: bool = Field(default=False, alias="bOvertime")
    ball: Ball | None = Field(default=None, alias="Ball")
    is_replay: bool = Field(default=False, alias="bReplay")
    has_winner: bool = Field(default=False, alias="bHasWinner")
    winner: str = Field(default="", alias="Winner")
    arena: str = Field(default="", alias="Arena")
    has_target: bool = Field(default=False, alias="bHasTarget")
    target: PlayerRef | None = Field(default=None, alias="Target")


# --- event payloads ----------------------------------------------------------

class HasMatchGuid(_Model):
    match_guid: str = Field(default="", alias="MatchGuid")


class MatchLifecycle(HasMatchGuid):
    """Used for MatchCreated, MatchInitialized, MatchDestroyed,
    CountdownBegin, RoundStarted, MatchPaused, MatchUnpaused,
    ReplayPlaybackStart, ReplayPlaybackEnd, ReplayWillEnd, PodiumStart,
    ReplayCreated. They all carry only MatchGuid."""


class MatchEnded(HasMatchGuid):
    winner_team_num: int = Field(alias="WinnerTeamNum")


class ClockUpdate(HasMatchGuid):
    time_seconds: float = Field(alias="TimeSeconds")
    is_overtime: bool = Field(default=False, alias="bOvertime")


class UpdateState(HasMatchGuid):
    players: list[PlayerState] = Field(default_factory=list, alias="Players")
    game: GameState = Field(alias="Game")


class GoalScored(HasMatchGuid):
    goal_speed: float = Field(default=0.0, alias="GoalSpeed")
    goal_time: int = Field(default=0, alias="GoalTime")
    impact_location: Vec3 | None = Field(default=None, alias="ImpactLocation")
    scorer: PlayerRef = Field(alias="Scorer")
    assister: PlayerRef | None = Field(default=None, alias="Assister")
    ball_last_touch: BallLastTouch | None = Field(default=None, alias="BallLastTouch")

    @property
    def is_replay_echo(self) -> bool:
        """Some captures emit a second GoalScored for the same goal during
        the replay, with an empty Scorer.Name and GoalSpeed=0. Use this to
        dedupe."""
        return self.scorer.name == "" and self.goal_speed == 0.0


class CrossbarHit(HasMatchGuid):
    ball_location: Vec3 | None = Field(default=None, alias="BallLocation")
    ball_speed: float = Field(default=0.0, alias="BallSpeed")
    impact_force: float = Field(default=0.0, alias="ImpactForce")
    ball_last_touch: BallLastTouch | None = Field(default=None, alias="BallLastTouch")


class BallHit(HasMatchGuid):
    """We've observed this fires but haven't captured the full payload shape
    yet. Keep open until we see a richer example."""


class StatfeedEvent(HasMatchGuid):
    """In-game statfeed: Shot, Goal, Assist, Save, Demo, Win, MVP, ..."""
    event_name: str = Field(alias="EventName")
    type: str = Field(alias="Type")
    main_target: PlayerRef = Field(alias="MainTarget")
    secondary_target: PlayerRef | None = Field(default=None, alias="SecondaryTarget")


# --- envelope dispatch -------------------------------------------------------

EventPayload = (
    MatchLifecycle | MatchEnded | ClockUpdate | UpdateState
    | GoalScored | CrossbarHit | BallHit | StatfeedEvent
)

EVENT_MODEL: dict[str, type[BaseModel]] = {
    "MatchCreated": MatchLifecycle,
    "MatchInitialized": MatchLifecycle,
    "MatchDestroyed": MatchLifecycle,
    "MatchPaused": MatchLifecycle,
    "MatchUnpaused": MatchLifecycle,
    "CountdownBegin": MatchLifecycle,
    "RoundStarted": MatchLifecycle,
    "ReplayPlaybackStart": MatchLifecycle,
    "ReplayPlaybackEnd": MatchLifecycle,
    "ReplayWillEnd": MatchLifecycle,
    "PodiumStart": MatchLifecycle,
    "ReplayCreated": MatchLifecycle,
    "MatchEnded": MatchEnded,
    "ClockUpdatedSeconds": ClockUpdate,
    "UpdateState": UpdateState,
    "GoalScored": GoalScored,
    "CrossbarHit": CrossbarHit,
    "BallHit": BallHit,
    "StatfeedEvent": StatfeedEvent,
}


class Envelope(_Model):
    """Outer wrapper: `Data` is a JSON-encoded string."""
    event: str = Field(alias="Event")
    data: str = Field(alias="Data")

    def parse_payload(self) -> tuple[str, dict[str, Any], BaseModel | None]:
        """Returns (event_name, raw_dict, parsed_or_None).

        `parsed_or_None` is None for events we don't yet have a model for,
        so callers can still keep the raw dict.
        """
        raw = json.loads(self.data) if self.data else {}
        model = EVENT_MODEL.get(self.event)
        if model is None:
            return self.event, raw, None
        return self.event, raw, model.model_validate(raw)


def parse_envelope_line(line: str) -> Envelope:
    """Parse one line of a .jsonl capture file into an Envelope."""
    # PowerShell's StreamWriter writes a UTF-8 BOM at file start; strip it.
    if line.startswith("﻿"):
        line = line.lstrip("﻿")
    return Envelope.model_validate_json(line)


def parse_envelope_obj(obj: dict[str, Any]) -> Envelope:
    """Parse one already-decoded envelope dict."""
    return Envelope.model_validate(obj)
