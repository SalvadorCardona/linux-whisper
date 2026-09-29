#!/usr/bin/env python3
"""Welcome window: the first launch, step by step, until a first dictation.

Launched with the system Python, like the other windows; welcome_proc.py
drives it over JSON lines:

    stdin   {"values", "choices", "shortcut", "gpu"}   what to offer
            {"level": 0.42, "meter": "..."}             the microphone gauge
            {"model": {"name", "loaded", "download", "error"}}
            {"tried": "recording"}  {"status": "..."}
    stdout  {"action": "test_mic" | "stop_mic", "device": "..."}
            {"action": "prepare", "values": {...}}
            {"action": "try"}  {"action": "finish"}
"""

from __future__ import annotations

import json
import sys
import threading

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

ACCENT = "#e46212"
CSS = f"""
.headline {{ font-size: x-large; font-weight: bold; }}
.lead {{ color: alpha(currentColor, 0.75); }}
.hint {{ color: alpha(currentColor, 0.60); font-size: small; }}
.keycap {{
    font-size: x-large;
    font-weight: bold;
    padding: 10px 22px;
    border: 1px solid alpha(currentColor, 0.25);
    border-bottom-width: 3px;
    border-radius: 10px;
}}
.trial {{ border: 1px solid alpha(currentColor, 0.20); border-radius: 8px; padding: 10px; }}
levelbar block.filled, progressbar progress {{ background-color: {ACCENT}; border-color: {ACCENT}; }}
"""

STEPS = ("microphone", "model", "download", "try")


def send(message: dict) -> None:
    try:
        print(json.dumps(message, ensure_ascii=False), flush=True)
    except (BrokenPipeError, OSError, ValueError):
        pass


def text(label: str, style: str = "lead") -> Gtk.Label:
    widget = Gtk.Label(label=label)
    widget.set_xalign(0.0)
    widget.set_line_wrap(True)
    widget.set_max_width_chars(58)
    widget.get_style_context().add_class(style)
    return widget


class WelcomeWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="Welcome to whisper-desk")
        self.set_default_size(620, 440)
        self.set_icon_name("audio-input-microphone")
        self.set_position(Gtk.WindowPosition.CENTER)
        self.step = 0
        self.values: dict = {}
        self.loaded = False
        self.prepared = False

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_screen(
            self.get_screen(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(layout)
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        layout.pack_start(self.stack, True, True, 0)

        self.stack.add_named(self._microphone_page(), "microphone")
        self.stack.add_named(self._model_page(), "model")
        self.stack.add_named(self._download_page(), "download")
        self.stack.add_named(self._try_page(), "try")

        layout.pack_start(Gtk.Separator(), False, False, 0)
        footer = Gtk.Box(spacing=8)
        footer.set_border_width(12)
        layout.pack_start(footer, False, False, 0)
        self.progress_label = Gtk.Label()
        self.progress_label.get_style_context().add_class("hint")
        footer.pack_start(self.progress_label, False, False, 0)
        self.next_button = Gtk.Button(label="Next")
        self.next_button.get_style_context().add_class("suggested-action")
        self.next_button.connect("clicked", lambda _button: self._go(self.step + 1))
        footer.pack_end(self.next_button, False, False, 0)
        self.back_button = Gtk.Button(label="Back")
        self.back_button.connect("clicked", lambda _button: self._go(self.step - 1))
        footer.pack_end(self.back_button, False, False, 0)

        self.connect("delete-event", lambda _widget, _event: send({"action": "stop_mic"}) or False)

    # -- pages -------------------------------------------------------------
    @staticmethod
    def _page(headline: str, lead: str) -> Gtk.Box:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        page.set_border_width(32)
        page.pack_start(text(headline, "headline"), False, False, 0)
        page.pack_start(text(lead), False, False, 0)
        return page

    def _microphone_page(self) -> Gtk.Box:
        page = self._page(
            "Dictation that stays on your computer",
            "whisper-desk turns your voice into text at the cursor, in any application, "
            "without anything leaving the machine. First, the microphone: speak, the gauge "
            "must move.",
        )
        self.device = Gtk.ComboBoxText()
        self.device.connect("changed", lambda _combo: self._test_microphone())
        page.pack_start(self.device, False, False, 6)
        self.meter = Gtk.LevelBar()
        self.meter.set_min_value(0.0)
        self.meter.set_max_value(1.0)
        page.pack_start(self.meter, False, False, 0)
        self.meter_hint = text("Nothing moves? Choose another microphone above.", "hint")
        page.pack_start(self.meter_hint, False, False, 0)
        return page

    def _model_page(self) -> Gtk.Box:
        page = self._page(
            "The language you speak, the model that listens",
            "The model runs on this computer. A larger one understands better, and takes "
            "more time and memory; it is downloaded once.",
        )
        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        self.language = Gtk.ComboBoxText()
        self.model = Gtk.ComboBoxText()
        self.language.set_hexpand(True)
        for row, (title, widget) in enumerate((("Language", self.language), ("Model", self.model))):
            label = Gtk.Label(label=title)
            label.set_xalign(1.0)
            label.get_style_context().add_class("dim-label")
            grid.attach(label, 0, row, 1, 1)
            grid.attach(widget, 1, row, 1, 1)
        page.pack_start(grid, False, False, 6)
        self.gpu_hint = text("", "hint")
        page.pack_start(self.gpu_hint, False, False, 0)
        return page

    def _download_page(self) -> Gtk.Box:
        page = self._page(
            "Getting the model ready",
            "The model is downloaded once, then loaded in memory: the following dictations "
            "start at once. You may go on meanwhile.",
        )
        self.download = Gtk.ProgressBar()
        self.download.set_show_text(True)
        page.pack_start(self.download, False, False, 10)
        self.download_hint = text("", "hint")
        page.pack_start(self.download_hint, False, False, 0)
        return page

    def _try_page(self) -> Gtk.Box:
        page = self._page(
            "Your shortcut",
            "Press it, speak, pause: the sentence is written where the cursor is. Press it "
            "again to finish — Esc or a click on the overlay cancels.",
        )
        self.keycap = Gtk.Label(label="…")
        self.keycap.get_style_context().add_class("keycap")
        self.keycap.set_halign(Gtk.Align.START)
        page.pack_start(self.keycap, False, False, 4)
        frame = Gtk.ScrolledWindow()
        frame.set_min_content_height(90)
        frame.get_style_context().add_class("trial")
        self.trial = Gtk.TextView()
        self.trial.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        frame.add(self.trial)
        page.pack_start(frame, False, False, 0)
        row = Gtk.Box(spacing=10)
        self.try_button = Gtk.Button(label="Start a test dictation")
        self.try_button.connect("clicked", self._on_try)
        row.pack_start(self.try_button, False, False, 0)
        self.try_hint = text("The text will appear in the box above.", "hint")
        row.pack_start(self.try_hint, True, True, 0)
        page.pack_start(row, False, False, 0)
        page.pack_start(text(
            "Later: whisper-desk settings to change all this, whisper-desk history to find "
            "a dictation again, and the tray icon when your desktop shows one.", "hint",
        ), False, False, 8)
        return page

    # -- navigation --------------------------------------------------------
    def _go(self, step: int) -> None:
        if step >= len(STEPS):
            send({"action": "finish"})
            self.close()
            return
        step = max(step, 0)
        leaving, self.step = STEPS[self.step], step
        if leaving == "microphone":
            send({"action": "stop_mic"})
        name = STEPS[step]
        self.stack.set_visible_child_name(name)
        if name == "microphone":
            self._test_microphone()
        if name == "download" and not self.prepared:
            self.prepared = True
            self._set_download(None, "Starting…")
            send({"action": "prepare", "values": self.collect()})
        if name == "try":
            self.trial.grab_focus()
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        name = STEPS[self.step]
        self.back_button.set_sensitive(self.step > 0)
        self.progress_label.set_text(f"Step {self.step + 1} of {len(STEPS)}")
        if name == "try":
            self.next_button.set_label("Finish")
        elif name == "download" and not self.loaded:
            self.next_button.set_label("Continue meanwhile")
        else:
            self.next_button.set_label("Next")

    def collect(self) -> dict:
        return {
            **self.values,
            "device": self.device.get_active_id() or "default",
            "language": self.language.get_active_id(),
            "model": self.model.get_active_id(),
        }

    def _test_microphone(self) -> None:
        if STEPS[self.step] == "microphone" and self.device.get_active_id():
            send({"action": "test_mic", "device": self.device.get_active_id()})

    def _on_try(self, _button: Gtk.Button) -> None:
        # The text lands where the focus is: in the box, not on the button.
        self.trial.grab_focus()
        send({"action": "try"})

    def _set_download(self, fraction: float | None, label: str) -> None:
        if fraction is None:
            self.download.pulse()
        else:
            self.download.set_fraction(fraction)
        self.download.set_text(label)

    # -- orders from the command line --------------------------------------
    def show_state(self, message: dict) -> bool:
        if "values" in message and "choices" in message:
            self.values = dict(message["values"])
            choices = message["choices"]
            for combo, key in ((self.device, "device"), (self.language, "language"),
                               (self.model, "model")):
                combo.remove_all()
                for value, label in choices[key]:
                    combo.append(value, label)
                combo.set_active_id(self.values[key])
            self.keycap.set_text(message.get("shortcut", "…"))
            self.gpu_hint.set_text(
                "An NVIDIA GPU was found: large-v3-turbo is fast and excellent."
                if message.get("gpu") else
                "No NVIDIA GPU: transcription runs on the CPU, where small is the right balance."
            )
            self._refresh_buttons()
            self._test_microphone()
        if "level" in message:
            self.meter.set_value(float(message["level"]))
        if message.get("meter"):
            self.meter_hint.set_text(message["meter"])
        if "model" in message:
            self._show_model(message["model"])
        if "tried" in message:
            self.try_hint.set_text(
                "Listening — speak, pause, then press the shortcut again."
                if message["tried"] == "recording" else "The dictation is ending…"
            )
        if message.get("status") and STEPS[self.step] == "try":
            self.try_hint.set_text(message["status"])
        return GLib.SOURCE_REMOVE

    def _show_model(self, model: dict) -> None:
        if model.get("error"):
            self._set_download(0.0, "The service does not answer")
            self.download_hint.set_text(f"{model['error']} — see whisper-desk doctor.")
            return
        name = model.get("name", "")
        if model.get("loaded"):
            self.loaded = True
            self._set_download(1.0, f"{name} is ready")
            self.download_hint.set_text("Nothing more to wait for.")
        elif model.get("download") is not None:
            percent = round(model["download"] * 100)
            self._set_download(model["download"], f"Downloading {name} — {percent} %")
            self.download_hint.set_text("It depends on your connection; only this once.")
        else:
            self._set_download(None, f"Loading {name}…")
            self.download_hint.set_text("A few seconds, the time to fill the memory.")
        self._refresh_buttons()


def read_orders(window: WelcomeWindow) -> None:
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict):
            GLib.idle_add(window.show_state, message)
    GLib.idle_add(Gtk.main_quit)


def main() -> int:
    window = WelcomeWindow()
    window.connect("destroy", Gtk.main_quit)
    window.show_all()
    window._refresh_buttons()
    threading.Thread(target=read_orders, args=(window,), daemon=True).start()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
