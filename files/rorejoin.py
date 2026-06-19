"""RoRejoin — multi-account Roblox crash watchdog + auto-rejoiner (entry point).

What it does
  - Holds the ROBLOX_singletonEvent mutex so multiple Roblox clients can run
    at once (multi-instance).
  - Stores one or more accounts by their .ROBLOSECURITY cookie. Cookies are
    encrypted on disk with Windows DPAPI (tied to your Windows user — a copied
    config file is useless on any other machine/account).
  - Each account can have its OWN place and its OWN rejoin delay, or fall back
    to the global defaults.
  - Launches each selected account itself via Roblox's authentication-ticket
    flow. Crashed accounts always rejoin into a fresh server.
  - Live per-account dashboard: state, uptime, crash count, last crash.
  - Optional Discord webhook notifications (sends usernames + events only —
    NEVER cookies), auto-kill / sync-kill / kill-now controls, kick &
    wrong-game detection, and a different-servers spreader.

Project layout (all in this one folder):
    rorejoin.py    – this entry point
    rr_const.py    – constants, Win32 handles, paths
    rr_theme.py    – colour palette + formatting helpers
    rr_storage.py  – config / session / bot-bridge persistence, DPAPI
    rr_windows.py  – process & window management, screenshots
    rr_roblox.py   – Roblox web API (auth, presence, launch, servers)
    rr_discord.py  – Discord webhook notifications
    rr_widgets.py  – custom widgets (iOS toggle)
    rr_app.py      – the App window + watcher worker
    bot.py         – optional standalone Discord control bot

Windows only. Build into a SINGLE .exe with (PyInstaller follows the imports
and bundles every rr_*.py module into the one file automatically — just keep
all the .py files together in this folder when you build):

    pip install customtkinter pyinstaller
    python -m PyInstaller --onefile --windowed --collect-all customtkinter --name RoRejoin rorejoin.py

SECURITY: never share your config folder or commit it anywhere. Anyone who
gets a raw cookie owns that account. Grab cookies from your own browser:
DevTools (F12) -> Application -> Cookies -> .ROBLOSECURITY.
"""

from __future__ import annotations

import os
import sys

# When frozen by PyInstaller (--onefile) the bundled modules live next to this
# script inside the unpacked temp dir; when running from source they're in this
# file's folder. Make sure that folder is importable either way.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import customtkinter as ctk

from rr_const import APP_NAME, IS_WINDOWS, ROBLOX_EXE
from rr_storage import acquire_multi_instance
from rr_app import App


def main() -> None:
    if not IS_WINDOWS:
        print(f"{APP_NAME} is Windows-only (it watches {ROBLOX_EXE}).")
        sys.exit(1)
    handles = acquire_multi_instance()
    ctk.set_appearance_mode("dark")
    App(handles).mainloop()


if __name__ == "__main__":
    main()
