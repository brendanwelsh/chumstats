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

import discord

from .analytics import MatchAnalytics, build_analytics
from .session import MatchSummary, SessionTotals

log = logging.getLogger("carball.bot")


def _team_emoji(team_num: int) -> str:
    return "🔵" if team_num == 0 else "🟠"


# Minimal icon set. Only kept emoji that *carry information* (team color,
# MVP, win/loss, form dots). Everything else uses plain labels.
ICON_MVP     = "⭐"
ICON_WIN     = "🏆"
ICON_LOSS    = "💀"


def _arena_pretty(arena: str) -> str:
    """Map RL arena codes to friendly names. Delegates to the canonical map
    in server.py so the dashboard and bot stay in sync."""
    from .server import _arena_nice
    return _arena_nice(arena)


def _team_block(team_num: int, team_name: str, team_score: int,
                players: list, me, is_mvp_map: dict[str, bool]) -> str:
    """One team's roster as a fixed-width code block. Sorted by score desc.

    Layout is tuned to fit within Discord's code-block width (~52 chars on
    desktop, ~38 on mobile). We use shortened-but-readable column labels
    and a single-line advanced row for teammates with spectator data.

    Row prefix:
       > = you  (so you can spot yourself at a glance)
       (space) everyone else

    Name suffix: (MVP), (BOT)
    """
    sorted_players = sorted(players, key=lambda p: p.score, reverse=True)

    # Column widths chosen so a typical row is ~46 chars. Targets:
    #   prefix(2) + name(15) + space + score(5) + sp + G(3) + sp + A(3)
    #   + sp + Sv(3) + sp + Sh(3) + sp + D(2) = 45
    name_w = 15
    rows: list[str] = []
    rows.append(
        f"  {'PLAYER':<{name_w}} {'Score':>5} {'G':>3} {'A':>3} "
        f"{'Sv':>3} {'Sh':>3} {'D':>2}"
    )

    for p in sorted_players:
        is_me = bool(me and p.team_num == me.team_num and p.name == me.name
                     and p.primary_id == me.primary_id)
        is_teammate = bool(me and p.team_num == me.team_num)
        mvp = is_mvp_map.get(p.primary_id, False) and not p.is_bot

        prefix = "> " if is_me else "  "

        # Compact (MVP)/(BOT) markers - we tag MVPs with an asterisk to save
        # 4 chars in the name field; (BOT) still spelled out so opponents are
        # clearly identifiable.
        suffix_bits: list[str] = []
        if mvp:      suffix_bits.append("*")
        if p.is_bot: suffix_bits.append("(BOT)")
        suffix = "".join(suffix_bits) if suffix_bits else ""

        name_with_suffix = f"{p.name}{suffix}"[:name_w]
        rows.append(
            f"{prefix}{name_with_suffix:<{name_w}} "
            f"{p.score:>5} {p.goals:>3} {p.assists:>3} "
            f"{p.saves:>3} {p.shots:>3} {p.demos:>2}"
        )
        # Indented adv line only for teammates with spectator-visible data.
        # Compact form, single-space separators - room for 5-digit boost.
        if is_teammate and p.ticks_total >= 200:
            rows.append(
                f"   air {p.pct_in_air * 100:.0f}% "
                f"wall {p.pct_on_wall * 100:.0f}% "
                f"sup {p.pct_supersonic * 100:.0f}% "
                f"spd {p.avg_speed:.0f} "
                f"boost {p.boost_used:.0f}"
            )

    # MVP star legend - only if we actually rendered a *
    legend = "  * = MVP" if any(is_mvp_map.get(p.primary_id) and not p.is_bot
                                  for p in sorted_players) else ""

    icon = _team_emoji(team_num)
    title = f"{icon}  **{team_name}**  —  **{team_score}**"
    body_lines = rows + ([legend] if legend else [])
    body = "```\n" + "\n".join(body_lines) + "\n```"
    return title + "\n" + body


def _session_form_dots(store, primary_id: str | None, n: int = 10) -> str:
    """Last N match results as colored dots from the DB."""
    if not store or not primary_id:
        return ""
    try:
        with store._conn() as con:
            rows = con.execute("""
                SELECT m.started_at, mps.team_num, m.winner_team_num
                FROM match_player_stats mps
                JOIN matches m ON m.id = mps.match_id
                WHERE mps.primary_id = ?
                ORDER BY m.started_at DESC
                LIMIT ?
            """, (primary_id, n)).fetchall()
    except Exception:
        return ""
    return "".join(
        "🟢" if r["team_num"] == r["winner_team_num"] else "🔴"
        for r in reversed(rows)
    )


def build_match_embed(
    s: MatchSummary,
    totals: SessionTotals,
    self_primary_id: str | None = None,
    self_name: str | None = None,
    store=None,
    friends: list[str] | None = None,
    public_url: str | None = None,
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
        title_text += f" · MVP"

    arena_label = _arena_pretty(s.arena)
    mode_tag = s.match_type
    duration_str = ""
    if s.duration_seconds:
        mm = int(s.duration_seconds // 60); ss = int(s.duration_seconds % 60)
        duration_str = f"{mm}:{ss:02d}"

    # Big result line. Team color emoji is the only decoration here - it's
    # functional (tells you which team is which).
    description = (
        f"{_team_emoji(0)} **{s.team0_name}  {s.team0_score}**"
        f"  —  "
        f"**{s.team1_score}  {s.team1_name}** {_team_emoji(1)}\n\n"
        f"`{arena_label}`  ·  `{mode_tag}`  ·  `{duration_str or '?'}`"
    )
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

    # ----- main scoreboard (single section, advanced stats inline) -----
    blue = [p for p in s.players if p.team_num == 0]
    orange = [p for p in s.players if p.team_num == 1]
    if blue:
        embed.add_field(
            name="​",
            value=_team_block(0, s.team0_name, s.team0_score, blue, me, s.is_mvp),
            inline=False,
        )
    if orange:
        embed.add_field(
            name="​",
            value=_team_block(1, s.team1_name, s.team1_score, orange, me, s.is_mvp),
            inline=False,
        )

    # ----- session -----
    me_pid = me.primary_id if me else self_primary_id
    form = _session_form_dots(store, me_pid, n=10)
    session_lines = [
        f"**{totals.wins}-{totals.losses}**  ·  {totals.win_rate * 100:.0f}% win rate  ·  streak **{totals.streak_label}**",
        f"Goals **{totals.goals}**  ·  Assists **{totals.assists}**  ·  Saves **{totals.saves}**  "
        f"·  Shots **{totals.shots}**  ·  Demos **{totals.demos}**",
    ]
    if form:
        session_lines.append(f"recent {form}")
    embed.add_field(
        name="Session",
        value="\n".join(session_lines),
        inline=False,
    )

    # ----- footer: misc match notes -----
    foot_parts: list[str] = []
    if s.crossbar_hits:
        foot_parts.append(f"{s.crossbar_hits} crossbar")
    if s.ball_touches:
        foot_parts.append(f"{len(s.ball_touches)} touches logged")
    if foot_parts:
        embed.set_footer(text="  ·  ".join(foot_parts))

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
                friends=friends,
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
