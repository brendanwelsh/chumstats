"""carball command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from .config import Settings
from .ingest import run_live
from .replay import iter_for_aggregator
from .session import MatchSummary, SessionTracker, run_aggregation
from .store import Store


def _print_match(s: MatchSummary, self_name: str | None) -> None:
    me = s.me(self_name=self_name)
    result = "W" if me and me.team_num == s.winner_team_num else "L"
    online = "online" if s.is_online else "offline"
    mvp = " MVP" if (me and s.is_mvp.get(me.primary_id)) else ""
    print(f"[{result}] {s.team0_score}-{s.team1_score} ({online}, {s.arena})")
    if me:
        print(f"  you: Goals {me.goals}  Shots {me.shots}  Assists {me.assists}  Saves {me.saves}  Demos {me.demos}  ({me.score} pts){mvp}")


def cmd_run(args: argparse.Namespace) -> int:
    """Live pipeline: ingest -> SQLite + Discord bot + WS overlay broadcaster."""
    import threading
    from dataclasses import asdict

    settings = Settings.from_env()
    store = Store(args.db or settings.db_path)
    session = SessionTracker(
        self_primary_id=args.primary_id or settings.player_primary_id,
        self_name=args.me or settings.player_name,
    )

    # Recover any matches whose live aggregation got dropped (e.g., we were
    # restarted mid-match). Cheap to run — only inserts NEW matches.
    try:
        recovered = store.backfill_from_raw_events()
        if recovered:
            print(f"[startup] backfilled {recovered} missing match(es) from raw_events")
    except Exception as e:
        print(f"[startup] backfill skipped: {e}")

    # Prune the high-rate tick events (UpdateState/BallHit/ClockUpdatedSeconds)
    # for any match that's already been aggregated. Lifecycle / scoring events
    # are kept forever so re-aggregation stays possible.
    if not getattr(args, "no_prune", False):
        try:
            result = store.prune_raw_events(keep_days=7)
            n = result.get("deleted") or 0
            if n:
                before = result.get("bytes_before") or 0
                after = result.get("bytes_after") or 0
                reclaimed = (before - after) / 1024 / 1024 if before and after else 0
                print(f"[startup] pruned {n:,} tick rows ({reclaimed:.0f} MB reclaimed)")
        except Exception as e:
            print(f"[startup] prune skipped: {e}")

    enable_bot = not args.no_bot and bool(settings.discord_token and settings.discord_channel_id)
    enable_server = not args.no_server

    async def main_async() -> None:
        loop = asyncio.get_running_loop()

        # Server / broadcaster
        broadcaster = None
        server_task = None
        if enable_server:
            from .server import Broadcaster, make_app, serve
            broadcaster = Broadcaster()
            app = make_app(
                broadcaster, store=store,
                self_primary_id=session.self_primary_id, self_name=session.self_name,
            )
            server_task = asyncio.create_task(serve(app, host=settings.server_host, port=settings.server_port))
            from .config import local_ip
            ip = local_ip()
            shown = settings.server_host if settings.server_host != "0.0.0.0" else "127.0.0.1"
            print(f"[server] overlay     http://{shown}:{settings.server_port}/")
            print(f"[server] dashboard   http://{shown}:{settings.server_port}/dashboard")
            if settings.server_host == "0.0.0.0" and ip and ip != "127.0.0.1":
                print(f"[server]   on your LAN: http://{ip}:{settings.server_port}/dashboard  (phone / other devices)")

        # Bot
        bot_task = None
        poster = None
        if enable_bot:
            from .bot import MatchPoster
            poster = MatchPoster(
                token=settings.discord_token,
                channel_id=settings.discord_channel_id,
                self_primary_id=session.self_primary_id,
                self_name=session.self_name,
                store=store,
                friends=settings.friends or [],
                public_url=settings.public_url,
            )
            bot_task = asyncio.create_task(poster.run())
            print(f"[bot] posting to channel {settings.discord_channel_id}")


        # Ingest runs in a thread; uses callbacks to reach back into async land.
        def on_match(s: MatchSummary) -> None:
            print()
            _print_match(s, session.self_name)
            t = session.totals
            print(f"  session: {t.wins}-{t.losses} ({t.win_rate * 100:.0f}%) "
                  f"streak {t.streak_label} | Goals {t.goals}  Assists {t.assists}  Saves {t.saves}")
            if poster:
                poster.enqueue(s, session.totals)
            if broadcaster:
                broadcaster.push_match_end(s, session.totals, loop)

        last_arena = ""
        last_t0_name = ""
        last_t1_name = ""

        def on_event(event_name: str, raw: dict) -> None:
            nonlocal last_arena, last_t0_name, last_t1_name
            if not broadcaster:
                return

            if event_name == "MatchCreated":
                broadcaster.push_match_start({"arena": last_arena}, loop)
            elif event_name == "UpdateState":
                game = raw.get("Game") or {}
                teams = game.get("Teams") or []
                t0 = next((t for t in teams if t.get("TeamNum") == 0), None) or {}
                t1 = next((t for t in teams if t.get("TeamNum") == 1), None) or {}
                last_arena = game.get("Arena") or last_arena
                last_t0_name = t0.get("Name") or last_t0_name or "Blue"
                last_t1_name = t1.get("Name") or last_t1_name or "Orange"
                ball = game.get("Ball") or {}
                payload = {
                    "team0_name":  last_t0_name,
                    "team1_name":  last_t1_name,
                    "team0_score": t0.get("Score", 0),
                    "team1_score": t1.get("Score", 0),
                    "time_seconds": game.get("TimeSeconds", 0),
                    "is_overtime":  game.get("bOvertime", False),
                    "arena":        last_arena,
                    "ball_speed":   ball.get("Speed", 0),
                    "ball_team":    ball.get("TeamNum"),
                    "has_winner":   game.get("bHasWinner", False),
                    "winner":       game.get("Winner") or "",
                    "players": [
                        {
                            "name":       p.get("Name", ""),
                            "primary_id": p.get("PrimaryId", ""),
                            "team_num":   p.get("TeamNum", 0),
                            "is_bot":     (p.get("PrimaryId") == "Unknown|0|0"),
                            "goals":      p.get("Goals", 0),
                            "assists":    p.get("Assists", 0),
                            "saves":      p.get("Saves", 0),
                            "shots":      p.get("Shots", 0),
                            "demos":      p.get("Demos", 0),
                            "score":      p.get("Score", 0),
                            "touches":    p.get("Touches", 0),
                            "car_touches":p.get("CarTouches", 0),
                            "boost":      p.get("Boost"),
                            "speed":      p.get("Speed"),
                            "on_ground":  p.get("bOnGround", False),
                            "on_wall":    p.get("bOnWall", False),
                            "has_car":    p.get("bHasCar", False),
                            "boosting":   p.get("bBoosting", False),
                            "supersonic": (p.get("Speed") or 0) >= 2200,
                        }
                        for p in (raw.get("Players") or [])
                    ],
                }
                broadcaster.push_tick(payload, loop)
            elif event_name == "GoalScored":
                broadcaster.push_event("goal", raw, loop)
            elif event_name == "CrossbarHit":
                broadcaster.push_event("crossbar", raw, loop)

        def run_thread() -> None:
            run_live(
                store, session,
                host=args.host, port=args.port,
                on_match=on_match,
                on_event=on_event,
            )

        t = threading.Thread(target=run_thread, daemon=True, name="carball-ingest")
        t.start()
        print("[ingest] started")
        print("press Ctrl+C to stop.")

        try:
            tasks = [x for x in (server_task, bot_task) if x]
            if tasks:
                # return_exceptions=True so one failing task (e.g. bot can't reach
                # its channel) doesn't kill the rest of the pipeline.
                await asyncio.gather(*tasks, return_exceptions=True)
            else:
                while True:
                    await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\nshutting down.")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    store = Store(args.db) if args.db else None
    session = SessionTracker(self_name=args.me)
    for path in args.files:
        print(f"\n=== {path} ===")
        summaries = run_aggregation(iter_for_aggregator(path))
        for s in summaries:
            _print_match(s, args.me)
            if store:
                store.save_match(s)
            session.add(s)
    t = session.totals
    print()
    print(f"session: {t.wins}-{t.losses} ({t.win_rate * 100:.0f}%) streak {t.streak_label} "
          f"| Goals {t.goals}  Assists {t.assists}  Saves {t.saves}  Shots {t.shots}  Demos {t.demos}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    store = Store(args.db)
    if args.primary_id:
        d = store.lifetime_for(primary_id=args.primary_id)
    else:
        d = store.lifetime_for(name=args.me)
    print(json.dumps(d, indent=2))
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Print a full career dashboard for one player."""
    from .analytics import build_dashboard, render_dashboard_text
    s = Settings.from_env()
    store = Store(args.db or s.db_path)
    primary_id = args.primary_id or s.player_primary_id
    name = args.me or s.player_name
    d = build_dashboard(store, primary_id=primary_id, name=name)
    print(render_dashboard_text(d))
    return 0


def cmd_match(args: argparse.Namespace) -> int:
    """Print analytics for one specific match from the DB."""
    from .analytics import build_analytics, render_text
    s = Settings.from_env()
    store = Store(args.db or s.db_path)
    primary_id = args.primary_id or s.player_primary_id
    name = args.me or s.player_name

    with store._conn() as con:
        match_row = con.execute("SELECT * FROM matches WHERE id = ?", (args.match_id,)).fetchone()
        if not match_row:
            print(f"no match with id {args.match_id}")
            return 1
        player_rows = con.execute("""
            SELECT * FROM match_player_stats WHERE match_id = ?
        """, (args.match_id,)).fetchall()
        extras = con.execute("SELECT * FROM match_extras WHERE match_id = ?", (args.match_id,)).fetchone()

    # Reconstruct a MatchSummary lite from DB rows (enough for analytics).
    from .session import MatchSummary, PlayerLine
    players = []
    for r in player_rows:
        p = PlayerLine(
            name=r["name"], primary_id=r["primary_id"], team_num=r["team_num"],
            goals=r["goals"], assists=r["assists"], saves=r["saves"], shots=r["shots"],
            demos=r["demos"], touches=r["touches"], score=r["score"],
            is_bot=bool(r["is_bot"]), platform=r["platform"],
            ticks_total=r["ticks_total"], ticks_on_wall=r["ticks_on_wall"],
            ticks_on_ground=r["ticks_on_ground"], ticks_in_air=r["ticks_in_air"],
            ticks_boosting=r["ticks_boosting"], ticks_supersonic=r["ticks_supersonic"],
            ticks_zero_boost=r["ticks_zero_boost"], ticks_full_boost=r["ticks_full_boost"],
            speed_sum=r["speed_sum"], speed_max=r["speed_max"], boost_used=r["boost_used"],
        )
        players.append(p)
    is_mvp = {p.primary_id: True for p in players
              if any(r["primary_id"] == p.primary_id and r["is_mvp"] for r in player_rows)}
    sm = MatchSummary(
        match_id=match_row["id"],
        started_at=match_row["started_at"], ended_at=match_row["ended_at"],
        arena=match_row["arena"],
        team0_score=match_row["team0_score"], team1_score=match_row["team1_score"],
        team0_name=match_row["team0_name"], team1_name=match_row["team1_name"],
        winner_team_num=match_row["winner_team_num"],
        players=players, is_mvp=is_mvp,
        crossbar_hits=match_row["crossbar_hits"],
        is_online=bool(match_row["is_online"]),
        duration_seconds=(extras["duration_seconds"] if extras else 0.0),
    )
    a = build_analytics(sm, self_primary_id=primary_id, self_name=name, store=store)
    print(render_text(a))
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    """Side-by-side lifetime stat comparison of two players."""
    from .analytics import build_comparison, render_comparison_text
    s = Settings.from_env()
    store = Store(args.db or s.db_path)
    # A defaults to YOU.
    a_pid = args.a_primary_id or s.player_primary_id
    a_name = args.a_name or s.player_name
    b_pid = args.b_primary_id
    b_name = args.name
    c = build_comparison(store, a_primary_id=a_pid, a_name=a_name,
                         b_primary_id=b_pid, b_name=b_name)
    print(render_comparison_text(c))
    return 0


def cmd_player(args: argparse.Namespace) -> int:
    """Print a career dashboard for any player by name. We capture stats
    for everyone we've ever shared a match with, so this works for
    teammates, opponents, randoms, friends, bots, anyone."""
    from .analytics import build_dashboard, render_dashboard_text
    s = Settings.from_env()
    store = Store(args.db or s.db_path)
    d = build_dashboard(store, name=args.name)
    if not d.overview.lines:
        print(f"no matches found for player named '{args.name}'.")
        print("try `carball replay <captures>` first or check the name spelling.")
        return 1
    print(render_dashboard_text(d))
    return 0


def cmd_players(args: argparse.Namespace) -> int:
    """List every player we have stats for, sorted by matches played."""
    s = Settings.from_env()
    store = Store(args.db or s.db_path)
    with store._conn() as con:
        rows = con.execute("""
            SELECT name, primary_id, COUNT(*) AS n,
                   SUM(goals) AS goals, SUM(saves) AS saves,
                   MAX(is_bot) AS is_bot, MIN(platform) AS platform
            FROM match_player_stats
            GROUP BY name, primary_id
            ORDER BY n DESC, name
        """).fetchall()
    if not rows:
        print("no players in the DB. play a match or run `carball replay`.")
        return 0
    print(f"{'PLAYER':<22} {'PLATFORM':<10} {'MATCHES':>7} {'GOALS':>6} {'SAVES':>6}")
    print("-" * 60)
    for r in rows:
        tag = " (BOT)" if r["is_bot"] else ""
        name = (r["name"] + tag)[:22]
        print(f"{name:<22} {r['platform']:<10} {r['n']:>7} {r['goals']:>6} {r['saves']:>6}")
    return 0


def cmd_vs(args: argparse.Namespace) -> int:
    """Head-to-head splits vs another player by name (or primary_id)."""
    from .analytics import _h2h_record, _coplay_record
    s = Settings.from_env()
    store = Store(args.db or s.db_path)
    my_pid = args.primary_id or s.player_primary_id
    if not my_pid:
        print("provide --primary-id or RL_PLAYER_PRIMARY_ID")
        return 1
    target_name = args.name
    target_pid = args.target_id
    with store._conn() as con:
        h2h = _h2h_record(con, my_pid, target_pid, target_name)
        co  = _coplay_record(con, my_pid, target_pid, target_name)
    print(f"vs {target_name or target_pid}")
    print(f"  as opponent: {h2h['wins']}-{h2h['losses']}  ({h2h['matches']} matches)")
    print(f"  as teammate: {co['wins']}-{co['losses']}  ({co['matches']} matches)")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    from pathlib import Path as _P
    from .config_wizard import run_wizard
    manual = _P(args.rl_path) if args.rl_path else None
    rep = run_wizard(enable=not args.disable, rate=args.rate, manual_path=manual)
    print()
    for a in rep.actions:
        print(f"  - {a}")
    if rep.error:
        print(f"\n  ERROR: {rep.error}")
        return 1
    if rep.after and rep.after.enabled:
        print(f"\n  OK: Stats API is ON. Port={rep.after.port} PacketSendRate={rep.after.packet_send_rate}")
    else:
        print(f"\n  OK: Stats API is OFF.")
    if rep.rl_running:
        print("  ! Restart Rocket League for the change to apply.")
    return 0


def cmd_post_test(args: argparse.Namespace) -> int:
    """One-shot: read captures (or DB matches), post one Discord embed each.
    Verifies the bot token/channel work end-to-end without leaving a long-
    running connection open."""
    from .bot import post_one

    s = Settings.from_env()
    token = args.token or s.discord_token
    channel = args.channel or s.discord_channel_id
    if not token or not channel:
        print("missing DISCORD_TOKEN or DISCORD_CHANNEL_ID. Set in .env or pass --token/--channel.")
        return 1

    me = args.me or s.player_name
    me_id = args.primary_id or s.player_primary_id

    tracker = SessionTracker(self_primary_id=me_id, self_name=me)
    summaries: list[MatchSummary] = []

    if args.files:
        for path in args.files:
            for sm in run_aggregation(iter_for_aggregator(path)):
                tracker.add(sm)
                summaries.append(sm)

    if not summaries:
        # Fall back to whatever's already in the DB.
        from .store import Store
        store = Store(args.db or s.db_path)
        rows = store.recent_matches(primary_id=me_id, limit=5)
        if not rows:
            print("no captures provided and no matches in DB - run `carball replay <file>` first.")
            return 1
        print(f"(no --files given, posting {len(rows)} most recent match(es) from DB)")
        # Crude: re-build MatchSummary from store query would be ugly; for the
        # one-shot test we re-run replay over the captures dir if it exists.
        captures = Path(__file__).resolve().parents[2] / "captures"
        for f in sorted(captures.glob("rl_*.jsonl"))[-5:]:
            for sm in run_aggregation(iter_for_aggregator(f)):
                tracker.add(sm)
                summaries.append(sm)

    print(f"posting {len(summaries)} embed(s) to channel {channel}...")

    from .store import Store
    store = Store(args.db or s.db_path)

    fail_count = 0
    async def go() -> None:
        nonlocal fail_count
        for sm in summaries:
            r = await post_one(
                token, channel, sm, tracker.totals,
                self_name=me, self_primary_id=me_id, store=store,
                friends=s.friends or [],
            )
            if r.success:
                print(f"  POSTED: {sm.team0_name} {sm.team0_score}-{sm.team1_score} {sm.team1_name} (msg {r.message_id})")
            else:
                fail_count += 1
                print(f"  FAILED: {sm.team0_name} {sm.team0_score}-{sm.team1_score} {sm.team1_name}")
                print(f"          {r.error}")
            await asyncio.sleep(1)

    asyncio.run(go())
    if fail_count:
        print(f"done with {fail_count} failure(s).")
        return 1
    print("done.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="carball")
    p.add_argument("--db", default=str(Path.home() / ".carball" / "carball.db"),
                   help="SQLite DB path")
    p.add_argument("--me", default=None, help="Your in-game name (e.g. @ChumtheWaters)")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Connect to RL and ingest live")
    p_run.add_argument("--host", default="127.0.0.1", help="RL Stats API host (default 127.0.0.1)")
    p_run.add_argument("--port", type=int, default=49123)
    p_run.add_argument("--primary-id", default=None, help="Your primary_id (e.g. Steam|765...|0)")
    p_run.add_argument("--no-bot", action="store_true", help="Disable Discord bot")
    p_run.add_argument("--no-server", action="store_true", help="Disable overlay server")
    p_run.add_argument("--no-prune", action="store_true", help="Skip startup prune of old tick events")
    p_run.set_defaults(func=cmd_run)

    p_replay = sub.add_parser("replay", help="Replay one or more .jsonl captures")
    p_replay.add_argument("files", nargs="+")
    p_replay.set_defaults(func=cmd_replay)

    p_stats = sub.add_parser("stats", help="Lifetime stats from the DB")
    p_stats.add_argument("--primary-id", default=None)
    p_stats.set_defaults(func=cmd_stats)

    p_pt = sub.add_parser("post-test", help="Post one Discord embed per matched fixture (sanity check)")
    p_pt.add_argument("files", nargs="*", help="optional .jsonl fixture files; falls back to captures/")
    p_pt.add_argument("--token", default=None)
    p_pt.add_argument("--channel", type=int, default=None)
    p_pt.add_argument("--primary-id", default=None)
    p_pt.set_defaults(func=cmd_post_test)

    p_setup = sub.add_parser("setup", help="Detect RL install and enable the Stats API")
    p_setup.add_argument("--disable", action="store_true", help="Set PacketSendRate=0 (turn it off)")
    p_setup.add_argument("--rate", type=int, default=30, help="PacketSendRate value (default 30, max 120)")
    p_setup.add_argument("--rl-path", default=None, help="Manual override: path to your rocketleague install root")
    p_setup.set_defaults(func=cmd_setup)

    p_dash = sub.add_parser("dashboard", help="Career dashboard for the configured player")
    p_dash.add_argument("--primary-id", default=None)
    p_dash.set_defaults(func=cmd_dashboard)

    p_match = sub.add_parser("match", help="Analytics for one specific match")
    p_match.add_argument("match_id", help="Match GUID or local-<uuid>")
    p_match.add_argument("--primary-id", default=None)
    p_match.set_defaults(func=cmd_match)

    p_vs = sub.add_parser("vs", help="Head-to-head and team-up splits vs another player")
    p_vs.add_argument("name", nargs="?", default=None, help="Opponent name (or pass --target-id)")
    p_vs.add_argument("--target-id", default=None)
    p_vs.add_argument("--primary-id", default=None, help="Your primary_id (defaults to .env)")
    p_vs.set_defaults(func=cmd_vs)

    p_player = sub.add_parser("player", help="Career dashboard for any player we've seen")
    p_player.add_argument("name", help="In-game name (case-sensitive)")
    p_player.set_defaults(func=cmd_player)

    p_players = sub.add_parser("players", help="List every player we have stats for")
    p_players.set_defaults(func=cmd_players)

    p_cmp = sub.add_parser("compare", help="Side-by-side lifetime stats: you vs another player")
    p_cmp.add_argument("name", help="Other player's name (you are side A by default)")
    p_cmp.add_argument("--a-name", default=None, help="Override side-A name (default: your configured name)")
    p_cmp.add_argument("--a-primary-id", default=None)
    p_cmp.add_argument("--b-primary-id", default=None)
    p_cmp.set_defaults(func=cmd_compare)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
