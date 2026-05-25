"""Carball Tracker tray launcher (Windows, no console window).

Double-click this file or run it with the venv's pythonw.exe:

    .venv\\Scripts\\pythonw.exe carball-tray.pyw

To auto-start on login, drop a shortcut to this file into ``shell:startup``
(Win+R -> ``shell:startup`` -> paste shortcut).

Environment variables read:
    RL_PLAYER_NAME    default "@ChumtheWaters"
    CARBALL_DB        default "data/carball.db"
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running directly from a checkout without `pip install -e .`.
_HERE = Path(__file__).resolve().parent
_SRC = _HERE / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from carball.tray import main

if __name__ == "__main__":
    raise SystemExit(main())
