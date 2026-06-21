"""First-run setup wizard for the Chumstats tray app.

Pure tkinter — bundles cleanly with PyInstaller, no extra deps.
Walks a friend through:
    1. Welcome
    2. Server URL + API key (with [Test Connection] hitting /api/v1/whoami)
    3. Their in-game name (auto-filled from the whoami response)
    4. Enable Rocket League's Stats API (runs `chumstats setup` via the
       same-process function, so no subprocess shell-out)

On Finish, writes the config to %LOCALAPPDATA%\\chumstats\\config.json and
calls back to the tray.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import tkinter as tk
from tkinter import ttk, messagebox

from . import autostart
from .tray_config import TrayConfig, load, save

log = logging.getLogger("chumstats.tray.wizard")

WINDOW_W = 560
WINDOW_H = 420


def _whoami(remote_url: str, api_key: str, timeout: float = 6.0) -> dict:
    """Call /api/v1/whoami. Returns the JSON on 200, raises with a friendly
    message otherwise."""
    url = remote_url.rstrip("/") + "/api/v1/whoami"
    req = Request(url, headers={"X-Chumstats-Key": api_key})
    try:
        with urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8") or "{}")
            return data
    except HTTPError as e:
        if e.code == 401:
            raise RuntimeError("Invalid API key — double-check what your friend sent you.")
        raise RuntimeError(f"Server returned HTTP {e.code}.")
    except URLError as e:
        raise RuntimeError(f"Couldn't reach the server.\n\n{e.reason}")
    except OSError as e:
        raise RuntimeError(f"Network error.\n\n{e}")


class WizardApp:
    def __init__(self, on_complete: Callable[[TrayConfig], None] | None = None) -> None:
        self.cfg = load()
        self.on_complete = on_complete
        self._step = 1
        self._whoami_data: dict | None = None  # filled in after Test Connection succeeds
        self.root = tk.Tk()
        self.root.title("Chumstats — Setup")
        self.root.geometry(f"{WINDOW_W}x{WINDOW_H}")
        self.root.resizable(False, False)
        self._render()

    # ---- step rendering --------------------------------------------------

    def _clear(self) -> None:
        for w in self.root.winfo_children():
            w.destroy()

    def _render(self) -> None:
        self._clear()
        if   self._step == 1: self._render_welcome()
        elif self._step == 2: self._render_server()
        elif self._step == 3: self._render_identity()
        elif self._step == 4: self._render_rl_setup()
        elif self._step == 5: self._render_done()

    def _header(self, text: str, sub: str = "") -> None:
        ttk.Label(self.root, text=text, font=("Segoe UI", 14, "bold")).pack(
            anchor="w", padx=24, pady=(20, 4))
        if sub:
            ttk.Label(self.root, text=sub, font=("Segoe UI", 9),
                      foreground="#666", wraplength=WINDOW_W - 48).pack(
                anchor="w", padx=24, pady=(0, 10))

    def _footer(self, *, on_back=None, on_next=None, next_label="Next") -> None:
        bar = ttk.Frame(self.root); bar.pack(side="bottom", fill="x", padx=24, pady=14)
        ttk.Label(bar, text=f"Step {self._step} of 4").pack(side="left")
        ttk.Button(bar, text=next_label,
                   command=on_next, state=("normal" if on_next else "disabled")).pack(side="right")
        if on_back:
            ttk.Button(bar, text="Back", command=on_back).pack(side="right", padx=(0, 6))

    # ---- step 1: welcome -------------------------------------------------

    def _render_welcome(self) -> None:
        self._header("Welcome to Chumstats",
                     "We'll get you set up to share match stats with your friend group. "
                     "Takes about 30 seconds.")
        body = ttk.Frame(self.root); body.pack(fill="both", expand=True, padx=24)
        ttk.Label(body, text=(
            "You can use Chumstats two ways:\n\n"
            "    •  Track locally on this PC — no server needed.\n\n"
            "    •  Connect to a friend's server to share your matches.\n"
            "        (You'll need the server URL + API key they sent you.)\n\n"
            "Pick one on the next screen. You can switch later from Settings."
        ), justify="left").pack(anchor="w", pady=8)
        self._footer(on_next=lambda: self._goto(2))

    # ---- step 2: server --------------------------------------------------

    def _render_server(self) -> None:
        self._header("How do you want to use Chumstats?",
                     "Track just on this PC, or connect to a friend's server to share "
                     "your matches.")
        body = ttk.Frame(self.root); body.pack(fill="x", expand=False, padx=24)

        if not hasattr(self, "var_mode"):
            # Default to remote only if a server was already configured.
            self.var_mode = tk.StringVar(value="remote" if self.cfg.remote_url else "local")

        ttk.Radiobutton(body, text="Just track locally on this PC (no server)",
                        variable=self.var_mode, value="local",
                        command=self._render).grid(row=0, column=0, columnspan=2, sticky="w", pady=(8, 2))
        ttk.Radiobutton(body, text="Connect to a friend's server (upload my matches)",
                        variable=self.var_mode, value="remote",
                        command=self._render).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 10))

        is_remote = self.var_mode.get() == "remote"
        status = None
        if is_remote:
            ttk.Label(body, text="Server URL").grid(row=2, column=0, sticky="w", pady=(8, 2))
            self.var_url = tk.StringVar(value=self.cfg.remote_url)
            ttk.Entry(body, textvariable=self.var_url, width=58).grid(row=3, column=0, columnspan=2, sticky="we")

            ttk.Label(body, text="API key").grid(row=4, column=0, sticky="w", pady=(12, 2))
            self.var_key = tk.StringVar(value=self.cfg.api_key)
            ttk.Entry(body, textvariable=self.var_key, width=58, show="•").grid(
                row=5, column=0, columnspan=2, sticky="we")

            test_btn = ttk.Button(body, text="Test connection",
                                  command=lambda: self._test_connection(test_btn, status))
            test_btn.grid(row=6, column=0, sticky="w", pady=(14, 0))
            status = ttk.Label(body, text="", foreground="#666")
            status.grid(row=6, column=1, sticky="w", padx=8, pady=(14, 0))
        else:
            ttk.Label(body, text=(
                "Your matches stay on this PC. You can connect to a friend's server "
                "later from the tray's Settings menu."),
                foreground="#666", wraplength=WINDOW_W - 60, justify="left").grid(
                row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))

        def goto_next():
            if self.var_mode.get() == "remote":
                self.cfg.remote_url = self.var_url.get().strip()
                self.cfg.api_key = self.var_key.get().strip()
                if self._whoami_data:
                    self.cfg.rl_player_name = self.cfg.rl_player_name or self._whoami_data.get("display_name", "")
                    self.cfg.rl_player_primary_id = self._whoami_data.get("primary_id", "")
            else:
                self.cfg.remote_url = ""
                self.cfg.api_key = ""
            self._goto(3)

        # Local mode: Next is always available. Remote: only after whoami succeeds.
        allow_next = (not is_remote) or bool(self._whoami_data)
        self._footer(on_back=lambda: self._goto(1),
                     on_next=goto_next if allow_next else None)

    def _test_connection(self, btn: ttk.Button, status: ttk.Label) -> None:
        url = self.var_url.get().strip()
        key = self.var_key.get().strip()
        if not url or not key:
            status.config(text="Fill in both fields first.", foreground="#a00")
            return
        btn.config(state="disabled"); status.config(text="Testing…", foreground="#666")

        def worker():
            try:
                data = _whoami(url, key)
            except Exception as e:
                self.root.after(0, lambda: (
                    btn.config(state="normal"),
                    status.config(text=str(e), foreground="#a00"),
                ))
                return

            def on_ok():
                self._whoami_data = data
                btn.config(state="normal")
                status.config(
                    text=f"OK — you are {data.get('display_name')} ({data.get('primary_id')})",
                    foreground="#070",
                )
                self._render()  # re-render so Next button becomes enabled
            self.root.after(0, on_ok)

        threading.Thread(target=worker, daemon=True).start()

    # ---- step 3: identity ------------------------------------------------

    def _render_identity(self) -> None:
        # Auto-detect the local account once so the user doesn't have to type it.
        # We key on the stable PrimaryId, not the typo-prone name. (Remote mode
        # already filled primary_id from the server's whoami, so this is a no-op
        # there.)
        if not self.cfg.rl_player_primary_id and not getattr(self, "_id_detected", False):
            self._id_detected = True
            try:
                from .identity import detect_local_identity
                det = detect_local_identity()
            except Exception:
                det = None
            if det:
                self.cfg.rl_player_primary_id = det.get("primary_id", "")
                if not self.cfg.rl_player_name:
                    self.cfg.rl_player_name = det.get("name", "")

        detected = bool(self.cfg.rl_player_primary_id)
        sub = ("We detected your account automatically — just confirm. You're matched "
               "by your account ID, so a name or clan-tag change never unlinks your stats."
               if detected else
               "Type it exactly as it appears in Rocket League — that's how we match "
               "your stats.")
        self._header("Your in-game name", sub)
        body = ttk.Frame(self.root); body.pack(fill="x", expand=False, padx=24)
        ttk.Label(body, text="In-game name").grid(row=0, column=0, sticky="w", pady=(8, 2))
        self.var_name = tk.StringVar(value=self.cfg.rl_player_name)
        ttk.Entry(body, textvariable=self.var_name, width=58).grid(row=1, column=0, sticky="we")

        if self.cfg.rl_player_primary_id:
            ttk.Label(body, text=f"Detected account: {self.cfg.rl_player_primary_id}",
                      foreground="#070", font=("Segoe UI", 9)).grid(
                row=2, column=0, sticky="w", pady=(10, 0))
            ttk.Label(body, text="We'll confirm your exact name from your first match.",
                      foreground="#666", font=("Segoe UI", 9)).grid(
                row=3, column=0, sticky="w", pady=(2, 0))

        def goto_next():
            self.cfg.rl_player_name = self.var_name.get().strip()
            # Proceed if we have EITHER a detected account or a typed name.
            if not self.cfg.rl_player_name and not self.cfg.rl_player_primary_id:
                messagebox.showwarning("Chumstats", "Enter your in-game name to continue.")
                return
            self._goto(4)

        self._footer(on_back=lambda: self._goto(2), on_next=goto_next)

    # ---- step 4: RL stats API --------------------------------------------

    def _render_rl_setup(self) -> None:
        self._header("Enable Rocket League's Stats API",
                     "This writes a small change to your RL config (PacketSendRate=30) so "
                     "chumstats can read your match data while you play.")
        body = ttk.Frame(self.root); body.pack(fill="x", expand=False, padx=24)

        status = ttk.Label(body, text="", foreground="#666", wraplength=WINDOW_W - 48, justify="left")
        run_btn = ttk.Button(body, text="Enable Stats API",
                             command=lambda: self._run_rl_setup(run_btn, status, finish_btn))
        run_btn.pack(anchor="w", pady=(8, 6))
        status.pack(anchor="w")

        skip_lbl = ttk.Label(body, text=(
            "Already enabled it? Click Skip below."), foreground="#999",
            font=("Segoe UI", 9))
        skip_lbl.pack(anchor="w", pady=(20, 0))

        self.var_autostart = tk.BooleanVar(value=True)
        ttk.Checkbutton(body, text="Start Chumstats automatically when Windows starts",
                        variable=self.var_autostart).pack(anchor="w", pady=(18, 0))

        bar = ttk.Frame(self.root); bar.pack(side="bottom", fill="x", padx=24, pady=14)
        ttk.Label(bar, text=f"Step 4 of 4").pack(side="left")
        finish_btn = ttk.Button(bar, text="Finish", command=self._finish)
        finish_btn.pack(side="right")
        ttk.Button(bar, text="Back", command=lambda: self._goto(3)).pack(side="right", padx=(0, 6))
        ttk.Button(bar, text="Skip", command=self._finish).pack(side="right", padx=(0, 6))

    def _run_rl_setup(self, run_btn, status, finish_btn) -> None:
        run_btn.config(state="disabled"); status.config(text="Detecting Rocket League install…", foreground="#666")

        def worker():
            try:
                from .config_wizard import run_wizard
                rep = run_wizard(enable=True, rate=30)
            except Exception as e:
                self.root.after(0, lambda: (
                    run_btn.config(state="normal"),
                    status.config(text=f"Error: {e}", foreground="#a00"),
                ))
                return

            def on_done():
                run_btn.config(state="normal")
                if rep.error:
                    status.config(text=f"Error: {rep.error}", foreground="#a00")
                    return
                self.cfg.rl_setup_done = True
                txt = "OK — Stats API enabled."
                if rep.rl_running:
                    txt += " Restart Rocket League for the change to apply."
                status.config(text=txt, foreground="#070")
            self.root.after(0, on_done)

        threading.Thread(target=worker, daemon=True).start()

    # ---- nav -------------------------------------------------------------

    def _goto(self, step: int) -> None:
        self._step = step
        self._render()

    def _finish(self) -> None:
        try:
            if getattr(self, "var_autostart", None) is not None:
                autostart.set_enabled(bool(self.var_autostart.get()))
                log.info("autostart set to %s", self.var_autostart.get())
        except Exception:
            log.exception("failed to apply autostart preference")
        save(self.cfg)
        log.info("wizard finished; config saved to %s", "config.json")
        if self.on_complete:
            try:
                self.on_complete(self.cfg)
            except Exception:
                log.exception("on_complete callback failed")
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


class SettingsDialog:
    """Single-page editor for the same fields the wizard collects. Used by the
    tray's right-click menu so friends can change their API key / server URL
    without re-running the full wizard."""

    def __init__(self, on_saved: Callable[[TrayConfig], None] | None = None) -> None:
        self.cfg = load()
        self.on_saved = on_saved
        self.root = tk.Tk()
        self.root.title("Chumstats — Settings")
        self.root.geometry(f"{WINDOW_W}x{WINDOW_H - 60}")
        self.root.resizable(False, False)
        self._build()

    def _build(self) -> None:
        ttk.Label(self.root, text="Settings", font=("Segoe UI", 14, "bold")).pack(
            anchor="w", padx=24, pady=(18, 4))
        ttk.Label(self.root, text="Changes take effect after the tracker restarts.",
                  foreground="#666", font=("Segoe UI", 9)).pack(anchor="w", padx=24)

        body = ttk.Frame(self.root); body.pack(fill="x", padx=24, pady=(12, 0))

        ttk.Label(body, text="Server URL").grid(row=0, column=0, sticky="w", pady=(8, 2))
        self.var_url = tk.StringVar(value=self.cfg.remote_url)
        ttk.Entry(body, textvariable=self.var_url, width=58).grid(row=1, column=0, sticky="we")

        ttk.Label(body, text="API key").grid(row=2, column=0, sticky="w", pady=(12, 2))
        self.var_key = tk.StringVar(value=self.cfg.api_key)
        ttk.Entry(body, textvariable=self.var_key, width=58, show="•").grid(row=3, column=0, sticky="we")

        ttk.Label(body, text="In-game name").grid(row=4, column=0, sticky="w", pady=(12, 2))
        self.var_name = tk.StringVar(value=self.cfg.rl_player_name)
        ttk.Entry(body, textvariable=self.var_name, width=58).grid(row=5, column=0, sticky="we")

        self.status = ttk.Label(body, text="", foreground="#666", wraplength=WINDOW_W - 48)
        self.status.grid(row=6, column=0, sticky="w", pady=(14, 0))

        test_btn = ttk.Button(body, text="Test connection",
                              command=lambda: self._test(test_btn))
        test_btn.grid(row=7, column=0, sticky="w", pady=(8, 0))

        bar = ttk.Frame(self.root); bar.pack(side="bottom", fill="x", padx=24, pady=14)
        ttk.Button(bar, text="Cancel", command=self.root.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(bar, text="Save",   command=self._save).pack(side="right")

    def _test(self, btn) -> None:
        url, key = self.var_url.get().strip(), self.var_key.get().strip()
        if not (url and key):
            self.status.config(text="Fill in URL and API key first.", foreground="#a00"); return
        btn.config(state="disabled"); self.status.config(text="Testing…", foreground="#666")

        def worker():
            try:
                data = _whoami(url, key)
                self.root.after(0, lambda: (
                    btn.config(state="normal"),
                    self.status.config(
                        text=f"OK — {data.get('display_name')} ({data.get('primary_id')})",
                        foreground="#070"),
                ))
            except Exception as e:
                self.root.after(0, lambda: (
                    btn.config(state="normal"),
                    self.status.config(text=str(e), foreground="#a00"),
                ))
        threading.Thread(target=worker, daemon=True).start()

    def _save(self) -> None:
        self.cfg.remote_url = self.var_url.get().strip()
        self.cfg.api_key = self.var_key.get().strip()
        self.cfg.rl_player_name = self.var_name.get().strip()
        save(self.cfg)
        if self.on_saved:
            try:
                self.on_saved(self.cfg)
            except Exception:
                log.exception("on_saved callback failed")
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def maybe_run_wizard(on_complete: Callable[[TrayConfig], None] | None = None) -> TrayConfig:
    """Open the wizard if the saved config isn't complete. Returns the (possibly
    just-saved) config. Blocks until the user closes the wizard."""
    cfg = load()
    if cfg.is_configured:
        return cfg
    app = WizardApp(on_complete=on_complete)
    app.run()
    return load()
