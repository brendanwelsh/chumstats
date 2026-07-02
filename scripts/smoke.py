"""Smoke test: replay our captured .jsonl fixtures through the aggregator
and print the resulting MatchSummary objects. No deps on pytest - just run it.

Usage:
    .venv\\Scripts\\python.exe scripts\\smoke.py
"""

from __future__ import annotations

from pathlib import Path

from chumstats.replay import iter_for_aggregator
from chumstats.session import SessionTracker, run_aggregation

CAPTURES = Path(__file__).resolve().parents[1] / "captures"


def main() -> None:
    files = sorted(CAPTURES.glob("rl_*.jsonl"))
    if not files:
        print(f"no captures found in {CAPTURES}")
        return

    tracker = SessionTracker(self_name="@ChumtheWaters")

    for f in files:
        print(f"\n=== {f.name} ({f.stat().st_size / 1024 / 1024:.1f} MB) ===")
        summaries = run_aggregation(iter_for_aggregator(f))
        print(f"  finalized matches: {len(summaries)}")
        for s in summaries:
            me = s.me(self_name="@ChumtheWaters")
            result = "W" if me and me.team_num == s.winner_team_num else "L"
            online = "online" if s.is_online else "offline"
            arena = s.arena or "?"
            mvp_mark = " MVP" if (me and s.is_mvp.get(me.primary_id)) else ""
            line = (
                f"    [{result}] {s.team0_score}-{s.team1_score} "
                f"({online}, {arena}) {len(s.players)}p"
            )
            print(line)
            if me:
                print(
                    f"        you: G{me.goals} S{me.shots} A{me.assists} "
                    f"Sv{me.saves} D{me.demos} {me.score}pts{mvp_mark}"
                )
                print(
                    f"           derived: avg-spd {me.avg_speed:.1f} | max-spd {me.speed_max:.1f} | "
                    f"super {me.pct_supersonic * 100:.0f}% | "
                    f"wall {me.pct_on_wall * 100:.0f}% | air {me.pct_in_air * 100:.0f}% | "
                    f"boost-used {me.boost_used} | at-0 {me.ticks_zero_boost} ticks"
                )
            print(f"        duration: {s.duration_seconds:.1f}s  ball-touches: {len(s.ball_touches)}  goals(deduped): {len(s.goal_events)}")
            for p in s.players:
                tag = " (bot)" if p.is_bot else f" ({p.platform})"
                print(
                    f"          T{p.team_num} {p.name}{tag}: "
                    f"G{p.goals} A{p.assists} Sv{p.saves} Sh{p.shots} D{p.demos}"
                )
            tracker.add(s)

    t = tracker.totals
    print("\n=== session totals ===")
    print(f"  played: {t.matches_played}  W-L: {t.wins}-{t.losses}  "
          f"win%: {t.win_rate * 100:.0f}  streak: {t.streak_label}")
    print(f"  G {t.goals} | A {t.assists} | Sv {t.saves} | Sh {t.shots} | D {t.demos}")
    print(f"  crossbar hits seen: {t.crossbar_hits}")


if __name__ == "__main__":
    main()
