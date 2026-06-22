"""Match data-integrity guard: a player's goals can never exceed their team's
final score. The upload endpoint rejects violators (corrupt / freeplay sessions)
so they can't pollute the all-friends leaderboard."""

from types import SimpleNamespace

from chumstats.server import _impossible_match_reason


def _row(name, team_num, goals):
    return SimpleNamespace(name=name, team_num=team_num, goals=goals)


def test_legit_match_passes():
    rows = [_row("A", 0, 2), _row("B", 0, 1), _row("C", 1, 2), _row("D", 1, 0)]
    assert _impossible_match_reason(3, 2, rows) is None


def test_one_player_scores_all_team_goals_ok():
    # scoring every one of the team's goals is legal (goals == team score)
    assert _impossible_match_reason(4, 3, [_row("A", 0, 4), _row("X", 1, 3)]) is None


def test_goals_exceed_score_rejected():
    # the audited corrupt case: 11 goals in a 4-goal game
    reason = _impossible_match_reason(4, 3, [_row("Chum", 0, 11)])
    assert reason is not None
    assert "11 goals" in reason and "team 0" in reason


def test_zero_zero():
    assert _impossible_match_reason(0, 0, [_row("A", 0, 0), _row("B", 1, 0)]) is None
    assert _impossible_match_reason(0, 0, [_row("A", 0, 1)]) is not None


def test_each_team_checked_independently():
    # team 1 player over their own (orange) score, team 0 fine
    assert _impossible_match_reason(5, 1, [_row("A", 0, 5), _row("B", 1, 2)]) is not None
