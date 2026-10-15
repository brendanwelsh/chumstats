"""Rocket League v2.72 / v2.76 Stats API changes + the admin stats cutoff."""

from __future__ import annotations

from pathlib import Path

from chumstats import config_wizard
from chumstats.config_wizard import RLInstall, read_ini, run_wizard
from chumstats.models import EVENT_MODEL, GoalScored, MatchEnded, UpdateState
from chumstats.replay import iter_for_aggregator
from chumstats.session import MatchAggregator, run_aggregation
from chumstats.store import Store

GUID = "ABC123"


def _update(goals=(1, 0), replay=False):
    return {
        "MatchGuid": GUID,
        "Players": [{"Name": "Me", "PrimaryId": "Steam|1|0", "TeamNum": 0,
                     "Score": 100, "Goals": goals[0], "Speed": 1500, "Boost": 50}],
        "Game": {"Teams": [{"Name": "Blue", "TeamNum": 0, "Score": goals[0]},
                           {"Name": "Orange", "TeamNum": 1, "Score": goals[1]}],
                 "Arena": "Stadium_P", "TimeSeconds": 200, "bReplay": replay},
    }


def _feed(agg, name, raw):
    model = EVENT_MODEL.get(name)
    agg.handle(name, model.model_validate(raw) if model else None, raw=raw, received_at=1.0)


def test_no_contest_is_not_a_result():
    a = MatchAggregator()
    _feed(a, "UpdateState", _update())
    _feed(a, "MatchEnded", {"MatchGuid": GUID, "WinnerTeamNum": -1})
    assert a.ended and a.no_contest
    # Neither a clean nor a force-salvaged finalize may invent a winner.
    assert a.finalize() is None
    assert a.finalize(force=True) is None


def test_goal_during_replay_counts_once():
    a = MatchAggregator()
    goal = {"MatchGuid": GUID, "GoalSpeed": 90.0, "GoalTime": 12.5,
            "Scorer": {"Name": "Me", "Shortcut": 1, "TeamNum": 0}}
    _feed(a, "UpdateState", _update())
    _feed(a, "GoalScored", goal)
    _feed(a, "GoalReplayStart", {"MatchGuid": GUID})
    _feed(a, "UpdateState", _update(replay=True))
    _feed(a, "GoalScored", goal)  # v2.76: replay now re-fires with the real scorer
    _feed(a, "GoalReplayEnd", {"MatchGuid": GUID})
    _feed(a, "UpdateState", _update())
    _feed(a, "MatchEnded", {"MatchGuid": GUID, "WinnerTeamNum": 0})
    s = a.finalize()
    assert s is not None and len(s.goal_events) == 1
    assert s.goal_events[0]["goal_time"] == 12.5  # float GoalTime no longer rejected


def test_wizard_writes_user_tastatsapi(tmp_path, monkeypatch):
    install = tmp_path / "rocketleague"
    cfg = install / "TAGame" / "Config"
    cfg.mkdir(parents=True)
    (cfg / "DefaultStatsAPI.ini").write_text(
        "[TAGame.MatchStatsExporter_TA]\nPort=49123\nWebPort=49124\nPacketSendRate=0\n")
    docs = tmp_path / "Documents"
    user_cfg = docs / "My Games" / "Rocket League" / "TAGame" / "Config"
    user_cfg.mkdir(parents=True)
    monkeypatch.setattr(config_wizard, "_documents_dir", lambda: docs)
    monkeypatch.setattr(config_wizard, "detect_install",
                        lambda manual_path=None: RLInstall("steam", install, cfg / "DefaultStatsAPI.ini"))
    rep = run_wizard(enable=True, rate=30)
    assert rep.error is None and rep.after.enabled
    assert read_ini(user_cfg / "TAStatsAPI.ini").packet_send_rate == 30  # created
    assert read_ini(cfg / "DefaultStatsAPI.ini").packet_send_rate == 30
    new = (user_cfg / "TAStatsAPI.ini").read_text()
    assert "WebPort=49124" in new and "Port=49123" in new


def test_port_zero_means_disabled(tmp_path):
    p = tmp_path / "TAStatsAPI.ini"
    p.write_text("[TAGame.MatchStatsExporter_TA]\nPort=0\nPacketSendRate=30\n")
    assert read_ini(p).enabled is False


def test_stats_since_scopes_reads_not_writes(tmp_path, online_capture):
    st = Store(tmp_path / "t.db")
    s = run_aggregation(iter_for_aggregator(online_capture))[0]
    st.save_match(s)
    with st._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 1
    st.set_stats_since(s.started_at + 1)
    with st._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM match_player_stats").fetchone()[0] == 0
    assert st.has_match(s.match_id)  # dedup still sees the archive
    assert st.lifetime_for(name="@ChumtheWaters").get("matches", 0) in (0, None)
    st.set_stats_since(None)
    with st._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 1


def _upload(mid, owner, started, s0=3, s1=1, win=0):
    row = {"primary_id": owner, "name": owner, "team_num": 0, "goals": s0, "is_mvp": True}
    return {"match_id": mid, "started_at": started, "ended_at": started + 300,
            "arena": "Stadium_P", "team0_score": s0, "team1_score": s1,
            "team0_name": "Blue", "team1_name": "Orange", "winner_team_num": win,
            "my_row": row, "other_rows": [], "raw_events": []}


def test_upload_twin_guid_merges_and_conflict_refused(tmp_path):
    import pytest
    from chumstats.store import UploadConflict
    st = Store(tmp_path / "t.db")
    st.upsert_uploaded_match(_upload("G1", "Steam|1|0", 1000.0), "Steam|1|0")
    r = st.upsert_uploaded_match(_upload("G2", "Steam|1|0", 1010.0), "Steam|1|0")
    assert r.get("merged_into") == "G1"
    with st._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 1
    with pytest.raises(UploadConflict):
        st.upsert_uploaded_match(_upload("G1", "Steam|1|0", 1000.0, s0=1, s1=3, win=1), "Steam|1|0")
