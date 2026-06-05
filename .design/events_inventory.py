import sqlite3, json, sys
match_id = sys.argv[1] if len(sys.argv) > 1 else "9E21A51A11F1509C07E53D81EC8E432E"
c = sqlite3.connect("data/ballshark.db")
c.row_factory = sqlite3.Row
print(f"event counts in match {match_id}:")
for r in c.execute(
    "SELECT event, COUNT(*) AS n FROM raw_events WHERE match_id = ? "
    "GROUP BY event ORDER BY n DESC", (match_id,)
).fetchall():
    print(f"  {r['event']:<35} {r['n']}")
print()
print("sample Goal payload:")
g = c.execute(
    "SELECT payload FROM raw_events WHERE match_id = ? AND event = 'Goal' LIMIT 1",
    (match_id,)
).fetchone()
if g:
    try:
        outer = json.loads(g["payload"])
        inner = json.loads(outer.get("Data") or "{}")
        print(json.dumps(inner, indent=2)[:800])
    except Exception as e:
        print("parse err:", e)
        print(g["payload"][:400])
print()
print("sample Demolish payload:")
d = c.execute(
    "SELECT payload FROM raw_events WHERE match_id = ? AND event = 'Demolish' LIMIT 1",
    (match_id,)
).fetchone()
if d:
    try:
        outer = json.loads(d["payload"])
        inner = json.loads(outer.get("Data") or "{}")
        print(json.dumps(inner, indent=2)[:800])
    except Exception as e:
        print("parse err:", e)
