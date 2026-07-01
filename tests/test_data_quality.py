"""Data-quality regressions found in the June 2026 audit of real captures.

Empirical facts these tests encode (measured on the live DB):
  - The game re-streams UpdateState during goal replays (bReplay=true, ~1000
    ticks per match) carrying the REPLAY's car speed/boost; statfeed and
    CrossbarHit events occasionally re-fire inside the replay window too.
  - Recovered/reprocessed matches used to be stamped with the wall clock at
    REPLAY time (75 of 180 matches shared one bogus minute).
  - Offline matches minted a fresh random `local-` id per aggregation run,
    duplicating the match on every startup backfill.
  - Private lobbies reuse the same MatchGuid for post-game segments; a
    force-salvaged segment used to INSERT OR REPLACE junk over the real match.
"""
from __future__ import annotations

import json

import pytest

from chumstats.models import EVENT_MODEL
from chumstats.session import MatchAggregator, MatchSummary, PlayerLine, run_aggregation
from chumstats.store import PLAYER_ALIASES, Store

GUID = "AAAA0000BBBB1111CCCC2222DDDD3333"
ME = "Steam|me|0"
FOE = "Epic|foe|0"


def _ev(event: str, data: dict):
    model = EVENT_MODEL.get(event)
    parsed = model.model_validate(data) if model is not None else None
    return event, data, parsed


def _h(agg: MatchAggregator, ev_tuple, received_at=None):
    event, raw, parsed = ev_tuple
    agg.handle(event, parsed, raw=raw, received_at=received_at)


def _tick(guid=GUID, s0=0, s1=0, replay=False, boost=100, speed=10.0,
          goals_me=0):
    return _ev("UpdateState", {
        "MatchGuid": guid,
        "Players": [
            {"Name": "Me", "PrimaryId": ME, "TeamNum": 0, "Score": 100,
             "Goals": goals_me, "Boost": boost, "Speed": speed,
             "bOnGround": True, "bOnWall": False},
            {"Name": "Foe", "PrimaryId": FOE, "TeamNum": 1, "Score": 50},
        ],
        "Game": {
            "Teams": [
                {"Name": "Blue", "TeamNum": 0, "Score": s0},
                {"Name": "Orange", "TeamNum": 1, "Score": s1},
            ],
            "Arena": "TestArena", "bReplay": replay, "TimeSeconds": 100.0,
        },
    })


def _statfeed(name, team, event_name):
    return _ev("StatfeedEvent", {
        "MatchGuid": GUID, "EventName": event_name, "Type": event_name,
        "MainTarget": {"Name": name, "TeamNum": team},
    })


class TestGoalReplayExclusion:
    def test_replay_ticks_do_not_feed_movement_or_boost(self):
        agg = MatchAggregator()
        _h(agg, _ev("MatchCreated", {"MatchGuid": GUID}))
        for i in range(10):
            _h(agg, _tick(boost=100 - i, speed=50.0))
        # Goal replay: high-speed replay car, boost draining to zero.
        for i in range(10):
            _h(agg, _tick(replay=True, boost=90 - 9 * i, speed=82.0))
        _h(agg, _ev("MatchEnded", {"MatchGuid": GUID, "WinnerTeamNum": 0}))
        _h(agg, _ev("MatchDestroyed", {"MatchGuid": GUID}))
        s = agg.finalize()
        me = s.me(self_primary_id=ME)
        assert me.ticks_total == 10          # replay ticks not counted
        assert me.speed_max == 50.0          # replay speed not counted
        assert me.ticks_supersonic == 0      # 82 km/h replay tick ignored
        assert me.boost_used == 9            # only the live 100->91 drain

    def test_replay_ticks_still_refresh_box_score(self):
        agg = MatchAggregator()
        _h(agg, _ev("MatchCreated", {"MatchGuid": GUID}))
        _h(agg, _tick(goals_me=0))
        # The tick that carries the new goal count arrives flagged as replay
        # (a goal straight into the replay is a real sequence at match end).
        _h(agg, _tick(s0=1, replay=True, goals_me=1))
        _h(agg, _ev("MatchEnded", {"MatchGuid": GUID, "WinnerTeamNum": 0}))
        s = agg.finalize()
        assert s.me(self_primary_id=ME).goals == 1

    def test_statfeed_and_crossbar_skipped_inside_replay_window(self):
        agg = MatchAggregator()
        _h(agg, _ev("MatchCreated", {"MatchGuid": GUID}))
        _h(agg, _tick())
        _h(agg, _statfeed("Me", 0, "EpicSave"))            # live: counts
        _h(agg, _ev("GoalReplayStart", {"MatchGuid": GUID}))
        _h(agg, _statfeed("Me", 0, "EpicSave"))            # echo: skipped
        _h(agg, _ev("CrossbarHit", {
            "MatchGuid": GUID,
            "BallLastTouch": {"Player": {"Name": "Me", "TeamNum": 0}, "Speed": 1.0},
        }))                                                # echo: skipped
        _h(agg, _ev("GoalReplayEnd", {"MatchGuid": GUID}))
        _h(agg, _statfeed("Me", 0, "EpicSave"))            # live: counts
        _h(agg, _ev("MatchEnded", {"MatchGuid": GUID, "WinnerTeamNum": 0}))
        s = agg.finalize()
        assert s.statfeed[ME]["EpicSave"] == 2
        assert s.crossbar_hits == 0


class TestRecoveredTimestamps:
    def test_run_aggregation_uses_received_at(self):
        events = [
            (*_ev("MatchCreated", {"MatchGuid": GUID}), 1000.0),
            (*_tick(), 1010.0),
            (*_tick(s0=1, goals_me=1), 1200.0),
            (*_ev("MatchEnded", {"MatchGuid": GUID, "WinnerTeamNum": 0}), 1290.0),
            (*_ev("MatchDestroyed", {"MatchGuid": GUID}), 1300.0),
        ]
        (s,) = run_aggregation(events)
        assert s.started_at == 1000.0
        assert s.ended_at == 1290.0

    def test_plain_triples_still_accepted(self):
        events = [
            _ev("MatchCreated", {"MatchGuid": GUID}),
            _tick(),
            _tick(s0=1, goals_me=1),
            _ev("MatchEnded", {"MatchGuid": GUID, "WinnerTeamNum": 0}),
            _ev("MatchDestroyed", {"MatchGuid": GUID}),
        ]
        (s,) = run_aggregation(events)
        assert s.winner_team_num == 0


def _offline_match_events(ts0=5000.0):
    """A complete offline (no MatchGuid) match as (event, raw, parsed, ts)."""
    return [
        (*_ev("MatchCreated", {"MatchGuid": ""}), ts0),
        (*_tick(guid=""), ts0 + 5),
        (*_tick(guid="", s0=2, s1=1, goals_me=2), ts0 + 250),
        (*_ev("MatchEnded", {"MatchGuid": "", "WinnerTeamNum": 0}), ts0 + 290),
        (*_ev("MatchDestroyed", {"MatchGuid": ""}), ts0 + 300),
    ]


class TestOfflineMatchIdentity:
    def test_offline_id_is_deterministic_across_runs(self):
        (a,) = run_aggregation(_offline_match_events())
        (b,) = run_aggregation(_offline_match_events())
        assert a.match_id.startswith("local-")
        assert a.match_id == b.match_id

    def test_different_offline_matches_get_different_ids(self):
        (a,) = run_aggregation(_offline_match_events(ts0=5000.0))
        (b,) = run_aggregation(_offline_match_events(ts0=9000.0))
        assert a.match_id != b.match_id

    def test_startup_backfill_does_not_duplicate_offline_matches(self, tmp_path):
        store = Store(str(tmp_path / "t.db"))
        rows = [(ts, None, ev, json.dumps(raw))
                for ev, raw, _parsed, ts in _offline_match_events()]
        store.save_raw_events_bulk(rows)
        assert store.backfill_from_raw_events() == 1
        # A second startup replays the same raw events: same derived id -> skip.
        assert store.backfill_from_raw_events() == 0
        with store._conn() as c:
            assert c.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 1


class TestGuidReuseDedupe:
    def test_forced_lobby_segment_does_not_displace_clean_match(self):
        clean = [
            (*_ev("MatchCreated", {"MatchGuid": GUID}), 100.0),
            (*_tick(), 105.0),
            (*_tick(s0=2, s1=1, goals_me=2), 380.0),
            (*_ev("MatchEnded", {"MatchGuid": GUID, "WinnerTeamNum": 0}), 395.0),
            (*_ev("MatchDestroyed", {"MatchGuid": GUID}), 400.0),
        ]
        # Post-game lobby segment reusing the guid: no MatchEnded, counters
        # partially reset (the real failure mode seen in a private-match capture).
        junk = [
            (*_ev("MatchCreated", {"MatchGuid": GUID}), 410.0),
            (*_tick(s0=2, s1=0, goals_me=2), 415.0),
            (*_ev("MatchDestroyed", {"MatchGuid": GUID}), 420.0),
        ]
        summaries = run_aggregation(clean + junk, force=True)
        assert len(summaries) == 1
        s = summaries[0]
        assert (s.team0_score, s.team1_score) == (2, 1)
        assert s.ended_at == 395.0

    def test_clean_summary_replaces_earlier_forced_one(self):
        forced = [
            (*_ev("MatchCreated", {"MatchGuid": GUID}), 100.0),
            (*_tick(s0=1, s1=0, goals_me=1), 150.0),
            (*_ev("MatchDestroyed", {"MatchGuid": GUID}), 160.0),
        ]
        clean = [
            (*_ev("MatchCreated", {"MatchGuid": GUID}), 200.0),
            (*_tick(s0=3, s1=1, goals_me=3), 480.0),
            (*_ev("MatchEnded", {"MatchGuid": GUID, "WinnerTeamNum": 0}), 495.0),
            (*_ev("MatchDestroyed", {"MatchGuid": GUID}), 500.0),
        ]
        summaries = run_aggregation(forced + clean, force=True)
        assert len(summaries) == 1
        assert summaries[0].team0_score == 3


class TestSelfIdentity:
    def _summary(self):
        return MatchSummary(
            match_id="m", started_at=0, ended_at=1, arena="a",
            team0_score=1, team1_score=0, team0_name="Blue", team1_name="Orange",
            winner_team_num=0,
            players=[PlayerLine(name="Friend", primary_id="Steam|friend|0", team_num=0)],
        )

    def test_configured_identity_absent_returns_none(self):
        s = self._summary()
        assert s.me(self_primary_id=ME) is None
        assert s.me(self_name="NotInMatch") is None

    def test_unconfigured_still_guesses_first_human(self):
        s = self._summary()
        assert s.me().name == "Friend"


class TestAliasUpsert:
    def test_alias_reupload_updates_canonical_row(self, tmp_path):
        alias_pid = "Steam|aliastest|0"
        canon = ("canonName", "Epic|canontest|0")
        PLAYER_ALIASES[alias_pid] = canon
        try:
            store = Store(str(tmp_path / "c.db"))
            row = dict(primary_id=alias_pid, name="aliasName", team_num=0,
                       goals=1, score=100)
            payload = dict(
                match_id="M1", started_at=1.0, ended_at=2.0, arena="A",
                team0_score=2, team1_score=1, team0_name="Blue",
                team1_name="Orange", winner_team_num=0,
                my_row=row, other_rows=[],
            )
            store.upsert_uploaded_match(payload, owner_primary_id=alias_pid)
            payload["my_row"] = {**row, "goals": 2}
            res = store.upsert_uploaded_match(payload, owner_primary_id=alias_pid)
            assert res["my_row_updated"] is True
            with store._conn() as c:
                rows = c.execute(
                    "SELECT primary_id, name, goals FROM match_player_stats "
                    "WHERE match_id='M1'").fetchall()
            assert len(rows) == 1
            assert rows[0]["primary_id"] == canon[1]
            assert rows[0]["goals"] == 2
        finally:
            PLAYER_ALIASES.pop(alias_pid, None)


def test_upload_wire_model_keeps_regulation_and_overtime():
    server = pytest.importorskip("chumstats.server")
    m = server.MatchSummaryUpload(
        match_id="X", started_at=1.0, ended_at=2.0, arena="A",
        team0_score=1, team1_score=0, winner_team_num=0,
        regulation_seconds=300.0, overtime_seconds=42.0,
        my_row=dict(primary_id=ME, name="Me", team_num=0),
    )
    d = m.model_dump()
    assert d["regulation_seconds"] == 300.0
    assert d["overtime_seconds"] == 42.0
