"""Envelope parsing against real captured event lines."""

from __future__ import annotations

import json

from chumstats.models import (
    EVENT_MODEL,
    Envelope,
    GoalScored,
    MatchEnded,
    StatfeedEvent,
    UpdateState,
    parse_envelope_line,
)


def test_envelope_shape(online_capture):
    with online_capture.open("r", encoding="utf-8") as f:
        line = f.readline().strip()
    env = parse_envelope_line(line)
    assert env.event == "MatchCreated"
    name, raw, parsed = env.parse_payload()
    assert name == "MatchCreated"
    assert isinstance(raw, dict)


def test_all_known_events_parse(online_capture):
    counts: dict[str, int] = {}
    failures: list[tuple[str, str]] = []
    with online_capture.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            env = parse_envelope_line(line)
            counts[env.event] = counts.get(env.event, 0) + 1
            try:
                env.parse_payload()
            except Exception as e:  # pragma: no cover - just record
                failures.append((env.event, str(e)))
    assert "UpdateState" in counts
    assert "GoalScored" in counts
    assert counts.get("MatchEnded", 0) == 1
    assert not failures, f"parse failures: {failures[:5]}"


def test_goal_dedupe_flag(online_capture):
    seen_real = 0
    seen_echo = 0
    with online_capture.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or '"Event":"GoalScored"' not in line:
                continue
            env = parse_envelope_line(line)
            _, _, parsed = env.parse_payload()
            assert isinstance(parsed, GoalScored)
            if parsed.is_replay_echo:
                seen_echo += 1
            else:
                seen_real += 1
    # Online match had 8 real goals (5-3) and 8 echoes during goal replays.
    assert seen_real == 8
    assert seen_echo == 8


def test_match_ended_payload(online_capture):
    with online_capture.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if '"Event":"MatchEnded"' in line:
                env = parse_envelope_line(line)
                _, _, parsed = env.parse_payload()
                assert isinstance(parsed, MatchEnded)
                assert parsed.winner_team_num in (0, 1)
                return
    raise AssertionError("MatchEnded not found in capture")
