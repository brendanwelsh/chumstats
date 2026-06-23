"""A physically-impossible (corrupt) match — a player with more goals than
their team's score, the signature of a freeplay/training session uploaded
before the ingest guard existed — must be excluded from every aggregate,
non-destructively (the raw rows stay in the DB)."""
from chumstats.store import Store
from chumstats.analytics import build_dashboard, _lifetime_row

ME = "Steam|111|0"


def _insert_match(store, mid, *, team0_score, team1_score, me_goals,
                  me_team=0, started=1000.0):
    winner = 0 if team0_score >= team1_score else 1
    opp_goals = team1_score if me_team == 0 else team0_score
    with store._conn() as con:
        con.execute(
            "INSERT INTO matches (id, started_at, ended_at, arena, team0_score, "
            "team1_score, winner_team_num, is_online) VALUES (?,?,?,?,?,?,?,1)",
            (mid, started, started + 300, "Champions Field",
             team0_score, team1_score, winner),
        )
        con.execute(
            "INSERT INTO match_player_stats (match_id, primary_id, name, "
            "team_num, goals) VALUES (?,?,?,?,?)",
            (mid, ME, "Chum", me_team, me_goals),
        )
        con.execute(
            "INSERT INTO match_player_stats (match_id, primary_id, name, "
            "team_num, goals) VALUES (?,?,?,?,?)",
            (mid, "Steam|222|0", "Opp", 1 - me_team, opp_goals),
        )


def test_corrupt_match_excluded_from_aggregates(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    # legit: 2 goals in a 3-1 win
    _insert_match(store, "GOOD", team0_score=3, team1_score=1, me_goals=2,
                  started=1000.0)
    # corrupt: 11 goals in a 4-goal game — impossible, must be ignored
    _insert_match(store, "BADD", team0_score=4, team1_score=0, me_goals=11,
                  started=2000.0)

    # profile / career aggregate (build_dashboard)
    d = build_dashboard(store, primary_id=ME)
    by_label = {ln.label: ln.value for ln in d.overview.lines}
    assert by_label["Total goals"] == "2", "corrupt 11 leaked into career goals"
    assert by_label["Matches"] == "1", "corrupt match counted in match total"

    # compare / generic aggregate (_lifetime_row)
    with store._conn() as con:
        row = _lifetime_row(con, ME, None)
    assert row["goals"] == 2
    assert row["matches"] == 1


def test_corrupt_match_excluded_from_history(tmp_path):
    """The History tab and recent-form strip pull rows from _match_history_rows,
    which must apply the same corrupt-match filter as the profile aggregates —
    otherwise a player's History totals/win-rate contradict their overview on
    the very same page."""
    from chumstats.server import _match_history_rows
    store = Store(str(tmp_path / "t.db"))
    _insert_match(store, "GOOD", team0_score=3, team1_score=1, me_goals=2,
                  started=1000.0)
    _insert_match(store, "BADD", team0_score=4, team1_score=0, me_goals=11,
                  started=2000.0)
    rows = _match_history_rows(store, ME, None, limit=2000)
    ids = {r["id"] for r in rows}
    assert "GOOD" in ids
    assert "BADD" not in ids, "corrupt match leaked into History/recent-form rows"


def test_clean_match_still_counts(tmp_path):
    """Guard must not over-reach: a normal match where goals <= team score is
    kept (the cap is goals > team score, so goals == team score is valid)."""
    store = Store(str(tmp_path / "t.db"))
    _insert_match(store, "OK1", team0_score=3, team1_score=2, me_goals=3,
                  started=1000.0)  # all 3 team goals scored by one player — legal
    with store._conn() as con:
        row = _lifetime_row(con, ME, None)
    assert row["matches"] == 1
    assert row["goals"] == 3
