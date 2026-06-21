"""Centralized config loader.

Reads from the project's `.env` if present, environment otherwise. All
optional - the app degrades gracefully (e.g. Discord bot just disabled if
no token).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import socket

try:
    from dotenv import load_dotenv  # type: ignore
except ImportError:  # pragma: no cover
    load_dotenv = None


def local_ip() -> str:
    """Best-effort LAN IP detection. Uses the OS routing table by opening
    a UDP socket toward a public IP (no packets are actually sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return "127.0.0.1"


def _load_env() -> None:
    if load_dotenv is None:
        return
    # Find .env walking up from cwd; fall back to project root next to pyproject.
    candidates = [Path.cwd() / ".env"]
    here = Path(__file__).resolve().parent
    for parent in (here, *here.parents):
        candidates.append(parent / ".env")
    for c in candidates:
        if c.is_file():
            load_dotenv(c, override=False)
            return


_load_env()


def _env(*names: str, default: str | None = None) -> str | None:
    """First non-empty env var among `names`. New CHUMSTATS_* names are passed
    first, legacy BALLSHARK_* / CARBALL_* names as fallback so pre-rename `.env`
    files and already-deployed installs keep working."""
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


def _migrate_legacy_home_dir() -> None:
    """One-time rename of the dev data dir to ~/.chumstats, walking the rename
    history (~/.carball -> ~/.ballshark -> ~/.chumstats). No-op if the new dir
    already exists or no legacy dir does. (The tray's friend bundle migrates its
    own %LOCALAPPDATA% dir in tray_config.app_dir.)"""
    new = Path.home() / ".chumstats"
    for legacy in (".ballshark", ".carball"):
        old = Path.home() / legacy
        try:
            if old.is_dir() and not new.exists():
                old.rename(new)
                break
        except OSError:
            pass
    # Rename the DB file (+ SQLite WAL/SHM sidecars) inside the dir.
    for old_stem in ("ballshark.db", "carball.db"):
        for suffix in ("", "-wal", "-shm", "-journal"):
            o = new / (old_stem + suffix)
            n = new / ("chumstats.db" + suffix)
            try:
                if o.exists() and not n.exists():
                    o.rename(n)
            except OSError:
                pass


@dataclass
class Settings:
    db_path: str = str(Path.home() / ".chumstats" / "chumstats.db")
    rl_host: str = "127.0.0.1"
    rl_port: int = 49123

    discord_token: str | None = None
    discord_channel_id: int | None = None
    discord_guild_id: int | None = None

    player_name: str | None = None
    player_primary_id: str | None = None
    friends: list[str] = None  # type: ignore

    # Bind to all interfaces by default so phones / other devices on the
    # same LAN can hit the dashboard + overlay. Loopback-only behaviour can
    # be restored by setting CHUMSTATS_SERVER_HOST=127.0.0.1.
    server_host: str = "0.0.0.0"
    server_port: int = 5050
    # Public URL used when generating links to share (e.g., Discord "View
    # match" link). Defaults to http://chumstats.local:<port> — add a hosts
    # file entry mapping chumstats.local -> 127.0.0.1 to make the alias
    # resolve. Override with CHUMSTATS_PUBLIC_URL when hosting on a real
    # domain (e.g. https://chumstats.com).
    public_url: str = "http://chumstats.local:5050"

    # Multi-user sync: when both remote_url and api_key are set, finalized
    # matches are POSTed to the central server. Locally this is harmless;
    # the local DB is always written first.
    remote_url: str | None = None
    api_key: str | None = None

    # Sync fidelity. False (default, "summary"): push match summaries + lifecycle
    # events + touches/goals, but NOT the 30 Hz UpdateState tick firehose — keeps
    # the central DB small. True ("full", BALLSHARK_SYNC_FULL_RAW=1): push the
    # complete raw stream including ticks, so the central server is a full raw
    # archive you can re-derive any future stat from (bigger DB + uploads).
    sync_full_raw: bool = False

    # How long to keep the 30 Hz tick firehose (UpdateState) in raw_events after
    # a match is aggregated, before prune drops it. This is the window in which
    # tick-derived stats can still be re-derived from raw if a bug is found.
    tick_keep_days: int = 14

    @classmethod
    def from_env(cls) -> "Settings":
        db_override = _env("CHUMSTATS_DB", "BALLSHARK_DB", "CARBALL_DB")
        if not db_override:
            # Only migrate the default ~/.chumstats dir when we'll actually use
            # it. Never move data out from under an explicit DB path (e.g. the
            # central server pointing at a fixed file).
            _migrate_legacy_home_dir()
        port = int(_env("CHUMSTATS_SERVER_PORT", "BALLSHARK_SERVER_PORT", "CARBALL_SERVER_PORT", default="5050"))
        return cls(
            db_path=db_override or str(Path.home() / ".chumstats" / "chumstats.db"),
            rl_host=os.environ.get("RL_HOST", "127.0.0.1"),
            rl_port=int(os.environ.get("RL_PORT", "49123")),
            discord_token=os.environ.get("DISCORD_TOKEN") or None,
            discord_channel_id=int(os.environ["DISCORD_CHANNEL_ID"]) if os.environ.get("DISCORD_CHANNEL_ID") else None,
            discord_guild_id=int(os.environ["DISCORD_GUILD_ID"]) if os.environ.get("DISCORD_GUILD_ID") else None,
            player_name=os.environ.get("RL_PLAYER_NAME") or None,
            player_primary_id=os.environ.get("RL_PLAYER_PRIMARY_ID") or None,
            friends=[s.strip() for s in (os.environ.get("RL_FRIENDS") or "").split(",") if s.strip()],
            server_host=_env("CHUMSTATS_SERVER_HOST", "BALLSHARK_SERVER_HOST", "CARBALL_SERVER_HOST", default="0.0.0.0"),
            server_port=port,
            public_url=(_env("CHUMSTATS_PUBLIC_URL", "BALLSHARK_PUBLIC_URL", "CARBALL_PUBLIC_URL")
                        or f"http://chumstats.local:{port}").rstrip("/"),
            remote_url=_env("CHUMSTATS_REMOTE_URL", "BALLSHARK_REMOTE_URL", "CARBALL_REMOTE_URL") or None,
            api_key=_env("CHUMSTATS_API_KEY", "BALLSHARK_API_KEY", "CARBALL_API_KEY") or None,
            sync_full_raw=(_env("CHUMSTATS_SYNC_FULL_RAW", "BALLSHARK_SYNC_FULL_RAW", "CARBALL_SYNC_FULL_RAW", default="")
                           or "").strip().lower() in ("1", "true", "yes", "full"),
            tick_keep_days=int(_env("CHUMSTATS_TICK_KEEP_DAYS", "BALLSHARK_TICK_KEEP_DAYS", "CARBALL_TICK_KEEP_DAYS", default="14")),
        )
