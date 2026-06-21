"""Multi-uploader dedup invariant: when several friends play the same match and
each uploads it, the central DB must store the match ONCE and keep every
uploader's OWN full-coverage player row (regardless of upload order). Opponents
(owned by no one) land once, first-writer-wins.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from ballshark.store import Store

CHUM = "Steam|chum|0"
BLAZE = "Steam|blaze|0"
OPP = "Epic|opp|0"


def _prow(pid, name, team, ticks):
    return dict(
        primary_id=pid, name=name, team_num=team, goals=1, shots=2, assists=0,
        saves=1, demos=0, touches=10, score=200, is_bot=False, is_mvp=False,
        platform="Steam", ticks_total=ticks, ticks_on_wall=0, ticks_on_ground=0,
        ticks_in_air=0, ticks_boosting=0, ticks_supersonic=0, ticks_zero_boost=0,
        ticks_full_boost=0, speed_sum=0.0, speed_max=0.0, boost_used=0.0,
    )


def _payload(my_row, others):
    return dict(
        match_id="MATCH-ABC-123", started_at=1.0, ended_at=300.0, arena="Stadium_P",
        team0_score=3, team1_score=2, team0_name="Blue", team1_name="Orange",
        winner_team_num=0, is_online=True, crossbar_hits=0, duration_seconds=300.0,
        ball_touches=[], goal_events=[], my_row=my_row, other_rows=others,
    )


@pytest.mark.parametrize("blaze_first", [False, True])
def test_same_match_dedups_and_keeps_each_owners_full_row(tmp_path, blaze_first):
    s = Store(str(tmp_path / "central.db"))
    chum_full = _prow(CHUM, "@ChumtheWaters", 0, 9000)
    blaze_full = _prow(BLAZE, "Blazed", 0, 8800)
    # Each client only has a ~10% tick sample of the OTHER team members.
    blaze_sampled = _prow(BLAZE, "Blazed", 0, 500)
    chum_sampled = _prow(CHUM, "@ChumtheWaters", 0, 480)
    opp = _prow(OPP, "randoEnemy", 1, 450)

    uploads = [
        (CHUM, _payload(chum_full, [blaze_sampled, opp])),
        (BLAZE, _payload(blaze_full, [chum_sampled, opp])),
    ]
    if blaze_first:
        uploads.reverse()
    for owner, payload in uploads:
        s.upsert_uploaded_match(payload, owner_primary_id=owner)

    with s._conn() as c:
        n_matches = c.execute(
            "SELECT COUNT(*) FROM matches WHERE id='MATCH-ABC-123'").fetchone()[0]
        ticks = {r[0]: r[1] for r in c.execute(
            "SELECT name, ticks_total FROM match_player_stats WHERE match_id='MATCH-ABC-123'")}

    assert n_matches == 1, "the shared match must be stored once, not per uploader"
    # Each uploader's OWN row wins with full coverage, regardless of order.
    assert ticks["@ChumtheWaters"] == 9000
    assert ticks["Blazed"] == 8800
    # The opponent (owned by no one) lands once.
    assert ticks["randoEnemy"] == 450
