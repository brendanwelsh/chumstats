"""Regression tests for the friend-facing hardening (bug pass, 2026-07-01).

The central server accepts match uploads from friends' clients and renders
user-controlled names on public pages. These lock in:
  - upload field bounds (no negative / absurd / NaN-Infinity values that would
    poison the shared leaderboard or overflow SQLite's 64-bit SUM), and
  - HTML-escaping of reflected/stored user-controlled strings (XSS).
"""
import math

import pytest
from pydantic import ValidationError

from chumstats.server import (
    MatchSummaryUpload,
    PlayerRowUpload,
    _not_found_html,
)


def _good_row(**over):
    base = dict(primary_id="Steam|1|0", name="Chum", team_num=0)
    base.update(over)
    return PlayerRowUpload(**base)


def test_row_rejects_negative_stats():
    with pytest.raises(ValidationError):
        _good_row(goals=-1)
    with pytest.raises(ValidationError):
        _good_row(score=-5)


def test_row_rejects_overflow_scale_values():
    # A value near int64 max would overflow SQLite's SUM across matches.
    with pytest.raises(ValidationError):
        _good_row(saves=9_223_372_036_854_775_807)
    with pytest.raises(ValidationError):
        _good_row(score=10**12)


def test_row_rejects_nan_and_infinity_floats():
    for bad in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValidationError):
            _good_row(speed_sum=bad)
        with pytest.raises(ValidationError):
            _good_row(boost_used=bad)


def test_row_rejects_absurdly_long_name():
    with pytest.raises(ValidationError):
        _good_row(name="x" * 5000)


def test_row_accepts_realistic_values():
    r = _good_row(goals=3, assists=1, saves=5, shots=7, score=612,
                  touches=180, speed_sum=1234.5, boost_used=980.0)
    assert r.goals == 3 and r.score == 612


def _match(**over):
    base = dict(
        match_id="M1", started_at=1000.0, ended_at=1300.0, arena="Arena",
        team0_score=3, team1_score=1, winner_team_num=0,
        my_row=_good_row(),
    )
    base.update(over)
    return MatchSummaryUpload(**base)


def test_match_rejects_absurd_team_score():
    with pytest.raises(ValidationError):
        _match(team0_score=10_000)


def test_match_rejects_nan_timestamp():
    with pytest.raises(ValidationError):
        _match(started_at=math.nan)


def test_match_caps_roster_size():
    with pytest.raises(ValidationError):
        _match(other_rows=[_good_row() for _ in range(50)])


def test_match_accepts_realistic_upload():
    m = _match(other_rows=[_good_row(name="Vex", team_num=1)])
    assert m.team0_score == 3 and len(m.other_rows) == 1


def test_not_found_page_escapes_reflected_name():
    """A crafted /player/<script> URL must not reflect raw HTML."""
    html = _not_found_html('<script>alert(1)</script>')
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;' in html
