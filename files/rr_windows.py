"""RoRejoin — Windows process & window management, plus client screenshots.

Finding/killing Roblox processes, locating and focusing their windows, tiling
or stacking those windows on screen, and grabbing a PNG of a single client for
the dashboard. All of the Win32/ctypes work lives here.
"""

from __future__ import annotations

import base64
import ctypes
import math
import subprocess
import time
from ctypes import wintypes

from rr_const import (CREATE_NO_WINDOW, ROBLOX_EXE, IS_WINDOWS,
                      user32, kernel32, gdi32)

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


# ─────────────────────────────────── joke: which executor is open? ─────────
# Maps a running process' executable base-name (lowercased, no ".exe") to a
# (display name, totally-serious-and-scientific rating /10). Matching is by
# exact base-name so we don't false-positive on random processes. Researched
# from 2026 executor listings (WEAO / executor trackers). Windows-only — mobile
# (Android/iOS) and macOS executors are intentionally left out since they can't
# run as a Windows process anyway. Purely a gag.
EXECUTOR_RATINGS: dict[str, tuple[str, int]] = {
    "volt":         ("Volt", 10),
    "wave":         ("Wave", "RAT"),
    "seliware":     ("Seliware", "6"),
    "potassium":    ("Potassium", "Tuff"),
    "synapse":      ("Synapse", 7),
    "synapsez":     ("Synapse Z", 7),
    "scriptware":   ("Script-Ware", 7),
    "matcha":       ("Matcha", 3),
    "velocity":     ("Velocity", 6),
    "madium":       ("Madium", 7),
    "bytebreaker":  ("ByteBreaker", 5),
    "swift":        ("Swift", 5),
    "codex":        ("Codex", 5),
    "fluxus":       ("Fluxus", 4),
    "krnl":         ("Krnl", 4),
    "valex":        ("Valex", 3),
    "evon":         ("Evon", "SAKRAT"),
    "luna":         ("Luna", 3),
    "solara":       ("Solara", "ASS"),
    "xeno":         ("Xeno", "TRASH"),
    "nihon":        ("Nihon", 2),
    "comet":        ("Comet", 1),
    "jjsploit":     ("JJSploit", 1),
}


def _matches_executor(text: str) -> tuple[str, int] | None:
    """Given a window title or process base-name, return the (name, rating) of
    the executor it represents, or None. Matching is anchored to the START of
    the cleaned text (executors put their brand first, e.g. 'Wave 4.2',
    'Xeno v1.0.0', 'Solara - ...') so we don't false-positive on unrelated
    windows that merely contain a word like 'wave' somewhere in the middle."""
    if not text:
        return None
    low = text.lower().strip()
    # collapse separators so "script-ware", "script ware", "scriptware" all match
    norm = (low.replace("-", "").replace("_", "").replace("|", " ")
               .replace(":", " "))
    norm_nospace = norm.replace(" ", "")
    first_word = norm.split()[0] if norm.split() else ""
    # check more-specific (longer) keys first so e.g. "synapsez" wins over
    # "synapse" and the more precise display name is shown
    for key in sorted(EXECUTOR_RATINGS, key=len, reverse=True):
        val = EXECUTOR_RATINGS[key]
        # exact, or brand at the very start followed by version/extra text
        if (norm_nospace == key
                or first_word == key
                or norm_nospace.startswith(key) and (
                    len(norm_nospace) == len(key)
                    or not norm_nospace[len(key)].isalpha())):
            return val
    return None


def _enum_window_titles() -> list[str]:
    """Titles of every visible, titled top-level window on screen."""
    if user32 is None:
        return []
    titles: list[str] = []
    try:
        user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                          ctypes.c_int]
    except Exception:
        pass
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _cb(h, _l):
        try:
            if user32.IsWindowVisible(h):
                n = user32.GetWindowTextLengthW(h)
                if n and n > 0:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    user32.GetWindowTextW(h, buf, n + 1)
                    t = buf.value.strip()
                    if t:
                        titles.append(t)
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(proto(_cb), 0)
    except Exception:
        pass
    return titles


def detect_executor() -> tuple[str, int] | None:
    """Figure out which Roblox executor is open and return its (name, joke
    rating). Primary signal is the OPEN WINDOW's title (most executors brand the
    window even when their process name is randomised to dodge detection);
    process image names are used as a secondary signal. Returns the
    highest-rated match, or None if nothing is open. Joke feature only."""
    if not IS_WINDOWS:
        return None
    best: tuple[str, int] | None = None

    # 1) window titles — the reliable signal
    for title in _enum_window_titles():
        hit = _matches_executor(title)
        if hit and (best is None or hit[1] > best[1]):
            best = hit

    # 2) process image names — catch executors whose window we missed
    try:
        out = subprocess.check_output(
            ["tasklist", "/FO", "CSV", "/NH"],
            creationflags=CREATE_NO_WINDOW, stderr=subprocess.DEVNULL,
            encoding="utf-8", errors="ignore")
    except Exception:
        out = ""
    for line in out.splitlines():
        first = line.split('","', 1)[0].strip().strip('"')
        if first.lower().endswith(".exe"):
            hit = _matches_executor(first[:-4])
            if hit and (best is None or hit[1] > best[1]):
                best = hit
    return best


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


