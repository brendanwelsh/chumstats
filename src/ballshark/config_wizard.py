"""Detect Rocket League installs and toggle the Stats API config.

Why this exists: people don't want to hand-edit `Program Files\\...\\DefaultStatsAPI.ini`
to enable the API. Same operation, behind a button (CLI or eventually GUI).

Safety:
  - Reads existing ini; only mutates the `PacketSendRate=` line.
  - Always writes a timestamped `.bak` next to the original.
  - Detects if RL is currently running (file change only takes effect on
    restart; we surface this in the report).
  - Provides a Diff so callers can show before/after to the user.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("ballshark.config_wizard")


@dataclass
class RLInstall:
    source: str  # "steam" | "epic" | "manual"
    install_path: Path
    ini_path: Path

    def exists(self) -> bool:
        return self.ini_path.is_file()


@dataclass
class IniState:
    port: int = 49123
    packet_send_rate: int = 0
    raw_text: str = ""

    @property
    def enabled(self) -> bool:
        return self.packet_send_rate > 0


# ----- install detection -----------------------------------------------------

def _steam_install_root() -> Path | None:
    """Read HKLM\\SOFTWARE\\Wow6432Node\\Valve\\Steam\\InstallPath."""
    try:
        import winreg  # type: ignore
    except ImportError:
        return None
    for hive, key in (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Valve\Steam"),
    ):
        try:
            with winreg.OpenKey(hive, key) as h:
                v, _ = winreg.QueryValueEx(h, "InstallPath")
                return Path(v)
        except OSError:
            continue
    return None


def _steam_library_paths(steam_root: Path) -> list[Path]:
    """Parse libraryfolders.vdf to find all Steam library roots."""
    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    out: list[Path] = [steam_root / "steamapps"]
    if not vdf.is_file():
        return out
    try:
        text = vdf.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return out
    # Very simple VDF scrape: any `"path" "X:\\..."` line.
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith('"path"'):
            parts = line.split('"')
            if len(parts) >= 4:
                p = Path(parts[3]) / "steamapps"
                if p not in out:
                    out.append(p)
    return out


def detect_steam_install() -> RLInstall | None:
    root = _steam_install_root()
    if not root:
        return None
    for lib in _steam_library_paths(root):
        rl_dir = lib / "common" / "rocketleague"
        ini = rl_dir / "TAGame" / "Config" / "DefaultStatsAPI.ini"
        if ini.is_file():
            return RLInstall(source="steam", install_path=rl_dir, ini_path=ini)
    return None


def detect_epic_install() -> RLInstall | None:
    manifests_dir = Path(r"C:\ProgramData\Epic\EpicGamesLauncher\Data\Manifests")
    if not manifests_dir.is_dir():
        return None
    for item in manifests_dir.glob("*.item"):
        try:
            data = json.loads(item.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        # RL's Epic codename is "Sugar"; DisplayName is "Rocket League"
        if data.get("AppName") == "Sugar" or data.get("DisplayName") == "Rocket League":
            inst = data.get("InstallLocation")
            if inst:
                rl_dir = Path(inst)
                ini = rl_dir / "TAGame" / "Config" / "DefaultStatsAPI.ini"
                if ini.is_file():
                    return RLInstall(source="epic", install_path=rl_dir, ini_path=ini)
    return None


def detect_install(manual_path: Path | None = None) -> RLInstall | None:
    if manual_path:
        ini = manual_path / "TAGame" / "Config" / "DefaultStatsAPI.ini"
        if ini.is_file():
            return RLInstall(source="manual", install_path=manual_path, ini_path=ini)
        return None
    return detect_steam_install() or detect_epic_install()


# ----- ini read / write -----------------------------------------------------

def read_ini(path: Path) -> IniState:
    text = path.read_text(encoding="utf-8", errors="ignore") if path.is_file() else ""
    port = 49123
    rate = 0
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(";") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        k = k.strip().lower()
        v = v.strip()
        if k == "port":
            try: port = int(v)
            except ValueError: pass
        elif k == "packetsendrate":
            try: rate = int(v)
            except ValueError: pass
    return IniState(port=port, packet_send_rate=rate, raw_text=text)


def diff_text(old: str, new: str) -> str:
    """Tiny unified-like diff for showing the user."""
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    out: list[str] = []
    for i, (a, b) in enumerate(zip(old_lines, new_lines)):
        if a != b:
            out.append(f"  L{i+1}: - {a}")
            out.append(f"  L{i+1}: + {b}")
    if len(old_lines) != len(new_lines):
        out.append(f"  (line count {len(old_lines)} -> {len(new_lines)})")
    return "\n".join(out) if out else "  (no changes)"


def write_packet_rate(path: Path, new_rate: int, *, backup: bool = True) -> tuple[IniState, IniState, Path | None]:
    """Return (before, after, backup_path or None). Always preserves comments
    and only mutates the `PacketSendRate=` value. Creates the file if missing.
    Skips the backup + write entirely if the rate is already at new_rate."""
    before = read_ini(path)

    if path.is_file():
        original = path.read_text(encoding="utf-8", errors="ignore")
    else:
        original = ""

    if before.packet_send_rate == new_rate and path.is_file():
        return before, before, None

    bak_path: Path | None = None
    if backup and path.is_file():
        bak_path = path.with_name(path.name + f".bak.{int(time.time())}")
        shutil.copy2(path, bak_path)

    if "[TAGame.MatchStatsExporter_TA]" not in original:
        new_text = (
            "[TAGame.MatchStatsExporter_TA]\n"
            f"Port={before.port}\n"
            f"PacketSendRate={new_rate}\n"
        )
    else:
        lines = original.splitlines()
        new_lines: list[str] = []
        wrote_rate = False
        for line in lines:
            stripped = line.strip()
            if stripped.lower().startswith("packetsendrate="):
                indent = line[: len(line) - len(line.lstrip())]
                new_lines.append(f"{indent}PacketSendRate={new_rate}")
                wrote_rate = True
            else:
                new_lines.append(line)
        if not wrote_rate:
            new_lines.append(f"PacketSendRate={new_rate}")
        new_text = "\n".join(new_lines)
        if original.endswith("\n"):
            new_text += "\n"

    path.write_text(new_text, encoding="utf-8")
    after = read_ini(path)
    return before, after, bak_path


# ----- RL process detection -------------------------------------------------

def is_rl_running() -> bool:
    """Check if RocketLeague.exe is in the process list."""
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq RocketLeague.exe", "/NH"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return "RocketLeague.exe" in out.stdout


# ----- wizard runner --------------------------------------------------------

@dataclass
class WizardReport:
    install: RLInstall | None = None
    before: IniState | None = None
    after: IniState | None = None
    backup: Path | None = None
    rl_running: bool = False
    actions: list[str] = field(default_factory=list)
    error: str | None = None


def run_wizard(
    *,
    enable: bool = True,
    rate: int = 30,
    manual_path: Path | None = None,
) -> WizardReport:
    """Idempotent, non-interactive: detects install, sets PacketSendRate.

    `enable=True, rate=30` -> turn on; `enable=False` -> set rate to 0.
    """
    rep = WizardReport()
    install = detect_install(manual_path=manual_path)
    rep.install = install
    if not install:
        rep.error = "Rocket League install not found via Steam or Epic. Pass --rl-path to override."
        rep.actions.append("Could not locate DefaultStatsAPI.ini.")
        return rep

    rep.actions.append(f"Detected RL ({install.source}) at {install.install_path}")

    target_rate = rate if enable else 0
    before, after, backup = write_packet_rate(install.ini_path, target_rate, backup=True)
    rep.before = before
    rep.after = after
    rep.backup = backup
    if backup:
        rep.actions.append(f"Wrote backup to {backup.name}")
    rep.actions.append(
        f"PacketSendRate: {before.packet_send_rate} -> {after.packet_send_rate}  "
        f"(Port={after.port})"
    )
    rep.rl_running = is_rl_running()
    if rep.rl_running:
        rep.actions.append("Rocket League is currently running — restart it for the change to take effect.")
    return rep
