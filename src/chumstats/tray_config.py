"""User-scoped JSON config for the tray app.

The tray needs settings that survive across PyInstaller re-installs and
don't live next to the executable (which may be in Program Files / read-only).
We put everything under ``%LOCALAPPDATA%\\chumstats\\``:

    config.json   — wizard-collected: server URL, API key, name, primary_id
    chumstats.db    — local match database
    logs/         — tray + server log files

On macOS/Linux this falls back to ~/.local/share/chumstats/ (not currently
shipped, but keeps the module portable for dev use).
"""

from __future__ import annotations

import json
import os
import platform
from dataclasses import asdict, dataclass, field
from pathlib import Path


def app_dir() -> Path:
    """Per-user writable directory for chumstats state."""
    if platform.system() == "Windows":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        root = Path(base)
    else:
        root = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    p = root / "chumstats"
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_path() -> Path:
    return app_dir() / "config.json"


def db_path() -> Path:
    """Friend bundle default: %LOCALAPPDATA%\\chumstats\\chumstats.db. Dev
    override: set CHUMSTATS_DB to your checkout's DB and the tray respects it."""
    override = os.environ.get("CHUMSTATS_DB")
    if override:
        return Path(override)
    return app_dir() / "chumstats.db"


@dataclass
class TrayConfig:
    """User-set, persisted across launches. Empty = wizard not yet run."""
    rl_player_name: str = ""
    rl_player_primary_id: str = ""      # auto-filled after first match if blank
    remote_url: str = ""                # e.g. https://stats.your-domain.com
    api_key: str = ""
    rl_setup_done: bool = False         # have we ever run chumstats setup?
    # RL updates reset DefaultStatsAPI.ini's PacketSendRate to 0 (June 2026:
    # five days of matches silently lost). When True, the tray watchdog
    # re-enables it automatically as soon as that's detected.
    auto_fix_stats_api: bool = True

    @property
    def is_configured(self) -> bool:
        """Whether the wizard is needed. We only require a player name to be
        useful — sync (remote_url + api_key) is opt-in. Friends get all three
        from the wizard; devs running locally just need RL_PLAYER_NAME in env."""
        return bool(self.rl_player_name or self.rl_player_primary_id)

    @property
    def sync_enabled(self) -> bool:
        return bool(self.remote_url and self.api_key)


def load() -> TrayConfig:
    """Load saved config, falling back to environment variables for any field
    not in the JSON. Dev convenience: if you already have a working `.env` from
    pre-tray-wizard days, the wizard won't pop up because the env vars satisfy
    `is_configured`."""
    p = config_path()
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    else:
        data = {}
    known = {f for f in TrayConfig.__dataclass_fields__}
    cfg = TrayConfig(**{k: v for k, v in data.items() if k in known})

    # Env fallbacks (only fill blanks; don't override explicit JSON values).
    if not cfg.rl_player_name:
        cfg.rl_player_name = os.environ.get("RL_PLAYER_NAME") or ""
    if not cfg.rl_player_primary_id:
        cfg.rl_player_primary_id = os.environ.get("RL_PLAYER_PRIMARY_ID") or ""
    if not cfg.remote_url:
        cfg.remote_url = os.environ.get("CHUMSTATS_REMOTE_URL") or ""
    if not cfg.api_key:
        cfg.api_key = os.environ.get("CHUMSTATS_API_KEY") or ""
    return cfg


def save(cfg: TrayConfig) -> None:
    p = config_path()
    p.write_text(json.dumps(asdict(cfg), indent=2), encoding="utf-8")
