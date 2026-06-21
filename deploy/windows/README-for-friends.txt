CHUMSTATS TRACKER
===============

What it is
----------
A small system-tray app that auto-uploads your Rocket League match stats
to a friend group's shared dashboard. No replay uploads, no third-party
trackers, no account signup -- everything runs locally and pushes only
match summaries (goals, saves, demos, etc.) to a server one of your
friends hosts.

Install
-------
1. Unzip this folder anywhere you like (Documents, Desktop, etc.).
2. Double-click Chumstats.exe.
3. The setup wizard opens. Paste the server URL and API key your friend
   sent you, choose your in-game name, and click "Enable Stats API"
   when prompted (writes one line to your Rocket League config).
4. Done. A circle icon appears in your system tray. Right-click for
   options. Play normally and matches auto-upload.

Tray icon colors (hover the icon for the exact state)
----------------
  red     not running yet (starting up, or crashed - check logs)
  yellow  running, waiting for Rocket League to open
  green   connected to Rocket League (in a match, or waiting for one)

Auto-start on login
-------------------
Press Win+R, type "shell:startup", press Enter. Drag a shortcut to
Chumstats.exe into that folder. Now it starts when you log in.

Changing settings later
-----------------------
Right-click the tray icon -> Settings. You can update the server URL,
API key, or in-game name without re-running the full setup.

Where data lives
----------------
Local DB:  %LOCALAPPDATA%\chumstats\chumstats.db
Config:    %LOCALAPPDATA%\chumstats\config.json
Logs:      %LOCALAPPDATA%\chumstats\logs\

To completely uninstall, quit the tray app and delete those files.

Troubleshooting
---------------
- Tray icon stays grey: open Logs folder (right-click -> Show Logs Folder)
  and look at tray.log for errors.
- "Connection refused" in the logs: Rocket League isn't running, or
  PacketSendRate isn't set in your RL config (re-run setup from the
  wizard).
- "Invalid API key" in tray.log: the key in Settings doesn't match what
  the server expects -- ask your friend for a fresh one.
