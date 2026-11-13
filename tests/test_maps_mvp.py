"""Goal map / shots map / derived MVP / air-dash regressions (10/24).

Uploaded matches store every raw event with one received_at, so the playback
time axis is rebuilt from the game clock; the live GoalScored impact point lags
one goal and the replay's scorer-less GoalScored carries the true one."""

from __future__ import annotations

import json

from chumstats import server
from chumstats.store import Store

MID = "MATCHMAP00000001"


def _j(o) -> str:
    return json.dumps(o, separators=(",", ":"))


def _seed(tmp_path):
    st = Store(tmp_path / "t.db")
    with st._conn(scoped=False) as con:
        con.execute(
            "INSERT INTO matches (id, started_at, ended_at, arena, team0_score, team1_score, "
            "winner_team_num, is_online) VALUES (?, 1000, 1300, 'Stadium_P', 1, 0, 0, 1)", (MID,))
        for pid, name, team, score in [("Steam|a|0", "Ann", 0, 400), ("Steam|b|0", "Bo", 0, 300),
                                       ("Epic|c|0", "Cy", 1, 500)]:
            con.execute(
                "INSERT INTO match_player_stats (match_id, primary_id, name, team_num, goals, "
                "shots, score, ticks_total, ticks_on_ground, ticks_on_wall, ticks_in_air) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 9000, 6000, 400, 0)",
                (MID, pid, name, team, 1 if name == "Ann" else 0, 1 if name == "Ann" else 0, score))
        ev = []
        for ts in (299, 298, 297):
            ev.append(("ClockUpdatedSeconds", {"TimeSeconds": ts, "bOvertime": False}))
        ev.append(("BallHit", {"Players": [{"Name": "Cy", "TeamNum": 1}],
                               "Ball": {"PostHitSpeed": 50, "Location": {"X": 0, "Y": 0, "Z": 90}}}))
        ev.append(("ClockUpdatedSeconds", {"TimeSeconds": 296, "bOvertime": False}))
        ev.append(("BallHit", {"Players": [{"Name": "Ann", "TeamNum": 0}],
                               "Ball": {"PostHitSpeed": 90, "Location": {"X": 300, "Y": 3000, "Z": 90}}}))
        ev.append(("StatfeedEvent", {"EventName": "Shot", "MainTarget": {"Name": "Ann", "TeamNum": 0}}))
        # Live envelope: stale impact at the WRONG end (Blue scores at +Y).
        ev.append(("GoalScored", {"GoalSpeed": 88, "Scorer": {"Name": "Ann", "TeamNum": 0},
                                  "ImpactLocation": {"X": 10, "Y": -5200, "Z": 100}}))
        ev.append(("GoalReplayStart", {}))
        ev.append(("GoalScored", {"GoalSpeed": 0, "Scorer": {"Name": "", "TeamNum": 0},
                                  "ImpactLocation": {"X": -200, "Y": 5250, "Z": 300}}))
        ev.append(("GoalReplayEnd", {}))
        for ts in (295, 294):
            ev.append(("ClockUpdatedSeconds", {"TimeSeconds": ts, "bOvertime": False}))
        con.executemany(
            "INSERT INTO raw_events (received_at, match_id, event, payload) VALUES (5000, ?, ?, ?)",
            [(MID, e, _j({"MatchGuid": MID, **d})) for e, d in ev])
    return st


def _pb(st):
    return server._build_playback_data(st, MID, 1000, "Blue", "Orange", 300)


def test_goal_uses_replay_impact_and_chain_found(tmp_path):
    st = _seed(tmp_path)
    pb = _pb(st)
    (g,) = pb["goals"]
    assert g["iy"] == 5250 and g["ix"] == -200 and g["impact_ok"]
    # Collapsed timestamps no longer hide the build-up: the chain has touches.
    assert len(g["chain"]) >= 2
    assert g["t"] > 0


def test_shots_map_places_shot_at_shooters_touch(tmp_path):
    st = _seed(tmp_path)
    pb = _pb(st)
    assert pb["shot_events"] == 1
    (sh,) = pb["shots"]
    assert sh["player"] == "Ann" and sh["y"] == 3000


def test_goal_map_html_is_goals_only(tmp_path):
    st = _seed(tmp_path)
    html = server._goal_map_html(_pb(st))
    assert "Goal map" in html and "fm-svg" in html and "fm-goal-path" in html
    assert "Shot" not in html


def test_derived_mvp_is_winning_top_scorer(tmp_path):
    st = _seed(tmp_path)
    with st._conn(scoped=False) as con:
        st.derive_missing_mvps(con)
        rows = {r["name"]: (r["is_mvp"], r["mvp_derived"]) for r in
                con.execute("SELECT name, is_mvp, mvp_derived FROM match_player_stats")}
    # Cy has the top score but lost; Ann is the winning team's top scorer.
    assert rows == {"Ann": (1, 1), "Bo": (0, 0), "Cy": (0, 0)}
    with st._conn(scoped=False) as con:
        assert st.derive_missing_mvps(con) == 0  # idempotent


def test_real_mvp_withdraws_derived(tmp_path):
    st = _seed(tmp_path)
    with st._conn(scoped=False) as con:
        st.derive_missing_mvps(con)
        con.execute("UPDATE match_player_stats SET is_mvp = 1 WHERE name = 'Bo'")
        st.derive_missing_mvps(con)
        rows = {r["name"]: r["is_mvp"] for r in con.execute("SELECT name, is_mvp FROM match_player_stats")}
    assert rows == {"Ann": 0, "Bo": 1, "Cy": 0}


def test_air_backfilled_from_ground_and_wall(tmp_path):
    # Old clients stored ticks_in_air = 0 (the feed omits false booleans). On
    # open, the store derives air = total - ground - wall (11/13 fix).
    _seed(tmp_path)
    st = Store(tmp_path / "t.db")
    page = server._compare_page_html(st, ["Ann"], last_n=0)
    row = page.split("Time in air %", 1)[1].split("</tr>", 1)[0]
    assert "28.9%" in row
