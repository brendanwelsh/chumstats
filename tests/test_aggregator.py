"""End-to-end aggregator behavior on real fixtures."""

from __future__ import annotations

from chumstats.replay import iter_for_aggregator
from chumstats.session import MatchAggregator, SessionTracker, run_aggregation


def test_online_match_summary(online_capture):
    matches = run_aggregation(iter_for_aggregator(online_capture))
    # Capture contains: one quit-out (no MatchEnded, discarded) + one full match.
    assert len(matches) == 1
    s = matches[0]
    assert s.is_online is True
    assert (s.team0_score, s.team1_score) == (3, 5)
    assert s.winner_team_num == 1
    assert s.arena == "TrainStation_Night_P"
    assert s.team1_name == "Orange"
    names = {p.name for p in s.players}
    assert "@ChumtheWaters" in names
    assert "Jenox7" in names
    me = s.me(self_name="@ChumtheWaters")
    assert me is not None
    assert me.goals == 4
    assert me.shots == 7
    assert s.is_mvp.get(me.primary_id) is True
    assert len(s.goal_events) == 8  # deduped
    assert s.ball_touches  # captured at least some hits
    assert s.duration_seconds > 0


def test_exhibition_match_summary(exhibition_capture):
    matches = run_aggregation(iter_for_aggregator(exhibition_capture))
    assert len(matches) == 1
    s = matches[0]
    assert s.is_online is False  # MatchGuid empty for exhibitions
    assert s.match_id.startswith("local-")
    assert (s.team0_score, s.team1_score) == (7, 5)
    assert s.winner_team_num == 0
    me = s.me(self_name="@ChumtheWaters")
    assert me is not None
    assert me.goals == 7
    assert me.is_bot is False
    assert me.platform == "Steam"
    # All 3 bots must appear distinctly (they share primary_id Unknown|0|0)
    bots = [p for p in s.players if p.is_bot]
    assert len(bots) == 3
    assert {b.name for b in bots} == {"Junker", "Rainmaker", "Scout"}


def test_session_tracker_aggregates(all_captures):
    tr = SessionTracker(self_name="@ChumtheWaters")
    for f in all_captures:
        for sm in run_aggregation(iter_for_aggregator(f)):
            tr.add(sm)
    t = tr.totals
    assert t.matches_played == 2
    assert t.wins == 2
    assert t.losses == 0
    assert t.current_streak == 2
    assert t.streak_label == "2W"
    assert t.goals == 11  # 4 + 7


def test_force_finalize_infers_winner_without_matchended(exhibition_capture):
    """If MatchEnded never fires (rage-quit / RL killed), force-finalize must
    still recover the match by inferring the winner from the final scores —
    while a normal finalize() correctly drops it."""
    agg = MatchAggregator()
    saw_end = False
    for event_name, raw, parsed in iter_for_aggregator(exhibition_capture):
        if event_name == "MatchEnded":
            saw_end = True
            break  # simulate RL killed at the end screen before MatchEnded fired
        if event_name == "MatchCreated":
            agg = MatchAggregator()  # single match in this fixture
        agg.handle(event_name, parsed, raw=raw)

    assert saw_end, "fixture should contain a MatchEnded that we skipped"
    assert agg.ended is False
    assert agg.finalize(force=False) is None          # lost without force
    s = agg.finalize(force=True)                       # recovered with force
    assert s is not None
    assert (s.team0_score, s.team1_score) == (7, 5)
    assert s.winner_team_num == 0                      # 7 > 5 -> team 0
