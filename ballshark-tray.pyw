"""Ballshark tray launcher (Windows, no console window).

Two roles in one entry point:
  - Default: launch the system-tray app.
  - With first arg `--cli`: route into ballshark.cli (used by the tray's
    subprocess invocation when running as a PyInstaller bundle, where
    sys.executable is this very binary).

Double-click this file or run it with the venv's pythonw.exe:

    .venv\\Scripts\\pythonw.exe ballshark-tray.pyw

To auto-start on login, drop a shortcut to this file into ``shell:startup``
(Win+R -> ``shell:startup`` -> paste shortcut).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running directly from a checkout without `pip install -e .`.
_HERE = Path(__file__).resolve().parent
_SRC = _HERE / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        # Strip the sentinel so argparse in ballshark.cli sees a clean argv.
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        from ballshark.cli import main as cli_main
        raise SystemExit(cli_main())
    from ballshark.tray import main as tray_main
    raise SystemExit(tray_main())
