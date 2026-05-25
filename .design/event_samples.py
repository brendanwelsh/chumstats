import sqlite3, json, sys
match_id = sys.argv[1] if len(sys.argv) > 1 else "9E21A51A11F1509C07E53D81EC8E432E"
c = sqlite3.connect("data/carball.db")
c.row_factory = sqlite3.Row

for name in ("GoalScored", "StatfeedEvent", "CrossbarHit", "RoundStarted", "MatchEnded"):
    print(f"\n=== {name} sample ===")
    rows = c.execute(
        "SELECT received_at, payload FROM raw_events WHERE match_id = ? AND event = ? "
        "ORDER BY received_at LIMIT 3", (match_id, name)
    ).fetchall()
    for r in rows:
        try:
            outer = json.loads(r["payload"])
            data_str = outer.get("Data") if isinstance(outer, dict) else None
            inner = json.loads(data_str) if isinstance(data_str, str) else outer
        except Exception:
            inner = r["payload"][:400]
        print(json.dumps(inner, indent=2)[:600])
        print()
