"""Best-effort detection of the local player's identity so the setup wizard can
auto-fill instead of making the user type their name.

The PrimaryId (e.g. ``Steam|76561197985273611|0``) is the STABLE key the rest of
the app matches on — names and clan tags change, the account ID doesn't. The
name we return is just a starting label; the live match stream has the real,
current name and the app keys on the ID regardless.

Steam is supported today (read from the registry, no files needed for the ID).
Epic isn't auto-detectable locally yet, so Epic users fall back to typing it.
"""

from __future__ import annotations

import os

_STEAM64_BASE = 76561197960265728


def _steam_active_account_id() -> int | None:
    """The currently logged-in Steam account id (SteamID3), or None."""
    try:
        import winreg  # type: ignore
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Valve\Steam\ActiveProcess") as k:
            v, _ = winreg.QueryValueEx(k, "ActiveUser")
        return int(v) or None
    except Exception:
        return None


def _steam_path() -> str | None:
    try:
        import winreg  # type: ignore
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            v, _ = winreg.QueryValueEx(k, "SteamPath")
        return v or None
    except Exception:
        return None


def _steam_persona(steamid64: int) -> str:
    """Best-effort Steam display name from loginusers.vdf. Not guaranteed to be
    the RL name (RL uses the Epic display name post-merger), but a sane default
    that the first match will correct."""
    paths = []
    sp = _steam_path()
    if sp:
        paths.append(os.path.join(sp, "config", "loginusers.vdf"))
    pf = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    paths.append(os.path.join(pf, "Steam", "config", "loginusers.vdf"))

    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except OSError:
            continue
        i = text.find(f'"{steamid64}"')
        if i == -1:
            continue
        j = text.find('"PersonaName"', i)
        if j == -1:
            continue
        k = text.find('"', j + len('"PersonaName"'))
        end = text.find('"', k + 1) if k != -1 else -1
        if k != -1 and end != -1:
            return text[k + 1:end]
    return ""


def detect_local_identity() -> dict | None:
    """Return {'primary_id', 'name', 'platform'} for the local player, or None
    if we can't detect it (e.g. Epic, or Steam not logged in)."""
    acct = _steam_active_account_id()
    if not acct:
        return None
    steamid64 = acct + _STEAM64_BASE
    return {
        "primary_id": f"Steam|{steamid64}|0",
        "name": _steam_persona(steamid64),
        "platform": "Steam",
    }
