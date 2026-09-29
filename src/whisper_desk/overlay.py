#!/usr/bin/env python3
"""Dictation overlay: says at every moment what whisper-desk is doing.

A standalone process launched by the daemon with the system Python (the only
one that has PyGObject). It reads its orders on stdin and answers on stdout;
the line protocol is described in overlay_protocol.py.

Why GTK3 and the X11 backend (Xwayland) rather than GTK4/Wayland: under
Wayland a client can neither refuse focus nor position itself. A window of
type NOTIFICATION under X11 does both — essential here, since stealing focus
would send the paste into the overlay instead of the target application.

Only gi + the stdlib are needed: no venv, no pycairo. Hence bars made of
widgets rather than a cairo drawing — fifteen boxes resized on every frame.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import math
import os
import signal
import sys
import threading

# X11 first: it is the only backend that allows "no focus" + positioning.
# To be set before importing gi — set_allowed_backends() would come too late.
if os.environ.get("DISPLAY") and not os.environ.get("GDK_BACKEND"):
    os.environ["GDK_BACKEND"] = "x11"

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

# Launched as a script: the protocol sits next to it, not in a package.
from overlay_protocol import FINAL_STATES, parse  # noqa: E402

WIDTH = 232
HEIGHT = 64
ACCENT = "#e46212"
POSITION = "bottom-center"
MARGIN = 96
BAR_COUNT = 15
MIC_SIZE = 24
# Margins of the equalizer area inside the pill: after the mic, before the edge.
BARS_LEFT = 66
BARS_RIGHT = 18
BAR_MIN_HEIGHT = 4         # in silence, the bar shrinks to a dot
BAR_HEIGHT_MARGIN = 20     # room left above and below, in pixels
CAPTION_HEIGHT = 26        # the line under the equalizer: hint, live text, advice
# The caption tucks itself under the equalizer rather than below its margins.
HEAD_TRIM = 10
CAPTION_PADDING = 18
# A sentence or a message needs room to be read: the card widens to this.
WIDE_WIDTH = 380
SHADOW = 14                # room around the card for its shadow, when composited
RADIUS = 20
ERROR_COLOR = "#ff6b5b"

# An equalizer is read on the rise: the bar jumps on the attack of the syllable
# and comes down slowly, like the needle of a VU meter. Two speeds, then.
ATTACK = 0.55
RELEASE = 0.14
# Without a voice, the equalizer breathes instead of freezing: a passing wave.
IDLE_WAVE = 0.10
IDLE_SPEED = 3.2
WORKING_SPEED = 4.5
LOADING_SPEED = 2.2

FADE_SECONDS = 0.16
WIDEN_RATE = 0.28          # share of the gap to the target width closed per frame
# How long a final state stays up once the daemon has said goodbye: a success
# is glanced at, an error has to be read.
LINGER = {"done": 1.1, "error": 3.2, "cancelled": 0.8, "paused": 2.2}

ICONS = {
    "loading": "audio-input-microphone-symbolic",
    "listening": "audio-input-microphone-symbolic",
    "working": "audio-input-microphone-symbolic",
    "done": "object-select-symbolic",
    "error": "dialog-warning-symbolic",
    "cancelled": "process-stop-symbolic",
    "paused": "media-playback-pause-symbolic",
}

CSS_TEMPLATE = """
#overlay-root {{
    background-color: rgba(23, 23, 26, 0.95);
    border: 1px solid rgba(255, 255, 255, 0.10);
    border-radius: {radius}px;
    box-shadow: {shadow};
}}
.bar {{
    background-color: {accent};
    border-radius: {bar_radius}px;
}}
.bar.working {{ background-color: #ffffff; }}
.icon {{ color: {accent}; }}
.icon.working, .icon.loading, .icon.cancelled, .icon.paused {{ color: rgba(255, 255, 255, 0.65); }}
.icon.error {{ color: {error}; }}
.halo {{
    background-image: radial-gradient(circle, {accent} 0%, rgba(0, 0, 0, 0) 70%);
    border-radius: 24px;
}}
.title {{
    color: #ffffff;
    font-weight: 600;
}}
.caption {{
    color: rgba(255, 255, 255, 0.50);
    font-size: small;
}}
.caption.live {{ color: rgba(255, 255, 255, 0.88); }}
"""


class EscapeKey:
    """Esc, caught without taking the focus away from the application.

    The overlay must never have the focus — the paste would land in it — so
    it cannot simply listen to its keyboard. A passive X11 grab of Esc alone
    lets every other key through, the paste shortcut included. Under a Wayland
    session it only fires while an X11 window has the focus: the global
    shortcut and a click remain the ways out that work everywhere.
    """

    KEY_PRESS = 2
    XK_ESCAPE = 0xFF1B
    # Esc must be caught with Caps Lock and Num Lock on as well.
    LOCK_MASKS = (0, 1 << 1, 1 << 4, (1 << 1) | (1 << 4))
    ERROR_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

    def __init__(self, on_press):
        self.on_press = on_press

    def start(self) -> bool:
        if os.environ.get("GDK_BACKEND") != "x11" or not os.environ.get("DISPLAY"):
            return False
        name = ctypes.util.find_library("X11")
        if not name:
            return False
        try:
            xlib = ctypes.CDLL(name)
            xlib.XOpenDisplay.restype = ctypes.c_void_p
            xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
            xlib.XDefaultRootWindow.restype = ctypes.c_ulong
            xlib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
            xlib.XKeysymToKeycode.restype = ctypes.c_ubyte
            xlib.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            xlib.XGrabKey.argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ulong,
                ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ]
            xlib.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
            xlib.XSetErrorHandler.restype = self.ERROR_HANDLER
            xlib.XSetErrorHandler.argtypes = [self.ERROR_HANDLER]
            xlib.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            xlib.XCloseDisplay.argtypes = [ctypes.c_void_p]
        except (OSError, AttributeError):
            return False

        display = xlib.XOpenDisplay(None)
        if not display:
            return False
        root = xlib.XDefaultRootWindow(display)
        keycode = xlib.XKeysymToKeycode(display, self.XK_ESCAPE)
        failed = []

        # Another client may own Esc already: Xlib's default answer to that
        # BadAccess is to exit the process. The handler is only swapped for the
        # time of the grab, before GTK starts talking to the server.
        handler = self.ERROR_HANDLER(lambda _display, _event: failed.append(True) or 0)
        previous = xlib.XSetErrorHandler(handler)
        for mask in self.LOCK_MASKS:
            xlib.XGrabKey(display, keycode, mask, root, 1, 1, 1)
        xlib.XSync(display, 0)
        xlib.XSetErrorHandler(previous)
        if failed or not keycode:
            xlib.XCloseDisplay(display)
            return False

        def listen() -> None:
            event = (ctypes.c_long * 24)()
            while True:
                xlib.XNextEvent(display, ctypes.byref(event))
                if ctypes.cast(event, ctypes.POINTER(ctypes.c_int))[0] == self.KEY_PRESS:
                    GLib.idle_add(self.on_press)

        # The grab dies with the connection, hence with the process.
        threading.Thread(target=listen, daemon=True).start()
        return True


class Overlay(Gtk.Window):
    def __init__(
        self,
        width: int,
        height: int,
        accent: str,
        position: str,
        margin: int,
        bars: int,
    ):
        super().__init__(type=Gtk.WindowType.POPUP)
        self.width, self.height = width, height
        self.position, self.margin = position, margin
        self.state = "listening"
        self._saved_clipboard: str | None = None
        self.level = 0.0
        self.smoothed = 0.0
        self.bar_count = max(bars, 1)
        # Targets sent by the daemon, and heights actually displayed: the gap
        # between the two is what gives the VU meter its inertia.
        self.targets = [0.0] * self.bar_count
        self.values = [0.0] * self.bar_count
        self.elapsed = 0.0
        self._last_frame: int | None = None
        self.progress: float | None = None
        self.hint = ""
        self.live_text = ""
        self.title = ""
        self.detail = ""
        self.escape = False
        self.cancel_sent = False
        # Closing: asked by the daemon, then the final state is left on screen
        # for its reading time, then the fade out.
        self.closing = False
        self.final_since = 0.0
        self.fading_out = False
        self.opacity = 0.0

        settings = Gtk.Settings.get_default()
        # The desktop's "reduce animations" switch reaches GTK through
        # XSETTINGS: no fade, no widening, the states simply swap.
        self.animated = bool(settings.props.gtk_enable_animations) if settings else True

        # No focus, no taskbar, no decoration: an information bubble.
        self.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
        self.set_accept_focus(False)
        self.set_focus_on_map(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_app_paintable(True)

        screen = self.get_screen()
        visual = screen.get_rgba_visual() if screen else None
        composited = visual is not None and screen.is_composited()
        if visual is not None:
            self.set_visual(visual)
        # Without a compositor the room around the card would be painted
        # black: the shadow is only drawn where it can be transparent.
        self.shadow = SHADOW if composited else 0

        # The bars share the available room: the more of them there are, the
        # thinner they get, without ever going below 2 px.
        span = max(width - BARS_LEFT - BARS_RIGHT, self.bar_count * 3)
        self.span = span
        self.pitch = span / self.bar_count
        self.bar_width = max(int(self.pitch * 0.58), 2)
        self.max_height = max(height - BAR_HEIGHT_MARGIN, BAR_MIN_HEIGHT + 2)

        provider = Gtk.CssProvider()
        provider.load_from_data(
            CSS_TEMPLATE.format(
                radius=RADIUS,
                accent=accent,
                error=ERROR_COLOR,
                bar_radius=self.bar_width // 2 + 1,
                shadow="0 4px 12px rgba(0, 0, 0, 0.40)" if self.shadow else "none",
            ).encode()
        )
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        # frame: the shadow's room — card: what the user sees.
        self.frame = Gtk.Box()
        self.frame.set_border_width(self.shadow)
        self.frame.set_opacity(0.0 if self.animated else 1.0)
        self.add(self.frame)
        self.card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.card.set_name("overlay-root")
        self.frame.pack_start(self.card, True, True, 0)

        head_height = height - HEAD_TRIM
        # Mic and equalizer stay together, centred, when the card widens.
        self.head = head = Gtk.Box()
        head.set_halign(Gtk.Align.CENTER)
        head.set_size_request(-1, head_height)
        self.card.pack_start(head, False, False, 0)

        self.lead = lead = Gtk.Fixed()
        lead.set_size_request(BARS_LEFT, head_height)
        head.pack_start(lead, False, False, 0)
        self.halo = Gtk.Box()
        self.halo.get_style_context().add_class("halo")
        self.halo.set_size_request(48, 48)
        self.halo.set_opacity(0.0)
        lead.put(self.halo, 10, head_height // 2 - 24)
        self.icon = Gtk.Image.new_from_icon_name(ICONS["listening"], Gtk.IconSize.DND)
        self.icon.set_pixel_size(MIC_SIZE)
        self.icon.get_style_context().add_class("icon")
        lead.put(self.icon, 34 - MIC_SIZE // 2, head_height // 2 - MIC_SIZE // 2)

        # The equalizer and the message of a final state take turns in the
        # same place: one says it listens, the other says how it ended.
        self.body = Gtk.Stack()
        self.body.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.body.set_transition_duration(150)
        self.body.set_hhomogeneous(False)
        head.pack_start(self.body, True, True, 0)

        self.bars_area = Gtk.Fixed()
        self.bars_area.set_size_request(span, head_height)
        self.bars_area.set_halign(Gtk.Align.CENTER)
        self.body.add_named(self.bars_area, "bars")

        message = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        message.set_valign(Gtk.Align.CENTER)
        self.title_label = self._label("title", Pango.EllipsizeMode.END)
        self.title_label.set_xalign(0.0)
        message.pack_start(self.title_label, False, False, 0)
        self.detail_label = self._label("caption", Pango.EllipsizeMode.END)
        self.detail_label.set_xalign(0.0)
        message.pack_start(self.detail_label, False, False, 0)
        self.body.add_named(message, "message")

        spacer = Gtk.Box()
        spacer.set_size_request(BARS_RIGHT, head_height)
        head.pack_start(spacer, False, False, 0)

        self.caption = self._label("caption", Pango.EllipsizeMode.END)
        self.caption.set_size_request(-1, CAPTION_HEIGHT)
        self.caption.set_valign(Gtk.Align.START)
        self.caption.set_margin_start(CAPTION_PADDING)
        self.caption.set_margin_end(CAPTION_PADDING)
        self.card.pack_start(self.caption, False, False, 0)
        # Shown or hidden by the state alone, whatever show_all() thinks of it.
        for label in (self.caption, self.detail_label):
            label.set_no_show_all(True)
        self.caption.show()

        self.center_y = head_height // 2
        self.bars: list[Gtk.Box] = []
        self.bar_x: list[int] = []
        # Height applied to each bar: remembering it avoids a resize — and hence
        # a window recomputation — when nothing has moved by a single pixel.
        self.bar_heights = [0] * self.bar_count
        for index in range(self.bar_count):
            bar = Gtk.Box()
            bar.get_style_context().add_class("bar")
            bar.set_size_request(self.bar_width, BAR_MIN_HEIGHT)
            x = int(index * self.pitch + (self.pitch - self.bar_width) / 2)
            self.bars_area.put(bar, x, self.center_y - BAR_MIN_HEIGHT // 2)
            self.bars.append(bar)
            self.bar_x.append(x)
            self.bar_heights[index] = BAR_MIN_HEIGHT

        # A stack only shows a child that is visible: they all are, from the start.
        self.card.show_all()
        self.card_width = float(width)
        self.target_width = width
        self._resizing = False
        self._apply_width(width)

        # A click answers even without the focus: it cancels, or dismisses.
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.connect("button-press-event", lambda _widget, _event: self.on_user_cancel())
        self.connect("realize", lambda _widget: self._place())
        self.add_tick_callback(self._tick)

    def _label(self, style: str, ellipsize: Pango.EllipsizeMode) -> Gtk.Label:
        label = Gtk.Label()
        label.get_style_context().add_class(style)
        label.set_ellipsize(ellipsize)
        label.set_single_line_mode(True)
        # Without this cap, the natural width of a long sentence would push
        # the window wider than the card: the card decides, the text gives in.
        label.set_max_width_chars(1)
        label.set_hexpand(True)
        return label

    # -- placement ---------------------------------------------------------
    def _card_height(self) -> int:
        return self.height - HEAD_TRIM + CAPTION_HEIGHT

    def _place(self) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        if monitor is None:
            return
        area = monitor.get_workarea()
        width = int(self.card_width) + 2 * self.shadow
        height = self._card_height() + 2 * self.shadow
        x = area.x + (area.width - width) // 2
        # The margin is measured from the card, not from its shadow.
        if self.position == "top-center":
            y = area.y + self.margin - self.shadow
        elif self.position == "center":
            y = area.y + (area.height - height) // 2
        else:  # bottom-center
            y = area.y + area.height - self._card_height() - self.margin - self.shadow
        self.move(x, y)

    def _apply_width(self, width: int) -> None:
        self.card.set_size_request(width, self._card_height())
        self.resize(width + 2 * self.shadow, self._card_height() + 2 * self.shadow)
        if self.get_realized():
            self._place()

    def _widen(self, wide: bool, text: str) -> None:
        """Gives the card the width its caption needs, up to the wide format."""
        wide_width = max(self.width, WIDE_WIDTH)
        if wide:
            self.target_width = wide_width
        else:
            needed, _height = self.caption.create_pango_layout(text).get_pixel_size()
            self.target_width = max(self.width, min(needed + 2 * CAPTION_PADDING, wide_width))
        if not self.animated:
            self.card_width = float(self.target_width)
            self._apply_width(self.target_width)

    # -- commands ----------------------------------------------------------
    def set_state(self, state: str, title: str = "", detail: str = "") -> None:
        previous, self.state = self.state, state
        final = state in FINAL_STATES
        if final and previous not in FINAL_STATES:
            self.final_since = self.elapsed
        self.title, self.detail = title, detail
        if state != "loading":
            self.progress = None

        self.icon.set_from_icon_name(ICONS.get(state, ICONS["listening"]), Gtk.IconSize.DND)
        self.icon.set_pixel_size(MIC_SIZE)
        context = self.icon.get_style_context()
        for name in ICONS:
            context.remove_class(name)
        context.add_class(state)
        working = state in ("working", "loading")
        for bar in self.bars:
            bar_context = bar.get_style_context()
            if state == "working":
                bar_context.add_class("working")
            else:
                bar_context.remove_class("working")
        if working or final:
            self.halo.set_opacity(0.0)
            # Nobody feeds the levels any more: start from zero, otherwise
            # going back to listening would replay the last syllable heard.
            self.level = 0.0
            self.targets = [0.0] * self.bar_count

        if final:
            self.title_label.set_text(title)
            self.detail_label.set_text(detail)
            self.detail_label.set_visible(bool(detail))
            self.body.set_visible_child_name("message")
            self.icon.set_opacity(1.0)
        else:
            self.body.set_visible_child_name("bars")
        self._layout(final)
        self._refresh_caption()

    def _layout(self, final: bool) -> None:
        """A final state is one message beside its icon: the caption line joins the head."""
        height = self._card_height() if final else self.height - HEAD_TRIM
        self.head.set_halign(Gtk.Align.FILL if final else Gtk.Align.CENTER)
        self.head.set_size_request(-1, height)
        self.lead.set_size_request(BARS_LEFT, height)
        self.lead.move(self.halo, 10, height // 2 - 24)
        self.lead.move(self.icon, 34 - MIC_SIZE // 2, height // 2 - MIC_SIZE // 2)
        self.caption.set_visible(not final)

    def set_text(self, text: str) -> None:
        self.live_text = " ".join(text.split())
        self._refresh_caption()

    def set_hint(self, hint: str) -> None:
        self.hint = hint
        self._refresh_caption()

    def set_progress(self, progress: float) -> None:
        self.progress = progress
        self._refresh_caption()

    def _full_hint(self) -> str:
        cancel = "Esc to cancel" if self.escape else "click to cancel"
        return " · ".join(part for part in (self.hint, cancel) if part)

    def _refresh_caption(self) -> None:
        """The line under the equalizer, which says the most useful thing of the moment."""
        live = False
        if self.state in FINAL_STATES:
            self._widen(True, "")
            return
        if self.state == "loading":
            text = self.title or "Loading the model…"
            if self.progress is not None:
                text = f"{text} {round(self.progress * 100)} %"
        elif self.live_text:
            text, live = self.live_text, True
        elif self.state == "working":
            text = "Transcribing…"
        else:
            text = self._full_hint()

        context = self.caption.get_style_context()
        if live:
            context.add_class("live")
            # The end of the sentence is the part just spoken: it is the
            # beginning that gives way.
            self.caption.set_ellipsize(Pango.EllipsizeMode.START)
            self.caption.set_xalign(0.0)
        else:
            context.remove_class("live")
            self.caption.set_ellipsize(Pango.EllipsizeMode.END)
            self.caption.set_xalign(0.5)
        self.caption.set_text(text)
        self._widen(live, text)

    def on_user_cancel(self) -> None:
        """Esc or a click: the dictation is dropped, or the final state dismissed."""
        if self.state in FINAL_STATES:
            self.closing = True
            self.final_since = -math.inf
            return
        if self.cancel_sent:
            return
        self.cancel_sent = True
        try:
            print("cancel", flush=True)
        except (BrokenPipeError, OSError, ValueError):
            pass

    def close(self) -> None:
        """The daemon is done with the window: it leaves once it has been read."""
        self.closing = True

    def set_level(self, level: float, bands: list[float]) -> None:
        self.level = max(0.0, min(level, 1.0))
        if bands:
            self.targets = [max(0.0, min(band, 1.0)) for band in self._fit(bands)]
        else:
            # No spectrum available: the overall volume animates every bar, in a
            # bell shape, to keep an equalizer silhouette rather than a block.
            middle = (self.bar_count - 1) / 2
            self.targets = [
                self.level * (1.0 - 0.55 * abs(index - middle) / max(middle, 1.0))
                for index in range(self.bar_count)
            ]

    def _fit(self, bands: list[float]) -> list[float]:
        """Maps the received bands onto the number of bars, if the daemon differs."""
        if len(bands) == self.bar_count:
            return bands
        ratio = len(bands) / self.bar_count
        return [bands[min(int(index * ratio), len(bands) - 1)] for index in range(self.bar_count)]

    def copy(self, text: str) -> None:
        """Puts the text in the clipboard (X11: no focus needed)."""
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(text, -1)
        clipboard.store()

    def save_clipboard(self) -> None:
        """Saves the user's clipboard before requisitioning it."""
        if self._saved_clipboard is None:
            self._saved_clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).wait_for_text() or ""

    def restore_clipboard(self) -> None:
        if self._saved_clipboard is None:
            return
        if self._saved_clipboard:
            self.copy(self._saved_clipboard)
        self._saved_clipboard = None

    # -- animation ---------------------------------------------------------
    def _tick(self, _widget: Gtk.Widget, clock) -> bool:
        now = clock.get_frame_time()
        step = 0.0
        if self._last_frame is not None:
            step = (now - self._last_frame) / 1_000_000
            self.elapsed += step
        self._last_frame = now
        self.smoothed += (self.level - self.smoothed) * ATTACK

        if not self._fade(step):
            return GLib.SOURCE_REMOVE
        if abs(self.target_width - self.card_width) >= 0.5 and not self._resizing:
            self.card_width += (self.target_width - self.card_width) * WIDEN_RATE
            if abs(self.target_width - self.card_width) < 1.0:
                self.card_width = float(self.target_width)
            # Resizing from inside the frame clock freezes it under X11: the
            # window is resized just after the frame instead.
            self._resizing = True
            GLib.idle_add(self._resize_step)

        if self.state in FINAL_STATES:
            return GLib.SOURCE_CONTINUE
        listening = self.state == "listening"
        if listening:
            breathe = 0.5 + 0.5 * math.sin(self.elapsed * 3.5)
            self.halo.set_opacity(0.10 + 0.22 * self.smoothed + 0.05 * breathe)
            self.icon.set_opacity(0.85 + 0.15 * breathe)
        else:
            self.icon.set_opacity(0.7)

        for index in range(self.bar_count):
            target = self._target(index)
            # A sharp rise on the attack, a slow fall: it is that asymmetry
            # that reads as an equalizer rather than as flickering.
            rate = ATTACK if target > self.values[index] else RELEASE
            self.values[index] += (target - self.values[index]) * rate
            self._draw_bar(index, self.values[index])
        return GLib.SOURCE_CONTINUE

    def _resize_step(self) -> bool:
        self._resizing = False
        self._apply_width(round(self.card_width))
        return GLib.SOURCE_REMOVE

    def _fade(self, step: float) -> bool:
        """Fades in, then out once closing; False when the window is gone."""
        if self.closing and not self.fading_out:
            linger = LINGER.get(self.state, 0.0)
            if self.state not in FINAL_STATES or self.elapsed - self.final_since >= linger:
                self.fading_out = True
        change = step / FADE_SECONDS if self.animated else 1.0
        if self.fading_out:
            self.opacity = max(self.opacity - change, 0.0)
        else:
            self.opacity = min(self.opacity + change, 1.0)
        self.frame.set_opacity(self.opacity)
        if self.fading_out and self.opacity <= 0.0:
            Gtk.main_quit()
            return False
        return True

    def _target(self, index: int) -> float:
        """Height aimed at by a bar, in 0..1."""
        if self.state == "loading":
            if self.progress is not None:
                # The equalizer turns into a progress gauge, left to right.
                filled = self.progress * self.bar_count
                return 0.30 if index < filled else 0.04
            phase = self.elapsed * LOADING_SPEED - index * 0.35
            return 0.06 + 0.16 * max(math.sin(phase), 0.0) ** 2
        if self.state != "listening":
            # Transcription in progress: a wave crosses the equalizer from left
            # to right, to say it is working without pretending to listen.
            phase = self.elapsed * WORKING_SPEED - index * 0.55
            return 0.10 + 0.32 * max(math.sin(phase), 0.0) ** 2
        wave = 0.5 + 0.5 * math.sin(self.elapsed * IDLE_SPEED - index * 0.45)
        return max(self.targets[index], IDLE_WAVE * wave)

    def _draw_bar(self, index: int, value: float) -> None:
        height = BAR_MIN_HEIGHT + int((self.max_height - BAR_MIN_HEIGHT) * value)
        bar = self.bars[index]
        if height != self.bar_heights[index]:
            self.bar_heights[index] = height
            bar.set_size_request(self.bar_width, height)
            self.bars_area.move(bar, self.bar_x[index], self.center_y - height // 2)
        if self.state == "listening":
            bar.set_opacity(0.45 + 0.55 * value)
        elif self.state == "loading" and self.progress is not None:
            bar.set_opacity(1.0 if value > 0.1 else 0.35)
        else:
            bar.set_opacity(0.30 + 0.60 * value)


def read_commands(window: Overlay) -> None:
    for line in sys.stdin:
        parsed = parse(line)
        if parsed is None:
            continue
        command, args = parsed
        if command == "quit":
            break
        handler = {
            "saveclip": window.save_clipboard,
            "restoreclip": window.restore_clipboard,
            "state": window.set_state,
            "level": window.set_level,
            "text": window.set_text,
            "hint": window.set_hint,
            "progress": window.set_progress,
            "copy": window.copy,
        }[command]
        GLib.idle_add(lambda handler=handler, args=args: handler(*args) and False)
    GLib.idle_add(window.close)


def main(argv: list[str]) -> int:
    width = int(argv[1]) if len(argv) > 1 else WIDTH
    height = int(argv[2]) if len(argv) > 2 else HEIGHT
    accent = argv[3] if len(argv) > 3 else ACCENT
    position = argv[4] if len(argv) > 4 else POSITION
    margin = int(argv[5]) if len(argv) > 5 else MARGIN
    bars = int(argv[6]) if len(argv) > 6 else BAR_COUNT

    window = Overlay(width, height, accent, position, margin, bars)
    window.escape = EscapeKey(window.on_user_cancel).start()
    window._refresh_caption()
    window.connect("destroy", Gtk.main_quit)
    # A new dictation dismisses the window still showing the last one: the
    # orders already queued — a clipboard to hand back — go through first.
    GLib.unix_signal_add(GLib.PRIORITY_LOW, signal.SIGTERM, lambda: Gtk.main_quit() or False)
    window.show_all()
    threading.Thread(target=read_commands, args=(window,), daemon=True).start()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
