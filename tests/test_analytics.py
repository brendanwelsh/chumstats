"""Analytics: match-level metrics and lifetime dashboard."""

from __future__ import annotations

from ballshark.analytics import build_analytics, build_dashboard
from ballshark.replay import iter_for_aggregator
from ballshark.session import run_aggregation
from ballshark.store import Store


ME_PID = "Steam|76561197985273611|0"
ME_NAME = "@ChumtheWaters"


def _load_db(tmp_path, online_capture, exhibition_capture) -> Store:
    db = tmp_path / "test.db"
    store = Store(str(db))
    for cap in (online_capture, exhibition_capture):
        for s in run_aggregation(iter_for_aggregator(cap)):
            store.save_match(s)
    return store


def test_match_analytics_includes_you_and_opponents(tmp_path, online_capture, exhibition_capture):
    store = _load_db(tmp_path, online_capture, exhibition_capture)
    matches = list(run_aggregation(iter_for_aggregator(online_capture)))
    assert len(matches) == 1
    s = matches[0]
    a = build_analytics(s, self_primary_id=ME_PID, self_name=ME_NAME, store=store)
    assert any(ml.label == "Line" for ml in a.you_block.lines)
    assert any(ml.label == "Avg speed" for ml in a.movement_block.lines)
    # Opponents in the online match are Jenox7 and Fabrooo9.
    opp_labels = {ml.label for ml in a.opponents_block.lines}
    assert "Jenox7" in opp_labels
    assert "Fabrooo9" in opp_labels


def test_dashboard_aggregates(tmp_path, online_capture, exhibition_capture):
    store = _load_db(tmp_path, online_capture, exhibition_capture)
    d = build_dashboard(store, primary_id=ME_PID)
    # Overview should have Matches, Win-loss, MVP count
    labels = {ml.label for ml in d.overview.lines}
    assert "Matches" in labels
    assert "Win-loss" in labels
    matches_line = next(ml for ml in d.overview.lines if ml.label == "Matches")
    assert matches_line.value == "2"
    # All matches were MVP wins
    mvp_line = next(ml for ml in d.overview.lines if ml.label == "MVP count")
    assert mvp_line.value == "2"
    # 11 total goals: 4 + 7
    g_line = next(ml for ml in d.overview.lines if ml.label == "Total goals")
    assert g_line.value == "11"
    # By-mode: one online, one offline
    mode_labels = {ml.label for ml in d.modes.lines}
    assert "Online" in mode_labels and "Offline" in mode_labels


def test_dashboard_records_and_form(tmp_path, online_capture, exhibition_capture):
    store = _load_db(tmp_path, online_capture, exhibition_capture)
    d = build_dashboard(store, primary_id=ME_PID)
    records = {ml.label: ml.value for ml in d.records.lines}
    assert records["Goals in a match"] == "7"
    assert records["Shots in a match"] == "7"
    form = {ml.label: ml.value for ml in d.recent_form.lines}
    # Two matches, both wins. Form is now spaced check/cross marks.
    # Find the "Last N" key (N = number of recent matches found).
    last_key = next(k for k in form if k.startswith("Last "))
    assert "✓" in form[last_key]
    assert "✗" not in form[last_key]


def test_h2h_against_jenox(tmp_path, online_capture, exhibition_capture):
    store = _load_db(tmp_path, online_capture, exhibition_capture)
    matches = list(run_aggregation(iter_for_aggregator(online_capture)))
    a = build_analytics(matches[0], self_primary_id=ME_PID, self_name=ME_NAME, store=store)
    # H2H should at minimum include Jenox7 as an opponent (1-0).
    h2h_labels = {ml.label: ml for ml in a.h2h_block.lines}
    assert any(k.startswith("vs Jenox7") for k in h2h_labels)
