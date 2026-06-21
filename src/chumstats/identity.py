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


def resolve_self_in_match(players: list[dict], cur_name: str,
                          cur_pid: str) -> tuple[str, str, bool]:
    """Decide the identity to persist after a finished match.

    Returns (name, primary_id, locked):
      - name: our CURRENT in-game name (auto-corrected from the match).
      - primary_id: unchanged if already set; otherwise CAPTURED from the match
        so a later name/clan-tag change never unlinks us again.
      - locked: True only when we newly captured a primary_id this call.

    We find ourselves by primary_id when we have one, else by the stored name —
    preferring a name match on our OWN team (a row that has tick telemetry) so we
    don't grab an opponent who happens to share the name. Inputs are returned
    unchanged when we can't find ourselves (and we never lock onto a bot).
    """
    me = None
    if cur_pid:
        me = next((p for p in players if p.get("primary_id") == cur_pid), None)
    elif cur_name:
        cands = [p for p in players if p.get("name") == cur_name]
        me = next((p for p in cands if (p.get("ticks_total") or 0) > 0), None)
        if me is None and cands:
            me = cands[0]
    if not me:
        return cur_name, cur_pid, False

    new_pid = cur_pid
    locked = False
    cand_pid = me.get("primary_id") or ""
    if not cur_pid and cand_pid and cand_pid != "Unknown|0|0":
        new_pid = cand_pid
        locked = True
    new_name = me.get("name") or cur_name
    return new_name, new_pid, locked


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
