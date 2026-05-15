"""SQLite roundtrip."""

from __future__ import annotations

from carball.replay import iter_for_aggregator
from carball.session import run_aggregation
from carball.store import Store


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
