"""Display-layer regressions in server.py: crossbar debounce, history chip
links, filtered-empty player page, bot exclusion on /players, real 404s."""

from __future__ import annotations

import json
import re

import pytest

from chumstats import server
from chumstats.store import Store

fastapi_testclient = pytest.importorskip("fastapi.testclient")

GUID = "MATCHA0000000001"
BOTGUID = "MATCHB0000000002"


def _j(o) -> str:
    return json.dumps(o, separators=(",", ":"))


def _crossbar(name: str, team: int = 0) -> str:
    return _j({"MatchGuid": GUID, "BallLocation": {"X": 800, "Y": 5100, "Z": 120},
               "BallSpeed": 30,
               "BallLastTouch": {"Player": {"Name": name, "TeamNum": team}}})


def _clock(ts: int) -> str:
    return _j({"MatchGuid": GUID, "TimeSeconds": ts, "bOvertime": False})


def _match(con, mid: str, started: float, players: list[tuple]) -> None:
    con.execute(
        "INSERT INTO matches (id, started_at, ended_at, arena, team0_score, team1_score, "
        "winner_team_num, is_online) VALUES (?, ?, ?, 'Stadium_P', 1, 0, 0, 1)",
        (mid, started, started + 300))
    for pid, name, team, is_bot, goals in players:
        con.execute(
            "INSERT INTO match_player_stats (match_id, primary_id, name, team_num, "
            "is_bot, goals) VALUES (?, ?, ?, ?, ?, ?)",
            (mid, pid, name, team, is_bot, goals))


@pytest.fixture()
def store(tmp_path):
    st = Store(str(tmp_path / "t.db"))
    with st._conn(scoped=False) as con:
        # A human-only match and an offline bot exhibition, both a year old
        # (so ?window=7d filters everything out).
        _match(con, GUID, 1.7e9, [("Steam|1|0", "Alice", 0, 0, 1),
                                   ("Steam|2|0", "Bob", 1, 0, 0)])
        _match(con, BOTGUID, 1.7e9 + 1000, [("Steam|1|0", "Alice", 0, 0, 1),
                                             ("Unknown|0|0", "Rookie", 1, 1, 0)])
    rows = [
        # Burst of 5 events from one bar contact at clock 250 -> counts once.
        *[(1000.0, GUID, "ClockUpdatedSeconds", _clock(250))],
        *[(1000.0, GUID, "CrossbarHit", _crossbar("Alice")) for _ in range(5)],
        # Goal replay re-shows it: ignored.
        (1000.0, GUID, "GoalReplayStart", _j({"MatchGuid": GUID})),
        *[(1000.0, GUID, "CrossbarHit", _crossbar("Alice")) for _ in range(3)],
        (1000.0, GUID, "GoalReplayEnd", _j({"MatchGuid": GUID})),
        # A separate post later in the match -> counts.
        (1000.0, GUID, "ClockUpdatedSeconds", _clock(120)),
        *[(1000.0, GUID, "CrossbarHit", _crossbar("Alice")) for _ in range(4)],
        # Bob's post in the same second is a different player -> counts.
        (1000.0, GUID, "CrossbarHit", _crossbar("Bob", 1)),
    ]
    st.save_raw_events_bulk(rows)
    return st


@pytest.fixture()
def client(store):
    app = server.make_app(server.Broadcaster(), store=store)
    return fastapi_testclient.TestClient(app)


def test_crossbar_debounced_everywhere(store):
    with store._conn() as con:
        kept = server._counted_crossbar_ids(con, "?", [GUID])
    assert len(kept) == 3  # Alice x2 contacts, Bob x1 — not 13 raw events
    extras = server._derive_match_extras(store, GUID)
    assert extras["Alice"]["crossbar_hits"] == 2
    assert extras["Bob"]["crossbar_hits"] == 1
    assert server._lifetime_derived(store, "Alice")["crossbar_hits"] == 2
    pb = server._build_playback_data(store, GUID, 1000.0, "Blue", "Orange", 300.0)
    assert sum(1 for e in pb["events"] if e["kind"] == "crossbar") == 3


def test_history_chips_keep_player_and_filters(store):
    page = server._history_page_html(store, "Steam|1|0", "Alice", include_bots=True,
                                     window_days=30, platform_filter="Steam",
                                     sort="recent", is_self=False)
    # Toolbar chips (the bare "/history" is the nav link).
    hrefs = [h for h in re.findall(r'href="(/history[^"]*)"', page) if "?" in h]
    assert hrefs and all("pid=Steam%7C1%7C0" in h and "window=30d" in h
                         and "platform=Steam" in h for h in hrefs)


def test_all_matches_sorts_do_not_crash(store):
    for s in ("recent", "score", "goals", "saves", "best"):
        assert "All matches" in server._all_matches_html(store, sort=s)


def test_player_page_filtered_empty_is_not_404(client):
    r = client.get("/player/Alice?window=7d")
    assert r.status_code == 200
    assert "No matches for this filter" in r.text
    assert client.get("/player/Nobody").status_code == 404


def test_players_leaderboard_excludes_bot_matches(client):
    r = client.get("/players")
    assert r.status_code == 200
    assert "Rookie" not in r.text
    # Alice's bot exhibition is excluded, as on her profile: 1 match, not 2.
    row = re.search(r"/player/Alice.*?</tr>", r.text, re.S).group(0)
    nums = re.findall(r'<td class="num[^"]*">([\d,]+)</td>', row)
    assert nums and nums[0] == "1"


def test_unknown_match_is_404(client):
    assert client.get("/match/NOPE").status_code == 404
    assert client.get(f"/match/{GUID}").status_code == 200


def test_hit_and_goal_speed_maxima_and_touch_rows(store):
    """Hardest hit = max PostHitSpeed over solo BallHits (shared hits are not
    credited); fastest goal = max GoalSpeed. Touch % lists every full name."""
    long_name = "AVeryLongGamertagThatMustNeverBeCutOff"
    with store._conn(scoped=False) as con:
        con.execute("UPDATE match_player_stats SET name = ?, touches = 3 WHERE match_id = ? AND name = 'Bob'",
                    (long_name, GUID))
        con.execute("UPDATE match_player_stats SET touches = 1 WHERE match_id = ? AND name = 'Alice'", (GUID,))

    def hit(players, speed):
        return _j({"MatchGuid": GUID, "Players": [{"Name": n, "TeamNum": t} for n, t in players],
                   "Ball": {"PreHitSpeed": 0, "PostHitSpeed": speed, "Location": {"X": 0, "Y": 0, "Z": 93}}})
    store.save_raw_events_bulk([
        (1001.0, GUID, "BallHit", hit([("Alice", 0)], 120.5)),
        (1001.0, GUID, "BallHit", hit([("Alice", 0)], 90.0)),
        (1001.0, GUID, "BallHit", hit([("Alice", 0), (long_name, 1)], 199.0)),  # shared: ignored
        (1001.0, GUID, "GoalScored", _j({"MatchGuid": GUID, "GoalSpeed": 101.2,
                                          "Scorer": {"Name": "Alice", "TeamNum": 0}})),
        (1001.0, GUID, "GoalScored", _j({"MatchGuid": GUID, "GoalSpeed": 77.0,
                                          "Scorer": {"Name": "Alice", "TeamNum": 0}})),
    ])
    d = server._derive_match_extras(store, GUID)
    assert d["Alice"]["hit_speed_max"] == 120.5
    assert d["Alice"]["goal_speed_max"] == 101.2
    assert d.get(long_name, server._empty_derived())["hit_speed_max"] == 0
    life = server._lifetime_derived(store, "Alice")
    assert life["hit_speed_max"] == 120.5 and life["goal_speed_max"] == 101.2

    page = server._match_detail_html(store, GUID, None, None)
    page = page.body.decode() if hasattr(page, "body") else page
    assert f'<span class="tp-name">{long_name}</span>' in page
    assert "Hardest hit" in page and "Demos taken" in page
