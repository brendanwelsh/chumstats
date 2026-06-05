"""Windows 'start on login' via the per-user Run registry key.

No admin rights needed — HKCU\\...\\Run is user-scoped. The tray exposes this as
a checkbox; we point the Run entry at the frozen Ballshark.exe (or, in a dev
checkout, pythonw + the tray launcher) so the app comes up on login.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "Ballshark"


def is_supported() -> bool:
    return os.name == "nt"


def _command() -> str:
    """The command Windows runs at login.

    Frozen (PyInstaller bundle): sys.executable *is* Ballshark.exe.
    Dev checkout: launch ballshark-tray.pyw with pythonw.exe (no console window).
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    launcher = Path(__file__).resolve().parents[2] / "ballshark-tray.pyw"
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = pyw if pyw.exists() else Path(sys.executable)
    return f'"{exe}" "{launcher}"'


def is_enabled() -> bool:
    if not is_supported():
        return False
    import winreg  # type: ignore
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            winreg.QueryValueEx(k, _VALUE_NAME)
        return True
    except OSError:
        return False


def enable() -> None:
    if not is_supported():
        return
    import winreg  # type: ignore
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
        winreg.SetValueEx(k, _VALUE_NAME, 0, winreg.REG_SZ, _command())


def disable() -> None:
    if not is_supported():
        return
    import winreg  # type: ignore
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, _VALUE_NAME)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def set_enabled(on: bool) -> None:
    enable() if on else disable()
