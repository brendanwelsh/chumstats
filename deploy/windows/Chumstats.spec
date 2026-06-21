# PyInstaller spec for the Chumstats friend distributable.
#
# Build:
#     ./deploy/windows/build.ps1
# or:
#     pyinstaller deploy/windows/Chumstats.spec --noconfirm
#
# Output: dist/Chumstats/Chumstats.exe (one-folder mode)
# Why one-folder vs one-file: faster startup, easier to debug, antivirus is
# less likely to flag it.

from pathlib import Path

# Resolve the project root from this spec file's location. PyInstaller invokes
# specs with __file__ pointing at the .spec.
ROOT = Path(SPECPATH).resolve().parents[1]  # deploy/windows -> deploy -> repo root
SRC  = ROOT / "src"

a = Analysis(
    [str(ROOT / "chumstats-tray.pyw")],
    pathex=[str(SRC)],
    binaries=[],
    datas=[
        # Bundle the overlay HTML/CSS/JS + icon PNGs so the embedded server
        # can serve them.
        (str(SRC / "chumstats" / "overlay"), "chumstats/overlay"),
    ],
    hiddenimports=[
        # pystray's platform backend selection is dynamic.
        "pystray._win32",
        # tkinter — used by the wizard + settings dialog.
        "tkinter", "tkinter.ttk", "tkinter.messagebox",
        # Pydantic v2 picks at runtime; PyInstaller's auto-detect usually finds
        # this, but list it to be safe.
        "pydantic", "pydantic_core",
        # Uvicorn + websockets aren't on the import graph from chumstats-tray.pyw
        # directly (they're imported lazily by the subprocess), but the
        # subprocess invokes sys.executable which inside the bundle is the
        # bundled interpreter — so it needs them too.
        "uvicorn", "uvicorn.lifespan.on", "uvicorn.lifespan.off",
        "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.loops.auto", "uvicorn.loops.asyncio",
        "websockets", "websockets.legacy", "websockets.legacy.server",
        # FastAPI internals
        "fastapi",
        # The lazily-imported chumstats submodules the subprocess + tray use.
        # (Several are imported inside functions, which PyInstaller's static
        # graph can miss — list them so the frozen build doesn't crash.)
        "chumstats.cli", "chumstats.server", "chumstats.ingest", "chumstats.session",
        "chumstats.store", "chumstats.analytics", "chumstats.models",
        "chumstats.bot", "chumstats.sync", "chumstats.replay",
        "chumstats.identity", "chumstats.autostart", "chumstats.config_wizard",
        "chumstats.tray_config", "chumstats.tray_wizard",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Things we know we don't need; trim the bundle.
        "matplotlib", "numpy", "pandas", "scipy", "PyQt5", "PyQt6", "PySide6",
        "pytest", "ruff", "black", "mypy",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Chumstats",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # UPX is fine, but adds AV false-positive risk
    console=False,       # tray app — no console window
    icon=None,           # TODO: drop a .ico in deploy/windows/icon.ico and reference here
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Chumstats",
)
