"""One-off poster: rehydrate a match from SQLite, build a current SessionTotals
from the on-disk session log if available, and call bot.post_one. Used to
back-post a match that finished while the live tracker was running --no-bot."""
import asyncio
import os
import sys
import sqlite3
from dotenv import load_dotenv

load_dotenv()
TOKEN = (os.environ.get("DISCORD_TOKEN") or os.environ.get("DISCORD_BOT_TOKEN")
         or os.environ.get("CARBALL_DISCORD_TOKEN"))
CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_ID")
                 or os.environ.get("CARBALL_DISCORD_CHANNEL_ID") or 0)
SELF_NAME = os.environ.get("RL_PLAYER_NAME") or "@ChumtheWaters"
SELF_PID = os.environ.get("RL_PLAYER_PRIMARY_ID") or "Steam|76561197985273611|0"

if not TOKEN or not CHANNEL_ID:
    print("Missing DISCORD_BOT_TOKEN or DISCORD_CHANNEL_ID in .env")
    sys.exit(1)

if len(sys.argv) < 2:
    print("usage: post_one_match.py <match_id>")
    sys.exit(1)

match_id = sys.argv[1]
sys.path.insert(0, "src")
from carball.session import MatchSummary, PlayerLine, SessionTotals
from carball.store import Store
from carball.bot import post_one

store = Store("data/carball.db")
with store._conn() as con:
    m = con.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
    if not m:
        print(f"no match with id {match_id}")
        sys.exit(1)
    player_rows = con.execute(
        "SELECT * FROM match_player_stats WHERE match_id = ?", (match_id,)
    ).fetchall()
    extras = con.execute(
        "SELECT * FROM match_extras WHERE match_id = ?", (match_id,)
    ).fetchone()

players = []
for r in player_rows:
    players.append(PlayerLine(
        name=r["name"], primary_id=r["primary_id"], team_num=r["team_num"],
        goals=r["goals"], assists=r["assists"], saves=r["saves"], shots=r["shots"],
        demos=r["demos"], touches=r["touches"], score=r["score"],
        is_bot=bool(r["is_bot"]), platform=r["platform"],
        ticks_total=r["ticks_total"], ticks_on_wall=r["ticks_on_wall"],
        ticks_on_ground=r["ticks_on_ground"], ticks_in_air=r["ticks_in_air"],
        ticks_boosting=r["ticks_boosting"], ticks_supersonic=r["ticks_supersonic"],
        ticks_zero_boost=r["ticks_zero_boost"], ticks_full_boost=r["ticks_full_boost"],
        speed_sum=r["speed_sum"], speed_max=r["speed_max"], boost_used=r["boost_used"],
    ))
is_mvp = {r["primary_id"]: True for r in player_rows if r["is_mvp"]}
sm = MatchSummary(
    match_id=m["id"],
    started_at=m["started_at"], ended_at=m["ended_at"],
    arena=m["arena"],
    team0_score=m["team0_score"], team1_score=m["team1_score"],
    team0_name=m["team0_name"], team1_name=m["team1_name"],
    winner_team_num=m["winner_team_num"],
    players=players, is_mvp=is_mvp,
    crossbar_hits=m["crossbar_hits"],
    is_online=bool(m["is_online"]),
    duration_seconds=(extras["duration_seconds"] if extras else 0.0),
)

me = next((p for p in players if p.primary_id == SELF_PID or p.name == SELF_NAME), None)
won = me and me.team_num == m["winner_team_num"]
totals = SessionTotals(
    matches_played=1,
    wins=1 if won else 0,
    losses=0 if won else 1,
    current_streak=1 if won else -1,
    goals=(me.goals if me else 0),
    assists=(me.assists if me else 0),
    saves=(me.saves if me else 0),
    shots=(me.shots if me else 0),
    demos=(me.demos if me else 0),
)

async def main():
    print(f"posting match {match_id} ({sm.team0_name} {sm.team0_score} - {sm.team1_score} {sm.team1_name}) to channel {CHANNEL_ID}...")
    result = await post_one(
        TOKEN, CHANNEL_ID, sm, totals,
        self_name=SELF_NAME, self_primary_id=SELF_PID,
        store=store,
    )
    if result.success:
        print(f"OK: posted message id={result.message_id} ({result.guilds} guilds)")
    else:
        print(f"FAIL: {result.error}")
        sys.exit(2)

asyncio.run(main())
