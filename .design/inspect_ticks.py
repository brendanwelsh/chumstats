import sqlite3
con = sqlite3.connect("data/carball.db")
con.row_factory = sqlite3.Row
ms = con.execute(
    "SELECT id, team0_name, team1_name, team0_score, team1_score, started_at, winner_team_num "
    "FROM matches ORDER BY started_at"
).fetchall()
for m in ms:
    print(f"\n=== match {m['id']}")
    print(f"  {m['team0_name']} {m['team0_score']} - {m['team1_score']} {m['team1_name']}  (winner team {m['winner_team_num']})")
    ps = con.execute(
        "SELECT name, primary_id, team_num, is_bot, ticks_total, ticks_supersonic, "
        "ticks_in_air, ticks_on_wall, ticks_on_ground, speed_sum, boost_used "
        "FROM match_player_stats WHERE match_id = ?", (m['id'],)
    ).fetchall()
    for p in ps:
        has_adv = p['ticks_total'] and p['ticks_total'] >= 200
        marker = "ADV" if has_adv else "..."
        print(f"  [{marker}] {p['name']:<22} team={p['team_num']} bot={p['is_bot']} "
              f"ticks={p['ticks_total']:>6} super={p['ticks_supersonic']:>4} "
              f"air={p['ticks_in_air']:>4} wall={p['ticks_on_wall']:>4} "
              f"ground={p['ticks_on_ground']:>4} boost={p['boost_used']:.1f}")
        print(f"        primary_id={p['primary_id']}")
