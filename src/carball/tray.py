"""Windows system tray app for the Carball Tracker server.

Spawns the carball server as a subprocess and surfaces a tray icon whose color
reflects connection state: grey (server down), orange (server up, idle),
green (tick received within last 5 s).

Single-click opens the live overlay. Right-click: Open Web UI /
Open BOOST VIEW / Restart Server / Show Logs Folder / Quit.

Auto-start: press Win+R, type ``shell:startup`` and drop a shortcut to
``carball-tray.pyw`` (in the repo root) into that folder.

Usage:
    python carball-tray.pyw          (no console window)
    python -m carball.tray           (console mode for debugging)

Env vars (override defaults):
    RL_PLAYER_NAME   default "@ChumtheWaters"
    CARBALL_DB       default "data/carball.db"
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

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 5050
PREFERRED_HOST = "carball.local"
HEALTH_URL = f"http://{SERVER_HOST}:{SERVER_PORT}/healthz"
WS_URL = f"ws://{SERVER_HOST}:{SERVER_PORT}/ws"
LIVE_PATH = "/live"
BOOST_PATH = "/live?mode=boost"

TICK_FRESH_SECONDS = 5.0
WS_RECONNECT_DELAY = 3.0
HEALTH_POLL_INTERVAL = 2.0
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 3

DEFAULT_PLAYER = "@ChumtheWaters"
DEFAULT_DB = "data/carball.db"
REPO_ROOT = Path(__file__).resolve().parents[2]

_COLORS = {
    "grey":   (140, 140, 140),
    "orange": (240, 145, 30),
    "green":  (40, 180, 80),
}
_TITLES = {
    "grey":   "Carball Tracker (offline)",
    "orange": "Carball Tracker (connected, idle)",
    "green":  "Carball Tracker (match active)",
}


def _log_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    p = Path(base) / "carball"
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


log = _setup_logger("carball.tray", "tray.log", with_ts=True)


def _make_icon(color_name: str) -> Image.Image:
    """A 64x64 RGBA icon: filled circle with a contrasting ring."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    rgb = _COLORS.get(color_name, _COLORS["grey"])
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
    """Owns the ``python -m carball.cli ... run`` subprocess and tees output."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self._log_path = _log_dir() / "carball-server.log"
        self._stop = threading.Event()
        self._tee_thread: threading.Thread | None = None

    @property
    def log_path(self) -> Path:
        return self._log_path

    def _argv(self) -> list[str]:
        me = os.environ.get("RL_PLAYER_NAME", DEFAULT_PLAYER)
        db = os.environ.get("CARBALL_DB", DEFAULT_DB)
        return [sys.executable, "-m", "carball.cli", "--me", me, "--db", db, "run"]

    def start(self) -> None:
        if self.proc and self.proc.poll() is None:
            log.info("server already running pid=%s", self.proc.pid)
            return
        argv = self._argv()
        log.info("starting carball server: %s", " ".join(argv))
        creationflags = 0
        if os.name == "nt":
            creationflags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        self.proc = subprocess.Popen(
            argv, cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=1, text=True, creationflags=creationflags,
        )
        self._stop.clear()
        self._tee_thread = threading.Thread(
            target=self._tee, name="carball-tee", daemon=True
        )
        self._tee_thread.start()

    def _tee(self) -> None:
        srv_log = _setup_logger("carball.tray.server", "carball-server.log",
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
        log.info("stopping carball server pid=%s", self.proc.pid)
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
        super().__init__(name="carball-monitor", daemon=True)
        self._server = server
        self._on_change = on_change
        self._stop = threading.Event()
        self._last_tick = 0.0
        self._server_up = False
        self._state = "grey"

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        threading.Thread(target=self._ws_loop, name="carball-ws", daemon=True).start()
        while not self._stop.is_set():
            self._server_up = self._probe_health()
            self._update_state()
            self._stop.wait(HEALTH_POLL_INTERVAL)

    def _probe_health(self) -> bool:
        from urllib.request import urlopen
        try:
            with urlopen(HEALTH_URL, timeout=1.5) as r:
                if r.status == 200:
                    json.loads(r.read().decode("utf-8") or "{}")
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
        if t in ("tick", "match_start"):
            self._last_tick = time.time()
        # match_end: fall through; tick-freshness window dictates state.
        if t in ("tick", "match_start", "match_end"):
            self._update_state()

    def _update_state(self) -> None:
        if not self._server_up:
            new_state = "grey"
        elif (time.time() - self._last_tick) <= TICK_FRESH_SECONDS:
            new_state = "green"
        else:
            new_state = "orange"
        if new_state != self._state:
            self._state = new_state
            log.info("state -> %s", new_state)
            try:
                self._on_change(new_state)
            except Exception:
                log.exception("on_change callback failed")


class TrayApp:
    def __init__(self) -> None:
        self.server = ServerProcess()
        self.icon = pystray.Icon(
            "carball",
            icon=_make_icon("grey"),
            title=_TITLES["grey"],
            menu=self._build_menu(),
        )
        self.monitor = StateMonitor(self.server, self._on_state_change)

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem("Open Web UI", self._on_open_live, default=True),
            pystray.MenuItem("Open BOOST VIEW", self._on_open_boost),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Restart Server", self._on_restart),
            pystray.MenuItem("Show Logs Folder", self._on_show_logs),
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

    def _on_show_logs(self, icon=None, item=None) -> None:
        path = _log_dir()
        log.info("opening logs folder: %s", path)
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except AttributeError:
            webbrowser.open(path.as_uri())

    def _on_quit(self, icon=None, item=None) -> None:
        log.info("menu: quit")
        self.monitor.stop()
        self.server.stop()
        self.icon.stop()

    def _on_state_change(self, state: str) -> None:
        self.icon.icon = _make_icon(state)
        self.icon.title = _TITLES.get(state, "Carball Tracker")

    def run(self) -> None:
        log.info("tray app starting; repo=%s", REPO_ROOT)
        self.server.start()
        self.monitor.start()
        self.icon.run()  # blocks until icon.stop()
        log.info("tray app exiting")


def main() -> int:
    try:
        TrayApp().run()
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("fatal error in tray main")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
