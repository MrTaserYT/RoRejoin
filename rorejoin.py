"""RoRejoin — multi-account Roblox crash watchdog + auto-rejoiner.

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
    NEVER cookies) and auto-kill / kill-now controls.

Windows only. Build into an .exe with:
    pip install customtkinter
    python -m PyInstaller --onefile --windowed --collect-all customtkinter --name RoRejoin rorejoin.py

SECURITY: never share your config folder or commit it anywhere. Anyone who
gets a raw cookie owns that account. Grab cookies from your own browser:
DevTools (F12) -> Application -> Cookies -> .ROBLOSECURITY.
"""

from __future__ import annotations

import base64
import concurrent.futures
import ctypes
import json
import math
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from ctypes import wintypes
from pathlib import Path

import customtkinter as ctk
import tkinter as tk

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

# ─────────────────────────────────── palette · iOS 18 dark · purple ──────
BG          = "#0A0A0C"
CARD        = "#141416"
CARD2       = "#1C1C1F"
FIELD       = "#1E1E22"
FIELD_HOVER = "#28282D"
BORDER      = "#2C2C32"
BORDER2     = "#3A3A42"
ACCENT      = "#7C3AED"
ACCENT_MID  = "#9B59FF"
ACCENT_SOFT = "#C4ACFF"
ACCENT_DARK = "#6D28D9"
TEXT        = "#F5F5F7"
TEXT2       = "#D1D1D6"
MUTED       = "#8E8E93"
GOOD        = "#34C759"
WARN        = "#FF9F0A"
BAD         = "#FF3B30"
KILL_BG       = "#2D1017"
KILL_BG_HOVER = "#3D1520"
ON_ACCENT   = "#FFFFFF"
SIDEBAR     = "#0E0E10"
NAV_ACTIVE  = "#7C3AED"
NAV_HOVER   = "#1C1C20"
SWITCH_OFF  = "#39393F"
SUBTLE      = "#111114"
PILL_FILL   = "#2A2A30"
TRACK       = "#0E0E10"


def lerp_hex(c1: str, c2: str, t: float) -> str:
    """Blend two #rrggbb colors; t in [0,1]."""
    t = max(0.0, min(1.0, t))
    a = tuple(int(c1[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(c2[i:i + 2], 16) for i in (1, 3, 5))
    m = tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))
    return f"#{m[0]:02x}{m[1]:02x}{m[2]:02x}"

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


def _atomic_write(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def read_remote_commands() -> list[dict]:
    """Pick up and delete pending command files dropped by the bot."""
    cmds = []
    try:
        files = sorted(CMD_DIR.glob("cmd_*.json"))
    except OSError:
        return cmds
    for f in files:
        try:
            cmds.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        try:
            f.unlink()
        except OSError:
            pass
    return cmds
LOG_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "Roblox" / "logs"
JOIN_RE = re.compile(r"Joining game '([0-9a-fA-F-]{30,})' place (\d+)")


def write_session_map(rt: dict) -> None:
    """Publish {username -> pid/place/state} so an external tool (the bot) can
    target a specific account's window. Never contains cookies or secrets."""
    data = []
    for st in rt.values():
        if not st.get("monitored", True):
            continue
        acc = st.get("acc", {})
        data.append({
            "username": acc.get("username"),
            "user_id": acc.get("user_id"),
            "pid": st.get("pid"),
            "place_id": acc.get("rplace"),
            "state": st.get("state"),
            "updated": time.time(),
        })
    try:
        SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
        SESSION_PATH.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


# ----------------------------------------------------------- DPAPI crypto --
class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob_out_to_bytes(blob: "_DataBlob") -> bytes:
    size = int(blob.cbData)
    buf = ctypes.create_string_buffer(size)
    ctypes.memmove(buf, blob.pbData, size)
    kernel32.LocalFree(blob.pbData)
    return buf.raw


def dpapi_encrypt(text: str) -> str:
    raw = text.encode("utf-8")
    src = ctypes.create_string_buffer(raw, len(raw))
    blob_in = _DataBlob(len(raw), ctypes.cast(src, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    return base64.b64encode(_blob_out_to_bytes(blob_out)).decode("ascii")


def dpapi_decrypt(b64: str) -> str:
    raw = base64.b64decode(b64)
    src = ctypes.create_string_buffer(raw, len(raw))
    blob_in = _DataBlob(len(raw), ctypes.cast(src, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    return _blob_out_to_bytes(blob_out).decode("utf-8")


# -------------------------------------------------------- multi-instance ---
def acquire_multi_instance() -> list:
    if kernel32 is None:
        return []
    handles = []
    for name in ("ROBLOX_singletonEvent", "ROBLOX_singletonMutex"):
        try:
            h = kernel32.CreateMutexW(None, True, name)
            if h:
                handles.append(h)
        except Exception:
            pass
    return handles


# ----------------------------------------------------------- proc helpers --
def roblox_pids() -> set[int]:
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", f"IMAGENAME eq {ROBLOX_EXE}", "/FO", "CSV", "/NH"],
            creationflags=CREATE_NO_WINDOW, stderr=subprocess.DEVNULL,
            encoding="utf-8", errors="ignore")
    except Exception:
        return set()
    pids = set()
    for line in out.splitlines():
        if line.lower().startswith(f'"{ROBLOX_EXE.lower()}"'):
            parts = [p.strip().strip('"') for p in line.split('","')]
            if len(parts) >= 2 and parts[1].isdigit():
                pids.add(int(parts[1]))
    return pids


def kill_pid(pid: int) -> bool:
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       creationflags=CREATE_NO_WINDOW,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def kill_all_roblox() -> None:
    try:
        subprocess.run(["taskkill", "/F", "/IM", ROBLOX_EXE],
                       creationflags=CREATE_NO_WINDOW,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def find_window_for_pid(pid: int):
    if user32 is None or not pid:
        return None
    matches = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _cb(h, _l):
        if user32.IsWindowVisible(h):
            owner = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(h, ctypes.byref(owner))
            if owner.value == pid and user32.GetWindowTextLengthW(h) > 0:
                matches.append(h)
        return True

    user32.EnumWindows(proto(_cb), 0)
    return matches[0] if matches else None


def find_all_windows_for_pid(pid: int) -> list:
    """All visible, titled, top-level windows owned by a pid (for dup detection)."""
    if user32 is None or not pid:
        return []
    matches = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _cb(h, _l):
        if user32.IsWindowVisible(h):
            owner = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(h, ctypes.byref(owner))
            if owner.value == pid and user32.GetWindowTextLengthW(h) > 0:
                # ignore tiny tool/utility windows; count real client windows
                r = wintypes.RECT()
                user32.GetWindowRect(h, ctypes.byref(r))
                if (r.right - r.left) > 200 and (r.bottom - r.top) > 200:
                    matches.append(h)
        return True

    user32.EnumWindows(proto(_cb), 0)
    return matches


def focus_window(hwnd) -> bool:
    if user32 is None or not hwnd:
        return False
    try:
        user32.keybd_event(0x12, 0, 0, 0)        # Alt down (unlock focus steal)
        user32.ShowWindow(hwnd, 9)               # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        user32.keybd_event(0x12, 0, 0x0002, 0)   # Alt up
        return True
    except Exception:
        return False


def get_work_area() -> tuple[int, int, int, int]:
    """Primary-monitor work area (screen minus taskbar)."""
    r = wintypes.RECT()
    if user32 is not None:
        user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)  # SPI_GETWORKAREA
    if r.right - r.left <= 0:                    # fallback to full virtual screen
        return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    return r.left, r.top, r.right, r.bottom


class _WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", wintypes.UINT),
                ("flags", wintypes.UINT),
                ("showCmd", wintypes.UINT),
                ("ptMinPosition", wintypes.POINT),
                ("ptMaxPosition", wintypes.POINT),
                ("rcNormalPosition", wintypes.RECT)]


def _place_window(hwnd, x: int, y: int, w: int, h: int) -> None:
    """Force a window to an exact rect, defeating maximize/snap-back.

    A maximized (or fullscreen-windowed) Roblox client ignores a plain
    SetWindowPos because Windows keeps re-applying the maximized rect. We first
    rewrite the window's *normal* placement via SetWindowPlacement (atomic: sets
    SW_SHOWNORMAL + the target rect), then follow with SetWindowPos/MoveWindow to
    nail the exact screen coordinates."""
    if user32 is None:
        return
    try:
        # 1) clear maximized state and set the normal rect in one shot
        wp = _WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(_WINDOWPLACEMENT)
        user32.GetWindowPlacement(hwnd, ctypes.byref(wp))
        wp.showCmd = 1                                   # SW_SHOWNORMAL
        wp.rcNormalPosition = wintypes.RECT(x, y, x + w, y + h)
        user32.SetWindowPlacement(hwnd, ctypes.byref(wp))
        # 2) pin exact screen coords (FRAMECHANGED forces the new frame to apply)
        flags = 0x0004 | 0x0010 | 0x0040 | 0x0020   # NOZORDER|NOACTIVATE|SHOW|FRAMECHANGED
        user32.SetWindowPos(hwnd, None, x, y, w, h, flags)
        # 3) belt-and-braces: MoveWindow repaints at the final rect
        user32.MoveWindow(hwnd, x, y, w, h, 1)
    except Exception:
        pass


def stack_windows(hwnds: list) -> None:
    """Cascade windows on top of each other (the 'stacked' layout)."""
    if user32 is None or not hwnds:
        return
    left, top, right, bottom = get_work_area()
    aw, ah = right - left, bottom - top
    w = int(aw * 0.62)
    h = int(ah * 0.72)
    step = 38
    for i, hwnd in enumerate(hwnds):
        x = left + 40 + (i * step)
        y = top + 30 + (i * step)
        if x + w > right:
            x = left + 40
        if y + h > bottom:
            y = top + 30
        _place_window(hwnd, x, y, w, h)


def grid_dims(n: int) -> tuple[int, int]:
    """cols, rows for n windows, biased to a landscape grid that fills the screen.
    1→1×1, 2→2×1, 3→3×1, 4→2×2, 5/6→3×2, 7/8→4×2, 9→3×3, 10→5×2, 12→4×3, 16→4×4."""
    table = {1: (1, 1), 2: (2, 1), 3: (3, 1), 4: (2, 2), 5: (3, 2), 6: (3, 2),
             7: (4, 2), 8: (4, 2), 9: (3, 3), 10: (5, 2), 11: (4, 3),
             12: (4, 3), 14: (5, 3), 15: (5, 3), 16: (4, 4)}
    if n in table:
        return table[n]
    if n <= 1:
        return 1, 1
    rows = int(math.floor(math.sqrt(n)))          # fewer rows than cols = landscape
    cols = int(math.ceil(n / rows))
    return cols, rows


def tile_windows(hwnds: list) -> None:
    """Lay the given windows out in a non-overlapping grid across the work area."""
    if user32 is None or not hwnds:
        return
    n = len(hwnds)
    cols, rows = grid_dims(n)
    left, top, right, bottom = get_work_area()
    cell_w = (right - left) // cols
    cell_h = (bottom - top) // rows
    if cell_w <= 0 or cell_h <= 0:
        return
    for i, hwnd in enumerate(hwnds):
        cx, cy = i % cols, i // cols
        x = left + cx * cell_w
        y = top + cy * cell_h
        _place_window(hwnd, x, y, cell_w, cell_h)


# ------------------------------------------------------- window capture ----
class _BMIHeader(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class _BMInfo(ctypes.Structure):
    _fields_ = [("bmiHeader", _BMIHeader), ("bmiColors", wintypes.DWORD * 3)]


def _grab_hwnd_image(hwnd, foreground: bool):
    """Capture a window to a PIL image. PrintWindow by default (no focus steal);
    foreground=True focuses it and grabs the screen region (reliable for DX)."""
    from PIL import Image  # lazy — only needed for the bot's screenshots

    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None

    if foreground:
        focus_window(hwnd)
        time.sleep(0.45)
        src_dc = user32.GetDC(None)          # whole-screen DC
        sx, sy = rect.left, rect.top
        release_target = None
    else:
        src_dc = user32.GetWindowDC(hwnd)
        sx, sy = 0, 0
        release_target = hwnd
    if not src_dc:
        return None

    mem_dc = gdi32.CreateCompatibleDC(src_dc)
    bmp = gdi32.CreateCompatibleBitmap(src_dc, w, h)
    old = gdi32.SelectObject(mem_dc, bmp)
    try:
        if foreground:
            gdi32.BitBlt(mem_dc, 0, 0, w, h, src_dc, sx, sy, 0x00CC0020)  # SRCCOPY
        else:
            # PW_RENDERFULLCONTENT (0x2) handles most modern/DWM windows
            if not user32.PrintWindow(hwnd, mem_dc, 0x00000002):
                user32.PrintWindow(hwnd, mem_dc, 0)

        bmi = _BMInfo()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BMIHeader)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h          # negative -> top-down rows
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0      # BI_RGB
        buf = (ctypes.c_char * (w * h * 4))()
        got = gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(bmi), 0)
        if not got:
            return None
        return Image.frombuffer("RGB", (w, h), bytes(buf), "raw", "BGRX", 0, 1)
    finally:
        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(release_target, src_dc)


def _is_mostly_black(img) -> bool:
    try:
        small = img.convert("L").resize((16, 16))
        px = list(small.getdata())
        return (sum(px) / len(px)) < 8
    except Exception:
        return False


def capture_account_png(pid: int, out_path: str, foreground: bool):
    """Returns (ok, error). error == 'BLACK' signals a DirectX black frame."""
    if not IS_WINDOWS or gdi32 is None:
        return False, "capture unavailable on this platform."
    hwnd = find_window_for_pid(pid)
    if not hwnd:
        return False, "no Roblox window found for that account."
    try:
        img = _grab_hwnd_image(hwnd, foreground)
    except ImportError:
        return False, "Pillow not installed (pip install pillow)."
    except Exception as e:
        return False, f"capture error: {e}"
    if img is None:
        return False, "capture failed (empty frame)."
    if not foreground and _is_mostly_black(img):
        return False, "BLACK"
    try:
        img.save(out_path, "PNG")
    except Exception as e:
        return False, f"could not save image: {e}"
    return True, ""


# --------------------------------------------------------- roblox web api --
def _post(url: str, cookie: str, csrf: str | None = None, body: bytes = b""):
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Cookie", f".ROBLOSECURITY={cookie}")
    req.add_header("Referer", "https://www.roblox.com/")
    req.add_header("Origin", "https://www.roblox.com")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", UA)
    if csrf:
        req.add_header("X-CSRF-TOKEN", csrf)
    return urllib.request.urlopen(req, timeout=15)


def get_account_info(cookie: str) -> tuple[int | None, str | None]:
    req = urllib.request.Request(USERS_AUTH_URL)
    req.add_header("Cookie", f".ROBLOSECURITY={cookie}")
    req.add_header("User-Agent", UA)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
            return data.get("id"), data.get("name")
    except Exception:
        return None, None


def get_csrf(cookie: str) -> str | None:
    try:
        resp = _post(AUTH_TICKET_URL, cookie)
        return resp.headers.get("x-csrf-token")
    except urllib.error.HTTPError as e:
        return e.headers.get("x-csrf-token")
    except Exception:
        return None


def get_auth_ticket(cookie: str) -> str | None:
    csrf = get_csrf(cookie)
    if not csrf:
        return None
    try:
        resp = _post(AUTH_TICKET_URL, cookie, csrf)
        return resp.headers.get("rbx-authentication-ticket")
    except urllib.error.HTTPError as e:
        return e.headers.get("rbx-authentication-ticket")
    except Exception:
        return None


def build_launch_uri(ticket: str, place_id: str, instance_id: str | None = None,
                     browser_tracker_id: int | None = None) -> str:
    """Build the roblox-player launch URI, matching the exact format used by
    Roblox Account Manager (the widely-working multi-account tool). Joining a
    specific server uses request=RequestGameJob with &gameId=<jobId>; open
    matchmaking uses request=RequestGame. The trailing +channel: field and the
    isPlayTogetherGame flag are intentionally omitted to match RAM."""
    if instance_id:
        # join one SPECIFIC server instance
        pl = ("https://assetgame.roblox.com/game/PlaceLauncher.ashx"
              f"?request=RequestGameJob&placeId={place_id}&gameId={instance_id}")
    else:
        # RequestGame -> Roblox drops you into an open server (matchmaking)
        pl = ("https://assetgame.roblox.com/game/PlaceLauncher.ashx"
              f"?request=RequestGame&placeId={place_id}&isPlayTogetherGame=false")
    pl_enc = urllib.parse.quote(pl, safe="")
    launch_ms = int(time.time() * 1000)
    btid = browser_tracker_id or random.randint(100_000_000_000, 999_999_999_999)
    return (f"roblox-player:1+launchmode:play+gameinfo:{ticket}"
            f"+launchtime:{launch_ms}+browsertrackerid:{btid}"
            f"+placelauncherurl:{pl_enc}"
            f"+robloxLocale:en_us+gameLocale:en_us")


def parse_place_id(raw: str) -> str | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    m = re.search(r"/games/(\d+)", raw)
    if m:
        return m.group(1)
    return raw if raw.isdigit() else None


def parse_share_link(raw: str) -> tuple[str | None, str | None]:
    """Pull (code, link_type) out of a Roblox share URL (or a bare code).
    e.g. https://www.roblox.com/share?code=ABC&type=ExperienceInvite."""
    raw = (raw or "").strip()
    if not raw:
        return None, None
    try:
        u = urllib.parse.urlparse(raw)
        if u.query:
            qs = urllib.parse.parse_qs(u.query)
            code = (qs.get("code") or [None])[0]
            ltype = (qs.get("type") or ["ExperienceInvite"])[0]
            if code:
                return code, ltype
    except Exception:
        pass
    if re.fullmatch(r"[A-Za-z0-9_\-]{10,}", raw):   # looks like a bare code
        return raw, "ExperienceInvite"
    return None, None


def _find_first(blob, keys: tuple):
    """Depth-first search a nested dict/list for the first present key."""
    stack = [blob]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k in keys:
                v = cur.get(k)
                if v not in (None, "", 0):
                    return v
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def resolve_share_link(cookie: str, code: str, link_type: str
                       ) -> tuple[str | None, str | None]:
    """Resolve a share link to (place_id, instance_id). instance_id is the
    specific server when the invite points at one, else None (open server).
    Returns (None, None) if it can't be resolved."""
    payload = json.dumps({"linkId": code,
                          "linkType": link_type or "ExperienceInvite"}).encode()

    def _try(token):
        resp = _post(SHARE_RESOLVE_URL, cookie, token, payload)
        return json.loads(resp.read().decode("utf-8", "ignore"))

    csrf = get_csrf(cookie)
    try:
        data = _try(csrf)
    except urllib.error.HTTPError as e:
        new = e.headers.get("x-csrf-token")
        if not new:
            return None, None
        try:
            data = _try(new)
        except Exception:
            return None, None
    except Exception:
        return None, None
    place = _find_first(data, ("placeId", "rootPlaceId"))
    inst = _find_first(data, ("gameInstanceId", "instanceId", "gameId",
                              "placeJobId", "jobId"))
    if place is None:
        return None, None
    return str(place), (str(inst) if inst else None)


def fetch_public_servers(place_id: str, cookie: str | None = None,
                         want: int = 50, min_free: int = 4, log=None) -> list[str]:
    """Return a list of job IDs (server instance IDs) for public running
    servers of a place, ordered lowest-ping first (with room).

    Only servers with room are kept. Note: Roblox's listed ping is sometimes
    inaccurate, so the ordering is best-effort. Returns [] on any error.
    """
    base = (f"https://games.roblox.com/v1/games/{place_id}/servers/Public"
            f"?sortOrder=Desc&limit=100")
    # collect (ping, free_slots, jid) for every server that has room
    rated: list[tuple[float, int, str]] = []   # has a real ping value
    unrated: list[tuple[int, str]] = []        # no ping data → order by room
    cursor = ""
    pages = 0
    total_seen = 0
    rej_full = 0
    try:
        while pages < 6 and (len(rated) + len(unrated)) < want:
            url = base + (f"&cursor={cursor}" if cursor else "")
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            if cookie:
                req.add_header("Cookie", f".ROBLOSECURITY={cookie}")
            with urllib.request.urlopen(req, timeout=12) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
            rows = data.get("data", [])
            total_seen += len(rows)
            for s in rows:
                jid = s.get("id")
                if not jid:
                    continue
                # 'playing' is NOT the live count on this endpoint (it equals
                # maxPlayers on nearly every server); the real occupancy is the
                # length of 'playerTokens'. Fall back only if tokens are absent.
                mx = (s.get("maxPlayers") or s.get("maxPlayerCount")
                      or s.get("capacity") or 0)
                mx = int(mx or 0)
                if mx <= 0:
                    mx = DEFAULT_SERVER_CAP
                toks = s.get("playerTokens")
                if isinstance(toks, list):
                    pl = len(toks)
                else:
                    pl = s.get("playing")
                    if pl is None:
                        pl = s.get("playerCount") or 0
                pl = int(pl or 0)
                free = mx - pl
                # playerTokens is only a SAMPLE of connected players, so a small
                # count doesn't prove the server is open — but a count at/above
                # capacity definitely means full. Require a real gap so we don't
                # keep picking servers that are actually full and bounce the
                # join into matchmaking (which caused same-server collisions).
                if free < max(2, min_free):
                    rej_full += 1
                    continue
                ping = s.get("ping")
                # Roblox's listed ping is frequently bogus (we've seen 2000+ ms),
                # so treat absurd values as "unknown" rather than sorting on them.
                if isinstance(ping, (int, float)) and 0 < ping < 400:
                    rated.append((float(ping), free, str(jid)))
                else:
                    unrated.append((free, str(jid)))
            cursor = data.get("nextPageCursor") or ""
            pages += 1
            if not cursor:
                break
    except urllib.error.HTTPError as e:
        if log:
            log(f"server list HTTP {e.code} (the game may hide its server list, "
                f"or the cookie was rejected).")
    except Exception as e:
        if log:
            log(f"server list fetch failed: {type(e).__name__}.")

    # Order PRIMARILY by lowest ping. Among servers with a real ping value, keep
    # only ones with at least `min_free` slots when we have enough of them (so we
    # still avoid near-full servers that bounce joins into matchmaking), but
    # never sacrifice ping just to grab an emptier server — that was the bug that
    # produced 200 ms picks. Free slots is only a tiebreaker for equal ping.
    rated.sort(key=lambda t: (t[0], -t[1]))    # ping asc, then most-empty
    roomy_rated = [t for t in rated if t[1] >= min_free]
    use = roomy_rated if len(roomy_rated) >= want else rated
    unrated.sort(key=lambda t: -t[0])          # most-empty first (no ping info)
    result = [jid for _, _, jid in use] + [jid for _, jid in unrated]

    if log:
        if use:
            best = int(use[0][0]) if isinstance(use[0], tuple) and len(use[0]) == 3 else None
            worst = int(use[-1][0]) if isinstance(use[-1], tuple) and len(use[-1]) == 3 else None
            png = (f", ping {best}–{worst} ms" if best is not None else "")
        else:
            png = ""
        log(f"server list: saw {total_seen}, {len(result)} joinable "
            f"({len(roomy_rated)} roomy+rated, {rej_full} full){png}.")
    return result


def _interruptible_sleep(seconds: float, stop=None) -> bool:
    """Sleep in small slices, checking the stop callable between them. Returns
    True if a stop was requested (so callers can abort), False if it slept the
    full duration. Keeps network-probe pauses from blocking a Stop request."""
    end = time.time() + max(0.0, seconds)
    while time.time() < end:
        try:
            if stop and stop():
                return True
        except Exception:
            pass
        time.sleep(min(0.1, end - time.time()) if end > time.time() else 0)
    return bool(stop and stop()) if stop else False


def fetch_presence(user_ids: list[int], cookie: str) -> dict[int, str]:
    """Return {userId: gameId} for the accounts that are currently in-game.

    The presence API reports the ACTUAL server (gameId / jobId) each user is in
    — this is ground truth, unlike the server we *requested* at launch (Roblox
    can silently reroute a join into matchmaking). Used to detect when two
    accounts ended up in the same server. Best-effort: returns {} on failure.
    """
    out: dict[int, str] = {}
    ids = [int(u) for u in user_ids if u]
    if not ids:
        return out
    url = "https://presence.roblox.com/v1/presence/users"
    body = json.dumps({"userIds": ids}).encode()

    def _try(token):
        resp = _post(url, cookie, token, body)
        return json.loads(resp.read().decode("utf-8", "ignore"))

    data = None
    try:
        data = _try(None)
    except urllib.error.HTTPError as e:
        tok = e.headers.get("x-csrf-token")
        if tok:
            try:
                data = _try(tok)
            except Exception:
                return out
        else:
            return out
    except Exception:
        return out
    if not isinstance(data, dict):
        return out
    for p in data.get("userPresences", []):
        # userPresenceType 2 == InGame; gameId is the server instance (jobId)
        if p.get("userPresenceType") == 2:
            uid = p.get("userId")
            gid = p.get("gameId")
            if uid and gid:
                out[int(uid)] = str(gid)
    return out


def presence_detail(user_id: int, cookie: str) -> tuple[str, set[str], str | None]:
    """One-account presence with a definite state, which place it's in, AND the
    server (gameId/jobId):
        ("ingame", {placeId, rootPlaceId}, gameId)  – in a game right now
        ("out",    set(),                  None)     – online/away/offline
        ("unknown", set(),                 None)     – lookup failed; callers must
                    NOT treat this as 'left'/'wrong game'/'collision'.
    The place set holds both placeId and rootPlaceId (as strings) so callers can
    match a configured place whether it's a root or a sub-place. gameId is the
    server instance, used to detect two accounts sharing a server."""
    ids = [int(user_id)] if user_id else []
    if not ids:
        return "unknown", set(), None
    url = "https://presence.roblox.com/v1/presence/users"
    body = json.dumps({"userIds": ids}).encode()

    def _try(token):
        resp = _post(url, cookie, token, body)
        return json.loads(resp.read().decode("utf-8", "ignore"))

    data = None
    try:
        data = _try(None)
    except urllib.error.HTTPError as e:
        tok = e.headers.get("x-csrf-token")
        if not tok:
            return "unknown", set(), None
        try:
            data = _try(tok)
        except Exception:
            return "unknown", set(), None
    except Exception:
        return "unknown", set(), None
    if not isinstance(data, dict):
        return "unknown", set(), None
    for p in data.get("userPresences", []):
        if p.get("userId") and int(p["userId"]) == int(user_id):
            # 2 == InGame; anything else (0 offline, 1 online, 3 studio) = out
            if p.get("userPresenceType") != 2:
                return "out", set(), None
            places = {str(p[k]) for k in ("placeId", "rootPlaceId")
                      if p.get(k)}
            gid = p.get("gameId")
            return "ingame", places, (str(gid) if gid else None)
    return "out", set(), None


def presence_state(user_id: int, cookie: str) -> str:
    """Back-compat thin wrapper: just the state, ignoring place/server."""
    return presence_detail(user_id, cookie)[0]


def detect_last_place() -> str | None:
    try:
        files = sorted(LOG_DIR.glob("*.log"), key=lambda f: -f.stat().st_mtime)
    except OSError:
        return None
    for f in files[:12]:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        hits = JOIN_RE.findall(text)
        if hits:
            return hits[-1][1]
    return None


# -------------------------------------------------------------- discord ----
def _is_discord_webhook(url: str) -> bool:
    try:
        p = urllib.parse.urlparse(url.strip())
    except Exception:
        return False
    if p.scheme != "https" or p.netloc.lower() not in DISCORD_HOSTS:
        return False
    return "/api/webhooks/" in p.path


def discord_send(url: str, username: str, avatar: str,
                 content: str) -> tuple[bool, str]:
    """Synchronously POST a message to a Discord webhook.

    Only ever sends the supplied text (account usernames + events). Never
    touches cookies or any secret. Returns (ok, error_message).
    """
    url = (url or "").strip()
    if not _is_discord_webhook(url):
        return False, "Not a valid Discord webhook URL."
    payload: dict = {"content": content[:1900],
                     "allowed_mentions": {"parse": []}}
    if username.strip():
        payload["username"] = username.strip()[:80]
    if avatar.strip():
        payload["avatar_url"] = avatar.strip()
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "RoRejoin")
    try:
        urllib.request.urlopen(req, timeout=10).read()
        return True, ""
    except urllib.error.HTTPError as e:
        return False, f"Discord returned HTTP {e.code}."
    except Exception as e:
        return False, f"Send failed: {e}"


def discord_notify(url: str, username: str, avatar: str, content: str) -> None:
    """Fire-and-forget version for use inside the watcher loop."""
    if not url:
        return
    threading.Thread(target=discord_send,
                     args=(url, username, avatar, content), daemon=True).start()


# ---------------------------------------------------------------- format ---
def fmt_dur(secs: float) -> str:
    secs = int(max(0, secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def fmt_ago(ts: float | None) -> str:
    if not ts:
        return "none"
    d = int(max(0, time.time() - ts))
    if d < 10:
        return "just now"
    if d < 60:
        return f"{d}s ago"
    if d < 3600:
        return f"{d // 60}m ago"
    return f"{d // 3600}h{(d % 3600) // 60}m ago"


# ----------------------------------------------------------- config store --
def load_config() -> dict:
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    cfg.setdefault("accounts", [])
    cfg.setdefault("place", "")
    cfg.setdefault("delay", "60")
    cfg.setdefault("autokill_minutes", "20")
    cfg.setdefault("selected", "all")
    cfg.setdefault("discord", {})
    cfg.setdefault("tile_windows", False)
    cfg.setdefault("autokill_on", False)
    cfg.setdefault("synckill_on", False)
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError:
        pass


# ─────────────────────────────────── iOS-style animated toggle switch ──────
# ─── vsync-aware frame timer ──────────────────────────────────────────────
def _frame_ms() -> int:
    """Frame delay in ms. All animations run at 240 fps (~4 ms) for maximum
    smoothness on high-refresh displays; on 60/120 Hz panels the extra frames
    are simply coalesced by the compositor with no downside."""
    return 4

# ─────────────────────────────────── iOS-style animated toggle switch ──────
class _IosSwitch(ctk.CTkFrame):
    """iOS toggle: pill track + animated knob, both CTkFrame (anti-aliased).
    Uses relx for knob position - no int() truncation, smooth at any speed.
    Public API: .get()  .select()  .deselect()  .configure(...)
    """
    W, H  = 56, 32
    PAD   = 6          # visual gap at each end
    KNOB  = 22         # knob diameter
    DUR   = 0.40       # animation seconds (longer = smoother glide)

    # relx positions (centre of knob as fraction of track width)
    _OFF_REL = (PAD + KNOB / 2) / W       # ≈ 0.304
    _ON_REL  = 1.0 - (PAD + KNOB / 2) / W # ≈ 0.696

    def __init__(self, master, text="", command=None,
                 on_color=None, off_color=None, knob_color="#FFFFFF",
                 text_color=None, font=None, initial="off", **kw):
        super().__init__(master, fg_color="transparent", **kw)
        self._value     = initial
        self._command   = command
        self._on_color  = on_color  or ACCENT
        self._off_color = off_color or SWITCH_OFF
        self._knob_col  = knob_color
        self._anim_id   = 0
        self._rel_from  = 0.0
        self._rel_to    = 0.0
        self._rel_cur   = (self._ON_REL if initial == "on" else self._OFF_REL)
        self._t_start   = 0.0

        # Track = a CTkButton (its native `command` fires reliably on click —
        # this is the key to making the whole switch clickable). Text empty,
        # hover disabled so it reads as a pill, not a button.
        # bg_color is set so the square corners behind the rounded pill blend
        # into the parent surface instead of showing the default gray.
        track_col = self._on_color if initial == "on" else self._off_color
        self._track = ctk.CTkButton(
            self, text="", width=self.W, height=self.H,
            corner_radius=self.H // 2, fg_color=track_col, bg_color=CARD2,
            hover=False, border_width=0, command=self._on_click)
        self._track.pack(side="left")

        # Knob = another CTkButton on top of the track. Its bg_color is set to
        # the track colour so the square behind the round knob is invisible.
        self._knob = ctk.CTkButton(
            self._track, text="", width=self.KNOB, height=self.KNOB,
            corner_radius=self.KNOB // 2, fg_color=self._knob_col,
            bg_color=track_col, hover=False, border_width=0,
            command=self._on_click)
        self._knob.place(relx=self._rel_cur, rely=0.5, anchor="center")

        if text:
            lbl = ctk.CTkLabel(self, text=text,
                               text_color=text_color or TEXT, font=font)
            lbl.pack(side="left", padx=(10, 0))
            lbl.bind("<Button-1>", lambda e: self._on_click())
            self._lbl = lbl

    # ── API ───────────────────────────────────────────────────────────────
    def get(self):
        return self._value

    def _set_track_color(self, col):
        """Set track colour and keep the knob's bg_color in sync so no gray
        square ever shows behind the round knob."""
        try:
            self._track.configure(fg_color=col)
            self._knob.configure(bg_color=col)
        except Exception:
            pass

    def select(self):
        self._value = "on"
        self._rel_cur = self._ON_REL
        self._knob.place_configure(relx=self._ON_REL)
        self._set_track_color(self._on_color)

    def deselect(self):
        self._value = "off"
        self._rel_cur = self._OFF_REL
        self._knob.place_configure(relx=self._OFF_REL)
        self._set_track_color(self._off_color)

    def configure(self, **kw):
        if "progress_color" in kw:
            self._on_color = kw["progress_color"]
        if "fg_color" in kw:
            self._off_color = kw["fg_color"]
        if "button_color" in kw:
            self._knob_col = kw["button_color"]
            try: self._knob.configure(fg_color=self._knob_col)
            except Exception: pass
        self._set_track_color(
            self._on_color if self._value == "on" else self._off_color)

    # ── click ─────────────────────────────────────────────────────────────
    def _on_click(self, _e=None):
        self._value = "off" if self._value == "on" else "on"
        self._anim_id += 1
        tok = self._anim_id
        self._rel_from = self._rel_cur
        self._rel_to   = self._ON_REL if self._value == "on" else self._OFF_REL
        self._t_start  = time.time()
        # set the track colour once, immediately (cheap: one redraw, not 60/sec)
        self._set_track_color(
            self._on_color if self._value == "on" else self._off_color)
        self._step(tok)
        if self._command:
            try: self._command()
            except Exception: pass

    # ── animation (easeOutQuint — long, glassy glide, zero overshoot) ─────
    def _step(self, tok):
        if tok != self._anim_id:
            return
        raw = min(1.0, (time.time() - self._t_start) / self.DUR)
        p   = 1.0 - (1.0 - raw) ** 5          # easeOutQuint: fast then glides in
        self._rel_cur = self._rel_from + (self._rel_to - self._rel_from) * p
        # Move the knob by absolute pixel x (sub-pixel rounded by Tk). relx would
        # quantise to track-width fractions; pixel x gives finer steps.
        try:
            self._knob.place_configure(relx=self._rel_cur)
        except Exception:
            return
        if raw < 1.0:
            try:
                self.after(_frame_ms(), lambda: self._step(tok))
            except Exception:
                pass
        else:
            self._set_track_color(
                self._on_color if self._value == "on" else self._off_color)


# ------------------------------------------------------------------- app ---

class App(ctk.CTk):
    def __init__(self, mutex_handles: list):
        super().__init__(fg_color=BG)
        self.mutex_handles = mutex_handles
        self.title(f"{APP_NAME} — multi-account auto-rejoiner")
        self.geometry("1120x630")
        self.minsize(960, 540)

        # start invisible so we can fade the whole window in (safely falls back
        # to fully visible if the platform ignores -alpha)
        self._faded_in = False
        try:
            self.attributes("-alpha", 0.0)
        except Exception:
            self._faded_in = True

        self.cfg = load_config()
        # accounts: {user_id, username, cookie, place, delay}
        self.accounts: list[dict] = []
        self._decrypt_accounts()
        sel = self.cfg.get("selected", "all")
        self.select_all_flag = (sel == "all")
        self.selected_ids: set[int] = set() if self.select_all_flag else set(sel)
        # separate "close these" selection used by Kill Now, so a user can end
        # specific clients without touching the watch (left-column) selection
        self.kill_sel_vars: dict[int, "ctk.StringVar"] = {}
        self.kill_selected_ids: set[int] = set()

        # discord settings held in memory (url decrypted)
        dc = self.cfg.get("discord", {})
        self.discord_url = ""
        if dc.get("url_enc") and IS_WINDOWS:
            try:
                self.discord_url = dpapi_decrypt(dc["url_enc"])
            except Exception:
                self.discord_url = ""
        self.discord_username = dc.get("username", "RoRejoin")
        self.discord_avatar = dc.get("avatar", "")
        # live snapshot of discord settings, refreshed each pump tick on the main
        # thread so the worker only ever reads a plain dict (never touches widgets)
        self.discord_runtime = {"url": "", "name": "", "avatar": ""}

        self.ui_q: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.kill_now_event = threading.Event()
        # which accounts the next Kill Now targets; None = use watch selection
        self.kill_now_ids: set[int] | None = None
        # accounts the bot asked to rejoin (restart); the worker drains this set
        self._rejoin_requests: set[int] = set()

        # runtime snapshots
        self.acct_stats: dict[int, dict] = {}
        self.dash_rows: dict[int, dict] = {}
        self._last_dash_tick = 0.0
        # main thread publishes these every tick so the worker can add/drop
        # accounts live as checkboxes change (worker never touches widgets)
        self._live_resolved: dict[int, dict] = {}
        self._live_desired: set[int] = set()
        # non-blocking launches: the worker queues these and a SINGLE serialized
        # launcher thread runs them one-at-a-time, so the loop never freezes and
        # concurrent launches can't assign clients each other's PIDs
        self._launching: set[int] = set()
        self._launch_queue: queue.Queue = queue.Queue()
        self._launch_results: queue.Queue = queue.Queue()
        self._launcher_token = 0

        # auto-kill UI state (per-account cooldowns; toggle persisted)
        self.autokill_armed = bool(self.cfg.get("autokill_on", False))
        self.synckill_enabled = bool(self.cfg.get("synckill_on", False))
        self.sync_kill_deadline: float | None = None   # written by worker
        self.sync_cycle_start: float | None = None     # shared-timer cycle start
        self.tile_enabled = bool(self.cfg.get("tile_windows", False))
        # adopt already-open Roblox clients instead of launching new ones
        self.detect_open_enabled = bool(self.cfg.get("detect_open", False))
        self.kickdetect_enabled = bool(self.cfg.get("kickdetect_on", False))
        # join-server-from-share-link
        self.joinserver_enabled = bool(self.cfg.get("joinserver_on", False))
        self._joinserver_url = self.cfg.get("joinserver_url", "")
        self._join_cache = {"url": "", "place": None, "instance": None, "err": None}
        # different-servers: spread accounts across distinct public servers
        self.diffserver_enabled = bool(self.cfg.get("diffserver_on", False))
        self._diffserver_pool: list[str] = []   # available job IDs (most-empty first)
        self._diffserver_assigned: dict[int, str] = {}  # uid -> jobId in use
        self._diffserver_place: str | None = None       # place the pool is for
        self._last_presence_check = 0.0
        self._last_kick_check = 0.0         # kick-detection poll throttle
        self._presence_snap = None          # per-cycle shared presence snapshot
        self._presence_snap_t = -1.0        # timestamp the snapshot was taken
        self._last_pids: set[int] = set()   # current Roblox pids (worker-updated)
        self._all_cookies: list[str] = []   # account cookies (full-check rotation)
        # bridge + animation timing
        self._last_state_pub = 0.0
        self._last_slow_refresh = 0.0    # throttle live-target/discord refresh
        # auto-maintenance timers (reset when their interval fires)
        self._last_log_clear = time.time()
        self._last_cache_clear = time.time()
        self._live_global_kill = 20
        self._cmd_results: dict[str, dict] = {}
        self._anim_t0 = time.time()
        self._crash_seen: dict[int, int] = {}
        self._flashes: dict[int, int] = {}
        self._remote_cmd_q: queue.Queue = queue.Queue()
        self._pill_anim_id = 0
        self._switch_flash_id = 0
        self._monitored_pids: list[int] = []   # worker publishes for live layout
        self._recent_log: list[str] = []        # rolling buffer for bot /log
        # log-box trimming: cap the on-screen Text widget so it never grows
        # without bound (which slowly lags the whole UI over a long session)
        self._log_lines = 0
        self._LOG_MAX = 600      # trim once it exceeds this many lines
        self._LOG_KEEP = 400     # ...back down to this many

        # per-account entry widget refs (rebuilt on render)
        self.sel_vars: dict[int, ctk.StringVar] = {}
        self.place_entries: dict[int, ctk.CTkEntry] = {}
        self.delay_entries: dict[int, ctk.CTkEntry] = {}
        self.kill_entries: dict[int, ctk.CTkEntry] = {}

        # fonts
        self.f_title = ctk.CTkFont("Segoe UI", 26, weight="bold")
        self.f_sub = ctk.CTkFont("Segoe UI", 12)
        self.f_section = ctk.CTkFont("Segoe UI", 11, weight="bold")
        self.f_base = ctk.CTkFont("Segoe UI", 13)
        self.f_small = ctk.CTkFont("Segoe UI", 12)
        self.f_btn = ctk.CTkFont("Segoe UI", 15, weight="bold")
        self.f_kill = ctk.CTkFont("Segoe UI", 13, weight="bold")
        self.f_mono = ctk.CTkFont("Consolas", 12)

        self._build_ui()
        self._refresh_account_list()
        self.after(50, self._style_titlebar)     # dark caption + rounded corners
        self.after(60, self._fade_in)
        self.after(900, self._ensure_visible)     # safety: never stay invisible
        self.after(100, self._pump)
        self.protocol("WM_DELETE_WINDOW", self._on_close)


    # ---------------------------------------------------------- accounts --
    def _decrypt_accounts(self) -> None:
        self.accounts = []
        for a in self.cfg.get("accounts", []):
            try:
                cookie = dpapi_decrypt(a["cookie_enc"]) if IS_WINDOWS else ""
            except Exception:
                cookie = ""
            self.accounts.append({
                "user_id": a.get("user_id"),
                "username": a.get("username", "unknown"),
                "cookie": cookie,
                # keep the original encrypted blob so an account whose cookie
                # couldn't be decrypted on this machine isn't silently dropped
                # when settings are next saved
                "cookie_enc": a.get("cookie_enc", ""),
                "place": a.get("place", ""),
                "delay": a.get("delay", ""),
                "killmin": a.get("killmin", ""),
            })

    def _sync_account_fields(self) -> None:
        """Pull current per-account entry values back into the account dicts."""
        for a in self.accounts:
            uid = a["user_id"]
            if uid in self.place_entries:
                try:
                    a["place"] = self.place_entries[uid].get().strip()
                    a["delay"] = self.delay_entries[uid].get().strip()
                    a["killmin"] = self.kill_entries[uid].get().strip()
                except Exception:
                    pass

    def _persist_accounts(self) -> None:
        self._sync_account_fields()
        self._write_accounts_to_cfg()
        save_config(self.cfg)

    def _selected_accounts(self) -> list[dict]:
        self._sync_account_fields()
        if self.select_all_flag:
            return [a for a in self.accounts if a.get("cookie")]
        return [a for a in self.accounts
                if a["user_id"] in self.selected_ids and a.get("cookie")]

    # ---------------------------------------------------------------- UI --
    def _section(self, parent, text: str) -> None:
        """iOS grouped-list uppercase section header."""
        ctk.CTkLabel(parent, text=text, font=self.f_section,
                     text_color=MUTED, anchor="w"
                     ).pack(fill="x", padx=2, pady=(18, 4))

    def _build_ui(self) -> None:
        self.configure(fg_color=BG)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._tab_names = ["Setup", "Accounts", "Monitor"]
        self._active_tab = "Setup"
        self._tab_frames: dict[str, ctk.CTkFrame] = {}
        self._tab_buttons: dict[str, ctk.CTkButton] = {}

        # ── outer container ──────────────────────────────────────────────
        root = ctk.CTkFrame(self, fg_color="transparent")
        root.grid(row=0, column=0, sticky="nsew", padx=12, pady=(12, 6))
        root.grid_columnconfigure(1, weight=1)
        root.grid_rowconfigure(0, weight=1)

        # ── sidebar ──────────────────────────────────────────────────────
        sidebar = ctk.CTkFrame(root, fg_color=SIDEBAR, corner_radius=20,
                               border_width=1, border_color=BORDER, width=196)
        sidebar.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        sidebar.grid_propagate(False)
        sidebar.grid_columnconfigure(0, weight=1)
        sidebar.grid_rowconfigure(3, weight=1)   # nav row expands

        # logo
        logo_frame = ctk.CTkFrame(sidebar, fg_color="transparent")
        logo_frame.grid(row=0, column=0, sticky="w", padx=18, pady=(22, 2))
        wm_font = ctk.CTkFont("Segoe UI", 22, weight="bold")
        self.wm_ro = ctk.CTkLabel(logo_frame, text="RO", font=wm_font,
                                  text_color=ACCENT_MID)
        self.wm_ro.pack(side="left")
        self.wm_rejoin = ctk.CTkLabel(logo_frame, text="REJOIN", font=wm_font,
                                      text_color=TEXT)
        self.wm_rejoin.pack(side="left")
        ctk.CTkLabel(sidebar, text="multi-account watchdog",
                     font=self.f_sub, text_color=MUTED
                     ).grid(row=1, column=0, sticky="w", padx=18, pady=(0, 20))

        # separator
        ctk.CTkFrame(sidebar, fg_color=BORDER, height=1, corner_radius=0
                     ).grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 14))

        # nav rail — a single tkinter Canvas that draws BOTH the sliding
        # purple highlight AND the tab text on one surface. This avoids all
        # CTk widget z-order conflicts (a CTk widget placed over a frame always
        # repaints its own background and hides whatever is behind it).
        nav = ctk.CTkFrame(sidebar, fg_color="transparent")
        nav.grid(row=3, column=0, sticky="new", padx=(6, 10))
        nav.grid_columnconfigure(0, weight=1)
        self._nav = nav

        self._nav_row_h   = 44          # height of each tab row
        self._nav_gap     = 6           # vertical gap between rows
        self._nav_pad_top = 8           # headroom so the spring overshoot at the
        self._nav_pad_bot = 8           # first/last tab doesn't clip the UI
        n_tabs = len(self._tab_names)
        rail_h = (self._nav_pad_top + self._nav_pad_bot
                  + self._nav_row_h * n_tabs + self._nav_gap * (n_tabs - 1))
        self._nav_canvas = tk.Canvas(
            nav, height=rail_h, highlightthickness=0, bd=0, bg=SIDEBAR)
        self._nav_canvas.grid(row=0, column=0, sticky="ew")
        self._nav_canvas.bind("<Button-1>", self._on_nav_click)
        self._nav_canvas.bind("<Configure>", lambda e: self._draw_nav())
        self._nav_canvas.bind("<Motion>", self._on_nav_motion)
        self._nav_canvas.bind("<Leave>", lambda e: self._on_nav_leave())

        self._nav_anim_id = 0
        init_i = self._tab_names.index(self._active_tab)
        self._nav_hl_y = float(self._nav_row_top(init_i))  # highlight top-y
        self._nav_hover_i = -1
        self.after(60, self._draw_nav)

        # status pill at bottom of sidebar
        ctk.CTkFrame(sidebar, fg_color=BORDER, height=1, corner_radius=0
                     ).grid(row=4, column=0, sticky="ew", padx=14, pady=(14, 10))
        status_card = ctk.CTkFrame(sidebar, fg_color=CARD2, corner_radius=14,
                                   border_width=1, border_color=BORDER2)
        status_card.grid(row=5, column=0, sticky="ew", padx=12, pady=(0, 16))
        status_card.grid_columnconfigure(1, weight=1)
        self.dot = ctk.CTkLabel(status_card, text="●", text_color=MUTED,
                                font=ctk.CTkFont(size=12))
        self.dot.grid(row=0, column=0, padx=(14, 6), pady=10, sticky="n")
        # wraplength lets long usernames wrap to multiple lines instead of being
        # clipped at the card edges (the sidebar is a fixed 196px wide).
        self.status_lbl = ctk.CTkLabel(
            status_card, text="Idle", font=self.f_small, text_color=TEXT,
            anchor="w", justify="left", wraplength=120)
        self.status_lbl.grid(row=0, column=1, padx=(0, 14), pady=10, sticky="w")

        # ── content card ─────────────────────────────────────────────────
        content = ctk.CTkFrame(root, fg_color=CARD, corner_radius=20,
                               border_width=1, border_color=BORDER)
        content.grid(row=0, column=1, sticky="nsew")
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(0, weight=1)

        for name in self._tab_names:
            f = ctk.CTkFrame(content, fg_color="transparent")
            f.grid(row=0, column=0, sticky="nsew", padx=20, pady=18)
            self._tab_frames[name] = f
        self._build_setup_tab(self._tab_frames["Setup"])
        self._build_accounts_tab(self._tab_frames["Accounts"])
        self._build_monitor_tab(self._tab_frames["Monitor"])
        for name in self._tab_names:
            if name != self._active_tab:
                self._tab_frames[name].grid_remove()

        # ── action buttons ───────────────────────────────────────────────
        act = ctk.CTkFrame(self, fg_color="transparent")
        act.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 4))
        act.grid_columnconfigure(0, weight=3)
        act.grid_columnconfigure(1, weight=1)
        self.start_btn = ctk.CTkButton(
            act, text="▶   START", height=52, corner_radius=16,
            font=self.f_btn, fg_color=ACCENT, hover_color=ACCENT_DARK,
            text_color=ON_ACCENT, command=self._toggle)
        self.start_btn.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.kill_now_btn = ctk.CTkButton(
            act, text="✕  KILL", height=52, corner_radius=16,
            font=self.f_kill, fg_color=KILL_BG, hover_color=KILL_BG_HOVER,
            text_color=BAD, command=self._kill_now)
        self.kill_now_btn.grid(row=0, column=1, sticky="ew")

        ctk.CTkLabel(self, text="RoRejoin launches your accounts itself.",
                     font=self.f_sub, text_color=MUTED
                     ).grid(row=2, column=0, pady=(0, 8))

        # click anywhere that isn't a text field → drop focus from the entry
        # being edited, so typing stops (like every other app)
        self.bind_all("<Button-1>", self._defocus_on_click, add="+")

    def _defocus_on_click(self, event) -> None:
        """If the click landed outside any text entry, move keyboard focus off
        the active entry so further keystrokes don't go into it."""
        w = getattr(event, "widget", None)
        # tkinter's Entry (which CTkEntry wraps) reports class 'Entry'/'TEntry'
        try:
            cls = w.winfo_class() if w is not None else ""
        except Exception:
            cls = ""
        if cls in ("Entry", "TEntry", "Text", "TCombobox"):
            return                      # clicked inside a text field → leave it
        # clicked elsewhere: if an entry currently holds focus, release it
        try:
            focused = self.focus_get()
        except Exception:
            focused = None
        if focused is not None:
            try:
                fcls = focused.winfo_class()
            except Exception:
                fcls = ""
            if fcls in ("Entry", "TEntry", "Text"):
                self.focus_set()        # park focus on the root window


    def _build_accounts_tab(self, tab) -> None:
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(2, weight=1)

        # top bar — row 0
        topbar = ctk.CTkFrame(tab, fg_color="transparent")
        topbar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        topbar.grid_columnconfigure(0, weight=1)
        self.allacct_var = ctk.StringVar(
            value="on" if self.select_all_flag else "off")
        ctk.CTkCheckBox(
            topbar, text="All accounts", variable=self.allacct_var,
            onvalue="on", offvalue="off", font=self.f_base, text_color=TEXT,
            fg_color=ACCENT, hover_color=ACCENT_DARK, border_color=BORDER2,
            checkbox_width=20, checkbox_height=20, command=self._toggle_all
            ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(topbar, text="＋  Add Account", width=148, height=36,
                      corner_radius=18, font=self.f_small, fg_color=ACCENT,
                      hover_color=ACCENT_DARK, text_color=ON_ACCENT,
                      command=self._add_account_dialog
                      ).grid(row=0, column=1, sticky="e")

        # column headers — row 1
        hdr = ctk.CTkFrame(tab, fg_color="transparent")
        hdr.grid(row=1, column=0, sticky="ew", padx=4, pady=(0, 2))
        ctk.CTkLabel(hdr, text="ACCOUNT", font=self.f_section,
                     text_color=MUTED, width=150, anchor="w").pack(side="left")
        ctk.CTkLabel(hdr, text="CLOSE", font=self.f_section,
                     text_color=MUTED, width=46, anchor="w").pack(side="left")
        ctk.CTkLabel(hdr, text="PLACE OVERRIDE", font=self.f_section,
                     text_color=MUTED, anchor="w").pack(side="left", padx=(8, 0))
        ctk.CTkLabel(hdr, text="KILL m", font=self.f_section, text_color=MUTED,
                     width=52, anchor="w").pack(side="right", padx=(0, 34))
        ctk.CTkLabel(hdr, text="REJOIN s", font=self.f_section, text_color=MUTED,
                     width=52, anchor="w").pack(side="right", padx=(0, 4))

        # account list — row 2 (expands)
        self.acct_list = ctk.CTkScrollableFrame(
            tab, fg_color=SUBTLE, corner_radius=14, border_width=1,
            border_color=BORDER)
        self.acct_list.grid(row=2, column=0, sticky="nsew", pady=(0, 4))

        # hint — row 3
        ctk.CTkLabel(tab, text="Blank override = use Setup defaults.",
                     font=self.f_sub, text_color=MUTED, anchor="w"
                     ).grid(row=3, column=0, sticky="w", pady=(4, 0))


    def _enable_scroll(self, scrollable, defer: bool = True) -> None:
        """Make the mouse wheel scroll a CTkScrollableFrame no matter which
        child widget the cursor is over.

        CTkScrollableFrame only binds the wheel to its own canvas, so when the
        cursor sits over a child (card, label, switch, entry) the wheel event
        goes to that child and scrolling appears to 'stick'. We recursively
        forward <MouseWheel> from every descendant to the frame's canvas.

        defer=True schedules two extra re-bind passes to catch lazily-created
        children (used at initial build). For dynamic rebuilds where every
        child already exists, pass defer=False to avoid stacking timers.
        """
        canvas = getattr(scrollable, "_parent_canvas", None)
        if canvas is None:
            return

        # Pixels to move per wheel notch. We scroll by an explicit pixel amount
        # via yview_moveto (computed against the content height), because a
        # tkinter Canvas "unit" is tiny (~1px) and varies — making
        # yview_scroll(..., "units") feel painfully slow. This is predictable
        # and tunable: bump PX_PER_NOTCH for faster scrolling.
        PX_PER_NOTCH = 90

        def _scroll_px(pixels: float):
            try:
                bbox = canvas.bbox("all")
                if not bbox:
                    return
                content_h = bbox[3] - bbox[1]
                if content_h <= 0:
                    return
                frac_delta = pixels / content_h
                new_top = canvas.yview()[0] + frac_delta
                new_top = max(0.0, min(1.0, new_top))
                canvas.yview_moveto(new_top)
            except Exception:
                pass

        def _on_wheel(event):
            # Windows / macOS: event.delta is a multiple of 120 per notch.
            # delta > 0 = wheel up = scroll toward the top (negative pixels).
            notches = -event.delta / 120
            _scroll_px(notches * PX_PER_NOTCH)
            return "break"

        def _on_wheel_linux(event, direction):
            _scroll_px(direction * PX_PER_NOTCH)
            return "break"

        # CTkScrollableFrame installs its OWN global <MouseWheel> handler
        # (self._mouse_wheel_all bound via bind_all) that scrolls by a tiny,
        # fixed number of "units". That runs independently of our per-widget
        # binding and our "break" can't stop it — so it was overriding our
        # speed. Replace that instance method with our pixel-based scroll so
        # there's a single, fast, consistent handler. Only act when the cursor
        # is over THIS frame's canvas (so the right tab scrolls).
        def _ctk_wheel_override(event):
            try:
                wx = canvas.winfo_rootx()
                wy = canvas.winfo_rooty()
                ww = canvas.winfo_width()
                wh = canvas.winfo_height()
                if (wx <= event.x_root <= wx + ww
                        and wy <= event.y_root <= wy + wh):
                    notches = -event.delta / 120
                    _scroll_px(notches * PX_PER_NOTCH)
            except Exception:
                pass
            return "break"
        try:
            scrollable._mouse_wheel_all = _ctk_wheel_override
        except Exception:
            pass

        def _bind(widget):
            try:
                widget.bind("<MouseWheel>", _on_wheel, add="+")
                # Linux uses Button-4 / Button-5 for wheel up/down
                widget.bind("<Button-4>",
                            lambda e: _on_wheel_linux(e, -1), add="+")
                widget.bind("<Button-5>",
                            lambda e: _on_wheel_linux(e, 1), add="+")
                cv = getattr(widget, "_canvas", None)
                if cv is not None:
                    cv.bind("<MouseWheel>", _on_wheel, add="+")
                    cv.bind("<Button-4>",
                            lambda e: _on_wheel_linux(e, -1), add="+")
                    cv.bind("<Button-5>",
                            lambda e: _on_wheel_linux(e, 1), add="+")
            except Exception:
                pass
            for child in widget.winfo_children():
                _bind(child)

        _bind(scrollable)
        if defer:
            # re-bind once layout settles to catch any lazily-created children
            self.after(200, lambda: _bind(scrollable))
            self.after(600, lambda: _bind(scrollable))

    def _build_setup_tab(self, tab) -> None:
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(0, weight=1)
        wrap = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        wrap.grid(row=0, column=0, sticky="nsew")
        wrap.grid_columnconfigure(0, weight=1)
        self._setup_scroll = wrap          # reference to the scrollable setup frame

        def sw(parent, text, cmd, **kw):
            """Create an animated iOS-style toggle using _IosSwitch."""
            return _IosSwitch(
                parent, text=text, command=cmd,
                on_color=ACCENT, off_color=SWITCH_OFF,
                knob_color=ON_ACCENT, text_color=TEXT,
                font=self.f_base, **kw)

        def card(parent, pady=(0, 10)):
            """iOS grouped card surface."""
            f = ctk.CTkFrame(parent, fg_color=CARD2, corner_radius=16,
                             border_width=1, border_color=BORDER2)
            f.pack(fill="x", pady=pady, padx=2)
            f.grid_columnconfigure(0, weight=1)
            return f

        def row(parent, pady=(10, 10)):
            f = ctk.CTkFrame(parent, fg_color="transparent")
            f.pack(fill="x", padx=16, pady=pady)
            return f

        def divider(parent):
            ctk.CTkFrame(parent, fg_color=BORDER, height=1, corner_radius=0
                         ).pack(fill="x", padx=16)

        # ── DEFAULT EXPERIENCE ────────────────────────────────────────────
        self._section(wrap, "DEFAULT EXPERIENCE")
        c = card(wrap)
        r = row(c)
        self.game_entry = ctk.CTkEntry(
            r, height=38, corner_radius=10, font=self.f_base, fg_color=FIELD,
            border_color=BORDER2, text_color=TEXT,
            placeholder_text="Game link or place ID",
            placeholder_text_color=MUTED)
        self.game_entry.pack(fill="x", expand=True, side="left")
        self.game_entry.insert(0, self.cfg.get("place", ""))
        divider(c)
        r2 = row(c)
        ctk.CTkButton(r2, text="Detect last played", width=148, height=32,
                      corner_radius=10, font=self.f_small, fg_color=FIELD,
                      hover_color=FIELD_HOVER, border_width=1,
                      border_color=BORDER2, text_color=ACCENT_SOFT,
                      command=self._detect_clicked).pack(side="left")
        self.detect_lbl = ctk.CTkLabel(r2, text="", font=self.f_small,
                                       text_color=MUTED)
        self.detect_lbl.pack(side="left", padx=12)

        # ── JOIN SERVER ───────────────────────────────────────────────────
        self._section(wrap, "JOIN SERVER")
        c = card(wrap)
        r = row(c)
        ctk.CTkLabel(r, text="Join from share link", font=self.f_base,
                     text_color=TEXT).pack(side="left", expand=True, anchor="w")
        self.joinserver_switch = sw(r, "", self._on_joinserver_toggle)
        self.joinserver_switch.pack(side="right")
        if self.joinserver_enabled:
            self.joinserver_switch.select()
        divider(c)
        r2 = row(c)
        self.joinserver_entry = ctk.CTkEntry(
            r2, height=36, corner_radius=10, font=self.f_mono, fg_color=FIELD,
            border_color=BORDER2, text_color=TEXT,
            placeholder_text="https://www.roblox.com/share?code=…",
            placeholder_text_color=MUTED)
        self.joinserver_entry.pack(fill="x", expand=True, side="left")
        if self._joinserver_url:
            self.joinserver_entry.insert(0, self._joinserver_url)
        divider(c)
        r3 = row(c)
        ctk.CTkButton(r3, text="Check link", width=110, height=32,
                      corner_radius=10, font=self.f_small, fg_color=FIELD,
                      hover_color=FIELD_HOVER, border_width=1,
                      border_color=BORDER2, text_color=ACCENT_SOFT,
                      command=self._check_joinserver).pack(side="left")
        self.joinserver_status = ctk.CTkLabel(r3, text="", font=self.f_small,
                                              text_color=MUTED)
        self.joinserver_status.pack(side="left", padx=12)

        # ── DIFFERENT SERVERS ─────────────────────────────────────────────
        self._section(wrap, "DIFFERENT SERVERS")
        c = card(wrap)
        r = row(c)
        ctk.CTkLabel(r, text="Different servers", font=self.f_base,
                     text_color=TEXT).pack(side="left", expand=True, anchor="w")
        self.diffserver_switch = sw(r, "", self._on_diffserver_toggle)
        self.diffserver_switch.pack(side="right")
        if self.diffserver_enabled:
            self.diffserver_switch.select()
        divider(c)
        ctk.CTkLabel(
            c, text="Put each account in its own server instead of letting "
            "them land together. Pulls the live public server list and gives "
            "every account a different one. (Can't be used with Join server.)",
            font=self.f_small, text_color=MUTED, justify="left",
            wraplength=560, anchor="w").pack(fill="x", padx=14, pady=(2, 12))

        # ── REJOIN DELAY ──────────────────────────────────────────────────
        self._section(wrap, "DEFAULT REJOIN DELAY")
        c = card(wrap)
        r = row(c)
        ctk.CTkLabel(r, text="Delay (seconds)", font=self.f_base,
                     text_color=TEXT).pack(side="left", expand=True, anchor="w")
        self.delay_entry = ctk.CTkEntry(
            r, width=68, height=34, corner_radius=10, font=self.f_base,
            fg_color=FIELD, border_color=BORDER2, text_color=TEXT,
            justify="center")
        self.delay_entry.pack(side="right")
        self.delay_entry.insert(0, str(self.cfg.get("delay", "60")))

        # ── LAUNCH ────────────────────────────────────────────────────────
        self._section(wrap, "LAUNCH")
        c = card(wrap)
        r = row(c)
        ctk.CTkLabel(r, text="Detect open clients", font=self.f_base,
                     text_color=TEXT).pack(side="left", expand=True, anchor="w")
        self.detect_open_switch = sw(r, "", self._on_detect_open_toggle)
        self.detect_open_switch.pack(side="right")
        if self.detect_open_enabled:
            self.detect_open_switch.select()
        divider(c)
        ctk.CTkLabel(
            c, text="Watch Roblox clients that are already open instead of "
            "launching new ones. Prevents the “client already running” errors "
            "you get when relaunching an account that's already in-game.",
            font=self.f_small, text_color=MUTED, justify="left",
            wraplength=560, anchor="w").pack(fill="x", padx=14, pady=(2, 12))

        # ── KICK DETECTION ────────────────────────────────────────────────
        self._section(wrap, "KICK DETECTION")
        c = card(wrap)
        r = row(c)
        ctk.CTkLabel(r, text="Detect kicks & rejoin", font=self.f_base,
                     text_color=TEXT).pack(side="left", expand=True, anchor="w")
        self.kickdetect_switch = sw(r, "", self._on_kickdetect_toggle)
        self.kickdetect_switch.pack(side="right")
        if self.kickdetect_enabled:
            self.kickdetect_switch.select()
        divider(c)
        ctk.CTkLabel(
            c, text="Watches whether each account is still in the correct game. "
            "If you get kicked or disconnected (the client stays open but you're "
            "no longer in-game), OR the account ends up in a different game than "
            "the one you set, RoRejoin closes it and rejoins the right game "
            "automatically. Works on its own — you don't need Auto Kill on.",
            font=self.f_small, text_color=MUTED, justify="left",
            wraplength=560, anchor="w").pack(fill="x", padx=14, pady=(2, 12))

        # ── WINDOW LAYOUT ─────────────────────────────────────────────────
        self._section(wrap, "WINDOW LAYOUT")
        c = card(wrap)
        r = row(c)
        ctk.CTkLabel(r, text="Tile windows in a grid", font=self.f_base,
                     text_color=TEXT).pack(side="left", expand=True, anchor="w")
        self.tile_switch = sw(r, "", self._on_tile_toggle)
        self.tile_switch.pack(side="right")
        if self.tile_enabled:
            self.tile_switch.select()

        # ── AUTO KILL ─────────────────────────────────────────────────────
        self._section(wrap, "AUTO KILL")
        c = card(wrap)
        r = row(c)
        ctk.CTkLabel(r, text="Auto Kill", font=self.f_base,
                     text_color=TEXT).pack(side="left", expand=True, anchor="w")
        self.autokill_switch = sw(r, "", self._on_autokill_toggle)
        self.autokill_switch.pack(side="right")
        if self.autokill_armed:
            self.autokill_switch.select()
        divider(c)
        r2 = row(c)
        ctk.CTkLabel(r2, text="Default cooldown (min)", font=self.f_base,
                     text_color=TEXT).pack(side="left", expand=True, anchor="w")
        self.kill_cd_entry = ctk.CTkEntry(
            r2, width=68, height=34, corner_radius=10, font=self.f_base,
            fg_color=FIELD, border_color=BORDER2, text_color=TEXT,
            justify="center")
        self.kill_cd_entry.pack(side="right")
        self.kill_cd_entry.insert(0, str(self.cfg.get("autokill_minutes", "20")))
        divider(c)
        r3 = row(c)
        ctk.CTkLabel(r3, text="Simultaneous kill (shared timer)",
                     font=self.f_base, text_color=TEXT
                     ).pack(side="left", expand=True, anchor="w")
        self.synckill_switch = sw(r3, "", self._on_synckill_toggle)
        self.synckill_switch.pack(side="right")
        if self.synckill_enabled:
            self.synckill_switch.select()

        # ── DISCORD WEBHOOK ───────────────────────────────────────────────
        self._section(wrap, "DISCORD WEBHOOK")
        c = card(wrap)
        r = row(c)
        self.webhook_entry = ctk.CTkEntry(
            r, height=36, corner_radius=10, font=self.f_mono, fg_color=FIELD,
            border_color=BORDER2, text_color=TEXT,
            placeholder_text="https://discord.com/api/webhooks/…",
            placeholder_text_color=MUTED)
        self.webhook_entry.pack(fill="x", expand=True, side="left")
        if self.discord_url:
            self.webhook_entry.insert(0, self.discord_url)
        divider(c)
        r2 = row(c)
        self.bot_name_entry = ctk.CTkEntry(
            r2, height=34, corner_radius=10, font=self.f_base, fg_color=FIELD,
            border_color=BORDER2, text_color=TEXT,
            placeholder_text="Bot username", placeholder_text_color=MUTED)
        self.bot_name_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.bot_name_entry.insert(0, self.discord_username or "")
        self.bot_avatar_entry = ctk.CTkEntry(
            r2, height=34, corner_radius=10, font=self.f_base, fg_color=FIELD,
            border_color=BORDER2, text_color=TEXT,
            placeholder_text="Avatar URL", placeholder_text_color=MUTED)
        self.bot_avatar_entry.pack(side="left", fill="x", expand=True)
        self.bot_avatar_entry.insert(0, self.discord_avatar or "")
        divider(c)
        r3 = row(c)
        ctk.CTkButton(r3, text="Send test", width=110, height=32,
                      corner_radius=10, font=self.f_small, fg_color=FIELD,
                      hover_color=FIELD_HOVER, border_width=1,
                      border_color=BORDER2, text_color=ACCENT_SOFT,
                      command=self._test_discord).pack(side="left")
        self.discord_status = ctk.CTkLabel(r3, text="", font=self.f_small,
                                           text_color=MUTED)
        self.discord_status.pack(side="left", padx=12)

        # make the wheel scroll over any child widget, not just the bare canvas
        self._enable_scroll(wrap)


    # --- Monitor tab -------------------------------------------------------
    def _build_monitor_tab(self, tab) -> None:
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(tab, text="LIVE STATUS", font=self.f_section,
                     text_color=MUTED, anchor="w"
                     ).grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self.dash = ctk.CTkScrollableFrame(
            tab, fg_color=SUBTLE, corner_radius=14, border_width=1,
            border_color=BORDER, height=180)
        self.dash.grid(row=1, column=0, sticky="nsew", pady=(0, 8))
        self.dash_empty = ctk.CTkLabel(
            self.dash, text="Start a session to see live status.",
            font=self.f_small, text_color=MUTED)
        self.dash_empty.pack(anchor="w", padx=12, pady=14)

        ctk.CTkLabel(tab, text="ACTIVITY LOG", font=self.f_section,
                     text_color=MUTED, anchor="w"
                     ).grid(row=2, column=0, sticky="ew", pady=(8, 4))
        self.log_box = ctk.CTkTextbox(
            tab, fg_color=SUBTLE, text_color=TEXT2, corner_radius=14,
            border_width=1, border_color=BORDER, font=self.f_mono, wrap="word")
        self.log_box.grid(row=3, column=0, sticky="nsew")
        self.log_box.configure(state="disabled")
        tab.grid_rowconfigure(3, weight=1)

        # --- auto-maintenance row: clear logs / clear cache on a timer --------
        maint = ctk.CTkFrame(tab, fg_color="transparent")
        maint.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        maint.grid_columnconfigure(0, weight=1)
        maint.grid_columnconfigure(1, weight=1)

        logcol = ctk.CTkFrame(maint, fg_color="transparent")
        logcol.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkLabel(logcol, text="Auto-clear logs", font=self.f_small,
                     text_color=TEXT2, anchor="w").pack(side="left")
        self.logclear_entry = ctk.CTkEntry(
            logcol, width=52, justify="center", font=self.f_small,
            fg_color=FIELD, border_color=BORDER, text_color=TEXT)
        self.logclear_entry.pack(side="left", padx=(8, 4))
        ctk.CTkLabel(logcol, text="min  (0 = off)", font=self.f_small,
                     text_color=MUTED, anchor="w").pack(side="left")
        self.logclear_entry.bind("<FocusOut>", lambda e: self._save_settings())
        self.logclear_entry.bind("<Return>", lambda e: self._save_settings())

        cachecol = ctk.CTkFrame(maint, fg_color="transparent")
        cachecol.grid(row=1, column=0, sticky="ew", padx=(0, 6), pady=(6, 0))
        ctk.CTkLabel(cachecol, text="Auto-clear cache", font=self.f_small,
                     text_color=TEXT2, anchor="w").pack(side="left")
        self.cacheclear_entry = ctk.CTkEntry(
            cachecol, width=52, justify="center", font=self.f_small,
            fg_color=FIELD, border_color=BORDER, text_color=TEXT)
        self.cacheclear_entry.pack(side="left", padx=(8, 4))
        ctk.CTkLabel(cachecol, text="min  (0 = off)", font=self.f_small,
                     text_color=MUTED, anchor="w").pack(side="left")
        self.cacheclear_entry.bind("<FocusOut>", lambda e: self._save_settings())
        self.cacheclear_entry.bind("<Return>", lambda e: self._save_settings())
        # seed both fields from saved config (default 0 = off)
        self.logclear_entry.insert(0, str(self.cfg.get("log_clear_min", 0)))
        self.cacheclear_entry.insert(0, str(self.cfg.get("cache_clear_min", 0)))
        ctk.CTkLabel(
            maint,
            text="Frees memory during long sessions. Cache = temporary runtime "
                 "data only — your accounts and settings are never touched.",
            font=self.f_small, text_color=MUTED, anchor="w",
            wraplength=520, justify="left"
        ).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))


    # --------------------------------------------------- account rendering -
    def _refresh_account_list(self) -> None:
        self._sync_account_fields()
        for w in self.acct_list.winfo_children():
            w.destroy()
        self.sel_vars.clear()
        self.kill_sel_vars.clear()
        self.place_entries.clear()
        self.delay_entries.clear()
        self.kill_entries.clear()

        if not self.accounts:
            ctk.CTkLabel(self.acct_list, text="No accounts yet.\nHit “+ Add "
                         "account” and paste a .ROBLOSECURITY cookie.",
                         font=self.f_small, text_color=MUTED, justify="left"
                         ).pack(anchor="w", padx=10, pady=10)
            self._sync_start_enabled()
            self._enable_scroll(self.acct_list, defer=False)
            return

        for a in self.accounts:
            uid = a["user_id"]
            row = ctk.CTkFrame(self.acct_list, fg_color="transparent")
            row.pack(fill="x", padx=4, pady=2)
            on = self.select_all_flag or uid in self.selected_ids
            var = ctk.StringVar(value="on" if on else "off")
            self.sel_vars[uid] = var
            valid = bool(a.get("cookie"))
            # truncate long usernames so they don't run into the CLOSE column
            uname = a["username"]
            if len(uname) > 15:
                uname = uname[:14] + "…"
            name = uname + ("" if valid else "  ⚠")
            ctk.CTkCheckBox(
                row, text=name, variable=var, onvalue="on", offvalue="off",
                font=self.f_base, text_color=TEXT if valid else BAD,
                fg_color=ACCENT, hover_color=ACCENT_DARK, border_color=BORDER,
                checkbox_width=20, checkbox_height=20, width=150,
                command=lambda u=uid: self._toggle_one(u)).pack(side="left")
            # "close" selection checkbox — marks this client for Kill Now only
            kvar = ctk.StringVar(value="on" if uid in self.kill_selected_ids
                                 else "off")
            self.kill_sel_vars[uid] = kvar
            kill_cb = ctk.CTkCheckBox(
                row, text="", variable=kvar, onvalue="on", offvalue="off",
                fg_color=BAD, hover_color=KILL_BG_HOVER, border_color=BORDER,
                checkbox_width=20, checkbox_height=20, width=46,
                command=lambda u=uid: self._toggle_kill_one(u))
            kill_cb.pack(side="left")
            pe = ctk.CTkEntry(
                row, height=30, corner_radius=8, font=self.f_small,
                fg_color=FIELD, border_color=BORDER, text_color=TEXT,
                placeholder_text="default place", placeholder_text_color=MUTED)
            pe.pack(side="left", fill="x", expand=True, padx=(8, 6))
            if a.get("place"):
                pe.insert(0, a["place"])
            self.place_entries[uid] = pe
            de = ctk.CTkEntry(
                row, width=52, height=30, corner_radius=8, font=self.f_small,
                fg_color=FIELD, border_color=BORDER, text_color=TEXT,
                justify="center", placeholder_text="def",
                placeholder_text_color=MUTED)
            de.pack(side="left", padx=(0, 4))
            if a.get("delay"):
                de.insert(0, a["delay"])
            self.delay_entries[uid] = de
            ke = ctk.CTkEntry(
                row, width=52, height=30, corner_radius=8, font=self.f_small,
                fg_color=FIELD, border_color=BORDER, text_color=TEXT,
                justify="center", placeholder_text="def",
                placeholder_text_color=MUTED)
            ke.pack(side="left", padx=(0, 6))
            if a.get("killmin"):
                ke.insert(0, a["killmin"])
            self.kill_entries[uid] = ke
            ctk.CTkButton(row, text="✕", width=26, height=28, corner_radius=6,
                          font=self.f_small, fg_color="transparent",
                          hover_color=KILL_BG, text_color=MUTED,
                          command=lambda u=uid: self._remove_account(u)
                          ).pack(side="left")
        self._sync_start_enabled()
        # rebind the wheel to every freshly-created row so scrolling works
        # no matter which child the cursor is over
        self._enable_scroll(self.acct_list, defer=False)

    def _sync_start_enabled(self) -> None:
        pass  # start handles empty-selection messaging itself

    def _toggle_all(self) -> None:
        self.select_all_flag = self.allacct_var.get() == "on"
        if self.select_all_flag:
            self.selected_ids = set()
        else:
            self.selected_ids = {u for u, v in self.sel_vars.items()
                                 if v.get() == "on"}
        self._refresh_account_list()

    def _toggle_one(self, uid: int) -> None:
        if self.select_all_flag:
            self.select_all_flag = False
            self.selected_ids = {a["user_id"] for a in self.accounts}
            self.allacct_var.set("off")
        if self.sel_vars[uid].get() == "on":
            self.selected_ids.add(uid)
        else:
            self.selected_ids.discard(uid)

    def _toggle_kill_one(self, uid: int) -> None:
        """Toggle whether this account is in the Kill Now 'close' selection."""
        if self.kill_sel_vars[uid].get() == "on":
            self.kill_selected_ids.add(uid)
        else:
            self.kill_selected_ids.discard(uid)

    def _remove_account(self, uid: int) -> None:
        self.accounts = [a for a in self.accounts if a["user_id"] != uid]
        self.selected_ids.discard(uid)
        self.kill_selected_ids.discard(uid)
        self._persist_accounts()
        self._refresh_account_list()

    def _add_account_dialog(self) -> None:
        dlg = ctk.CTkToplevel(self)
        dlg.title("Add account")
        dlg.geometry("470x320")
        dlg.configure(fg_color=BG)
        dlg.transient(self)
        dlg.after(80, dlg.grab_set)
        ctk.CTkLabel(dlg, text="Paste .ROBLOSECURITY cookie", font=self.f_section,
                     text_color=MUTED).pack(anchor="w", padx=18, pady=(18, 4))
        box = ctk.CTkTextbox(dlg, height=110, fg_color=FIELD, text_color=TEXT,
                             corner_radius=10, border_width=1, border_color=BORDER,
                             font=self.f_mono, wrap="word")
        box.pack(fill="x", padx=18)
        ctk.CTkLabel(dlg, text="Browser → F12 → Application → Cookies → "
                               ".ROBLOSECURITY. Stays encrypted on this PC only.",
                     font=self.f_sub, text_color=MUTED, wraplength=430,
                     justify="left").pack(anchor="w", padx=18, pady=(6, 8))
        status = ctk.CTkLabel(dlg, text="", font=self.f_small, text_color=WARN)
        status.pack(anchor="w", padx=18)

        def save():
            cookie = box.get("1.0", "end").strip()
            if not cookie:
                status.configure(text="Paste a cookie first.", text_color=BAD)
                return
            status.configure(text="Checking cookie…", text_color=WARN)

            def finish(uid, uname):
                # back on the UI thread (via .after) — safe to touch widgets
                if not uid:
                    status.configure(text="Cookie rejected — expired or invalid.",
                                     text_color=BAD)
                    return
                self._sync_account_fields()
                self.accounts = [a for a in self.accounts
                                 if a["user_id"] != uid]
                self.accounts.append({"user_id": uid,
                                      "username": uname or str(uid),
                                      "cookie": cookie, "place": "", "delay": "",
                                      "killmin": ""})
                self._persist_accounts()
                self._refresh_account_list()
                self._append_log(f"Added account: {uname} ({uid}).")
                try:
                    dlg.destroy()
                except Exception:
                    pass

            def work():
                uid, uname = get_account_info(cookie)
                # marshal the result back onto the UI thread
                try:
                    self.after(0, lambda: finish(uid, uname))
                except Exception:
                    pass

            threading.Thread(target=work, daemon=True).start()

        btns = ctk.CTkFrame(dlg, fg_color="transparent")
        btns.pack(fill="x", padx=18, pady=16, side="bottom")
        ctk.CTkButton(btns, text="Cancel", width=90, height=36, corner_radius=9,
                      font=self.f_small, fg_color=FIELD, hover_color=FIELD_HOVER,
                      text_color=TEXT, command=dlg.destroy).pack(side="right")
        ctk.CTkButton(btns, text="Save account", width=120, height=36,
                      corner_radius=9, font=self.f_small, fg_color=ACCENT,
                      hover_color=ACCENT_DARK, text_color="#ffffff",
                      command=save).pack(side="right", padx=(0, 8))

    # --------------------------------------------------------- live targets -
    def _on_tile_toggle(self) -> None:
        self.tile_enabled = self.tile_switch.get() in ("on", 1, True)
        self._flash_switch(self.tile_switch)
        self._save_settings()
        self._apply_layout_now()          # rearrange windows immediately
        if self.tile_enabled:
            self._append_log("Window tiling on — arranging windows in a grid.")
        else:
            self._append_log("Window tiling off — stacking windows.")

    def _on_detect_open_toggle(self) -> None:
        self.detect_open_enabled = self.detect_open_switch.get() in ("on", 1, True)
        self._flash_switch(self.detect_open_switch)
        self._save_settings()
        if self.detect_open_enabled:
            n = len(roblox_pids())
            self._append_log(
                f"Detect open clients ON — will adopt currently-open Roblox "
                f"client(s) on start ({n} open right now) instead of launching.")
        else:
            self._append_log("Detect open clients OFF — accounts launch normally.")

    def _on_kickdetect_toggle(self) -> None:
        self.kickdetect_enabled = self.kickdetect_switch.get() in ("on", 1, True)
        self._flash_switch(self.kickdetect_switch)
        self._save_settings()
        if self.kickdetect_enabled:
            self._append_log("Kick detection ON — if an account gets kicked or "
                             "disconnected, it'll be closed and rejoined.")
        else:
            self._append_log("Kick detection OFF.")

    def _on_joinserver_toggle(self) -> None:
        self.joinserver_enabled = self.joinserver_switch.get() in ("on", 1, True)
        self._flash_switch(self.joinserver_switch)
        if self.joinserver_enabled:
            # mutually exclusive with different-servers
            if getattr(self, "diffserver_enabled", False):
                self.diffserver_enabled = False
                try:
                    self.diffserver_switch.deselect()
                except Exception:
                    pass
                self._append_log("Different servers turned off (Join server "
                                 "can't be used at the same time).")
            self._resolve_join_async()    # warm the cache so START is instant
        else:
            self.joinserver_status.configure(text="")
        self._save_settings()

    def _check_joinserver(self) -> None:
        self._resolve_join_async()

    def _resolve_join_async(self) -> None:
        """Resolve the share link off the UI thread and report status. Caches the
        result so launching can use it without another network round-trip."""
        url = self.joinserver_entry.get().strip()
        self._joinserver_url = url
        code, ltype = parse_share_link(url)
        if not code:
            self.joinserver_status.configure(text="Paste a valid share link.",
                                             text_color=WARN)
            return
        cookie = next((a["cookie"] for a in self.accounts if a.get("cookie")), None)
        if not cookie:
            self.joinserver_status.configure(text="Add an account first.",
                                             text_color=WARN)
            return
        self.joinserver_status.configure(text="Checking…", text_color=ACCENT_SOFT)

        def work():
            place, inst = resolve_share_link(cookie, code, ltype)

            def done():
                if place:
                    self._join_cache = {"url": url, "place": place,
                                        "instance": inst, "err": None}
                    msg = (f"✓ place {place}"
                           + (" · specific server" if inst else " · open server"))
                    self.joinserver_status.configure(text=msg, text_color=GOOD)
                else:
                    self._join_cache = {"url": url, "place": None,
                                        "instance": None, "err": "failed"}
                    self.joinserver_status.configure(
                        text="Couldn't resolve that link.", text_color=BAD)
            try:
                self.after(0, done)
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _on_diffserver_toggle(self) -> None:
        self.diffserver_enabled = self.diffserver_switch.get() in ("on", 1, True)
        self._flash_switch(self.diffserver_switch)
        if self.diffserver_enabled:
            # mutually exclusive with join-server (one spreads, one gathers)
            if self.joinserver_enabled:
                self.joinserver_enabled = False
                try:
                    self.joinserver_switch.deselect()
                    self.joinserver_status.configure(text="")
                except Exception:
                    pass
                self._append_log("Join server turned off (different servers "
                                 "can't be used at the same time).")
            # reset the pool so the next START fetches a fresh server list
            self._diffserver_pool = []
            self._diffserver_assigned = {}
            self._diffserver_place = None
            self._append_log("Different servers ON — each account will join "
                             "its own server.")
        else:
            self._append_log("Different servers OFF.")
        self._save_settings()

    def _apply_layout_now(self) -> None:
        """Immediately tile or stack the currently-monitored windows."""
        hwnds = []
        for pid in list(self._monitored_pids):
            h = find_window_for_pid(pid)
            if h:
                hwnds.append(h)
        if not hwnds:
            return
        if self.tile_enabled:
            tile_windows(hwnds)
        else:
            stack_windows(hwnds)

    def _refresh_live_targets(self) -> None:
        """Main-thread snapshot of what SHOULD be running, so the worker can add
        or drop accounts live as the checkboxes change. Resolves every account's
        place/delay (own override or the defaults). Thread-safe for the worker
        because it only ever reads the resulting plain dict/set."""
        try:
            default_place = parse_place_id(self.game_entry.get())
            default_delay = self._delay_value()
            default_kill = self._killmin_value()
        except Exception:
            return
        self._sync_account_fields()
        # Join-server mode: everyone targets the one resolved place/server,
        # ignoring per-account and default place IDs.
        js_on = self.joinserver_enabled and bool(self._join_cache.get("place"))
        js_place = self._join_cache.get("place")
        js_inst = self._join_cache.get("instance")
        resolved: dict[int, dict] = {}
        for a in self.accounts:
            if not a.get("cookie"):
                continue
            if js_on:
                place, inst = js_place, js_inst
            else:
                place = parse_place_id(a.get("place")) or default_place
                # different-servers mode lets Roblox matchmake (no forced server)
                inst = None
            if not place:
                continue
            try:
                d = max(0, min(3600, int(a.get("delay") or "")))
            except ValueError:
                d = default_delay
            try:
                km = max(1, min(720, int(a.get("killmin") or "")))
            except ValueError:
                km = default_kill
            resolved[a["user_id"]] = {
                "user_id": a["user_id"], "username": a["username"],
                "cookie": a["cookie"], "rplace": place, "rinstance": inst,
                "rdelay": d, "rkill": km}
        if self.select_all_flag:
            desired = set(resolved.keys())
        else:
            desired = set(self.selected_ids) & set(resolved.keys())
        self._live_resolved = resolved
        self._live_desired = desired
        self._live_global_kill = default_kill   # for simultaneous-kill timer

    def _apply_remote_commands(self, rt: dict, emit, now: float) -> None:
        """Commands are applied on the main thread (they touch widgets); the
        worker only needs to react to settings that changed as a result, which
        it already reads live each loop. Kept as a hook for future rt-level ops."""
        return

    # ------------------------------------------------------------- bridge --
    def _publish_state(self) -> None:
        """Write a snapshot the bot can read, and ingest any commands it left.
        Runs on the main thread (from the pump), so reading widgets is safe."""
        now = time.time()
        if now - self._last_state_pub < 1.0:
            return
        self._last_state_pub = now
        try:
            accounts = []
            for a in self.accounts:
                if not a.get("cookie"):
                    continue
                uid = a["user_id"]
                on = self.select_all_flag or uid in self.selected_ids
                stat = self.acct_stats.get(uid, {})
                accounts.append({
                    "user_id": uid, "username": a["username"],
                    "monitored": bool(on),
                    "rejoin_delay": a.get("delay", "") or "",
                    "kill_cooldown": a.get("killmin", "") or "",
                    "place": a.get("place", "") or "",
                    "crashes": int(stat.get("crashes", 0) or 0),
                })
            running = self.worker is not None and self.worker.is_alive()
            state = {
                "updated": now,
                "running": running,
                "auto_kill": bool(self.autokill_armed),
                "sync_kill": bool(self.synckill_enabled),
                "tile": bool(self.tile_enabled),
                "detect_open": bool(self.detect_open_enabled),
                "kick_detect": bool(self.kickdetect_enabled),
                "join_server": bool(self.joinserver_enabled),
                "diff_server": bool(self.diffserver_enabled),
                "join_server_url": self._joinserver_url or "",
                "global_rejoin_delay": self.delay_entry.get().strip() or "60",
                "global_kill_cooldown": self.kill_cd_entry.get().strip() or "20",
                "default_place": self.game_entry.get().strip(),
                "accounts": accounts,
                "recent_log": self._recent_log[-15:],
            }
            _atomic_write(STATE_PATH, json.dumps(state))
        except Exception:
            pass
        # ingest commands the bot dropped, queue for application
        for cmd in read_remote_commands():
            self._remote_cmd_q.put(cmd)
        self._drain_remote_commands()

    def _account_by_name(self, name: str) -> dict | None:
        n = (name or "").strip().lower()
        for a in self.accounts:
            if (a.get("username") or "").lower() == n:
                return a
        return None

    def _drain_remote_commands(self) -> None:
        """Apply queued bot commands on the main thread (touches widgets)."""
        applied = False
        try:
            while True:
                cmd = self._remote_cmd_q.get_nowait()
                ok, msg = self._apply_one_command(cmd)
                cid = cmd.get("id")
                if cid:
                    self._cmd_results[cid] = {
                        "ok": ok, "message": msg, "ts": time.time()}
                self._append_log(f"[bot] {cmd.get('action','?')}: {msg}")
                applied = True
        except queue.Empty:
            pass
        if applied:
            # trim old results, write back for the bot to read
            cutoff = time.time() - 120
            self._cmd_results = {k: v for k, v in self._cmd_results.items()
                                 if v.get("ts", 0) > cutoff}
            _atomic_write(RESULTS_PATH, json.dumps(self._cmd_results))
            self._save_settings()

    def _apply_one_command(self, cmd: dict) -> tuple[bool, str]:
        action = cmd.get("action")
        user = cmd.get("username")
        value = cmd.get("value")

        def set_switch(switch, flag_attr, handler):
            want = str(value).lower() in ("on", "true", "1", "yes")
            cur = getattr(self, flag_attr)
            if want == cur:
                return True, f"already {'on' if want else 'off'}"
            switch.select() if want else switch.deselect()
            handler()
            return True, f"turned {'on' if want else 'off'}"

        if action == "auto_kill":
            return set_switch(self.autokill_switch, "autokill_armed",
                              self._on_autokill_toggle)
        if action == "sync_kill":
            return set_switch(self.synckill_switch, "synckill_enabled",
                              self._on_synckill_toggle)
        if action == "tile":
            return set_switch(self.tile_switch, "tile_enabled",
                              self._on_tile_toggle)
        if action == "detect_open":
            return set_switch(self.detect_open_switch, "detect_open_enabled",
                              self._on_detect_open_toggle)
        if action == "kick_detect":
            return set_switch(self.kickdetect_switch, "kickdetect_enabled",
                              self._on_kickdetect_toggle)
        if action == "diff_server":
            return set_switch(self.diffserver_switch, "diffserver_enabled",
                              self._on_diffserver_toggle)
        if action == "join_server":
            return set_switch(self.joinserver_switch, "joinserver_enabled",
                              self._on_joinserver_toggle)

        if action == "account":      # enable/disable an account in the menu
            a = self._account_by_name(user)
            if not a:
                return False, f"no account '{user}'"
            want = str(value).lower() in ("on", "true", "1", "yes")
            uid = a["user_id"]
            if want:
                if self.select_all_flag:
                    return True, f"{a['username']} already on (all selected)"
                self.selected_ids.add(uid)
            else:
                if self.select_all_flag:
                    # materialise the set so we can drop just this one
                    self.select_all_flag = False
                    self.selected_ids = {x["user_id"] for x in self.accounts
                                         if x.get("cookie")}
                    self.allacct_var.set("off")
                self.selected_ids.discard(uid)
            self._refresh_account_list()
            return True, f"{a['username']} turned {'on' if want else 'off'}"

        if action == "set_kill_cooldown":
            try:
                v = max(1, min(720, int(value)))
            except (TypeError, ValueError):
                return False, f"'{value}' isn't a valid number of minutes"
            if user:
                a = self._account_by_name(user)
                if not a:
                    return False, f"no account '{user}'"
                a["killmin"] = str(v)
                if a["user_id"] in self.kill_entries:
                    e = self.kill_entries[a["user_id"]]
                    e.delete(0, "end"); e.insert(0, str(v))
                return True, f"{a['username']} kill cooldown = {v} min"
            # global: clear per-account overrides + set the default
            self.kill_cd_entry.delete(0, "end"); self.kill_cd_entry.insert(0, str(v))
            for a in self.accounts:
                a["killmin"] = ""
            self._refresh_account_list()
            return True, f"global kill cooldown = {v} min (overrides cleared)"

        if action == "set_rejoin_delay":
            try:
                v = max(0, min(3600, int(value)))
            except (TypeError, ValueError):
                return False, f"'{value}' isn't a valid number of seconds"
            if user:
                a = self._account_by_name(user)
                if not a:
                    return False, f"no account '{user}'"
                a["delay"] = str(v)
                if a["user_id"] in self.delay_entries:
                    e = self.delay_entries[a["user_id"]]
                    e.delete(0, "end"); e.insert(0, str(v))
                return True, f"{a['username']} rejoin delay = {v}s"
            self.delay_entry.delete(0, "end"); self.delay_entry.insert(0, str(v))
            for a in self.accounts:
                a["delay"] = ""
            self._refresh_account_list()
            return True, f"global rejoin delay = {v}s (overrides cleared)"

        if action == "set_place":
            pid = parse_place_id(str(value))
            if not pid:
                return False, (f"'{value}' isn't a valid place ID or Roblox "
                               "game link")
            if user:
                a = self._account_by_name(user)
                if not a:
                    return False, f"no account '{user}'"
                a["place"] = str(pid)
                if a["user_id"] in self.place_entries:
                    e = self.place_entries[a["user_id"]]
                    e.delete(0, "end"); e.insert(0, str(pid))
                return True, (f"{a['username']} game = {pid} (applies on its "
                              "next rejoin)")
            # global: set the default place + clear per-account overrides
            self.game_entry.delete(0, "end"); self.game_entry.insert(0, str(pid))
            for a in self.accounts:
                a["place"] = ""
            self._refresh_account_list()
            return True, (f"global game = {pid} (overrides cleared; applies as "
                          "each account next rejoins)")

        if action == "watch":
            want = str(value).lower() in ("on", "true", "1", "yes", "start")
            running = self.worker is not None and self.worker.is_alive()
            if want and not running:
                self._toggle()                       # starts the watcher
                started = self.worker is not None and self.worker.is_alive()
                return (started, "started watching" if started else
                        "couldn't start — check that accounts are selected and a "
                        "game is set")
            if not want and running:
                self._toggle()                       # stops the watcher
                return True, "stopped watching"
            return True, f"already {'running' if running else 'stopped'}"

        if action == "rejoin":
            a = self._account_by_name(user)
            if not a:
                return False, f"no account '{user}'"
            running = self.worker is not None and self.worker.is_alive()
            if not running:
                return False, "not watching — start the watcher first"
            # hand the uid to the worker; it kills + relaunches on its next loop
            self._rejoin_requests.add(a["user_id"])
            return True, f"restarting {a['username']}"

        return False, f"unknown action '{action}'"

    # ------------------------------------------------------------ discord --
    def _refresh_discord_runtime(self) -> None:
        """Main-thread refresh of the discord snapshot the worker reads."""
        try:
            url = self.webhook_entry.get()
            name = self.bot_name_entry.get()
            avatar = self.bot_avatar_entry.get()
        except Exception:
            return
        self.discord_runtime = {
            "url": url.strip() if _is_discord_webhook(url) else "",
            "name": name, "avatar": avatar}

    def _gather_discord(self) -> tuple[str, str, str]:
        return (self.webhook_entry.get(), self.bot_name_entry.get(),
                self.bot_avatar_entry.get())

    def _test_discord(self) -> None:
        url, name, avatar = self._gather_discord()
        if not _is_discord_webhook(url):
            self.discord_status.configure(
                text="Enter a valid Discord webhook URL.", text_color=BAD)
            return
        self.discord_status.configure(text="Sending…", text_color=WARN)

        def finish(ok, err):
            if ok:
                self._save_settings()
                self.discord_status.configure(text="Sent! Check your channel.",
                                              text_color=GOOD)
            else:
                self.discord_status.configure(text=err, text_color=BAD)

        def work():
            ok, err = discord_send(url, name, avatar,
                                   "✅ RoRejoin webhook test — you're connected.")
            try:
                self.after(0, lambda: finish(ok, err))
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------- queue --
    def _emit(self, kind: str, payload=None) -> None:
        self.ui_q.put((kind, payload))

    def _pump(self) -> None:
        latest_status = None      # collapse repeated status updates in one drain
        try:
            while True:
                kind, payload = self.ui_q.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "status":
                    latest_status = payload     # remember, apply after draining
                elif kind == "session_start":
                    self._build_dashboard(payload)
                elif kind == "acctstat":
                    self.acct_stats = payload
                elif kind == "done":
                    self._set_running_ui(False)
        except queue.Empty:
            pass
        if latest_status is not None:
            text, color = latest_status
            try:
                self.status_lbl.configure(text=text)
                self.dot.configure(text_color=color)
            except Exception:
                pass
        self._tick_dashboard()
        self._tick_anim()
        # These do real work (widget reads, regex, dict rebuilds / file writes).
        # The pump runs ~12×/sec for smooth animation, but these only need to be
        # roughly once a second — running them every tick was needless main-thread
        # churn that added up to UI lag. _publish_state already self-throttles.
        now = time.time()
        if now - self._last_slow_refresh >= 1.0:
            self._last_slow_refresh = now
            self._refresh_discord_runtime()
            self._refresh_live_targets()
            self._run_auto_maintenance(now)
        self._publish_state()
        self.after(80, self._pump)

    def _run_auto_maintenance(self, now: float) -> None:
        """Periodically clear the on-screen log and/or transient caches, on the
        intervals the user set. Frees memory on long sessions. Never touches
        user data (accounts, delays, places, Discord settings)."""
        log_min = self.cfg.get("log_clear_min", 0) or 0
        if log_min and (now - self._last_log_clear) >= log_min * 60:
            self._last_log_clear = now
            self._clear_logs(auto=True)
        cache_min = self.cfg.get("cache_clear_min", 0) or 0
        if cache_min and (now - self._last_cache_clear) >= cache_min * 60:
            self._last_cache_clear = now
            self._clear_cache(auto=True)

    def _clear_logs(self, auto: bool = False) -> None:
        """Wipe the activity-log textbox (purely cosmetic/runtime — no data)."""
        try:
            self.log_box.configure(state="normal")
            self.log_box.delete("1.0", "end")
            self.log_box.configure(state="disabled")
        except Exception:
            pass
        self._log_lines = 0
        if auto:
            self._append_log("🧹 Logs auto-cleared (scheduled maintenance).")

    def _clear_cache(self, auto: bool = False) -> None:
        """Drop transient, rebuildable runtime data and reclaim memory. Does NOT
        touch accounts, settings, or anything persisted to config."""
        # rolling buffers / bot bridge scratch
        self._recent_log = []
        self._cmd_results = {}
        # different-servers scratch (safe: rebuilt from presence on next loop)
        self._diffserver_pool = []
        # resolved join-link cache (re-resolved automatically when needed)
        if not (self.worker and self.worker.is_alive()):
            # only clear the join cache while idle, so a running session that
            # depends on the resolved server isn't disturbed mid-flight
            self._join_cache = {"url": "", "place": None,
                                "instance": None, "err": None}
        try:
            import gc
            gc.collect()
        except Exception:
            pass
        if auto:
            self._append_log("🧹 Cache cleared (scheduled maintenance).")

    def _append_log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{ts}]  {msg}\n")
        # Trim the widget to a rolling window. A Tk Text widget gets steadily
        # slower as its line count grows, so an unbounded log makes the whole
        # UI laggy over a long session — keep only the most recent lines.
        self._log_lines += 1
        if self._log_lines > self._LOG_MAX:
            # delete the oldest lines in one batch (cheaper than line-by-line)
            drop = self._log_lines - self._LOG_KEEP
            self.log_box.delete("1.0", f"{drop + 1}.0")
            self._log_lines = self._LOG_KEEP
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
        # keep a short rolling buffer for the Discord /log command
        self._recent_log.append(f"[{ts}] {msg}")
        if len(self._recent_log) > 40:
            self._recent_log = self._recent_log[-40:]

    def _fade_in(self) -> None:
        """Smoothly fade the window from transparent to opaque on launch."""
        try:
            cur = float(self.attributes("-alpha"))
        except Exception:
            self._faded_in = True
            return
        cur = min(1.0, cur + 0.07)
        try:
            self.attributes("-alpha", cur)
        except Exception:
            self._faded_in = True
            return
        if cur < 1.0:
            self.after(16, self._fade_in)
        else:
            self._faded_in = True

    def _ensure_visible(self) -> None:
        """Belt-and-suspenders: guarantee the window is opaque even if the fade
        loop was interrupted, so it can never get stuck invisible."""
        if not self._faded_in:
            try:
                self.attributes("-alpha", 1.0)
            except Exception:
                pass
            self._faded_in = True

    def _cfg_if_changed(self, widget, key: str, **props) -> None:
        """Call widget.configure(**props) ONLY when the values differ from what
        was last applied to this (widget, key). Tk's .configure() is a relatively
        costly Tcl round-trip; the breathing/glow animations recompute colours
        every frame but for a slow sine many frames round to the same hex, so
        skipping the no-op calls keeps the menu snappy without changing how the
        animation looks. Cache lives on the widget so it's auto-collected."""
        cache = getattr(widget, "_anim_cache", None)
        if cache is None:
            cache = {}
            try:
                widget._anim_cache = cache
            except Exception:
                pass
        if cache.get(key) == props:
            return
        cache[key] = props
        try:
            widget.configure(**props)
        except Exception:
            pass

    def _tick_anim(self) -> None:
        """Lightweight UI animations: pulsing run-dot, breathing wordmark,
        launching spinner, idle/active button glow + fading row flashes."""
        try:
            t = time.time() - self._anim_t0
            running = self.worker is not None and self.worker.is_alive()

            # breathing wordmark — always on, very subtle
            wm = (math.sin(t * 1.3) + 1) / 2
            self._cfg_if_changed(self.wm_ro, "tc",
                                 text_color=lerp_hex(ACCENT, ACCENT_MID, wm))

            if running:
                phase = (math.sin(t * 3.0) + 1) / 2
                self._cfg_if_changed(self.dot, "tc",
                                     text_color=lerp_hex(ACCENT, ACCENT_SOFT, phase))
                # Kill Now breathes a red border while a session is live
                kp = (math.sin(t * 2.6) + 1) / 2
                self._cfg_if_changed(
                    self.kill_now_btn, "bd", border_width=2,
                    border_color=lerp_hex(KILL_BG_HOVER, BAD, kp))
                self._cfg_if_changed(self.start_btn, "bd", border_width=0)
            else:
                self._cfg_if_changed(self.kill_now_btn, "bd", border_width=0)
                # soft breathing glow ring on the START button while idle
                g = (math.sin(t * 2.2) + 1) / 2
                self._cfg_if_changed(
                    self.start_btn, "bd", border_width=2,
                    border_color=lerp_hex(ACCENT_DARK, ACCENT_SOFT, g))

            # animated spinner for launching / rejoining accounts.
            # Braille dots all share the same glyph width, so the row never
            # jumps in size as it cycles (half-circle glyphs varied per font).
            frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
            spin = frames[int(t * 12) % len(frames)]
            for uid, st in self.acct_stats.items():
                refs = self.dash_rows.get(uid)
                if not refs:
                    continue
                s = st.get("state")
                if s == "launching":
                    refs["state"].configure(text=f"{spin}  launching",
                                            text_color=ACCENT_SOFT)
                elif s == "leaving":
                    refs["state"].configure(text=f"{spin}  leaving",
                                            text_color=WARN)
                elif s == "waiting":
                    refs["state"].configure(text=f"{spin}  rejoining",
                                            text_color=WARN)

            for uid, st in self.acct_stats.items():
                c = st.get("crashes", 0)
                prev = self._crash_seen.get(uid)
                if prev is not None and c > prev:
                    self._flashes[uid] = ("crash", time.time())
                self._crash_seen[uid] = c
            done = []
            for uid, (kind, t0) in self._flashes.items():
                refs = self.dash_rows.get(uid)
                if not refs:
                    done.append(uid)
                    continue
                age = time.time() - t0
                if age > 1.2:
                    refs["state"].configure(fg_color="transparent")
                    done.append(uid)
                else:
                    base = BAD if kind == "crash" else GOOD
                    refs["state"].configure(fg_color=lerp_hex(base, CARD, age / 1.2))
            for uid in done:
                self._flashes.pop(uid, None)
        except Exception:
            pass

    # ── nav rail (single-canvas: highlight + text together) ───────────────
    def _nav_row_top(self, i: int) -> int:
        """Y of the top of tab row i within the nav canvas."""
        return self._nav_pad_top + i * (self._nav_row_h + self._nav_gap)

    def _round_rect_pts(self, x1, y1, x2, y2, r):
        """Polygon points approximating a rounded rectangle."""
        import math as _m
        pts = []
        # corners: tl, tr, br, bl  (each a quarter arc)
        corners = [
            (x1 + r, y1 + r, _m.pi,        1.5 * _m.pi),   # top-left
            (x2 - r, y1 + r, 1.5 * _m.pi,  2.0 * _m.pi),   # top-right
            (x2 - r, y2 - r, 0.0,          0.5 * _m.pi),   # bottom-right
            (x1 + r, y2 - r, 0.5 * _m.pi,  _m.pi),         # bottom-left
        ]
        for cx, cy, a0, a1 in corners:
            for k in range(7):
                a = a0 + (a1 - a0) * k / 6
                pts += [cx + r * _m.cos(a), cy + r * _m.sin(a)]
        return pts

    def _draw_nav(self) -> None:
        """Redraw the entire nav canvas: sliding highlight + all tab labels."""
        cv = getattr(self, "_nav_canvas", None)
        if cv is None:
            return
        try:
            cv.delete("all")
            w = cv.winfo_width()
            if w < 2:
                w = 184
            ch = cv.winfo_height()
            if ch < 2:
                ch = (self._nav_pad_top + self._nav_pad_bot
                      + self._nav_row_h * len(self._tab_names)
                      + self._nav_gap * (len(self._tab_names) - 1))
            r = 12
            # sliding highlight (rounded rect) at current animated y,
            # clamped so the spring overshoot can't draw outside the canvas
            hy = self._nav_hl_y
            hy = max(1.0, min(hy, ch - self._nav_row_h - 1))
            pts = self._round_rect_pts(2, hy, w - 2, hy + self._nav_row_h, r)
            cv.create_polygon(pts, fill=NAV_ACTIVE, outline="", smooth=True)
            # tab labels
            for i, name in enumerate(self._tab_names):
                top = self._nav_row_top(i)
                cy = top + self._nav_row_h / 2
                active = name == self._active_tab
                color = ON_ACCENT if active else (
                    TEXT2 if i == self._nav_hover_i else MUTED)
                cv.create_text(20, cy, text=name, anchor="w",
                               fill=color, font=("Segoe UI", 13))
        except Exception:
            pass

    def _nav_index_at(self, y: int) -> int:
        """Which tab row contains canvas-y, or -1."""
        for i in range(len(self._tab_names)):
            top = self._nav_row_top(i)
            if top <= y <= top + self._nav_row_h:
                return i
        return -1

    def _on_nav_click(self, event) -> None:
        i = self._nav_index_at(event.y)
        if i >= 0:
            self._select_tab(self._tab_names[i])

    def _on_nav_motion(self, event) -> None:
        i = self._nav_index_at(event.y)
        if i != self._nav_hover_i:
            self._nav_hover_i = i
            self._draw_nav()
        try:
            self._nav_canvas.configure(
                cursor="hand2" if i >= 0 else "")
        except Exception:
            pass

    def _on_nav_leave(self) -> None:
        if self._nav_hover_i != -1:
            self._nav_hover_i = -1
            self._draw_nav()

    def _select_tab(self, name: str) -> None:
        if name not in self._tab_frames:
            return
        if name != self._active_tab:
            self._tab_frames[self._active_tab].grid_remove()
            self._tab_frames[name].grid()
            self._active_tab = name
        new_i = self._tab_names.index(name)
        self._animate_nav_indicator(float(self._nav_row_top(new_i)))

    def _animate_nav_indicator(self, to_y: float) -> None:
        """Slide the highlight to the target tab with a spring (easeOutBack)."""
        self._nav_anim_id += 1
        tok     = self._nav_anim_id
        from_y  = self._nav_hl_y
        t_start = time.time()
        dur     = 0.38
        s       = 1.70    # overshoot factor

        def step():
            if tok != self._nav_anim_id:
                return
            raw = min(1.0, (time.time() - t_start) / dur)
            t = raw - 1.0
            p = t * t * ((s + 1) * t + s) + 1.0   # easeOutBack
            self._nav_hl_y = from_y + (to_y - from_y) * p
            self._draw_nav()
            if raw < 1.0:
                try: self.after(_frame_ms(), step)
                except Exception: pass
            else:
                self._nav_hl_y = to_y
                self._draw_nav()

        step()

    def _sync_nav_indicator(self, name: str) -> None:
        """Snap the highlight to a tab (no animation)."""
        if name in self._tab_names:
            i = self._tab_names.index(name)
            self._nav_hl_y = float(self._nav_row_top(i))
            self._draw_nav()

    def _flash_switch(self, switch) -> None:
        """Brief knob glow after toggle — works with both _IosSwitch and
        CTkSwitch. The _IosSwitch already animates its own knob travel;
        this adds a subtle luminance kiss on the knob for tactile feedback."""
        self._switch_flash_id += 1
        token = self._switch_flash_id
        start = time.time()
        dur = 0.35
        peak = "#E8DAFF"          # very light violet kiss

        def step() -> None:
            if token != self._switch_flash_id:
                return
            p = min(1.0, (time.time() - start) / dur)
            glow = (1.0 - p) ** 2
            color = lerp_hex(ON_ACCENT, peak, glow)
            try:
                switch.configure(button_color=color)
            except Exception:
                pass
            if p < 1.0:
                self.after(12, step)
            else:
                try:
                    switch.configure(button_color=ON_ACCENT)
                except Exception:
                    pass

        step()

    # --------------------------------------------------------- dashboard ---
    def _build_dashboard(self, accounts: list) -> None:
        for w in self.dash.winfo_children():
            w.destroy()
        self.dash_rows.clear()
        hdr = ctk.CTkFrame(self.dash, fg_color="transparent")
        hdr.pack(fill="x", padx=8, pady=(6, 2))
        for txt, w in (("ACCOUNT", 120), ("STATE", 96), ("UPTIME", 66),
                       ("CRASHES", 58), ("LAST CRASH", 78), ("AUTO-KILL", 78)):
            ctk.CTkLabel(hdr, text=txt, font=self.f_section, text_color=MUTED,
                         width=w, anchor="w").pack(side="left")
        for uid, uname in accounts:
            row = ctk.CTkFrame(self.dash, fg_color="transparent")
            row.pack(fill="x", padx=8, pady=1)
            disp = uname if len(uname) <= 15 else uname[:14] + "…"
            name_l = ctk.CTkLabel(row, text=disp, font=self.f_small,
                                  text_color=TEXT, width=120, anchor="w")
            name_l.pack(side="left")
            state_l = ctk.CTkLabel(row, text="starting…", font=self.f_small,
                                   text_color=MUTED, width=96, anchor="w",
                                   corner_radius=6)
            state_l.pack(side="left")
            up_l = ctk.CTkLabel(row, text="—", font=self.f_mono, text_color=TEXT,
                                width=66, anchor="w")
            up_l.pack(side="left")
            cr_l = ctk.CTkLabel(row, text="0", font=self.f_mono, text_color=TEXT,
                                width=58, anchor="w")
            cr_l.pack(side="left")
            lc_l = ctk.CTkLabel(row, text="none", font=self.f_small,
                                text_color=MUTED, width=78, anchor="w")
            lc_l.pack(side="left")
            ak_l = ctk.CTkLabel(row, text="—", font=self.f_mono, text_color=MUTED,
                                width=78, anchor="w")
            ak_l.pack(side="left")
            self.dash_rows[uid] = {"state": state_l, "up": up_l, "cr": cr_l,
                                   "lc": lc_l, "ak": ak_l}
        # wheel scrolling over any row, not just the bare canvas
        self._enable_scroll(self.dash, defer=False)

    def _tick_dashboard(self) -> None:
        now = time.time()
        if now - self._last_dash_tick < 1.0:
            return
        self._last_dash_tick = now
        if not self.dash_rows:
            return
        for uid, refs in self.dash_rows.items():
            st = self.acct_stats.get(uid)
            if not st:
                continue
            state = st.get("state", "?")
            if state == "running":
                refs["state"].configure(text="● in game", text_color=GOOD)
                jt = st.get("joined_at")
                refs["up"].configure(text=fmt_dur(now - jt) if jt else "—")
            elif state in ("launching", "waiting", "leaving"):
                # animated spinner text is driven by _tick_anim (runs every
                # 80ms); don't fight it here or the glyph will flicker.
                refs["up"].configure(text="—")
            else:
                refs["state"].configure(text="○ stopped", text_color=MUTED)
                refs["up"].configure(text="—")
            refs["cr"].configure(text=str(st.get("crashes", 0)))
            refs["lc"].configure(text=fmt_ago(st.get("last_crash")))
            # AUTO-KILL countdown
            ak, rk = st.get("open_since"), st.get("rkill")
            if self.synckill_enabled and state in ("running", "waiting", "leaving"):
                # simultaneous mode: ONE shared timer for everyone, independent
                # of each account's uptime — all accounts show the same value
                dl = self.sync_kill_deadline
                if dl:
                    rem = dl - now
                    txt = fmt_dur(rem) if rem > 0 else "0:00"
                else:
                    # just toggled on; worker hasn't seeded the cycle yet
                    txt = fmt_dur(self._killmin_value() * 60)
                refs["ak"].configure(text=f"⇄ {txt}", text_color=ACCENT_SOFT)
            elif self.autokill_armed and state == "running" and ak and rk:
                rem = (ak + rk * 60) - now
                refs["ak"].configure(text=fmt_dur(rem) if rem > 0 else "0:00",
                                     text_color=WARN)
            else:
                refs["ak"].configure(text="—", text_color=MUTED)

    # ----------------------------------------------------------- actions --
    def _detect_clicked(self) -> None:
        place = detect_last_place()
        if place:
            self.game_entry.delete(0, "end")
            self.game_entry.insert(0, place)
            self.detect_lbl.configure(text=f"found {place}")
        else:
            self.detect_lbl.configure(text="nothing in Roblox logs")

    def _on_autokill_toggle(self) -> None:
        self.autokill_armed = self.autokill_switch.get() in ("on", 1, True)
        self._flash_switch(self.autokill_switch)
        self._save_settings()
        if self.autokill_armed:
            self._append_log("Auto-kill ON — each account refreshes after its "
                             "cooldown of open time (per-account timers on the "
                             "Monitor tab).")
        else:
            self._append_log("Auto-kill OFF.")

    def _on_synckill_toggle(self) -> None:
        self.synckill_enabled = self.synckill_switch.get() in ("on", 1, True)
        self._flash_switch(self.synckill_switch)
        if self.synckill_enabled:
            # start one shared cycle right now so every account's countdown is
            # identical immediately (the worker keeps it live from here)
            now = time.time()
            self.sync_cycle_start = now
            self.sync_kill_deadline = now + self._killmin_value() * 60
        else:
            self.sync_cycle_start = None
            self.sync_kill_deadline = None
        self._save_settings()
        if self.synckill_enabled:
            self._append_log("Simultaneous kill ON — all clients share one "
                             "cooldown and get killed together.")
        else:
            self._append_log("Simultaneous kill OFF — back to per-account "
                             "open-time cooldowns.")

    def _delay_value(self) -> int:
        try:
            return max(0, min(3600, int(self.delay_entry.get().strip() or "60")))
        except ValueError:
            return 60

    def _killmin_value(self) -> int:
        try:
            return max(1, min(720, int(self.kill_cd_entry.get().strip() or "20")))
        except (ValueError, AttributeError):
            return 20

    @staticmethod
    def _clean_int(raw, default: int = 0, lo: int = 0, hi: int = 10080) -> int:
        """Parse a user-entered integer, clamped to [lo, hi]. Blank/garbage
        falls back to default. Used for the auto-clear minute fields."""
        try:
            return max(lo, min(hi, int(str(raw).strip() or default)))
        except (ValueError, TypeError):
            return default

    def _toggle(self) -> None:
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            self._append_log("Stopping…")
            self._set_running_ui(False)
            return
        accts = self._selected_accounts()
        if not accts:
            self._append_log("No valid accounts selected — add one and tick it "
                             "on the Accounts tab.")
            self._select_tab("Accounts")
            return
        default_place = parse_place_id(self.game_entry.get())
        # Join-server mode: the share link supplies the place/server; resolution
        # happens in the worker. Just require a parseable link here.
        js_on = self.joinserver_enabled
        if js_on:
            self._joinserver_url = self.joinserver_entry.get().strip()
            js_code, _ = parse_share_link(self._joinserver_url)
            if not js_code:
                self._append_log("Join server is on but the share link is empty "
                                 "or invalid — paste a Roblox share URL on Setup, "
                                 "or switch Join server off.")
                self._select_tab("Setup")
                return
        # validate each account resolves to some place
        resolved = []
        for a in accts:
            if js_on:
                pid = self._join_cache.get("place")     # worker resolves if None
                inst = self._join_cache.get("instance")
            else:
                pid = parse_place_id(a.get("place")) or default_place
                inst = None
                if not pid:
                    self._append_log(f"{a['username']} has no place set and "
                                     "there's no default — skipping it.")
                    continue
            try:
                d = max(0, min(3600, int(a.get("delay") or "")))
            except ValueError:
                d = self._delay_value()
            try:
                km = max(1, min(720, int(a.get("killmin") or "")))
            except ValueError:
                km = self._killmin_value()
            resolved.append({**a, "rplace": pid, "rinstance": inst,
                             "rdelay": d, "rkill": km})
        if not resolved:
            self._append_log("Nothing to launch — set a default place on Setup "
                             "or a per-account place.")
            self._select_tab("Setup")
            return

        self._save_settings()
        self._refresh_discord_runtime()  # ensure worker sees current webhook
        self._refresh_live_targets()     # prime the live add/drop snapshot

        self.stop_event = threading.Event()
        self.kill_now_event.clear()
        self.acct_stats = {}
        self._select_tab("Monitor")
        self.worker = threading.Thread(
            target=self._monitor_loop, daemon=True, args=(resolved,))
        self.worker.start()
        self._set_running_ui(True)

    def _kill_now(self) -> None:
        if self.worker and self.worker.is_alive():
            # if any "close" boxes are ticked, Kill Now targets exactly those;
            # otherwise it falls back to the watch (left-column) selection
            if self.kill_selected_ids:
                self.kill_now_ids = set(self.kill_selected_ids)
                n = len(self.kill_now_ids)
                self.kill_now_event.set()
                self._append_log(
                    f"Kill Now requested for {n} account(s) marked CLOSE…")
                # reset the column so it doesn't silently target them next time
                self._clear_kill_selection()
            else:
                self.kill_now_ids = None      # None = use the watch selection
                n = (len(self.accounts) if self.select_all_flag
                     else len(self.selected_ids))
                self.kill_now_event.set()
                self._append_log(
                    f"Kill Now requested for {n} selected account(s)…")
        else:
            # not watching: close the marked clients if any, else all
            if self.kill_selected_ids:
                killed = self._kill_selected_pids()
                self._append_log(
                    f"Kill Now — not watching; closed {killed} marked "
                    f"client(s).")
                self._clear_kill_selection()
            else:
                kill_all_roblox()
                self._append_log("Kill Now — not watching, so closed all "
                                 "Roblox clients (none will rejoin).")

    def _clear_kill_selection(self) -> None:
        """Untick every CLOSE checkbox and forget the close-selection."""
        self.kill_selected_ids.clear()
        for var in self.kill_sel_vars.values():
            try:
                var.set("off")
            except Exception:
                pass

    def _kill_selected_pids(self) -> int:
        """Close Roblox clients for the kill-selected accounts (used when not
        actively watching). Best-effort match via the latest live stats."""
        killed = 0
        for uid in self.kill_selected_ids:
            st = self.acct_stats.get(uid)
            pid = st.get("pid") if st else None
            if pid:
                kill_pid(pid)
                killed += 1
        return killed

    def _set_running_ui(self, running: bool) -> None:
        if running:
            self.start_btn.configure(text="■   STOP", fg_color=CARD2,
                                     hover_color=FIELD, text_color=ACCENT_SOFT,
                                     border_width=1, border_color=BORDER2)
        else:
            self.start_btn.configure(text="▶   START", fg_color=ACCENT,
                                     hover_color=ACCENT_DARK, text_color="#ffffff",
                                     border_width=0)
        state = "disabled" if running else "normal"
        self.game_entry.configure(state=state)
        self.delay_entry.configure(state=state)

    # ----------------------------------------------------- titlebar color -
    def _style_titlebar(self) -> None:
        if not IS_WINDOWS:
            return
        try:
            self.update_idletasks()
            hwnd = user32.GetParent(self.winfo_id())
            dwm = ctypes.windll.dwmapi

            def colorref(hx: str) -> ctypes.c_int:
                r, g, b = int(hx[1:3], 16), int(hx[3:5], 16), int(hx[5:7], 16)
                return ctypes.c_int((b << 16) | (g << 8) | r)

            cap, txt = colorref(BG), colorref(TEXT)
            res = dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(cap), 4)
            dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(txt), 4)
            dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(cap), 4)
            # force a DARK titlebar to match the dark theme
            dark = ctypes.c_int(1)
            if dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), 4) != 0:
                dwm.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(dark), 4)
            # rounded corners (Windows 11): DWMWA_WINDOW_CORNER_PREFERENCE=33,
            # DWMWCP_ROUND=2 — ignored gracefully on Windows 10
            pref = ctypes.c_int(2)
            dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), 4)
            user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0027)
        except Exception:
            pass

    # ------------------------------------------------------ worker thread --
    def _notify(self, msg: str) -> None:
        dc = getattr(self, "discord_runtime", None)
        if dc and dc.get("url"):
            discord_notify(dc["url"], dc.get("name", ""), dc.get("avatar", ""), msg)

    def _poll_departures(self, rt: dict, now: float) -> None:
        """Parallel, throttled presence check for every account that's waiting
        to relaunch (state 'waiting', past its rejoin time). Writes a per-account
        '_left_ok' the transition reads, so _has_left_game never blocks the loop
        with a network call (important when sync-kill puts many accounts here at
        once)."""
        due = []
        for uid, st in rt.items():
            if not st.get("monitored") or st["state"] != "waiting":
                continue
            if now < st.get("rejoin_at", 0):
                continue                       # still in the rejoin-delay window
            # If this is a NEW kill (rejoin_at changed since we last tracked it),
            # drop any stale departure bookkeeping so we don't relaunch instantly
            # on a previous "_left_ok". Detected by comparing the rejoin_at the
            # deadline was based on.
            if st.get("_leave_for") != st.get("rejoin_at"):
                st["_leave_for"] = st.get("rejoin_at")
                st.pop("_leave_deadline", None)
                st.pop("_next_leave_poll", None)
                st.pop("_leave_logged", None)
                st["_left_ok"] = False
            # process still alive → definitely not left; no need to poll
            if st.get("pid") and st["pid"] in self._last_pids:
                st["_left_ok"] = False
                continue
            if not st["acc"].get("cookie"):
                st["_left_ok"] = True          # can't check — allow relaunch
                continue
            # hard deadline: stop waiting after LEAVE_MAX_WAIT
            dl = st.get("_leave_deadline")
            if dl is None:
                dl = now + LEAVE_MAX_WAIT
                st["_leave_deadline"] = dl
            if now >= dl:
                if not st.get("_leave_logged"):
                    st["_leave_logged"] = True
                    self._emit("log", f"{st['acc']['username']} — still shown "
                                      f"in-game after {int(LEAVE_MAX_WAIT)}s; "
                                      f"rejoining anyway.")
                st["_left_ok"] = True
                continue
            if now < st.get("_next_leave_poll", 0):
                continue                       # throttled; keep prior _left_ok
            st["_next_leave_poll"] = now + LEAVE_POLL
            due.append((uid, st["acc"]["cookie"]))
        if not due:
            return

        def _one(pair):
            uid, ck = pair
            return uid, presence_state(uid, ck)

        results: dict[int, str] = {}
        try:
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(8, max(1, len(due)))) as ex:
                for uid, state in ex.map(_one, due):
                    results[uid] = state
        except Exception:
            for uid, ck in due:
                results[uid] = presence_state(uid, ck)
        for uid, state in results.items():
            # in-game → keep waiting; "out"/"unknown" → safe enough to relaunch
            rt[uid]["_left_ok"] = (state != "ingame")

    def _has_left_game(self, uid: int, st: dict, now: float) -> bool:
        """True once it's safe to relaunch this killed/crashed account — i.e.
        Roblox no longer reports it in-game (so a new login won't collide with a
        still-live session and trigger error 264). The actual presence polling
        happens in _poll_departures (parallel, throttled); this just reads the
        result so it never blocks the watcher loop."""
        if st.get("pid") and st["pid"] in self._last_pids:
            return False               # OS process still alive → hasn't left
        if not st["acc"].get("cookie"):
            return True                # can't check — don't block the relaunch
        # default False until the first poll result arrives (so we wait rather
        # than risk an immediate 264-prone relaunch)
        return bool(st.get("_left_ok", False))

    def _running_presence(self, rt: dict, now: float) -> dict:
        """Presence for every running account, fetched ONCE per cycle and shared
        by kick-detection and collision-detection so we don't hammer the API with
        duplicate polls (which would cause rate-limited 'unknown' results and
        make both features flaky). Parallel, each with its own cookie. Memoised
        for the current loop tick. Returns {uid: (state, places, game_id)}."""
        if self._presence_snap_t == now and self._presence_snap is not None:
            return self._presence_snap
        cands = [(uid, st["acc"]["cookie"]) for uid, st in rt.items()
                 if st.get("monitored") and st["state"] == "running"
                 and st.get("pid") and st["acc"].get("cookie")]
        snap: dict = {}
        if cands:
            def _one(pair):
                uid, ck = pair
                return uid, presence_detail(uid, ck)
            try:
                with concurrent.futures.ThreadPoolExecutor(
                        max_workers=min(8, max(1, len(cands)))) as ex:
                    for uid, detail in ex.map(_one, cands):
                        snap[uid] = detail
            except Exception:
                for uid, ck in cands:
                    snap[uid] = presence_detail(uid, ck)
        self._presence_snap = snap
        self._presence_snap_t = now
        return snap

    def _check_kicks(self, rt: dict, emit, now: float) -> None:
        """Detect kicks/disconnects AND wrong-game: the client process is still
        alive but the account is either no longer in ANY game, or it's in a
        DIFFERENT game than the one configured for it. Only fires once the bad
        state has persisted for KICK_GRACE seconds (and only after the join
        settle window), so normal loading and brief teleport transitions don't
        trip it. Independent of Auto Kill."""
        if not self.kickdetect_enabled:
            return
        if now - self._last_kick_check < KICK_POLL:
            return
        # candidates: monitored, running, process alive, past the join-settle
        # grace (during which presence legitimately isn't in-game yet)
        cands = [(uid, st) for uid, st in rt.items()
                 if st.get("monitored") and st["state"] == "running"
                 and st.get("pid") and st["pid"] in self._last_pids
                 and (now - (st.get("open_since") or now)) >= PRESENCE_SETTLE
                 and st["acc"].get("cookie")]
        if not cands:
            return
        self._last_kick_check = now
        snap = self._running_presence(rt, now)

        for uid, st in cands:
            state, places, _gid = snap.get(uid, ("unknown", set(), None))
            if state == "unknown":
                continue                        # flaky lookup — never act on it
            target = str(st["acc"].get("rplace") or "") or None

            reason = None
            if state == "out":
                # not in any game — "out" is also normal while still loading, so
                # only treat it as a kick once we've CONFIRMED it was in-game
                if st.get("_was_ingame"):
                    reason = "kicked/disconnected (client open, not in-game)"
            elif state == "ingame":
                if target and target not in places:
                    # in the WRONG game. The PRESENCE_SETTLE grace already
                    # excludes the loading window and KICK_GRACE below requires
                    # it to persist, so this is safe to act on even if we never
                    # saw it in the right game (e.g. a bad initial placement).
                    reason = "in a different game than configured"
                else:
                    # in the right game (or no target to compare) → healthy
                    st["_was_ingame"] = True
                    st.pop("_kick_since", None)
                    continue

            if reason is None:
                # e.g. "out" but never confirmed in-game yet → ignore (loading)
                st.pop("_kick_since", None)
                continue

            # bad state seen; require it to persist for the grace window
            since = st.get("_kick_since")
            if since is None:
                st["_kick_since"] = now
                st["_kick_reason"] = reason
                continue
            if now - since < KICK_GRACE:
                continue
            # confirmed — close the stuck/misplaced client and rejoin
            if st.get("pid"):
                kill_pid(st["pid"])
            st["state"] = "waiting"
            st["intentional"] = True            # we closed it, not a crash
            st["rejoin_at"] = now + max(2, int(st["acc"]["rdelay"]))
            why = st.get("_kick_reason", reason)
            st.pop("_kick_since", None)
            st.pop("_kick_reason", None)
            st.pop("_was_ingame", None)
            emit("log", f"🥾 {st['acc']['username']} — {why}; "
                        f"closing & rejoining.")
            self._notify(f"🥾 {st['acc']['username']} — {why}; rejoining.")

    def _check_server_collisions(self, rt: dict, emit, now: float) -> None:
        """When 'different servers' is on, ask Roblox's presence API which server
        each running account is ACTUALLY in (Roblox can silently reroute a join
        into matchmaking, landing two accounts together). If two share a server,
        keep the alphabetically-first one and move the rest. Runs indefinitely —
        as long as a collision exists it keeps splitting them (the per-account
        settle window keeps that from becoming a tight loop)."""
        if not (self.diffserver_enabled and not self.joinserver_enabled):
            return
        if now - self._last_presence_check < PRESENCE_INTERVAL:
            return
        # Only consider accounts that have been in-game a few seconds — a freshly
        # (re)joined account's presence can briefly read its OLD server, which
        # would cause a needless double-kick. The grace window avoids that.
        running = [(uid, st) for uid, st in rt.items()
                   if st.get("monitored") and st["state"] == "running"
                   and st.get("pid")
                   and (now - (st.get("open_since") or now)) >= PRESENCE_SETTLE]
        if len(running) < 2:
            return
        self._last_presence_check = now
        # Use the shared per-cycle presence snapshot (an account sees its own
        # server reliably; this is fetched once and reused by kick-detection too
        # so we don't double-poll the API). Group by the server (gameId) each
        # account is ACTUALLY in.
        snap = self._running_presence(rt, now)
        presence: dict[int, str] = {}
        for uid, _st in running:
            state, _places, gid = snap.get(uid, ("unknown", set(), None))
            if state == "ingame" and gid:
                presence[uid] = gid
        if not presence:
            return
        # group running accounts by the server they're actually in
        by_server: dict[str, list[int]] = {}
        for uid, gid in presence.items():
            by_server.setdefault(gid, []).append(uid)

        for gid, uids in by_server.items():
            if len(uids) < 2:
                continue
            # Deterministically KEEP the account whose username sorts first
            # (A–Z); move all the others. Sorting by name is cheaper and more
            # predictable than a random 50/50 pick — the same account always
            # "wins" a given server, so accounts settle into a stable order
            # instead of being re-rolled each time. There's no cap: as long as
            # two accounts share a server they'll keep getting split, so the
            # feature works indefinitely (the per-account settle window paces it
            # so it never becomes a tight loop).
            ordered = sorted(
                uids, key=lambda u: (rt[u]["acc"].get("username") or "").lower())
            keep = ordered[0]
            for uid in ordered[1:]:
                st = rt[uid]
                self._diffserver_assigned.pop(uid, None)
                if st["pid"]:
                    kill_pid(st["pid"])
                st["state"] = "waiting"
                st["intentional"] = True       # not a crash
                st["rejoin_at"] = now + 3      # short, fixed — re-spread fast
                emit("log", f"⚠️ {st['acc']['username']} shared a server with "
                            f"{rt[keep]['acc']['username']} — moving it to a "
                            f"different one.")
                self._notify(f"🔀 {st['acc']['username']} was sharing a server; "
                             f"moving it to a different one.")


    def _launch_account(self, acc: dict, place_id: str
                        ) -> tuple[int | None, str | None]:
        # Decide the exact server target for THIS launch, explicitly, every
        # time — never inherit a stale rinstance from a previous launch/mode.
        inst = None
        if self.joinserver_enabled:
            inst = acc.get("rinstance")          # shared-server (join link) mode
        else:
            # Different-servers AND plain mode both launch via Roblox's own
            # open matchmaking (request=RequestGame, no forced server). For
            # different-servers we DON'T pick a specific server here: Roblox's
            # matchmaking never drops you into a full server (forcing a specific
            # one from the public list did, because that list's fullness data is
            # unreliable). After each account joins we read the server it
            # actually landed in (presence API) and the collision corrector
            # relaunches any that ended up sharing a server.
            inst = None
            acc["rinstance"] = None
        ticket = get_auth_ticket(acc["cookie"])
        if not ticket:
            return None, "auth failed (cookie may be expired)"
        # stable browser-tracker id per account (RAM assigns one per account so
        # Roblox can track that account's instance consistently)
        btid = acc.get("_btid")
        if not btid:
            btid = random.randint(100_000_000_000, 999_999_999_999)
            acc["_btid"] = btid
        before = roblox_pids()
        try:
            os.startfile(build_launch_uri(ticket, place_id, inst,
                                          browser_tracker_id=btid))
        except Exception as e:
            return None, f"launch error: {e}"
        deadline = time.time() + 60
        while time.time() < deadline:
            new = roblox_pids() - before
            if new:
                return max(new), None
            if self.stop_event.wait(1):
                return None, "stopped"
        return None, "Roblox window never started"

    def _new_state(self, acc: dict, state: str = "launching") -> dict:
        return {"acc": acc, "pid": None, "state": state, "joined_at": None,
                "open_since": None, "crashes": 0, "last_crash": None,
                "rejoin_at": 0.0, "intentional": False, "monitored": True}

    def _begin_launch(self, uid: int, acc: dict, emit, delay: float = 0.0) -> None:
        """Queue a launch for the single serialized launcher thread. The worker
        loop never blocks (so live add/drop stays responsive), and launches are
        processed ONE AT A TIME so the before/after PID diff can't be polluted by
        a concurrent launch — which previously made clients steal each other's
        PIDs on a mass relaunch (crash wave / simultaneous-kill)."""
        if uid in self._launching:
            return
        self._launching.add(uid)
        self._launch_queue.put((uid, acc))

    def _launcher_loop(self, tok: int, lq: "queue.Queue", rq: "queue.Queue") -> None:
        """One launch at a time. Captures its own queues so a restarted session
        can't cross-contaminate results."""
        while tok == self._launcher_token and not self.stop_event.is_set():
            try:
                item = lq.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            uid, acc = item
            if self.stop_event.is_set() or tok != self._launcher_token:
                rq.put((uid, None, "stopped"))
                break
            try:
                pid, err = self._launch_account(acc, acc["rplace"])
            except Exception as e:               # never let the launcher die
                pid, err = None, f"launch crashed: {e}"
            rq.put((uid, pid, err))

    def _drain_launches(self, rt: dict, emit, now: float) -> None:
        """Apply finished launches: mark the account running, or queue a retry."""
        try:
            while True:
                uid, pid, err = self._launch_results.get_nowait()
                self._launching.discard(uid)
                st = rt.get(uid)
                if st is None:
                    continue
                # defensive: never let two accounts hold the same PID
                if pid and any(o is not st and o.get("pid") == pid
                               and o.get("state") == "running"
                               for o in rt.values()):
                    emit("log", f"{st['acc']['username']} launch returned a PID "
                                "already in use — retrying.")
                    st["state"] = "waiting"
                    st["rejoin_at"] = now + max(8, st["acc"].get("rdelay", 8))
                    continue
                if pid:
                    st["pid"] = pid
                    st["state"] = "running"
                    st["joined_at"] = time.time()
                    st["open_since"] = time.time()
                    st["intentional"] = False
                    # reset kick-detection tracking for the new session: it must
                    # re-confirm its state before any future kick can be flagged
                    st.pop("_was_ingame", None)
                    st.pop("_kick_since", None)
                    st.pop("_kick_reason", None)
                    emit("log", f"{st['acc']['username']} is in (pid {pid}). ✓")
                    if (self.diffserver_enabled and not self.joinserver_enabled):
                        # open_since (set above) starts the PRESENCE_SETTLE grace
                        # before this account is eligible for a collision check,
                        # so we don't act on a stale server right after joining.
                        emit("log", f"   ↳ {st['acc']['username']} joined via "
                                    f"matchmaking — will verify its server next.")
                    self._notify(f"✅ {st['acc']['username']} launched.")
                    hwnd = find_window_for_pid(pid)
                    if hwnd:
                        focus_window(hwnd)
                elif err == "stopped":
                    pass
                else:
                    st["state"] = "waiting"
                    st["rejoin_at"] = now + max(8, st["acc"].get("rdelay", 8))
                    emit("log", f"{st['acc']['username']} launch failed ({err}); "
                                "retrying shortly.")
        except queue.Empty:
            pass

    def _reconcile_live(self, rt: dict, pids: set, emit, now: float) -> None:
        """Add accounts that got checked, drop ones that got unchecked — live."""
        desired = set(self._live_desired)
        resolved = dict(self._live_resolved)
        changed = False

        # unchecked -> stop monitoring, but leave the client running
        for uid, st in rt.items():
            if st.get("monitored") and uid not in desired:
                st["monitored"] = False
                emit("log", f"{st['acc']['username']} unchecked — stopped "
                            "monitoring (its client is left running).")
                self._notify(f"⏸️ Stopped monitoring {st['acc']['username']} "
                             "(left running).")
                changed = True

        # checked -> launch new, or re-attach one we stopped monitoring
        for uid in desired:
            acc = resolved.get(uid)
            if not acc:
                continue
            st = rt.get(uid)
            if st is None:
                # brand-new: create a record and kick off a non-blocking launch
                rt[uid] = self._new_state(acc, "launching")
                emit("log", f"{acc['username']} checked — launching…")
                self._begin_launch(uid, acc, emit)
                self._notify(f"➕ Now monitoring {acc['username']}.")
                changed = True
            elif not st.get("monitored"):
                st["monitored"] = True
                st["acc"] = acc
                if st.get("pid") and st["pid"] in pids:
                    st["state"] = "running"
                    if not st.get("joined_at"):
                        st["joined_at"] = now
                    st["open_since"] = now      # restart its auto-kill cooldown
                    emit("log", f"{acc['username']} re-checked — resumed "
                                "monitoring (still running).")
                elif uid in self._launching:
                    st["state"] = "launching"
                else:
                    st["state"] = "launching"
                    emit("log", f"{acc['username']} re-checked — relaunching…")
                    self._begin_launch(uid, acc, emit)
                self._notify(f"▶ Resumed monitoring {acc['username']}.")
                changed = True

        if changed:
            monitored = [(u, s["acc"]["username"]) for u, s in rt.items()
                         if s.get("monitored")]
            emit("status", (f"Watching {len(monitored)} account(s)", GOOD))
            emit("session_start", monitored)   # rebuild dashboard rows

    def _emit_live_status(self, rt: dict, emit) -> None:
        """Compute the steady-state status pill text from current account
        states so it's always accurate and never stuck on a stale message.

        Priority: any launching/rejoining → show that; else count running.
        """
        monitored = [st for st in rt.values() if st.get("monitored", True)]
        if not monitored:
            emit("status", ("No accounts selected", MUTED))
            return
        # accounts currently coming up (launching or waiting to rejoin)
        now = time.time()
        busy = [st for st in monitored
                if st["state"] in ("launching", "waiting")]
        if busy:
            if len(busy) == 1:
                b = busy[0]
                uname = b["acc"].get("username", "account")
                if b["state"] == "launching":
                    verb = "Launching"
                elif now >= b.get("rejoin_at", 0):
                    verb = "Leaving"      # held by the error-264 departure check
                else:
                    verb = "Rejoining"
                emit("status", (f"{verb} {uname}…", WARN))
            else:
                emit("status", (f"Launching {len(busy)} account(s)…", WARN))
            return
        running = sum(1 for st in monitored if st["state"] == "running")
        if running:
            emit("status", (f"Watching {running} account(s)", GOOD))
        else:
            emit("status", ("Watching…", GOOD))

    def _emit_stats(self, rt: dict) -> None:
        now = time.time()
        snap = {}
        for uid, st in rt.items():
            if not st.get("monitored", True):
                continue
            disp = st["state"]
            # A killed account that's passed its rejoin time but is still in
            # "waiting" is being held back by the departure check (_has_left_game)
            # — i.e. we're waiting for Roblox to stop reporting it in the server
            # before we relaunch (error-264 guard). Surface that as "leaving".
            if disp == "waiting" and now >= st.get("rejoin_at", 0):
                disp = "leaving"
            snap[uid] = {"state": disp, "joined_at": st["joined_at"],
                         "crashes": st["crashes"], "last_crash": st["last_crash"],
                         "open_since": st.get("open_since"),
                         "rkill": st["acc"].get("rkill")}
        self._emit("acctstat", snap)
        write_session_map({uid: st for uid, st in rt.items()
                           if st.get("monitored", True)})

    def _monitor_loop(self, accts):
        emit = self._emit
        try:
            # Join-server mode: resolve the share link here (worker thread, so a
            # blocking network call is fine) and point every account at it.
            if self.joinserver_enabled:
                code, ltype = parse_share_link(self._joinserver_url)
                place, inst = (None, None)
                if code:
                    cookie = next((a["cookie"] for a in accts
                                   if a.get("cookie")), None)
                    if not (self._join_cache.get("place")
                            and self._join_cache.get("url") == self._joinserver_url):
                        emit("log", "Resolving the Join-server share link…")
                        if cookie:
                            place, inst = resolve_share_link(cookie, code, ltype)
                    else:
                        place = self._join_cache.get("place")
                        inst = self._join_cache.get("instance")
                if not place:
                    emit("log", "⚠️ Couldn't resolve the Join-server link — "
                                "stopping. Check the URL (or turn Join server off "
                                "to use place IDs).")
                    self._notify("⚠️ Join-server link could not be resolved.")
                    emit("status", ("Join-server link failed", BAD))
                    emit("done", None)
                    return
                self._join_cache = {"url": self._joinserver_url, "place": place,
                                    "instance": inst, "err": None}
                for acc in accts:
                    acc["rplace"] = place
                    acc["rinstance"] = inst
                emit("log", f"Join server → place {place}"
                            + (f", server {inst[:8]}…" if inst else " (open server)"))

            # Different-servers mode: we DON'T pre-pick servers anymore. Each
            # account launches via Roblox's open matchmaking (which never drops
            # you into a full server). After they join we read each one's actual
            # server via the presence API and the collision corrector relaunches
            # any that ended up sharing a server. This avoids the full-server
            # joins that came from forcing servers off the unreliable public list.
            if self.diffserver_enabled and not self.joinserver_enabled:
                place = None
                for a in accts:
                    p = parse_place_id(a.get("rplace") or a.get("place") or "")
                    if p:
                        place = p
                        break
                if not place:
                    place = parse_place_id(self._diffserver_place or "")
                # reset collision bookkeeping from any previous run
                self._diffserver_assigned = {}
                self._diffserver_pool = []
                self._diffserver_place = place
                # first eligibility is governed per-account by PRESENCE_SETTLE
                # now, so don't push the whole check far into the future here
                self._last_presence_check = 0.0
                self._all_cookies = [a["cookie"] for a in accts
                                     if a.get("cookie")]
                emit("log", "Different servers: letting Roblox matchmake each "
                            "account, then checking they're on separate servers.")

            emit("log", f"Starting {len(accts)} account(s). "
                        "Multi-instance lock held.")
            emit("session_start", [(a["user_id"], a["username"]) for a in accts])
            self._notify(f"▶ RoRejoin started watching {len(accts)} account(s): "
                         + ", ".join(a["username"] for a in accts))

            # fresh launch bookkeeping for this run + start the serialized
            # launcher (one launch at a time → clean PID detection)
            self._launching = set()
            self._launch_queue = queue.Queue()
            self._launch_results = queue.Queue()
            self._launcher_token += 1
            tok = self._launcher_token
            threading.Thread(
                target=self._launcher_loop,
                args=(tok, self._launch_queue, self._launch_results),
                daemon=True).start()

            rt: dict[int, dict] = {}
            now0 = time.time()
            # "Detect open clients": adopt the Roblox windows that are ALREADY
            # running and assign them to the selected accounts, rather than
            # launching fresh clients (which errors when an account is already
            # in-game). We can't tell which PID belongs to which account from
            # the OS, so we pair open PIDs to accounts in order; any accounts
            # left over (more accounts than open clients) launch normally.
            adopt_pids: list[int] = []
            if self.detect_open_enabled:
                adopt_pids = sorted(roblox_pids())
                if adopt_pids:
                    emit("log", f"Detect open clients: found {len(adopt_pids)} "
                                f"open Roblox client(s) — adopting them.")
                else:
                    emit("log", "Detect open clients: none open — launching "
                                "normally.")

            for idx, acc in enumerate(accts):
                if self.stop_event.is_set():
                    return
                if self.detect_open_enabled and idx < len(adopt_pids):
                    # adopt an existing client: mark running immediately, no launch
                    pid = adopt_pids[idx]
                    st = self._new_state(acc, "running")
                    st["pid"] = pid
                    st["joined_at"] = now0
                    st["open_since"] = now0
                    rt[acc["user_id"]] = st
                    emit("log", f"Watching {acc['username']} on existing "
                                f"client (PID {pid}).")
                else:
                    rt[acc["user_id"]] = self._new_state(acc, "launching")
                    self._begin_launch(acc["user_id"], acc, emit)
            self._emit_stats(rt)

            prev_armed = False            # detect arm transitions to reset cooldowns
            last_tile_sig = None          # re-tile only when window set changes
            self.sync_cycle_start = None  # fresh shared cycle for this run
            self.sync_kill_deadline = None
            # status reflects ground truth (some adopted as running, some launching)
            self._emit_live_status(rt, emit)

            while not self.stop_event.is_set():
                now = time.time()
                pids = roblox_pids()
                self._last_pids = pids       # shared with _has_left_game

                # ---- apply finished background launches --------------------
                self._drain_launches(rt, emit, now)


                # ---- live add/drop as checkboxes change --------------------
                self._reconcile_live(rt, pids, emit, now)

                # ---- keep each account's cooldown/delay/place LIVE ---------
                # so changing them takes effect immediately, not next rejoin
                for uid, st in rt.items():
                    live = self._live_resolved.get(uid)
                    if st.get("monitored") and live:
                        st["acc"] = live

                # publish monitored pids so the GUI can re-layout live
                self._monitored_pids = [st["pid"] for st in rt.values()
                                        if st.get("monitored") and st.get("pid")]

                # ---- duplicate-window guard: one account, 2+ windows -------
                for uid, st in rt.items():
                    if not st.get("monitored") or st["state"] != "running":
                        continue
                    if not st.get("pid") or st["pid"] not in pids:
                        continue
                    # ignore the brief multi-window launch phase
                    if st.get("open_since") and now - st["open_since"] < 15:
                        continue
                    wins = find_all_windows_for_pid(st["pid"])
                    if len(wins) >= 2:
                        kill_pid(st["pid"])
                        st["state"] = "waiting"
                        st["intentional"] = True
                        st["rejoin_at"] = now + st["acc"]["rdelay"]
                        emit("log", f"{st['acc']['username']} had {len(wins)} "
                                    "windows (error state) — closing & rejoining.")
                        self._notify(f"⚠️ {st['acc']['username']} hit a duplicate-"
                                     "window error — closed and rejoining.")

                # ---- tile windows when enabled & the set changed -----------
                if self.tile_enabled:
                    hwnds = []
                    for st in rt.values():
                        if not st.get("monitored") or not st.get("pid"):
                            continue
                        h = find_window_for_pid(st["pid"])
                        if h:
                            hwnds.append(h)
                    sig = tuple(hwnds)
                    if sig and sig != last_tile_sig:
                        try:
                            tile_windows(hwnds)
                        except Exception as e:
                            emit("log", f"Tiling skipped: {e}")
                        last_tile_sig = sig
                else:
                    last_tile_sig = None

                # ---- apply any commands the Discord bot dropped ------------
                self._apply_remote_commands(rt, emit, now)

                # ---- arm transition: (re)start the cooldown for open clients
                if self.autokill_armed and not prev_armed:
                    for st in rt.values():
                        if st.get("monitored") and st["state"] == "running":
                            st["open_since"] = now
                prev_armed = self.autokill_armed

                # ---- simultaneous kill: HIGHEST PRIORITY, one shared timer --
                # Works whenever it's enabled (independent of Auto Kill) and
                # engages immediately for clients that are already open.
                sync_mode = self.synckill_enabled
                if sync_mode:
                    if self.sync_cycle_start is None:
                        # fresh cycle (seeded here if the toggle handler didn't):
                        # counts every monitored client whether open or not
                        self.sync_cycle_start = now
                    gkill = max(1, self._live_global_kill)
                    # deadline tracks the LIVE cooldown from the cycle start, so
                    # changing the global cooldown moves it immediately
                    self.sync_kill_deadline = self.sync_cycle_start + gkill * 60
                    if now >= self.sync_kill_deadline:
                        names = []
                        for st in rt.values():
                            if not st.get("monitored"):
                                continue
                            if st["pid"]:
                                kill_pid(st["pid"])
                            st["state"] = "waiting"
                            st["intentional"] = True
                            st["rejoin_at"] = now + st["acc"]["rdelay"]
                            names.append(st["acc"]["username"])
                        self.sync_cycle_start = now      # next shared cycle
                        self.sync_kill_deadline = now + gkill * 60
                        if names:
                            emit("log", "Simultaneous kill — ended all: "
                                 + ", ".join(names))
                            self._notify("🔪 Simultaneous kill — ended all "
                                         f"({len(names)}) on shared {gkill}min "
                                         "timer.")
                else:
                    self.sync_kill_deadline = None
                    self.sync_cycle_start = None

                manual_kill = self.kill_now_event.is_set()
                if manual_kill:
                    self.kill_now_event.clear()
                    monitored_uids = {u for u, s in rt.items() if s.get("monitored")}
                    explicit = self.kill_now_ids
                    if explicit is not None:
                        # close exactly the marked accounts (intersect monitored)
                        targets = set(explicit) & monitored_uids
                    elif self.select_all_flag:
                        targets = monitored_uids
                    else:
                        targets = set(self.selected_ids) & monitored_uids
                    self.kill_now_ids = None      # consume the one-shot selection
                    names = []
                    for uid, st in rt.items():
                        if uid not in targets:
                            continue
                        if st["pid"]:
                            kill_pid(st["pid"])
                        st["state"] = "waiting"
                        st["intentional"] = True
                        st["rejoin_at"] = now + st["acc"]["rdelay"]
                        names.append(st["acc"]["username"])
                    if names:
                        emit("log", "Kill Now — ended: " + ", ".join(names))
                        self._notify("🔪 Kill Now — ended: " + ", ".join(names))
                    else:
                        emit("log", "Kill Now — no selected account is running.")

                # ---- error-264 guard: check (in parallel) which killed/waiting
                # ---- accounts have actually left their server, before relaunch
                self._poll_departures(rt, now)

                for uid, st in rt.items():
                    if self.stop_event.is_set():
                        break
                    if not st.get("monitored"):
                        continue   # unchecked: left running, not managed
                    acc = st["acc"]
                    if st["state"] == "running":
                        if st["pid"] not in pids:
                            st["state"] = "waiting"
                            st["rejoin_at"] = now + acc["rdelay"]
                            if not st["intentional"]:
                                st["crashes"] += 1
                                st["last_crash"] = now
                                emit("log", f"{acc['username']} crashed — rejoining "
                                            f"in {acc['rdelay']}s.")
                                self._notify(f"💥 {acc['username']} crashed — "
                                             f"rejoining in {acc['rdelay']}s "
                                             f"(crash #{st['crashes']}).")
                        elif (self.autokill_armed and not sync_mode
                              and st.get("open_since")
                              and now - st["open_since"] >= acc["rkill"] * 60):
                            # per-account cooldown elapsed *while open* -> refresh
                            kill_pid(st["pid"])
                            st["state"] = "waiting"
                            st["intentional"] = True
                            st["rejoin_at"] = now + acc["rdelay"]
                            emit("log", f"Auto-kill: {acc['username']} open "
                                        f"{acc['rkill']}min — rejoining in "
                                        f"{acc['rdelay']}s.")
                            self._notify(f"🔪 Auto-kill — {acc['username']} "
                                         f"(every {acc['rkill']}min open).")
                    elif st["state"] == "waiting" and now >= st["rejoin_at"]:
                        # Don't relaunch until Roblox confirms this account has
                        # actually LEFT its game. Logging back in while the old
                        # session is still alive server-side is what triggers the
                        # "Disconnected (error 264)" boot. Covers every kill path
                        # (auto-kill, sync-kill, Kill Now, collision-move, bot
                        # rejoin, duplicate-window) plus crashes, since they all
                        # funnel through this one transition.
                        if not self._has_left_game(uid, st, now):
                            continue          # still in-game — re-check shortly
                        st["intentional"] = False
                        st["state"] = "launching"
                        for _k in ("_leave_deadline", "_next_leave_poll",
                                   "_leave_logged", "_left_ok", "_leave_for"):
                            st.pop(_k, None)
                        emit("log", f"Rejoining {acc['username']}…")
                        self._begin_launch(uid, acc, emit)

                # ---- bot-requested rejoins: kill now, watcher relaunches ------
                if self._rejoin_requests:
                    for uid in list(self._rejoin_requests):
                        st = rt.get(uid)
                        if not st or not st.get("monitored"):
                            continue
                        if st["pid"]:
                            kill_pid(st["pid"])
                        st["state"] = "waiting"
                        st["intentional"] = True       # not a crash
                        st["rejoin_at"] = now + max(2, int(st["acc"]["rdelay"]))
                        emit("log", f"{st['acc']['username']} — rejoin requested "
                                    f"from Discord; restarting.")
                    self._rejoin_requests.clear()

                # ---- kick detection: kicked/disconnected but client open -----
                self._check_kicks(rt, emit, now)

                # ---- different-servers: detect & break up shared servers -----
                self._check_server_collisions(rt, emit, now)

                # ---- recompute the steady-state status pill each cycle so it
                # ---- never gets stuck on a stale transient message ----------
                self._emit_live_status(rt, emit)
                self._emit_stats(rt)
                if self.stop_event.wait(POLL_SECONDS):
                    break

            for st in rt.values():
                st["state"] = "stopped"
            self._emit_stats(rt)
        except Exception as exc:
            import traceback
            emit("log", f"⚠️ Watcher hit an error and stopped: {exc}")
            emit("log", traceback.format_exc().strip().splitlines()[-1])
            self._notify(f"⚠️ RoRejoin watcher error: {exc}")
        finally:
            emit("status", ("Stopped", MUTED))
            write_session_map({})  # clear — nothing running
            self._notify("■ RoRejoin stopped watching.")
            emit("done")

    # ------------------------------------------------------------ config --
    def _save_settings(self) -> None:
        self._sync_account_fields()
        self.cfg["place"] = self.game_entry.get().strip()
        self.cfg["delay"] = self.delay_entry.get().strip() or "60"
        self.cfg["autokill_minutes"] = self.kill_cd_entry.get().strip() or "20"
        self.cfg["selected"] = ("all" if self.select_all_flag
                                else sorted(self.selected_ids))
        self.cfg["tile_windows"] = bool(self.tile_enabled)
        self.cfg["detect_open"] = bool(self.detect_open_enabled)
        self.cfg["kickdetect_on"] = bool(self.kickdetect_enabled)
        self.cfg["autokill_on"] = bool(self.autokill_armed)
        self.cfg["synckill_on"] = bool(self.synckill_enabled)
        if hasattr(self, "joinserver_entry"):
            self._joinserver_url = self.joinserver_entry.get().strip()
        self.cfg["joinserver_on"] = bool(self.joinserver_enabled)
        self.cfg["diffserver_on"] = bool(self.diffserver_enabled)
        # auto-maintenance intervals (minutes; 0 = disabled)
        if hasattr(self, "logclear_entry"):
            self.cfg["log_clear_min"] = self._clean_int(
                self.logclear_entry.get(), default=0, lo=0, hi=10080)
        if hasattr(self, "cacheclear_entry"):
            self.cfg["cache_clear_min"] = self._clean_int(
                self.cacheclear_entry.get(), default=0, lo=0, hi=10080)
        # region/ping features were removed (Roblox rate-limits the only APIs
        # that could power them) — drop any stale keys from older configs
        for _k in ("regions", "region_filter", "region", "max_ping"):
            self.cfg.pop(_k, None)
        self.cfg["joinserver_url"] = self._joinserver_url
        # discord
        url, name, avatar = self._gather_discord()
        dc = {"username": name.strip(), "avatar": avatar.strip()}
        if url.strip() and IS_WINDOWS:
            try:
                dc["url_enc"] = dpapi_encrypt(url.strip())
            except Exception:
                pass
        self.cfg["discord"] = dc
        # accounts (place/delay may have changed) — reuse the encrypted writer
        self._write_accounts_to_cfg()
        save_config(self.cfg)

    def _write_accounts_to_cfg(self) -> None:
        enc = []
        for a in self.accounts:
            cookie = a.get("cookie")
            if cookie:
                try:
                    blob = dpapi_encrypt(cookie)
                except Exception:
                    blob = a.get("cookie_enc", "")
            else:
                # couldn't decrypt this account earlier (e.g. config from another
                # PC) — preserve its original blob instead of dropping it
                blob = a.get("cookie_enc", "")
            if not blob:
                continue
            enc.append({"user_id": a["user_id"], "username": a["username"],
                        "cookie_enc": blob,
                        "place": a.get("place", ""), "delay": a.get("delay", ""),
                        "killmin": a.get("killmin", "")})
        self.cfg["accounts"] = enc

    def _on_close(self) -> None:
        self.stop_event.set()
        try:
            self._save_settings()
        except Exception:
            pass
        self.destroy()


def main() -> None:
    if not IS_WINDOWS:
        print(f"{APP_NAME} is Windows-only (it watches {ROBLOX_EXE}).")
        sys.exit(1)
    handles = acquire_multi_instance()
    ctk.set_appearance_mode("dark")
    App(handles).mainloop()


if __name__ == "__main__":
    main()
