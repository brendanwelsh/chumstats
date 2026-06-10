"""Windows system tray app for the Ballshark server.

Spawns the ballshark server as a subprocess and surfaces a tray icon whose color
reflects status: red (server down / problem), yellow (waiting for Rocket League
to open), green (connected + hooked to Rocket League). Hover the icon for the
exact state.

On first launch the setup wizard appears to collect server URL + API key.
After that config lives at %LOCALAPPDATA%\\ballshark\\config.json and friends
never have to touch a .env file.

Single-click opens the live overlay. Right-click: Open Web UI / Settings /
Restart Server / Show Logs Folder / Quit.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pystray
import websocket  # websocket-client
from PIL import Image, ImageDraw

from . import autostart, tray_config

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 5050
PREFERRED_HOST = "ballshark.local"
HEALTH_URL = f"http://{SERVER_HOST}:{SERVER_PORT}/healthz"
WS_URL = f"ws://{SERVER_HOST}:{SERVER_PORT}/ws"
LIVE_PATH = "/live"
BOOST_PATH = "/live?mode=boost"

TICK_FRESH_SECONDS = 5.0
WS_RECONNECT_DELAY = 3.0
HEALTH_POLL_INTERVAL = 2.0
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 3

REPO_ROOT = Path(__file__).resolve().parents[2]

# Three-state status. The color reflects health; the tray tooltip (icon.title)
# gives the detail on hover.
_COLORS = {
    "red":    (210, 60, 60),    # local server down / problem
    "yellow": (235, 180, 40),   # up, but waiting for Rocket League to open
    "green":  (45, 185, 85),    # connected + hooked to Rocket League
}
_START_TITLE = "Ballshark - starting..."


def _log_dir() -> Path:
    p = tray_config.app_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _setup_logger(name: str, filename: str, with_ts: bool) -> logging.Logger:
    handler = RotatingFileHandler(
        _log_dir() / filename, maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT, encoding="utf-8",
    )
    fmt = "%(asctime)s %(levelname)s %(message)s" if with_ts else "%(message)s"
    handler.setFormatter(logging.Formatter(fmt))
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if not any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        logger.addHandler(handler)
    return logger


log = _setup_logger("ballshark.tray", "tray.log", with_ts=True)


def _make_icon(color_name: str) -> Image.Image:
    """A 64x64 RGBA icon: filled circle with a contrasting ring."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    rgb = _COLORS.get(color_name, _COLORS["red"])
    d.ellipse((4, 4, size - 4, size - 4), fill=rgb + (255,),
              outline=(20, 20, 20, 255), width=2)
    return img


def _resolves(host: str) -> bool:
    try:
        socket.gethostbyname(host)
        return True
    except OSError:
        return False


def _base_url() -> str:
    host = PREFERRED_HOST if _resolves(PREFERRED_HOST) else SERVER_HOST
    return f"http://{host}:{SERVER_PORT}"


def open_live() -> None:
    url = _base_url() + LIVE_PATH
    log.info("opening %s", url)
    webbrowser.open(url)


def open_boost() -> None:
    url = _base_url() + BOOST_PATH
    log.info("opening %s", url)
    webbrowser.open(url)


class ServerProcess:
    """Owns the ``python -m ballshark.cli ... run`` subprocess and tees output."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self._log_path = _log_dir() / "ballshark-server.log"
        self._stop = threading.Event()
        self._tee_thread: threading.Thread | None = None

    @property
    def log_path(self) -> Path:
        return self._log_path

    def _argv(self) -> list[str]:
        cfg = tray_config.load()
        db = str(tray_config.db_path())
        me = cfg.rl_player_name or "(unknown)"
        if getattr(sys, "frozen", False):
            # PyInstaller bundle: sys.executable IS Ballshark.exe.
            # Re-exec ourselves with the --cli sentinel handled in
            # ballshark-tray.pyw to route into the CLI entry point.
            return [sys.executable, "--cli", "--me", me, "--db", db, "run"]
        return [sys.executable, "-m", "ballshark.cli", "--me", me, "--db", db, "run"]

    def _env(self) -> dict[str, str]:
        """Subprocess env: take parent env, overlay the tray's persisted config
        so the cli picks up BALLSHARK_REMOTE_URL / BALLSHARK_API_KEY / primary_id
        without the user ever editing a .env."""
        cfg = tray_config.load()
        env = dict(os.environ)
        if cfg.remote_url:           env["BALLSHARK_REMOTE_URL"] = cfg.remote_url
        if cfg.api_key:              env["BALLSHARK_API_KEY"]    = cfg.api_key
        if cfg.rl_player_primary_id: env["RL_PLAYER_PRIMARY_ID"] = cfg.rl_player_primary_id
        if cfg.rl_player_name:       env["RL_PLAYER_NAME"]       = cfg.rl_player_name
        env["BALLSHARK_DB"] = str(tray_config.db_path())
        # Friend mode: lock the local server to LIVE + OBS overlay only.
        # All analytical pages live on the central host.
        env["BALLSHARK_FRIEND_MODE"] = "1"
        return env

    def start(self) -> None:
        if self.proc and self.proc.poll() is None:
            log.info("server already running pid=%s", self.proc.pid)
            return
        argv = self._argv()
        log.info("starting ballshark server: %s", " ".join(argv))
        creationflags = 0
        if os.name == "nt":
            creationflags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        self.proc = subprocess.Popen(
            argv, cwd=str(REPO_ROOT), env=self._env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=1, text=True, creationflags=creationflags,
        )
        self._stop.clear()
        self._tee_thread = threading.Thread(
            target=self._tee, name="ballshark-tee", daemon=True
        )
        self._tee_thread.start()

    def _tee(self) -> None:
        srv_log = _setup_logger("ballshark.tray.server", "ballshark-server.log",
                                with_ts=False)
        try:
            assert self.proc and self.proc.stdout
            for line in self.proc.stdout:
                srv_log.info(line.rstrip())
                if self._stop.is_set():
                    break
        except Exception:
            log.exception("tee thread died")

    def stop(self, timeout: float = 8.0) -> None:
        self._stop.set()
        if not self.proc or self.proc.poll() is not None:
            return
        log.info("stopping ballshark server pid=%s", self.proc.pid)
        try:
            self.proc.terminate()
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log.warning("server did not exit within %ss; killing", timeout)
            self.proc.kill()
            try:
                self.proc.wait(timeout=3.0)
            except Exception:
                pass
        except Exception:
            log.exception("error stopping server")

    def restart(self) -> None:
        self.stop()
        time.sleep(0.5)
        self.start()

    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)


class StateMonitor(threading.Thread):
    """Background thread; maintains grey/orange/green state via /healthz + /ws."""

    def __init__(self, server: ServerProcess, on_change) -> None:
        super().__init__(name="ballshark-monitor", daemon=True)
        self._server = server
        self._on_change = on_change
        self._stop = threading.Event()
        self._last_tick = 0.0
        self._server_up = False
        self._rl_connected = False
        self._paused = False
        self._state = "red"
        self._title = _START_TITLE

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        threading.Thread(target=self._ws_loop, name="ballshark-ws", daemon=True).start()
        while not self._stop.is_set():
            self._server_up = self._probe_health()
            self._update_state()
            self._stop.wait(HEALTH_POLL_INTERVAL)

    def _probe_health(self) -> bool:
        from urllib.request import urlopen
        try:
            with urlopen(HEALTH_URL, timeout=1.5) as r:
                if r.status == 200:
                    data = json.loads(r.read().decode("utf-8") or "{}")
                    self._rl_connected = bool(data.get("rl_connected", False))
                    return True
        except Exception:
            return False
        return False

    def _ws_loop(self) -> None:
        while not self._stop.is_set():
            try:
                ws = websocket.create_connection(WS_URL, timeout=3.0)
            except Exception:
                if self._stop.wait(WS_RECONNECT_DELAY):
                    return
                continue
            log.info("ws connected")
            try:
                ws.settimeout(2.0)
                while not self._stop.is_set():
                    try:
                        msg = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    except Exception:
                        break
                    if msg:
                        self._handle_ws_message(msg)
            finally:
                try:
                    ws.close()
                except Exception:
                    pass
            log.info("ws disconnected; reconnecting")
            if self._stop.wait(WS_RECONNECT_DELAY):
                return

    def _handle_ws_message(self, raw: str) -> None:
        try:
            obj = json.loads(raw)
        except Exception:
            return
        t = obj.get("type")
        if t == "rl_status":
            self._rl_connected = bool((obj.get("data") or {}).get("connected", False))
            self._update_state()
            return
        if t in ("tick", "match_start"):
            self._last_tick = time.time()
        if t == "match_end":
            self._maybe_persist_identity(obj.get("data") or {})
        # match_end: fall through; tick-freshness window dictates state.
        if t in ("tick", "match_start", "match_end"):
            self._update_state()

    def _maybe_persist_identity(self, summary: dict) -> None:
        """Keep the stored identity in sync with the real match: lock our account
        ID the first time we see ourselves (rename-safe from game one — matters
        for Epic users who typed a name once) and refresh the display name to the
        current in-game name. All keyed on the stable primary_id once we have it."""
        players = summary.get("players") or []
        if not players:
            return
        try:
            from . import tray_config
            from .identity import resolve_self_in_match
            cfg = tray_config.load()
        except Exception:
            return
        new_name, new_pid, locked = resolve_self_in_match(
            players, cfg.rl_player_name, cfg.rl_player_primary_id)
        if new_name == cfg.rl_player_name and new_pid == cfg.rl_player_primary_id:
            return
        cfg.rl_player_name = new_name
        cfg.rl_player_primary_id = new_pid
        try:
            tray_config.save(cfg)
            log.info("identity synced from match: name=%r primary_id=%r (locked=%s)",
                     new_name, new_pid, locked)
        except Exception:
            log.exception("failed to persist identity")
            return
        if locked:
            # Restart so the tracker self-identifies by the locked account id
            # (also enables upload for friends who had no id configured before).
            threading.Thread(target=self._server.restart, daemon=True).start()

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self._update_state()

    def _update_state(self) -> None:
        now = time.time()
        if self._paused:
            color = "red"
            title = "Ballshark - paused (transmission off)"
        elif not self._server_up:
            color = "red"
            title = "Ballshark - not running (starting up, or crashed - check logs)"
        elif not self._rl_connected:
            color = "yellow"
            title = "Ballshark - waiting for Rocket League to open"
        elif (now - self._last_tick) <= TICK_FRESH_SECONDS:
            color = "green"
            title = "Ballshark - connected - match in progress"
        else:
            color = "green"
            title = "Ballshark - connected to Rocket League (waiting for a match)"
        if color != self._state or title != self._title:
            self._state = color
            self._title = title
            log.info("state -> %s (%s)", color, title)
            try:
                self._on_change(color, title)
            except Exception:
                log.exception("on_change callback failed")


class TrayApp:
    def __init__(self) -> None:
        self.server = ServerProcess()
        self._paused = False
        self.icon = pystray.Icon(
            "ballshark",
            icon=_make_icon("red"),
            title=_START_TITLE,
            menu=self._build_menu(),
        )
        self.monitor = StateMonitor(self.server, self._on_state_change)

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem("Open Web UI", self._on_open_live, default=True),
            pystray.MenuItem("Open BOOST VIEW", self._on_open_boost),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Pause tracking (go red)",
                             self._on_toggle_pause,
                             checked=lambda item: self._paused),
            pystray.MenuItem("Settings…", self._on_settings),
            pystray.MenuItem("Restart Server", self._on_restart),
            pystray.MenuItem("Show Logs Folder", self._on_show_logs),
            pystray.MenuItem(
                "Start with Windows",
                self._on_toggle_autostart,
                checked=lambda item: autostart.is_enabled(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._on_quit),
        )

    def _on_open_live(self, icon=None, item=None) -> None:
        open_live()

    def _on_open_boost(self, icon=None, item=None) -> None:
        open_boost()

    def _on_restart(self, icon=None, item=None) -> None:
        log.info("menu: restart server")
        threading.Thread(target=self.server.restart, daemon=True).start()

    def _on_settings(self, icon=None, item=None) -> None:
        log.info("menu: settings")
        # tkinter must run in the main thread on Windows. The tray's icon.run()
        # loop blocks the main thread, so we spawn the dialog in a side thread.
        # This is OK on Windows because tkinter creates its own Tcl interpreter
        # per Tk() instance; the wizard module already does that.
        def show():
            from .tray_wizard import SettingsDialog
            SettingsDialog(on_saved=lambda cfg: self.server.restart()).run()
        threading.Thread(target=show, daemon=True).start()

    def _on_show_logs(self, icon=None, item=None) -> None:
        path = _log_dir()
        log.info("opening logs folder: %s", path)
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except AttributeError:
            webbrowser.open(path.as_uri())

    def _on_toggle_autostart(self, icon=None, item=None) -> None:
        try:
            autostart.set_enabled(not autostart.is_enabled())
            log.info("autostart -> %s", autostart.is_enabled())
        except Exception:
            log.exception("failed to toggle autostart")
        try:
            if icon is not None:
                icon.update_menu()  # refresh the checkmark immediately
        except Exception:
            pass

    def _on_toggle_pause(self, icon=None, item=None) -> None:
        """Pause/resume tracking. Paused stops the tracker subprocess entirely
        (no capture, no upload) and forces the icon red."""
        self._paused = not self._paused
        log.info("paused = %s", self._paused)
        try:
            self.monitor.set_paused(self._paused)
        except Exception:
            log.exception("set_paused failed")
        if self._paused:
            self._on_state_change("red", "Ballshark - paused (transmission off)")
            threading.Thread(target=self.server.stop, daemon=True).start()
        else:
            threading.Thread(target=self.server.start, daemon=True).start()
        try:
            if icon is not None:
                icon.update_menu()
        except Exception:
            pass

    def _on_quit(self, icon=None, item=None) -> None:
        log.info("menu: quit")
        self.monitor.stop()
        self.server.stop()
        self.icon.stop()

    def _on_state_change(self, color: str, title: str) -> None:
        self.icon.icon = _make_icon(color)
        self.icon.title = title

    def run(self) -> None:
        log.info("tray app starting; repo=%s", REPO_ROOT)
        self.server.start()
        self.monitor.start()
        self.icon.run()  # blocks until icon.stop()
        log.info("tray app exiting")


# Loopback port used purely as a single-instance lock (not a real service).
_SINGLETON_LOCK_PORT = 5051
_SINGLETON_SOCK = None  # keep the bound socket alive for the process lifetime


def _acquire_single_instance() -> bool:
    """Ensure only one tray runs at a time.

    The tray can be launched from more than one place — the HKCU ``Run``
    autostart entry (which points at the venv's pythonw) and a manual
    double-click of ``ballshark-tray.pyw`` (which the OS opens with the *system*
    Python via the .pyw file association). Without a guard you end up with two
    trays, two ``ballshark run`` subprocesses, and two servers fighting over
    port 5050.

    We grab an exclusive bind on a fixed loopback port. Without SO_REUSEADDR a
    second bind to the same address fails (WSAEADDRINUSE / EADDRINUSE) on both
    Windows and Linux, so the second launch detects us and no-ops. The OS frees
    the port automatically when this process exits.

    Returns True if we are the only instance, False if another already holds it.
    """
    global _SINGLETON_SOCK
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", _SINGLETON_LOCK_PORT))
        s.listen(1)
    except OSError:
        s.close()
        return False
    _SINGLETON_SOCK = s  # hold the lock until the process dies
    return True


def main() -> int:
    if not _acquire_single_instance():
        log.info("another Ballshark tray instance is already running; exiting")
        return 0
    try:
        # First-run wizard: blocks the main thread until the user closes it.
        # If config already exists, returns immediately. We do this BEFORE
        # starting the tray so the user isn't seeing a tray icon with no
        # working subprocess.
        from .tray_wizard import maybe_run_wizard
        cfg = maybe_run_wizard()
        if not cfg.is_configured:
            log.warning("wizard closed without complete config; exiting")
            return 0
        TrayApp().run()
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("fatal error in tray main")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
