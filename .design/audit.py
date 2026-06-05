"""Cross-check the recorded per-player stats against the raw event stream.
We want to surface display bugs / counting bugs before we add new stats."""
import sqlite3, json, sys
from collections import Counter, defaultdict

match_id = sys.argv[1] if len(sys.argv) > 1 else "9E21A51A11F1509C07E53D81EC8E432E"
c = sqlite3.connect("data/ballshark.db")
c.row_factory = sqlite3.Row

m = c.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
extras = c.execute("SELECT * FROM match_extras WHERE match_id = ?", (match_id,)).fetchone()
ps = c.execute("SELECT * FROM match_player_stats WHERE match_id = ? ORDER BY team_num, score DESC",
               (match_id,)).fetchall()

print(f"=== AUDIT FOR MATCH {match_id} ===")
print(f"  {m['team0_name']} {m['team0_score']} - {m['team1_score']} {m['team1_name']}")
print(f"  duration_seconds = {extras['duration_seconds']:.1f}  (expected ticks at 30Hz: {int(extras['duration_seconds']*30)})")
print(f"  recorded crossbar_hits = {m['crossbar_hits']}")
print()

# Re-derive everything from raw_events
goal_scored = []
crossbars = 0
demos_per_attacker = Counter()
demos_per_victim = Counter()
shots_per_player = Counter()
saves_per_player = Counter()
assists_per_player = Counter()
specials_per_player = defaultdict(Counter)
goal_speed_sum = defaultdict(float)
goal_speed_n = defaultdict(int)

for r in c.execute("SELECT received_at, event, payload FROM raw_events WHERE match_id = ? "
                   "ORDER BY received_at", (match_id,)).fetchall():
    try:
        d = json.loads(r["payload"])
    except Exception:
        continue
    e = r["event"]
    if e == "GoalScored":
        scorer = (d.get("Scorer") or {}).get("Name") or ""
        if scorer:
            speed = d.get("GoalSpeed") or 0
            goal_speed_sum[scorer] += speed
            goal_speed_n[scorer] += 1
            goal_scored.append((r["received_at"], scorer, speed, d.get("GoalTime")))
    elif e == "CrossbarHit":
        crossbars += 1
    elif e == "StatfeedEvent":
        ev = d.get("EventName")
        main = (d.get("MainTarget") or {}).get("Name") or ""
        sec = (d.get("SecondaryTarget") or {}).get("Name") or ""
        if ev == "Demolish":
            demos_per_attacker[main] += 1
            if sec:
                demos_per_victim[sec] += 1
        elif ev == "Shot":
            shots_per_player[main] += 1
        elif ev == "Save":
            saves_per_player[main] += 1
        elif ev == "Assist":
            assists_per_player[main] += 1
        elif ev in ("EpicSave", "AerialGoal", "BicycleHit", "BackwardsGoal", "LongGoal",
                    "FlipReset", "HatTrick", "Savior", "LowFive"):
            specials_per_player[main][ev] += 1

print(f"--- RAW EVENT AGGREGATES ---")
print(f"  GoalScored events: {len(goal_scored)}  (DB team_score total: {(m['team0_score'] or 0) + (m['team1_score'] or 0)})")
print(f"  CrossbarHit events: {crossbars}  (DB crossbar_hits: {m['crossbar_hits']})")
print()
print(f"--- PER-PLAYER CROSS-CHECK ---")
for p in ps:
    name = p["name"]
    print(f"  {name} (team {p['team_num']}, bot={p['is_bot']})")
    print(f"    DB:    goals={p['goals']}  assists={p['assists']}  saves={p['saves']}  shots={p['shots']}  demos={p['demos']}")
    print(f"    Feed:  goals={goal_speed_n.get(name, 0)}  assists={assists_per_player.get(name, 0)}  "
          f"saves={saves_per_player.get(name, 0)}  shots={shots_per_player.get(name, 0)}  "
          f"demos_given={demos_per_attacker.get(name, 0)}")
    print(f"    Derived NEW: demos_received={demos_per_victim.get(name, 0)}  "
          f"avg_goal_speed={(goal_speed_sum.get(name, 0) / goal_speed_n.get(name, 1)) if goal_speed_n.get(name) else 0:.0f}  "
          f"specials={dict(specials_per_player.get(name, {}))}")
    # Tick sanity
    tt = p["ticks_total"] or 0
    if tt:
        g = p["ticks_on_ground"] or 0
        a = p["ticks_in_air"] or 0
        w = p["ticks_on_wall"] or 0
        sup = p["ticks_supersonic"] or 0
        sumg = g + a + w
        print(f"    Ticks: total={tt}  ground+air+wall={sumg} ({sumg/tt*100:.1f}%)  "
              f"supersonic={sup} ({sup/tt*100:.1f}%)")
    print()

# Goal participation: (player goals + assists) / team goals
print(f"--- GOAL PARTICIPATION ---")
for team in (0, 1):
    team_players = [p for p in ps if p["team_num"] == team]
    team_goals = sum(p["goals"] or 0 for p in team_players)
    print(f"  Team {team} ({m[f'team{team}_name']}): {team_goals} goals")
    for p in team_players:
        contrib = (p["goals"] or 0) + (p["assists"] or 0)
        pct = (contrib / team_goals * 100) if team_goals else 0
        print(f"    {p['name']:<22} g={p['goals']}+a={p['assists']}={contrib} = {pct:.0f}% of team")
