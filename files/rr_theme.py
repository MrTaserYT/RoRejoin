"""RoRejoin — colour palette and small UI formatting helpers.

The iOS-dark purple palette plus the pure helpers (colour blending, duration /
\"time ago\" formatting) used across the GUI. No dependencies on the rest of the
app, so anything can import these freely.
"""

from __future__ import annotations

import time

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


