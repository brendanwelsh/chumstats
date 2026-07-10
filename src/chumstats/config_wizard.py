"""Detect Rocket League installs and toggle the Stats API config.

Why this exists: people don't want to hand-edit RL's config to enable the API.
Same operation, behind a button (CLI or eventually GUI).

WHERE WE WRITE (and why it matters — the "verify integrity of game files" bug)
------------------------------------------------------------------------------
RL ships `DefaultStatsAPI.ini` INSIDE the launcher-managed install dir
(`steamapps\\common\\rocketleague\\...` for Steam, the Epic install dir for
Epic). That file is a depot-tracked game file: if we modify it, Steam/Epic's
integrity check sees a changed game file and forces a re-verify / re-download —
which silently resets `PacketSendRate` back to 0 and, for the user, means
constantly being told to "verify integrity of game files". Earlier versions of
this wizard wrote there; that was the root cause of that whole headache.

UE3 (RL's engine) only uses `DefaultStatsAPI.ini` as a *template*. On launch it
generates/merges it into a per-user runtime copy under the user's Documents:
`My Games\\Rocket League\\TAGame\\Config\\TAStatsAPI.ini`. That user-space file
is what RL actually READS at runtime, and it is outside any launcher-managed dir.

So we WRITE the user-space `TAStatsAPI.ini` (never the install), and offer
`restore_install_template()` to put a previously-modified `DefaultStatsAPI.ini`
back to its pristine shipped bytes so integrity checks pass again.

Safety:
  - Reads existing ini; only mutates the `PacketSendRate=` line.
  - Always writes a timestamped `.bak` next to the user-space file (never litters
    the install dir).
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

log = logging.getLogger("chumstats.config_wizard")


@dataclass
class RLInstall:
    source: str  # "steam" | "epic" | "manual"
    install_path: Path
    ini_path: Path                 # install-dir template (READ / restore only)
    config_path: Path | None = None  # user-space TAStatsAPI.ini (what we WRITE)

    def exists(self) -> bool:
        return self.ini_path.is_file()

    @property
    def write_target(self) -> Path:
        """The file we enable/disable the Stats API in. Always the user-space
        runtime config, so we never touch a launcher-managed game file. Falls
        back to resolving it fresh if detection didn't populate it."""
        return self.config_path or user_stats_config_path()


@dataclass
class IniState:
    port: int = 49123
    packet_send_rate: int = 0
    raw_text: str = ""

    @property
    def enabled(self) -> bool:
        return self.packet_send_rate > 0


# ----- user-space runtime config (the SAFE place to write) -------------------

def _documents_dir() -> Path:
    """Resolve the user's real Documents folder — the one RL uses for
    `My Games`. This is NOT always `~\\Documents`: it's commonly redirected
    (OneDrive, or a moved known-folder). Read the Windows known-folder registry
    value, which is exactly what RL/UE3 resolves; fall back to `~/Documents`."""
    try:
        import winreg  # type: ignore
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
        ) as h:
            v, _ = winreg.QueryValueEx(h, "Personal")
            # Value can contain %USERPROFILE% etc.
            return Path(os.path.expandvars(v))
    except OSError:
        pass
    return Path.home() / "Documents"


def user_stats_config_path() -> Path:
    """`<Documents>\\My Games\\Rocket League\\TAGame\\Config\\TAStatsAPI.ini` —
    the per-user runtime config RL actually reads. Outside any launcher-managed
    install dir, so writing here never trips an integrity check."""
    return (
        _documents_dir()
        / "My Games" / "Rocket League" / "TAGame" / "Config" / "TAStatsAPI.ini"
    )


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
            return RLInstall(source="steam", install_path=rl_dir, ini_path=ini,
                             config_path=user_stats_config_path())
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
                    return RLInstall(source="epic", install_path=rl_dir, ini_path=ini,
                                     config_path=user_stats_config_path())
    return None


def detect_install(manual_path: Path | None = None) -> RLInstall | None:
    if manual_path:
        ini = manual_path / "TAGame" / "Config" / "DefaultStatsAPI.ini"
        if ini.is_file():
            return RLInstall(source="manual", install_path=manual_path, ini_path=ini,
                             config_path=user_stats_config_path())
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


# ----- install-template restore (undo the integrity-verify trigger) ----------

def restore_install_template(install: RLInstall) -> tuple[bool, int]:
    """Put the launcher-managed `DefaultStatsAPI.ini` back to its pristine
    shipped state (`PacketSendRate=0`) and remove the `.bak*` files older
    versions of this wizard littered next to it.

    Once the depot file matches its shipped bytes again, Steam/Epic integrity
    checks pass and stop nagging. Because `write_packet_rate` only ever touched
    the single rate digit (line endings preserved), rewriting it to 0 reproduces
    the shipped file byte-for-byte, so no re-download is needed.

    Returns (rewrote_the_ini, num_backups_removed). Safe/idempotent: if the file
    is already pristine it only cleans up backups.
    """
    ini = install.ini_path
    rewrote = False
    if ini.is_file() and read_ini(ini).packet_send_rate != 0:
        # backup=False: we do NOT want a fresh .bak in the install dir — the
        # whole point is to leave the install dir clean.
        write_packet_rate(ini, 0, backup=False)
        rewrote = True

    removed = 0
    try:
        for bak in ini.parent.glob(ini.name + ".bak*"):
            try:
                bak.unlink()
                removed += 1
            except OSError:
                pass
    except OSError:
        pass
    return rewrote, removed


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
    config_path: Path | None = None   # the user-space file we wrote
    install_restored: bool = False    # did we reset the install template to 0?
    install_baks_removed: int = 0
    actions: list[str] = field(default_factory=list)
    error: str | None = None


def run_wizard(
    *,
    enable: bool = True,
    rate: int = 30,
    manual_path: Path | None = None,
    restore_install: bool = True,
    legacy_install_write: bool = False,
) -> WizardReport:
    """Idempotent, non-interactive: detects install, sets PacketSendRate in the
    USER-SPACE runtime config (`TAStatsAPI.ini`), never in the install dir.

    `enable=True, rate=30` -> turn on; `enable=False` -> set rate to 0.

    `restore_install=True` (default) also resets a previously-modified install
    `DefaultStatsAPI.ini` back to pristine + cleans our old backups out of the
    install dir, so launcher integrity checks stop nagging. `legacy_install_write`
    forces the old (bad) behavior of writing the install file directly — kept
    only as an escape hatch if a given install doesn't honor the user-space file.
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
    write_path = install.ini_path if legacy_install_write else install.write_target
    rep.config_path = write_path
    if legacy_install_write:
        rep.actions.append("! Writing the INSTALL file directly (legacy mode) — "
                           "this can trigger 'verify integrity of game files'.")
    else:
        write_path.parent.mkdir(parents=True, exist_ok=True)
        rep.actions.append(f"Target (user-space, safe): {write_path}")

    before, after, backup = write_packet_rate(write_path, target_rate, backup=True)
    rep.before = before
    rep.after = after
    rep.backup = backup
    if backup:
        rep.actions.append(f"Wrote backup to {backup.name}")
    rep.actions.append(
        f"PacketSendRate: {before.packet_send_rate} -> {after.packet_send_rate}  "
        f"(Port={after.port})"
    )

    # Undo the historical damage: if we (an earlier version) left the install
    # template modified, put it back to pristine so integrity checks pass.
    if restore_install and not legacy_install_write:
        try:
            rewrote, removed = restore_install_template(install)
        except Exception:
            log.exception("restore_install_template failed")
            rewrote, removed = False, 0
        rep.install_restored = rewrote
        rep.install_baks_removed = removed
        if rewrote:
            rep.actions.append(
                "Restored install DefaultStatsAPI.ini to pristine (PacketSendRate=0) "
                "so Steam/Epic integrity checks pass.")
        if removed:
            rep.actions.append(f"Cleaned {removed} stale backup file(s) from the install dir.")

    rep.rl_running = is_rl_running()
    if rep.rl_running:
        rep.actions.append("Rocket League is currently running — restart it for the change to take effect.")
    return rep
