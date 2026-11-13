"""Reliability pass (11/13): alias names in raw events, air time, movement
coverage (not tracked), demos delivered, field-map flip and pass detection,
Compare layouts and the removed Pro tier column."""

from __future__ import annotations

import json

from chumstats import fieldmap as fm
from chumstats import server
from chumstats.analytics import _lifetime_row
from chumstats.models import EVENT_MODEL
from chumstats.session import MatchAggregator
from chumstats.store import PLAYER_ALIASES, Store

MID = "MATCHREL00000001"
ALIAS_PID = "Steam|76561197972356941|0"
CANON_NAME, CANON_PID = PLAYER_ALIASES[ALIAS_PID]


def _j(o) -> str:
    return json.dumps(o, separators=(",", ":"))


def _seed(tmp_path, *, ticks=None):
    """One match: Me (team 0, fully tracked), the alias account's canonical
    row (team 0), an opponent Foe with scraps of car state (team 1)."""
    ticks = ticks or {"Me": 9000, CANON_NAME: 8800, "Foe": 900}
    st = Store(tmp_path / "t.db")
    with st._conn(scoped=False) as con:
        con.execute(
            "INSERT INTO matches (id, started_at, ended_at, arena, team0_score, team1_score, "
            "winner_team_num, is_online) VALUES (?, 1000, 1300, 'Stadium_P', 2, 1, 0, 1)", (MID,))
        for pid, name, team, demos in [("Steam|me|0", "Me", 0, 3), (CANON_PID, CANON_NAME, 0, 1),
                                       ("Epic|foe|0", "Foe", 1, 0)]:
            t = ticks[name]
            con.execute(
                "INSERT INTO match_player_stats (match_id, primary_id, name, team_num, demos, "
                "ticks_total, ticks_on_ground, ticks_on_wall, ticks_in_air, speed_sum, boost_used) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (MID, pid, name, team, demos, t, int(t * 0.6), int(t * 0.08), t * 45.0, 2000.0))
        ev = [
            ("PlayerJoined", {"PlayerName": "vexxlol", "PrimaryId": ALIAS_PID}),
            ("StatfeedEvent", {"EventName": "Demolish", "Type": "Demolition",
                               "MainTarget": {"Name": "vexxlol", "TeamNum": 0},
                               "SecondaryTarget": {"Name": "Foe", "TeamNum": 1}}),
            ("StatfeedEvent", {"EventName": "Demolish", "Type": "Demolition",
                               "MainTarget": {"Name": "Foe", "TeamNum": 1},
                               "SecondaryTarget": {"Name": "vexxlol", "TeamNum": 0}}),
            # A different player whose name merely starts with the alias name.
            ("StatfeedEvent", {"EventName": "Save", "Type": "Save",
                               "MainTarget": {"Name": "vexxlol2", "TeamNum": 1}}),
            ("BallHit", {"Players": [{"Name": "vexxlol", "TeamNum": 0}],
                         "Ball": {"PostHitSpeed": 120, "Location": {"X": 0, "Y": 2000, "Z": 90}}}),
        ]
        con.executemany(
            "INSERT INTO raw_events (received_at, match_id, event, payload) VALUES (5000, ?, ?, ?)",
            [(MID, e, _j({"MatchGuid": MID, **d})) for e, d in ev])
    return Store(tmp_path / "t.db")  # reopen: startup passes run


def test_alias_name_rewritten_in_raw_events(tmp_path):
    st = _seed(tmp_path)
    with st._conn() as con:
        payloads = [r[0] for r in con.execute("SELECT payload FROM raw_events")]
    assert not any('"Name":"vexxlol"' in p for p in payloads)
    assert any('"Name":"vexxlol2"' in p for p in payloads)  # exact token only
    d = server._lifetime_derived(st, CANON_NAME)
    assert d["demos_given"] == 1 and d["demos_received"] == 1
    assert d["hit_speed_max"] == 120
    # Idempotent.
    with st._conn(scoped=False) as con:
        assert Store.apply_raw_name_aliases(con) == 0


def test_air_backfill_is_total_minus_ground_and_wall(tmp_path):
    st = _seed(tmp_path)
    with st._conn() as con:
        r = con.execute("SELECT ticks_total, ticks_on_ground, ticks_on_wall, ticks_in_air "
                        "FROM match_player_stats WHERE name = 'Me'").fetchone()
    assert r[3] == r[0] - r[1] - r[2] > 0
    with st._conn(scoped=False) as con:
        assert Store.backfill_air_ticks(con) == 0


def _tick(players):
    data = {"MatchGuid": MID, "Players": players,
            "Game": {"Teams": [{"Name": "Blue", "TeamNum": 0, "Score": 0},
                               {"Name": "Orange", "TeamNum": 1, "Score": 0}],
                     "Arena": "Stadium_P", "bReplay": False, "TimeSeconds": 200}}
    return "UpdateState", data, EVENT_MODEL["UpdateState"].model_validate(data)


def test_session_counts_air_when_feed_omits_false_booleans():
    agg = MatchAggregator()
    base = {"Name": "Me", "PrimaryId": "Steam|me|0", "TeamNum": 0, "Speed": 40.0, "Boost": 50}
    seq = [
        {**base, "bHasCar": True, "bOnGround": True},                  # ground
        {**base, "bHasCar": True, "bOnGround": True, "bOnWall": True},  # wall
        {**base, "bHasCar": True},                                     # air
        {**base, "bHasCar": True},                                     # air
        {**base},                                                      # no car flag: not air
    ]
    for p in seq:
        e, raw, parsed = _tick([p])
        agg.handle(e, parsed, raw=raw)
    line = next(iter(agg._lines.values()))
    assert (line.ticks_on_ground, line.ticks_on_wall, line.ticks_in_air) == (1, 1, 2)
    assert line.ticks_total == 5


def test_opponent_scraps_are_not_tracked(tmp_path):
    st = _seed(tmp_path)
    with st._conn() as con:
        foe = _lifetime_row(con, None, "Foe")
        me = _lifetime_row(con, None, "Me")
    assert foe["tracked_matches"] == 0 and (foe["ticks"] or 0) == 0
    assert me["tracked_matches"] == 1 and me["ticks"] == 9000
    page = server._compare_page_html(st, ["Me", "Foe"], last_n=0)
    row = page.split("Time in air %", 1)[1].split("</tr>", 1)[0]
    assert 'class="nt"' in row and "%" in row  # Me has a number, Foe is not tracked


def test_demos_delivered_is_scoreboard_count(tmp_path):
    st = _seed(tmp_path)
    page = server._compare_page_html(st, ["Me", CANON_NAME], last_n=0)
    assert "Demos delivered" not in page  # shown once, as Demos under Per-stat output
    kd = page.split("Demo K/D", 1)[1].split("</tr>", 1)[0]
    assert "1.00" in kd  # Vex: 1 scoreboard demo / 1 received


def test_compare_has_layouts_and_no_pro_tier(tmp_path):
    st = _seed(tmp_path)
    page = server._compare_page_html(st, ["Me", CANON_NAME], last_n=0)
    assert "Pro tier" not in page
    assert 'data-layout="side"' in page and 'data-layout="stack"' in page
    assert "cmp-stack-table" in page


def test_flip_rotates_orange_about_centre():
    assert fm.flip(fm.CX + 100, fm.CY - 20, 1) == (fm.CX - 100, fm.CY + 20)
    assert fm.flip(500.0, 100.0, 0) == (500.0, 100.0)


def _bh(t, player, team, x, y, kickoff=False):
    sx = fm.PAD_X + (y + 5120) / 10240 * fm.PITCH_W
    sy = fm.PAD_Y + (x + 4096) / 8192 * fm.PITCH_H
    return {"t": t, "player": player, "team": team, "x": x, "y": y, "sx": sx, "sy": sy,
            "is_kickoff": kickoff}


def test_pass_detection():
    pb = {"ball_track": [
        _bh(1.0, "Me", 0, 0, -2000),
        _bh(2.5, "Mate", 0, 500, 0),       # pass Me -> Mate (2000+ uu, 1.5 s)
        _bh(3.0, "Me", 0, 520, 50),        # too close to count from Mate, and not Me's pass
        _bh(9.0, "Mate", 0, 0, 3000),      # 6 s later: not a pass
        _bh(9.5, "Me", 0, 0, 3200),
        _bh(10.0, "Foe", 1, 0, 2000),      # intercepted: not a pass
    ], "goals": [], "shots": [], "events": []}
    sp = server._match_spatial(pb, "Me", 0)
    assert len(sp["passes"]) == 1
    assert sp["passes"][0]["tip"] == "Pass to Mate"
    # Orange: the same pass is drawn flipped.
    sp1 = server._match_spatial(pb, "Me", 1)
    p0, p1 = sp["passes"][0], sp1["passes"][0]
    assert round(p0["sx"] + p1["sx"], 1) == round(2 * fm.CX, 1)


def test_maps_block_lists_not_tracked_kinds(tmp_path):
    st = _seed(tmp_path)
    html = server._maps_block([("", server._lifetime_maps(st, CANON_NAME))], title="Maps", key="k")
    for lbl, _ in fm.NOT_TRACKED:
        assert lbl in html
    assert "You attack this way" in html and "Your goal" in html and "Their goal" in html
