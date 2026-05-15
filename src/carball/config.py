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


@dataclass
class Settings:
    db_path: str = str(Path.home() / ".carball" / "carball.db")
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
    # be restored by setting CARBALL_SERVER_HOST=127.0.0.1.
    server_host: str = "0.0.0.0"
    server_port: int = 5050

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=os.environ.get("CARBALL_DB", str(Path.home() / ".carball" / "carball.db")),
            rl_host=os.environ.get("RL_HOST", "127.0.0.1"),
            rl_port=int(os.environ.get("RL_PORT", "49123")),
            discord_token=os.environ.get("DISCORD_TOKEN") or None,
            discord_channel_id=int(os.environ["DISCORD_CHANNEL_ID"]) if os.environ.get("DISCORD_CHANNEL_ID") else None,
            discord_guild_id=int(os.environ["DISCORD_GUILD_ID"]) if os.environ.get("DISCORD_GUILD_ID") else None,
            player_name=os.environ.get("RL_PLAYER_NAME") or None,
            player_primary_id=os.environ.get("RL_PLAYER_PRIMARY_ID") or None,
            friends=[s.strip() for s in (os.environ.get("RL_FRIENDS") or "").split(",") if s.strip()],
            server_host=os.environ.get("CARBALL_SERVER_HOST", "0.0.0.0"),
            server_port=int(os.environ.get("CARBALL_SERVER_PORT", "5050")),
        )
