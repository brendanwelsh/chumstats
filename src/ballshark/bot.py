"""Discord bot: posts a rich analytics embed after each finalized match.

This runs as a coroutine alongside the live ingest. The ingest pipeline calls
`enqueue()` whenever a MatchSummary is produced; the bot drains the queue,
computes match analytics (including DB-backed head-to-head splits), and
posts a structured stat embed to the configured channel.

Tone: competitive analytics, not narrative. No emoji vibes - just labeled
numbers and direct comparisons.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

import discord

from .analytics import MatchAnalytics, build_analytics
from .session import MatchSummary, SessionTotals

log = logging.getLogger("ballshark.bot")


ICON_WIN  = "🏆"
ICON_LOSS = "💀"


# --- Discord ANSI code-block coloring -------------------------------------
# Discord renders SGR color only inside ```ansi blocks, and only the 4-bit
# palette below (no 256-color / truecolor). Orange has no ANSI slot, so the
# orange team is rendered in yellow - the conventional RL-bot substitute.
_ESC = ""
A_GRAY, A_RED, A_GREEN, A_YELLOW, A_BLUE, A_MAGENTA, A_CYAN, A_WHITE = (
    30, 31, 32, 33, 34, 35, 36, 37)
A_BOLD = 1
_TEAM_FG = {0: A_BLUE, 1: A_YELLOW}  # blue team / orange team (yellow ~= orange)


def _sgr(text: str, *codes: int) -> str:
    """Wrap text in an ANSI SGR sequence followed by a reset."""
    return f"{_ESC}[{';'.join(str(c) for c in codes)}m{text}{_ESC}[0m"


# ---- StatfeedEvent highlights -> custom Discord emoji -----------------------
# Notable plays we surface as a highlights line. Box-score events (Goal/Assist/
# Shot/Save/Win) are left out - they're already in the scoreboard. Each maps to
# a custom-emoji name and the saved RL point-icon PNG it's uploaded from.
_ICON_DIR = Path(__file__).resolve().parent / "overlay" / "icons"
HIGHLIGHT_EVENTS: dict[str, tuple[str, str]] = {
    "EpicSave":      ("bs_epicsave",   "Epic_Save_points_icon.png"),
    "Savior":        ("bs_savior",     "Savior_points_icon.png"),
    "Demolish":      ("bs_demo",       "Demolition_points_icon.png"),
    "HatTrick":      ("bs_hattrick",   "Hat_Trick_points_icon.png"),
    "AerialGoal":    ("bs_aerialgoal", "Aerial_Goal_points_icon.png"),
    "BicycleHit":    ("bs_bikehit",    "Bicycle_Hit_points_icon.png"),
    "BicycleGoal":   ("bs_bikegoal",   "Bicycle_Goal_points_icon.png"),
    "LongGoal":      ("bs_longgoal",   "Long_Goal_points_icon.png"),
    "OvertimeGoal":  ("bs_otgoal",     "Overtime_Goal_points_icon.png"),
    "Playmaker":     ("bs_playmaker",  "Playmaker_points_icon.png"),
    "PoolShot":      ("bs_poolshot",   "Pool_Shot_points_icon.png"),
    "BackwardsGoal": ("bs_backgoal",   "Backwards_Goal_points_icon.png"),
    "TurtleGoal":    ("bs_turtlegoal", "Turtle_Goal_points_icon.png"),
    "SwishGoal":     ("bs_swishgoal",  "Swish_Goal_points_icon.png"),
    "Extermination": ("bs_extermin",   "Extermination_points_icon.png"),
}
# Plain-text fallback labels when a custom emoji isn't uploaded yet.
_HL_LABEL = {
    "EpicSave": "epic save", "Savior": "savior", "Demolish": "demo",
    "HatTrick": "hat trick", "AerialGoal": "aerial", "BicycleHit": "bike hit",
    "BicycleGoal": "bicycle", "LongGoal": "long goal", "OvertimeGoal": "OT goal",
    "Playmaker": "playmaker", "PoolShot": "pool shot", "BackwardsGoal": "backwards",
    "TurtleGoal": "turtle", "SwishGoal": "swish", "Extermination": "extermination",
}


def emoji_map_from_client(client) -> dict[str, str]:
    """Map highlight emoji-names -> their `<:name:id>` render token from whatever
    custom emoji the bot can see. Empty until the icons are uploaded."""
    out: dict[str, str] = {}
    try:
        for e in client.emojis:
            out[e.name] = f"<:{e.name}:{e.id}>"
    except Exception:
        pass
    return out


def _highlights_field(s, me, emoji_map: dict[str, str] | None) -> str | None:
    """Per-player highlights line for the viewer's team: notable plays as icons
    (custom emoji) or a text fallback. None if nobody on the team had one."""
    if not me:
        return None
    emoji_map = emoji_map or {}
    team = [p for p in s.players if p.team_num == me.team_num]
    team.sort(key=lambda p: (p.primary_id != me.primary_id, -p.score))
    lines: list[str] = []
    for p in team:
        evs = s.statfeed.get(p.primary_id) or {}
        bits: list[str] = []
        for ev, (emoji_name, _icon) in HIGHLIGHT_EVENTS.items():
            n = evs.get(ev, 0)
            if not n:
                continue
            tok = emoji_map.get(emoji_name) or f"`{_HL_LABEL.get(ev, ev)}`"
            bits.append(tok + (f"×{n}" if n > 1 else ""))
        if bits:
            tag = "▸ " if p.primary_id == me.primary_id else "   "
            lines.append(f"{tag}**{p.name}**  " + " ".join(bits))
    return "\n".join(lines) if lines else None


async def ensure_highlight_emojis(client, guild_id: int) -> dict:
    """Upload any missing highlight icons as custom guild emoji. Requires the bot
    to have Manage Emojis & Stickers. Idempotent (skips existing)."""
    guild = client.get_guild(guild_id) or await client.fetch_guild(guild_id)
    existing = {e.name for e in guild.emojis}
    res: dict[str, list] = {"created": [], "skipped": [], "errors": []}
    for ev, (name, fname) in HIGHLIGHT_EVENTS.items():
        if name in existing:
            res["skipped"].append(name); continue
        path = _ICON_DIR / fname
        if not path.is_file():
            res["errors"].append(f"{name}: missing {fname}"); continue
        try:
            e = await guild.create_custom_emoji(
                name=name, image=_emoji_png_bytes(path),
                reason="Ballshark highlight icons")
            res["created"].append(f"{name}={e.id}")
        except discord.Forbidden:
            res["errors"].append(f"{name}: Forbidden (grant Manage Emojis)")
            break
        except Exception as ex:
            res["errors"].append(f"{name}: {type(ex).__name__}: {ex}")
    return res


def _emoji_png_bytes(path: Path) -> bytes:
    """Icon bytes as a Discord-emoji-safe PNG (<256KB). Downscales if needed."""
    raw = path.read_bytes()
    if len(raw) <= 256_000:
        return raw
    from io import BytesIO
    from PIL import Image
    im = Image.open(path).convert("RGBA")
    im.thumbnail((128, 128))
    buf = BytesIO(); im.save(buf, format="PNG")
    return buf.getvalue()


def _arena_pretty(arena: str) -> str:
    """Map RL arena codes to friendly names. Delegates to the canonical map
    in server.py so the dashboard and bot stay in sync."""
    from .server import _arena_nice
    return _arena_nice(arena)


# Scoreboard columns. Kept tight (~33 chars) so the block survives Discord's
# narrow mobile code-block width without wrapping. Names are truncated; the
# MVP badge sits past the last aligned column so it never shifts the grid.
_SB_NAME_W = 13
_SB_HEADER = (f"  {'PLAYER':<{_SB_NAME_W}}{'PTS':>6} {'G':>2} {'A':>2} "
              f"{'SV':>2} {'SH':>2} {'D':>2}")


def _mvp_player_ids(s) -> set[int]:
    """Resolve which player *objects* should show the MVP badge.

    Real players key cleanly on primary_id. Bots all share the sentinel id
    'Unknown|0|0', so flagging by primary_id would badge every bot at once -
    instead, when a bot is MVP we award it to the single top scorer on the
    winning team, matching RL's actual MVP rule."""
    flagged = s.is_mvp or {}
    if not flagged:
        return set()
    out = {id(p) for p in s.players
           if not p.is_bot and flagged.get(p.primary_id)}
    if any(p.is_bot and flagged.get(p.primary_id) for p in s.players):
        winning_bots = [p for p in s.players
                        if p.is_bot and p.team_num == s.winner_team_num]
        if winning_bots:
            out.add(id(max(winning_bots, key=lambda p: p.score)))
    return out


def _team_section(s, team_num: int, is_winner: bool, me,
                  mvp_ids: set[int]) -> list[str]:
    """One team's colored scoreboard lines: header row + one row per player,
    sorted by score. Team identity is carried by color; the winner is tagged
    and the viewer's own row is bolded and marked with a caret."""
    fg = _TEAM_FG[team_num]
    players = sorted((p for p in s.players if p.team_num == team_num),
                     key=lambda p: p.score, reverse=True)
    name = (s.team_name(team_num) or ("Blue" if team_num == 0 else "Orange")).upper()
    head = _sgr(f"{name}  {s.team_score(team_num)}", A_BOLD, fg)
    if is_winner:
        head += "  " + _sgr("WIN", A_BOLD, A_GREEN)
    # Column header row (PLAYER / PTS / G / A ...) in white.
    lines = [head, _sgr(_SB_HEADER, A_WHITE)]
    for p in players:
        is_you = bool(me and p.primary_id == me.primary_id
                      and p.name == me.name and p.team_num == me.team_num)
        nm = f"{p.name}{' (BOT)' if p.is_bot else ''}"[:_SB_NAME_W]
        prefix = "▸ " if is_you else "  "
        # Names in the team's color (blue team blue, orange team orange/yellow),
        # numbers in white; the viewer's own row is bolded.
        name_codes = (A_BOLD, fg) if is_you else (fg,)
        num_codes  = (A_BOLD, A_WHITE) if is_you else (A_WHITE,)
        nums = (f"{p.score:>6} {p.goals:>2} {p.assists:>2} "
                f"{p.saves:>2} {p.shots:>2} {p.demos:>2}")
        seg = (_sgr(f"{prefix}{nm:<{_SB_NAME_W}}", *name_codes)
               + _sgr(nums, *num_codes))
        if id(p) in mvp_ids:
            # MVP marker in the winning team's own color (MVP is always on the
            # winner, so fg here is that color), bold so it still reads.
            seg += "  " + _sgr("MVP", A_BOLD, fg)
        lines.append(seg)
    return lines


def _adv_stats_block(s, me) -> str | None:
    """Your team's current-match detail, colored in your team color: a one-line
    team summary (post hits, touches, possession, avg hit speed) plus per-player
    movement & boost rows. Only our-team data is shown - opponent movement isn't
    in the feed and nothing else is confidently derivable per team."""
    if not me:
        return None
    fg = _TEAM_FG.get(me.team_num, A_WHITE)
    lines: list[str] = []

    # Team summary from the (reliable) ball-touch stream + per-team post hits.
    ours = [t for t in s.ball_touches if t.team_num == me.team_num]
    posts = s.crossbar_by_team.get(me.team_num, 0)
    if ours or posts:
        bits = [f"posts {posts}"]
        if ours:
            poss = (len(ours) / len(s.ball_touches)) if s.ball_touches else 0.0
            avg_hit = sum(t.post_speed for t in ours) / len(ours)
            bits.append(f"touches {len(ours)} ({poss * 100:.0f}%)")
            bits.append(f"avg hit {avg_hit:.0f}")
        lines.append(_sgr(" · ".join(bits), A_BOLD, fg))

    # Per-player movement & boost (spectator-only -> our team).
    team = sorted((p for p in s.players
                   if p.team_num == me.team_num and p.ticks_total >= 200),
                  key=lambda p: p.score, reverse=True)
    if team:
        lines.append(_sgr(f"{'PLAYER':<{_SB_NAME_W}}{'AIR':>5}{'WALL':>6}{'SUP':>5}"
                          f"{'SPD':>5}{'BOOST':>7}", A_WHITE))
        for p in team:
            nm = f"{p.name}{' (BOT)' if p.is_bot else ''}"[:_SB_NAME_W]
            nums = (f"{p.pct_in_air * 100:>4.0f}%"
                    f"{p.pct_on_wall * 100:>5.0f}%{p.pct_supersonic * 100:>4.0f}%"
                    f"{p.avg_speed:>5.0f}{p.boost_used:>7.0f}")
            lines.append(_sgr(f"{nm:<{_SB_NAME_W}}", fg) + _sgr(nums, A_WHITE))

    if not lines:
        return None
    return "```ansi\n" + "\n".join(lines) + "\n```"


def _last_n_stats(store, primary_id: str | None, n: int = 10) -> dict | None:
    """Aggregate the player's last N matches from the DB.

    This replaces the in-memory "session" totals on the embed. A session number
    drifts with how long the app has been running; "last 10" means the same
    thing every time you read it — your current form. The just-finished match is
    already persisted by the time the bot builds the embed, so it is included.
    """
    if not store or not primary_id:
        return None
    try:
        with store._conn() as con:
            rows = con.execute("""
                SELECT mps.team_num, m.winner_team_num,
                       mps.goals, mps.assists, mps.saves, mps.shots, mps.demos
                FROM match_player_stats mps
                JOIN matches m ON m.id = mps.match_id
                WHERE mps.primary_id = ?
                ORDER BY m.started_at DESC
                LIMIT ?
            """, (primary_id, n)).fetchall()
    except Exception:
        return None
    if not rows:
        return None

    def won(r) -> bool:
        return r["team_num"] == r["winner_team_num"]

    wins = sum(1 for r in rows if won(r))  # rows are newest-first
    # Streak: walk from the newest match while the result matches.
    newest_won = won(rows[0])
    streak = 0
    for r in rows:
        if won(r) == newest_won:
            streak += 1
        else:
            break
    return {
        "count": len(rows),
        "wins": wins,
        "losses": len(rows) - wins,
        "win_rate": wins / len(rows),
        "streak_label": f"{'W' if newest_won else 'L'}{streak}",
        "goals":   sum(r["goals"]   for r in rows),
        "assists": sum(r["assists"] for r in rows),
        "saves":   sum(r["saves"]   for r in rows),
        "shots":   sum(r["shots"]   for r in rows),
        "demos":   sum(r["demos"]   for r in rows),
        # Oldest -> newest so the dots read left-to-right like a timeline.
        "form": "".join("🟢" if won(r) else "🔴" for r in reversed(rows)),
    }


def build_match_embed(
    s: MatchSummary,
    totals: SessionTotals,
    self_primary_id: str | None = None,
    self_name: str | None = None,
    store=None,
    friends: list[str] | None = None,
    public_url: str | None = None,
    emoji_map: dict[str, str] | None = None,
) -> discord.Embed:
    me = s.me(self_primary_id, self_name)
    won = bool(me and me.team_num == s.winner_team_num)
    is_mvp = bool(me and s.is_mvp.get(me.primary_id))

    if won:
        color = discord.Color.from_rgb(74, 222, 128)
        title_icon = ICON_WIN
        title_text = "Win"
    elif me:
        color = discord.Color.from_rgb(248, 113, 113)
        title_icon = ICON_LOSS
        title_text = "Loss"
    else:
        color = discord.Color.greyple()
        title_icon = ""
        title_text = "Match"

    if is_mvp:
        title_text += " · MVP"

    arena_label = _arena_pretty(s.arena)
    # Game length from the in-game clock (regulation + overtime); fall back to
    # the wall/tick duration only when no clock was captured. Normal games show
    # the full match length; OT games add the extra time and an "OT" tag.
    game_secs = (s.regulation_seconds + s.overtime_seconds) or s.duration_seconds
    duration_str = ""
    if game_secs:
        mm = int(game_secs // 60); ss = int(game_secs % 60)
        duration_str = f"{mm}:{ss:02d}" + (" OT" if s.is_overtime else "")

    # ----- scoreboard: one colored ANSI block, winner on top -----
    # Color carries team identity (blue / yellow), a green WIN tag marks the
    # winner, and the viewer's row is bolded. The context line groups the
    # match facts (type, arena, length) in one place.
    context_bits = [s.match_type, arena_label]
    if duration_str:
        context_bits.append(duration_str)
    order = ([s.winner_team_num, 1 - s.winner_team_num]
             if s.winner_team_num in (0, 1) else [0, 1])
    mvp_ids = _mvp_player_ids(s)
    # White (not gray) so it's readable on Discord's dark code-block background.
    sb_lines = [_sgr(" · ".join(context_bits), A_WHITE)]
    for team_num in order:
        sb_lines.append("")
        sb_lines += _team_section(s, team_num, team_num == s.winner_team_num,
                                  me, mvp_ids)
    description = "```ansi\n" + "\n".join(sb_lines) + "\n```"

    # If we have a public URL configured, make the title a clickable link to
    # the match-detail page.
    embed_url = None
    if public_url and s.match_id:
        embed_url = f"{public_url.rstrip('/')}/match/{s.match_id}"

    embed = discord.Embed(
        title=f"{title_icon} {title_text}".strip(),
        url=embed_url,
        color=color, description=description,
    )
    # Stamp the embed with when the match actually ended, not when we post it
    # (posting can lag, or replay a saved capture). Discord shows it in the
    # footer in each viewer's local timezone.
    match_ts = s.ended_at or s.started_at
    if match_ts:
        embed.timestamp = datetime.fromtimestamp(match_ts, tz=timezone.utc)

    # ----- your team's movement & boost (separate, secondary section) -----
    adv = _adv_stats_block(s, me)
    if adv:
        embed.add_field(name="Your team · this match", value=adv, inline=False)

    # ----- highlights: notable plays as icons (your team) -----
    hl = _highlights_field(s, me, emoji_map)
    if hl:
        embed.add_field(name="Highlights", value=hl, inline=False)

    # ----- last 10 matches (rolling form, DB-backed) -----
    # Falls back to the in-memory session totals only when there's no DB or we
    # can't identify the player (e.g. post-test against raw fixtures).
    me_pid = me.primary_id if me else self_primary_id
    recent = _last_n_stats(store, me_pid, n=10)
    def _form_box(wins, losses, win_rate, streak, goals, assists, saves,
                  shots, demos, form=None):
        st_col = A_GREEN if streak.startswith("W") else A_RED
        box = "```ansi\n" + "\n".join([
            _sgr(f"{wins}-{losses}   {win_rate * 100:.0f}% WR   streak ", A_WHITE)
            + _sgr(streak, A_BOLD, st_col),
            _sgr(f"G {goals}  A {assists}  Sv {saves}  Sh {shots}  D {demos}",
                 A_WHITE),
        ]) + "\n```"
        if form:
            # Form dots OUTSIDE the box so Discord renders them as full-color
            # emoji (inside a code block they shrink to monochrome), spaced out.
            box += "\n" + " ".join(form)
        return box

    if recent:
        field_name = f"Last {recent['count']}" if recent["count"] < 10 else "Last 10"
        value = _form_box(
            recent["wins"], recent["losses"], recent["win_rate"],
            recent["streak_label"], recent["goals"], recent["assists"],
            recent["saves"], recent["shots"], recent["demos"], recent["form"],
        )
    else:
        field_name = "Session"
        value = _form_box(
            totals.wins, totals.losses, totals.win_rate, totals.streak_label,
            totals.goals, totals.assists, totals.saves, totals.shots,
            totals.demos,
        )
    embed.add_field(name=field_name, value=value, inline=False)

    # ----- footer: just the match end timestamp -----
    # Crossbar hits moved up into the match context line (top), so every
    # "this match" stat sits together above the rolling "Last 10" box and
    # nothing this-match dangles under it. The timestamp set above still
    # renders here on its own.

    return embed


class MatchPoster:
    """Owns the discord.Client lifecycle and a posting queue."""

    def __init__(self, token: str, channel_id: int,
                 self_primary_id: str | None = None, self_name: str | None = None,
                 store=None, friends: list[str] | None = None,
                 public_url: str | None = None) -> None:
        self.token = token
        self.channel_id = channel_id
        self.self_primary_id = self_primary_id
        self.self_name = self_name
        self.store = store
        self.friends = friends or []
        self.public_url = (public_url or "").rstrip("/")
        self.queue: asyncio.Queue[tuple[MatchSummary, SessionTotals]] = asyncio.Queue()

        intents = discord.Intents.default()
        # We don't read messages, only post. No privileged intents required.
        self.client = discord.Client(intents=intents)

        @self.client.event
        async def on_ready() -> None:
            log.info("logged in as %s", self.client.user)
            n_guilds = len(self.client.guilds)
            print(f"[bot] logged in as {self.client.user} - {n_guilds} guild(s)")
            if n_guilds == 0:
                app_id = self.client.user.id if self.client.user else None
                perms = 19456  # View Channel + Send Messages + Embed Links
                if app_id:
                    invite = (
                        f"https://discord.com/api/oauth2/authorize?"
                        f"client_id={app_id}&permissions={perms}&scope=bot"
                    )
                    print("[bot] WARNING: bot is not in any server.")
                    print(f"[bot] Open this URL in your browser to invite it to your server:")
                    print(f"[bot]   {invite}")
            self.client.loop.create_task(self._drain())

        @self.client.event
        async def on_error(event, *args, **kwargs) -> None:
            log.exception("discord error in %s", event)

    async def _drain(self) -> None:
        channel = None
        # Discord may finish chunking guilds shortly after on_ready fires.
        # Retry get_channel a few times before falling back to a network fetch.
        for _ in range(5):
            channel = self.client.get_channel(self.channel_id)
            if channel is not None:
                break
            await asyncio.sleep(0.5)

        if channel is None:
            try:
                channel = await self.client.fetch_channel(self.channel_id)
            except discord.Forbidden:
                print(
                    f"[bot] ERROR: bot cannot access channel {self.channel_id} (Missing Access).\n"
                    f"[bot]   - is the bot in the server containing that channel?\n"
                    f"[bot]   - does the bot role have View Channel + Send Messages + Embed Links?\n"
                    f"[bot] ingest and overlay server keep running regardless."
                )
                return
            except discord.NotFound:
                print(f"[bot] ERROR: channel {self.channel_id} not found. Check DISCORD_CHANNEL_ID in .env.")
                return
            except Exception as e:
                print(f"[bot] ERROR resolving channel: {e}")
                return

        if channel is None:
            print(f"[bot] ERROR: channel {self.channel_id} could not be resolved.")
            return

        print(f"[bot] connected; ready to post to #{getattr(channel, 'name', self.channel_id)}")

        # Best-effort: make sure our highlight icons exist as custom emoji.
        gid = getattr(getattr(channel, "guild", None), "id", None)
        if gid:
            try:
                r = await ensure_highlight_emojis(self.client, gid)
                if r["created"]:
                    print(f"[bot] uploaded {len(r['created'])} highlight emoji")
                if any("Forbidden" in e for e in r["errors"]):
                    print("[bot] grant Manage Emojis to show highlight icons")
            except Exception:
                log.exception("highlight-emoji ensure failed")

        while True:
            summary, totals = await self.queue.get()
            try:
                embed = build_match_embed(
                    summary, totals,
                    self_primary_id=self.self_primary_id,
                    self_name=self.self_name,
                    store=self.store,
                    friends=self.friends,
                    public_url=self.public_url,
                    emoji_map=emoji_map_from_client(self.client),
                )
                await channel.send(embed=embed)
            except discord.Forbidden:
                print(f"[bot] ERROR posting: bot lost access to channel {self.channel_id}")
            except Exception:
                log.exception("failed to post match summary")
            finally:
                self.queue.task_done()

    def enqueue(self, summary: MatchSummary, totals: SessionTotals) -> None:
        """Thread-safe enqueue. Use from non-async callers."""
        # Posting from non-loop threads: schedule on the loop.
        try:
            loop = self.client.loop
        except Exception:
            loop = None
        if loop and loop.is_running():
            loop.call_soon_threadsafe(self.queue.put_nowait, (summary, totals))
        else:
            self.queue.put_nowait((summary, totals))

    async def run(self) -> None:
        try:
            await self.client.start(self.token)
        except discord.LoginFailure:
            print("[bot] ERROR: Discord login failed. The bot token is invalid - regenerate it at")
            print("[bot]        https://discord.com/developers/applications -> your bot -> Reset Token")
        except Exception as e:
            print(f"[bot] ERROR: bot crashed: {e}")


class PostResult:
    """Mutable result holder for post_one - the on_ready handler writes here."""
    def __init__(self) -> None:
        self.success: bool = False
        self.error: str = ""
        self.message_id: int | None = None
        self.guilds: int = 0


async def post_one(token: str, channel_id: int, summary: MatchSummary, totals: SessionTotals,
                   self_name: str | None = None, self_primary_id: str | None = None,
                   store=None, friends: list[str] | None = None) -> PostResult:
    """Standalone helper: log in, post one embed, log out. Returns a
    PostResult so callers can detect failure (the previous version swallowed
    exceptions silently)."""
    result = PostResult()
    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        try:
            result.guilds = len(client.guilds)
            if result.guilds == 0:
                app_id = client.user.id if client.user else None
                perms = 19456
                invite = (
                    f"https://discord.com/api/oauth2/authorize?"
                    f"client_id={app_id}&permissions={perms}&scope=bot"
                )
                result.error = f"bot is not in any server. Invite it: {invite}"
                return
            channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
            embed = build_match_embed(
                summary, totals,
                self_primary_id=self_primary_id, self_name=self_name, store=store,
                friends=friends, emoji_map=emoji_map_from_client(client),
            )
            msg = await channel.send(embed=embed)
            result.success = True
            result.message_id = msg.id
        except discord.Forbidden as e:
            result.error = f"Missing Access on channel {channel_id} - bot needs View Channel + Send Messages + Embed Links"
        except discord.NotFound:
            result.error = f"channel {channel_id} not found"
        except Exception as e:
            result.error = f"{type(e).__name__}: {e}"
        finally:
            await client.close()

    try:
        await client.start(token)
    except discord.LoginFailure:
        result.error = "Discord login failed - token invalid"
    except Exception as e:
        if not result.error:
            result.error = f"{type(e).__name__}: {e}"
    return result
