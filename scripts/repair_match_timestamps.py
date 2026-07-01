"""Repair matches whose started_at/ended_at were stamped at REPLAY time.

Before the received_at fix (parser v3), the startup backfill and `chumstats
reprocess` stamped recovered matches with the wall clock of the moment the
raw events were RE-READ, not when the match was played — e.g. one migration
run left 75 matches all "started" in the same minute. The original times are
still in raw_events.received_at (lifecycle rows are kept forever), so this
restores them:

  started_at <- MIN(received_at) of the match's raw events
  ended_at   <- received_at of its MatchEnded, else MAX(received_at)

Only matches whose stored started_at is more than --threshold seconds away
from the raw evidence are touched. Dry-run by default; pass --apply to write
(stop the tray/`chumstats run` first so the DB isn't mid-write).

Also reports (never auto-fixes) matches that look like a private-lobby
guid-reuse overwrite: stored goal_events is empty although raw GoalScored
rows exist. Re-derive those with `chumstats reprocess` if their ticks are
still inside the retention window.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=None, help="path to chumstats.db (default: from env/config)")
    ap.add_argument("--apply", action="store_true", help="write the fixes (default: dry run)")
    ap.add_argument("--threshold", type=float, default=3600.0,
                    help="only fix matches whose started_at is off by more than this many seconds")
    args = ap.parse_args()

    db = args.db
    if not db:
        from chumstats.config import Settings
        db = Settings.from_env().db_path

    mode = "" if args.apply else "?mode=ro"
    con = sqlite3.connect(f"file:{db}{mode}", uri=True)
    con.row_factory = sqlite3.Row

    rows = con.execute("""
        SELECT m.id, m.started_at, m.ended_at,
               (SELECT MIN(received_at) FROM raw_events re WHERE re.match_id = m.id) AS true_start,
               (SELECT received_at FROM raw_events re
                WHERE re.match_id = m.id AND re.event = 'MatchEnded' LIMIT 1)       AS ended_ts,
               (SELECT MAX(received_at) FROM raw_events re WHERE re.match_id = m.id) AS last_ts
        FROM matches m
    """).fetchall()

    fixes = []
    for r in rows:
        if r["true_start"] is None:
            continue  # no raw events (e.g. friend-uploaded summary) — nothing to restore
        if abs(r["started_at"] - r["true_start"]) <= args.threshold:
            continue
        new_end = r["ended_ts"] if r["ended_ts"] is not None else r["last_ts"]
        fixes.append((r["id"], r["started_at"], r["true_start"], new_end))

    print(f"{len(fixes)} match(es) with replay-time timestamps (threshold {args.threshold:.0f}s):")
    for mid, old, new, _new_end in fixes:
        print(f"  {mid[:24]:<26} {time.strftime('%Y-%m-%d %H:%M', time.localtime(old))}"
              f" -> {time.strftime('%Y-%m-%d %H:%M', time.localtime(new))}")

    if fixes and args.apply:
        con.executemany(
            "UPDATE matches SET started_at = ?, ended_at = ? WHERE id = ?",
            [(new, new_end, mid) for mid, _old, new, new_end in fixes],
        )
        con.commit()
        print(f"applied {len(fixes)} timestamp fix(es).")
    elif fixes:
        print("dry run — re-run with --apply to write (stop chumstats first).")

    suspects = []
    for r in con.execute("""
        SELECT m.id, m.team0_score + m.team1_score AS total, e.goal_events AS ge
        FROM matches m JOIN match_extras e ON e.match_id = m.id
        WHERE m.team0_score + m.team1_score > 0
    """):
        try:
            stored = len(json.loads(r["ge"]))
        except Exception:
            continue
        if stored:
            continue
        raw_goals = con.execute(
            "SELECT COUNT(*) FROM raw_events WHERE match_id = ? AND event = 'GoalScored'",
            (r["id"],),
        ).fetchone()[0]
        if raw_goals:
            suspects.append(r["id"])
    if suspects:
        print(f"\n{len(suspects)} match(es) look overwritten by a guid-reuse lobby segment")
        print("(empty stored goal_events but raw GoalScored rows exist):")
        for mid in suspects:
            print(f"  {mid}")
        print("re-derive with `chumstats reprocess` (works while their ticks are retained).")

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
