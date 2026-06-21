"""Golden-master snapshot of the full aggregator output.

The aggregator is the single point where bad data is born, and the tick-derived
stats (boost, supersonic, air/ground time, speed) are exactly the ones that
can't be re-derived once ticks are pruned. This test serializes the COMPLETE
MatchSummary — including those fragile fields — and compares against committed
snapshots so any aggregation regression fails loudly instead of silently
poisoning the DB.

To intentionally update a snapshot after a deliberate logic change, delete the
relevant tests/golden/*.json and re-run (it regenerates, then asserts).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chumstats.replay import iter_for_aggregator
from chumstats.session import run_aggregation

GOLDEN = Path(__file__).resolve().parent / "golden"


def _player(p) -> dict:
    return {
        "name": p.name, "primary_id": p.primary_id, "team_num": p.team_num,
        "goals": p.goals, "shots": p.shots, "assists": p.assists, "saves": p.saves,
        "demos": p.demos, "touches": p.touches, "score": p.score,
        "is_bot": p.is_bot, "platform": p.platform,
        "ticks_total": p.ticks_total, "ticks_on_wall": p.ticks_on_wall,
        "ticks_on_ground": p.ticks_on_ground, "ticks_in_air": p.ticks_in_air,
        "ticks_boosting": p.ticks_boosting, "ticks_supersonic": p.ticks_supersonic,
        "ticks_zero_boost": p.ticks_zero_boost, "ticks_full_boost": p.ticks_full_boost,
        "speed_sum": round(p.speed_sum, 3), "speed_max": round(p.speed_max, 3),
        "boost_used": round(p.boost_used, 3),
    }


def _summary(s) -> dict:
    # match_id / started_at / ended_at / duration use wall-clock or uuids; omit
    # the non-deterministic bits and keep everything derived.
    return {
        "is_online": s.is_online,
        "arena": s.arena,
        "team0_score": s.team0_score, "team1_score": s.team1_score,
        "team0_name": s.team0_name, "team1_name": s.team1_name,
        "winner_team_num": s.winner_team_num,
        "crossbar_hits": s.crossbar_hits,
        "n_goal_events": len(s.goal_events),
        "n_ball_touches": len(s.ball_touches),
        "players": sorted((_player(p) for p in s.players),
                          key=lambda d: (d["team_num"], d["name"])),
    }


def _run(path) -> list[dict]:
    return [_summary(s) for s in run_aggregation(iter_for_aggregator(path))]


@pytest.mark.parametrize("name", ["online", "exhibition"])
def test_golden_snapshot(name, request):
    fixture = request.getfixturevalue(f"{name}_capture")
    got = _run(fixture)
    GOLDEN.mkdir(exist_ok=True)
    gp = GOLDEN / f"{name}.json"
    if not gp.is_file():
        gp.write_text(json.dumps(got, indent=2), encoding="utf-8")
        pytest.skip(f"generated golden {gp.name}; re-run to assert against it")
    expected = json.loads(gp.read_text(encoding="utf-8"))
    assert got == expected, (
        f"aggregator output drifted from tests/golden/{name}.json — if this was "
        f"an intentional logic change, delete that file and re-run to refresh it."
    )
