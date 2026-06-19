"""RoRejoin — custom widgets.

The iOS-style animated toggle switch used throughout the UI, plus a small
vsync-aware frame-timer helper for its animation.
"""

from __future__ import annotations

import time
import tkinter as tk

import customtkinter as ctk

from rr_theme import ACCENT, CARD2, SWITCH_OFF, TEXT

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
