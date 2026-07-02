"""The profile Breakdown tab renders the full Compare stat sheet for one
player (volume / efficiency / combat / highlights / per-stat output / movement
/ boost / ball positioning), reusing the same data helpers as /compare."""
from chumstats.store import Store
from chumstats.server import _player_breakdown_html

NAME = "BdPlayer"
PID = "Steam|777|0"


def _add(store, mid, *, t0, t1, goals, started):
    winner = 0 if t0 >= t1 else 1
    with store._conn() as con:
        con.execute(
            "INSERT INTO matches (id, started_at, ended_at, arena, team0_score, "
            "team1_score, winner_team_num, is_online) VALUES (?,?,?,?,?,?,?,1)",
            (mid, started, started + 300, "cs_p", t0, t1, winner))
        con.execute(
            "INSERT INTO match_player_stats (match_id, primary_id, name, team_num, "
            "goals, shots, assists, saves, demos, touches, score, is_mvp, platform, "
            "ticks_total, ticks_on_ground, ticks_in_air, ticks_supersonic, "
            "speed_sum, speed_max, boost_used) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, PID, NAME, 0, goals, goals + 2, 1, 2, 0, 40, 350, 1, "Steam",
             9000, 6000, 3000, 700, 540000.0, 102.0, 1300.0))
        con.execute(
            "INSERT INTO match_player_stats (match_id, primary_id, name, team_num, goals) "
            "VALUES (?,?,?,?,?)", (mid, "Steam|2|0", "Opp", 1, t1))


def test_breakdown_has_full_compare_stat_set(tmp_path):
    store = Store(str(tmp_path / "b.db"))
    _add(store, "M1", t0=3, t1=1, goals=2, started=1000.0)
    _add(store, "M2", t0=2, t1=4, goals=1, started=2000.0)

    html = _player_breakdown_html(store, PID, NAME)
    # every compare section is present
    for section in ["Volume", "Efficiency", "Combat", "Highlights",
                    "Per-stat output", "Movement", "Boost (total",
                    "Boost timing", "Ball positioning"]:
        assert section in html, f"missing section: {section}"
    # representative rows from across the sheet. (Flip resets is intentionally
    # not surfaced — the Stats API only emits FlipReset for the recording
    # client's own team, so it's unreliable for everyone else.)
    for row in ["Win rate", "Shooting %", "Goal participation", "Demo K/D",
                "Epic saves", "Aerial goals", "Supersonic %", "BPM", "Touches"]:
        assert row in html, f"missing row: {row}"


def test_breakdown_empty_for_unknown_player(tmp_path):
    store = Store(str(tmp_path / "b.db"))
    assert _player_breakdown_html(store, "Steam|nobody|0", "Nobody") == ""
