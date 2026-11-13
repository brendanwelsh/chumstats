"""Build the pro benchmark file (src/chumstats/pro_benchmarks.json) from
ballchasing.com group "Players stats" pages saved by hand.

Usage:
    python scripts/build_pro_benchmarks.py --fetched 2026-11-13 \
        --mode 3 worlds-3v3.html https://ballchasing.com/group/<id>/players-stats "RLCS 2026 Worlds" \
        --mode 2 worlds-2v2.html https://ballchasing.com/group/<id>/players-stats "RLCS 2026 Worlds 2v2"

Each input is one saved page (one page view, no crawling; ballchasing's API
needs a token and none is on file). The page carries per-player tables in two
forms (per-game average and total); this pools them:
  - counts (goals, assists, saves, shots, score, demos inflicted/taken):
    sum of totals / sum of games (player-games)
  - shooting %: total goals / total shots (same as ChumStats: goals / shots)
  - rates (BPM, avg speed, supersonic %): game-weighted mean of each player's
    per-game value
  - time at 100 boost %: total seconds at 100 / total seconds in the four
    boost buckets (time the car had boost data)
Only stats defined the same way as ChumStats are kept. Not comparable, left
out on purpose: time on ground / wall / in air (ballchasing splits by car
height, ChumStats by wheel contact), thirds (ballchasing = share of time,
ChumStats = share of touches), touches, win rate, MVP rate, goal speed.
"""

from __future__ import annotations

import argparse
import html as _html
import json
import re
import sys
from pathlib import Path

UU_PER_S_TO_KMH = 0.036  # 1 uu = 1 cm


def _cell(s: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", "", s))).strip()


def _num(s: str) -> float | None:
    s = s.replace("%", "").replace("uu", "").replace("s", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def parse_tables(page: str) -> dict[str, list[dict]]:
    """Return {table_id: [row dict]} for every stats table on the page."""
    out: dict[str, list[dict]] = {}
    for m in re.finditer(r'<table[^>]*id="([^"]+)"[^>]*>(.*?)</table>', page, re.S):
        tid, body = m.group(1), m.group(2)
        heads = [_cell(h) for h in re.findall(r"<th[^>]*>(.*?)</th>", body, re.S)]
        rows = []
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S):
            cells = [_cell(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            if len(cells) == len(heads) and cells:
                rows.append(dict(zip(heads, cells)))
        out[tid] = rows
    return out


def pool(page: str) -> dict:
    t = parse_tables(page)
    core_tot = t["core-stats-tot"] if "core-stats-tot" in t else None
    if core_tot is None:
        raise SystemExit(f"no core totals table; tables found: {sorted(t)}")

    def tot(tid: str, col: str) -> tuple[float, float]:
        s = g = 0.0
        for r in t[tid]:
            v, n = _num(r[col]), _num(r["Games"])
            if v is not None and n:
                s += v
                g += n
        return s, g

    def wavg(tid: str, col: str) -> float:
        s = g = 0.0
        for r in t[tid]:
            v, n = _num(r[col]), _num(r["Games"])
            if v is not None and n:
                s += v * n
                g += n
        return s / g if g else float("nan")

    goals, games = tot("core-stats-tot", "Goals")
    shots, _ = tot("core-stats-tot", "Shots")
    vals = {
        "goals_pm": goals / games,
        "assists_pm": tot("core-stats-tot", "Assists")[0] / games,
        "saves_pm": tot("core-stats-tot", "Saves")[0] / games,
        "shots_pm": shots / games,
        "score_pm": tot("core-stats-tot", "Score")[0] / games,
        "shooting_pct": goals / shots * 100,
        "demos_pm": tot("demo-stats-tot", "DemosInflicted")[0] / games,
        "demos_received_pm": tot("demo-stats-tot", "DemosTaken")[0] / games,
        "bpm": wavg("boost-stats-avg", "BPM"),
        "supersonic_pct": wavg("move-stats-avg", "% SupersonicSpeed"),
        "avg_speed_kmh": wavg("move-stats-avg", "Avg Speed") * UU_PER_S_TO_KMH,
    }
    full = tot("boost-stats-tot", "Time 100 Boost")[0]
    buckets = sum(tot("boost-stats-tot", c)[0] for c in (
        "Time Boost 0-25%", "Time Boost 25-50%", "Time Boost 50-75%", "Time Boost 75-100%"))
    vals["full_boost_pct"] = full / buckets * 100 if buckets else None
    return {
        "players": len(t["core-stats-tot"]),
        "player_games": int(games),
        "values": {k: round(v, 3) for k, v in vals.items() if v is not None},
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fetched", required=True, help="date the pages were saved, YYYY-MM-DD")
    ap.add_argument("--mode", nargs=4, action="append", required=True,
                    metavar=("TEAM_SIZE", "HTML", "URL", "LABEL"))
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent
                                         / "src" / "chumstats" / "pro_benchmarks.json"))
    a = ap.parse_args(argv)
    modes = {}
    for size, path, url, label in a.mode:
        p = pool(Path(path).read_text(encoding="utf-8"))
        n = int(size)
        p["games"] = p["player_games"] // (2 * n)
        p.update({"team_size": n, "label": label, "source": "ballchasing.com", "url": url})
        modes[size] = p
    data = {
        "fetched": a.fetched,
        "note": ("Pooled from ballchasing.com group Players stats pages (one saved page "
                 "each). Built by scripts/build_pro_benchmarks.py."),
        "modes": modes,
    }
    Path(a.out).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
