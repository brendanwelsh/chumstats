import sqlite3, datetime
c = sqlite3.connect("data/carball.db")
c.row_factory = sqlite3.Row
rows = c.execute(
    "SELECT id, started_at, team0_name, team0_score, team1_score, team1_name, "
    "winner_team_num, is_online FROM matches ORDER BY started_at DESC LIMIT 5"
).fetchall()
print("most recent matches:")
for r in rows:
    ts = datetime.datetime.fromtimestamp(r["started_at"]).strftime("%Y-%m-%d %H:%M")
    mode = "online" if r["is_online"] else "offline"
    print(f"  {ts} | {r['team0_name']} {r['team0_score']} - "
          f"{r['team1_score']} {r['team1_name']} ({mode}) | id={r['id']}")
