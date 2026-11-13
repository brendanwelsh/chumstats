"""Pros column on Compare (11/13): sourced from ballchasing RLCS 2026 Worlds,
only stats defined like ours, blank otherwise, per mode."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from chumstats import server

from test_reliability import CANON_NAME, _seed

ROOT = Path(__file__).resolve().parent.parent


def test_bundled_json_is_sourced_and_dated():
    data = json.loads(server.PRO_BENCHMARKS_PATH.read_text())
    assert data["fetched"]
    for key, size in (("3", 3), ("2", 2)):
        m = data["modes"][key]
        assert m["team_size"] == size
        assert m["url"].startswith("https://ballchasing.com/group/")
        assert m["player_games"] == m["games"] * 2 * size
        # every value maps to a Compare row
        keys = {spec[0] for spec in server._PRO_ROWS.values()}
        assert set(m["values"]) <= keys


def test_pick_by_mode():
    data = server._load_pro_benchmarks()
    assert server._pro_bench_for_mode(None, data)["team_size"] == 3
    assert server._pro_bench_for_mode(3, data)["team_size"] == 3
    assert server._pro_bench_for_mode(2, data)["team_size"] == 2
    assert server._pro_bench_for_mode(1, data) is None
    assert server._pro_bench_for_mode(4, data) is None


def test_compare_shows_pros_column_with_source(tmp_path):
    st = _seed(tmp_path)
    page = server._compare_page_html(st, ["Me", CANON_NAME], last_n=0)
    assert 'class="compare-pro-col"' in page
    assert "ballchasing, RLCS 2026 Worlds" in page
    assert "player-games" in page and "https://ballchasing.com/group/" in page
    v = server._pro_bench_for_mode(None)["values"]
    assert f'{v["shooting_pct"]:.1f}%' in page
    assert f'{v["goals_pm"]:.2f} / match' in page
    # stacked layout gets a Pros row too
    assert "cmp-stack-pro-row" in page
    assert "Pro tier" not in page


def test_not_comparable_rows_stay_blank(tmp_path):
    st = _seed(tmp_path)
    page = server._compare_page_html(st, ["Me", CANON_NAME], last_n=0)
    side = page.split('<table class="compare-table">', 1)[1].split("</table>", 1)[0]
    for label in ("Win rate", "Time on ground %", "Time in air %", "Defensive third %", "Touches"):
        row = side.split(f"{label}</td>", 1)[1].split("</tr>", 1)[0]
        assert row.rstrip().endswith('<td class="compare-pro-val dim"></td>'), label


def test_one_v_one_has_no_pros_column(tmp_path):
    st = _seed(tmp_path)
    page = server._compare_page_html(st, ["Me"], last_n=0, mode_filter=1)
    assert 'class="compare-pro-col"' not in page


def test_builder_pools_totals(tmp_path):
    spec = importlib.util.spec_from_file_location("bpb", ROOT / "scripts" / "build_pro_benchmarks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def table(tid, heads, rows):
        h = "".join(f"<th>{x}</th>" for x in heads)
        b = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
        return f'<table class="t" id="{tid}"><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table>'

    page = "".join([
        table("core-stats-tot", ["Player", "Games", "Score", "Shots", "Goals", "Assists", "Saves"],
              [["A", "10", "4000", "30", "6", "5", "20"], ["B", "30", "9000", "90", "30", "9", "40"]]),
        table("demo-stats-tot", ["Player", "Games", "DemosInflicted", "DemosTaken"],
              [["A", "10", "10", "20"], ["B", "30", "30", "20"]]),
        table("boost-stats-avg", ["Player", "Games", "BPM"], [["A", "10", "400"], ["B", "30", "500"]]),
        table("boost-stats-tot", ["Player", "Games", "Time 100 Boost", "Time Boost 0-25%",
                                  "Time Boost 25-50%", "Time Boost 50-75%", "Time Boost 75-100%"],
              [["A", "10", "100.0 s", "250", "250", "250", "250"], ["B", "30", "0", "0", "0", "0", "0"]]),
        table("move-stats-avg", ["Player", "Games", "Avg Speed", "% SupersonicSpeed"],
              [["A", "10", "1000", "10.00 %"], ["B", "30", "2000", "20.00 %"]]),
    ])
    out = mod.pool(page)
    v = out["values"]
    assert out["player_games"] == 40
    assert v["goals_pm"] == round(36 / 40, 3)
    assert v["shooting_pct"] == round(36 / 120 * 100, 3)
    assert v["bpm"] == 475.0                      # game-weighted
    assert v["supersonic_pct"] == 17.5
    assert v["avg_speed_kmh"] == round(1750 * 0.036, 3)
    assert v["full_boost_pct"] == 10.0
    assert v["demos_received_pm"] == 1.0
