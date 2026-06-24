"""SQLite roundtrip."""

from __future__ import annotations

import json

from chumstats.replay import iter_for_aggregator
from chumstats.session import run_aggregation
from chumstats.store import Store


def _forfeit_raw_rows(guid: str) -> list[tuple]:
    """raw_events rows for a forfeited match: MatchCreated -> UpdateState (1-0,
    one player, one goal) -> MatchDestroyed, with NO MatchEnded — the on-disk
    shape the live worker persists for an abandoned game."""
    update = {
        "MatchGuid": guid,
        "Players": [{"Name": "Me", "PrimaryId": "Steam|123|0", "TeamNum": 0,
                     "Score": 100, "Goals": 1, "Speed": 1500, "Boost": 50}],
        "Game": {"Teams": [{"Name": "Blue", "TeamNum": 0, "Score": 1},
                           {"Name": "Orange", "TeamNum": 1, "Score": 0}],
                 "Arena": "TestArena"},
    }
    j = lambda o: json.dumps(o, separators=(",", ":"))
    return [
        (1000.0, guid, "MatchCreated", j({"MatchGuid": guid})),
        (1001.0, guid, "UpdateState", j(update)),
        (1002.0, guid, "MatchDestroyed", j({"MatchGuid": guid})),  # no MatchEnded
    ]


def test_backfill_recovers_forfeit_without_matchended(tmp_path):
    """Regression: the built-in recovery (backfill_from_raw_events) must salvage a
    forfeit/early-leave that has raw_events but no MatchEnded. Before the
    run_aggregation force fix it silently dropped exactly these — the same class
    of bug as the live ingest path."""
    store = Store(str(tmp_path / "bf.db"))
    guid = "FORFEITGUID0001"
    store.save_raw_events_bulk(_forfeit_raw_rows(guid))

    saved = store.backfill_from_raw_events()
    assert saved == 1, "forfeit (MatchDestroyed without MatchEnded) was not recovered"

    m = store.recent_matches(limit=5)
    assert len(m) == 1
    row = m[0]
    assert row["id"] == guid
    assert (row["team0_score"], row["team1_score"]) == (1, 0)
    assert row["winner_team_num"] == 0      # inferred from the 1-0 score

    # Idempotent: a second backfill recovers nothing new (already present).
    assert store.backfill_from_raw_events() == 0


def test_store_roundtrip(tmp_path, online_capture, exhibition_capture):
    db = tmp_path / "test.db"
    store = Store(str(db))
    for cap in (online_capture, exhibition_capture):
        for s in run_aggregation(iter_for_aggregator(cap)):
            store.save_match(s)

    recent = store.recent_matches(limit=10)
    assert len(recent) == 2
    arenas = {r["arena"] for r in recent}
    assert "TrainStation_Night_P" in arenas
    assert "stadium_day_p" in arenas

    # Lifetime for @ChumtheWaters (by primary_id since exhibition is local-).
    life = store.lifetime_for(primary_id="Steam|76561197985273611|0")
    assert life["matches"] == 2
    assert life["goals"] == 11
    assert life["wins"] == 2
    assert life["losses"] == 0
    assert life["mvp_count"] == 2
