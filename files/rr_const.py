"""RoRejoin — shared constants, platform handles, and filesystem paths.

Everything that the other modules need to agree on lives here: app metadata,
timing/tuning constants, Roblox endpoint URLs, the Windows API handles (with
their ctypes signatures configured once at import), and the on-disk paths used
for config and the Discord-bot bridge.
"""

from __future__ import annotations

import ctypes
import os
import re
import sys
from ctypes import wintypes
from pathlib import Path

APP_NAME = "RoRejoin"
ROBLOX_EXE = "RobloxPlayerBeta.exe"
POLL_SECONDS = 2.0
LAUNCH_STAGGER = 8          # seconds between sequential account launches
# different-servers collision detection cadence
PRESENCE_INTERVAL = 3.0    # how often to re-check which server each acct is in
PRESENCE_SETTLE = 6.0      # grace after a (re)join before an acct is checked
                           # (lets Roblox presence catch up so we don't act on
                           # a stale server and double-kick)
# error-264 avoidance: after a kill, wait until Roblox stops reporting the
# account in-game before relaunching (a fresh login while the old session is
# still alive server-side triggers "Disconnected (error 264)")
LEAVE_POLL = 2.5           # how often to re-check whether a killed acct has left
LEAVE_MAX_WAIT = 40.0      # …but relaunch anyway after this, so we never hang
# kick detection: process alive but no longer in-game = likely kicked
KICK_GRACE = 10.0          # not-in-game must persist this long to count as a kick
KICK_POLL = 4.0            # how often to poll presence for kick detection
CREATE_NO_WINDOW = 0x08000000
DEFAULT_SERVER_CAP = 50    # assumed max players when the API omits capacity

AUTH_TICKET_URL = "https://auth.roblox.com/v1/authentication-ticket/"
USERS_AUTH_URL = "https://users.roblox.com/v1/users/authenticated"
SHARE_RESOLVE_URL = "https://apis.roblox.com/sharelinks/v1/resolve-link"
UA = "Roblox/WinInet"
DISCORD_HOSTS = ("discord.com", "discordapp.com", "canary.discord.com",
                 "ptb.discord.com")

IS_WINDOWS = sys.platform == "win32"
user32 = ctypes.windll.user32 if IS_WINDOWS else None
kernel32 = ctypes.windll.kernel32 if IS_WINDOWS else None
gdi32 = ctypes.windll.gdi32 if IS_WINDOWS else None
if user32 is not None:
    c_vp, c_int = ctypes.c_void_p, ctypes.c_int
    user32.FindWindowW.restype = c_vp
    user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    user32.IsWindowVisible.argtypes = [c_vp]
    user32.GetWindowTextLengthW.argtypes = [c_vp]
    user32.GetWindowThreadProcessId.argtypes = [c_vp,
                                                ctypes.POINTER(ctypes.c_ulong)]
    user32.ShowWindow.argtypes = [c_vp, c_int]
    user32.SetForegroundWindow.argtypes = [c_vp]
    user32.BringWindowToTop.argtypes = [c_vp]
    user32.GetParent.restype = c_vp
    user32.GetParent.argtypes = [c_vp]
    # --- capture-related (handles MUST be void_p so 64-bit isn't truncated) ---
    user32.GetWindowDC.restype = c_vp
    user32.GetWindowDC.argtypes = [c_vp]
    user32.GetDC.restype = c_vp
    user32.GetDC.argtypes = [c_vp]
    user32.ReleaseDC.argtypes = [c_vp, c_vp]
    user32.ReleaseDC.restype = c_int
    user32.GetWindowRect.argtypes = [c_vp, ctypes.POINTER(wintypes.RECT)]
    user32.PrintWindow.argtypes = [c_vp, c_vp, wintypes.UINT]
    user32.PrintWindow.restype = c_int
    # --- window tiling ---
    user32.SetWindowPos.argtypes = [c_vp, c_vp, c_int, c_int, c_int, c_int,
                                    ctypes.c_uint]
    user32.SetWindowPos.restype = ctypes.c_int
    user32.ShowWindow.restype = ctypes.c_int
    user32.MoveWindow.argtypes = [c_vp, c_int, c_int, c_int, c_int, ctypes.c_int]
    user32.MoveWindow.restype = ctypes.c_int
    user32.SetWindowPlacement.argtypes = [c_vp, c_vp]
    user32.SetWindowPlacement.restype = ctypes.c_int
    user32.GetWindowPlacement.argtypes = [c_vp, c_vp]
    user32.GetWindowPlacement.restype = ctypes.c_int
    user32.IsZoomed.argtypes = [c_vp]
    user32.IsZoomed.restype = ctypes.c_int
    user32.SystemParametersInfoW.argtypes = [ctypes.c_uint, ctypes.c_uint,
                                             c_vp, ctypes.c_uint]
    user32.IsIconic.argtypes = [c_vp]
    gdi32.CreateCompatibleDC.restype = c_vp
    gdi32.CreateCompatibleDC.argtypes = [c_vp]
    gdi32.CreateCompatibleBitmap.restype = c_vp
    gdi32.CreateCompatibleBitmap.argtypes = [c_vp, c_int, c_int]
    gdi32.SelectObject.restype = c_vp
    gdi32.SelectObject.argtypes = [c_vp, c_vp]
    gdi32.BitBlt.argtypes = [c_vp, c_int, c_int, c_int, c_int, c_vp, c_int,
                             c_int, wintypes.DWORD]
    gdi32.BitBlt.restype = c_int
    gdi32.GetDIBits.argtypes = [c_vp, c_vp, ctypes.c_uint, ctypes.c_uint, c_vp,
                                c_vp, ctypes.c_uint]
    gdi32.GetDIBits.restype = c_int
    gdi32.DeleteObject.argtypes = [c_vp]
    gdi32.DeleteObject.restype = c_int
    gdi32.DeleteDC.argtypes = [c_vp]
    gdi32.DeleteDC.restype = c_int

CONFIG_PATH = (Path(os.environ.get("APPDATA", str(Path.home())))
               / APP_NAME / "config.json")
# Live map of which account is on which PID, for the Discord bot to read.
SESSION_PATH = CONFIG_PATH.parent / "sessions.json"
# Two-way bridge with the Discord bot (no secrets ever cross this):
#   state.json    RoRejoin -> bot   settings + account snapshot, ~1/sec
#   commands/     bot -> RoRejoin   one JSON file per command
#   results.json  RoRejoin -> bot   {cmd_id: {ok, message}} for confirmations
STATE_PATH = CONFIG_PATH.parent / "state.json"
CMD_DIR = CONFIG_PATH.parent / "commands"
RESULTS_PATH = CONFIG_PATH.parent / "results.json"

LOG_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "Roblox" / "logs"
JOIN_RE = re.compile(r"Joining game '([0-9a-fA-F-]{30,})' place (\d+)")
