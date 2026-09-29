#!/usr/bin/env python3
"""Settings window: the essentials of config.toml, without opening it.

A view launched with the system Python, like the overlay and the history:
settings_proc.py sends it the values and the choices on stdin, and receives
on stdout what the user asks for, one JSON object per line:

    stdin   {"values": {...}, "choices": {...}, "hotkey": {"binding", "label"}}
            {"level": 0.42, "meter": "..."}  {"status": "...", "saved": true}
    stdout  {"action": "save", "values": {...}}
            {"action": "test_mic" | "stop_mic", "device": "..."}
            {"action": "hotkey", "accel": "<Primary><Alt>d" | "auto"}
"""

from __future__ import annotations

import json
import sys
import threading

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

CSS = """
.section { font-weight: bold; margin-top: 18px; }
.section.first { margin-top: 0; }
.hint { color: alpha(currentColor, 0.60); font-size: small; }
.keycap {
    font-weight: bold;
    padding: 4px 10px;
    border: 1px solid alpha(currentColor, 0.25);
    border-radius: 6px;
}
.keycap.capturing { border-color: @theme_selected_bg_color; }
"""

# Modifier keys alone are not a shortcut: the capture waits for the real key.
MODIFIER_KEYS = {
    Gdk.KEY_Shift_L, Gdk.KEY_Shift_R, Gdk.KEY_Control_L, Gdk.KEY_Control_R,
    Gdk.KEY_Alt_L, Gdk.KEY_Alt_R, Gdk.KEY_Super_L, Gdk.KEY_Super_R,
    Gdk.KEY_Meta_L, Gdk.KEY_Meta_R, Gdk.KEY_ISO_Level3_Shift, Gdk.KEY_Caps_Lock,
    Gdk.KEY_Hyper_L, Gdk.KEY_Hyper_R,
}


def send(message: dict) -> None:
    try:
        print(json.dumps(message, ensure_ascii=False), flush=True)
    except (BrokenPipeError, OSError, ValueError):
        pass


def hex_colour(rgba: Gdk.RGBA) -> str:
    return "#{:02x}{:02x}{:02x}".format(
        round(rgba.red * 255), round(rgba.green * 255), round(rgba.blue * 255)
    )


class SettingsWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="whisper-desk settings")
        self.set_default_size(620, 720)
        self.set_icon_name("audio-input-microphone")
        self.binding = "auto"
        self.capturing = False
        self.testing = False

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_screen(
            self.get_screen(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        header = Gtk.HeaderBar()
        header.set_title("Settings")
        header.set_subtitle("whisper-desk")
        cancel = Gtk.Button(label="Close")
        cancel.connect("clicked", lambda _button: self.close())
        header.pack_start(cancel)
        self.save_button = Gtk.Button(label="Save")
        self.save_button.get_style_context().add_class("suggested-action")
        self.save_button.connect("clicked", self._on_save)
        header.pack_end(self.save_button)
        self.set_titlebar(header)

        layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(layout)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        layout.pack_start(scroller, True, True, 0)

        self.grid = Gtk.Grid(column_spacing=16, row_spacing=10)
        self.grid.set_border_width(22)
        scroller.add(self.grid)
        self.row = 0

        self._section("Dictation", first=True)
        self.language = self._combo("Language")
        self.model = self._combo("Model")
        self._hint("Larger models understand better and ask for more time and memory. "
                   "A new model is downloaded once, in the background.")
        self.vocabulary = Gtk.Entry()
        self.vocabulary.set_placeholder_text("Animalink, Kubernetes, pull request…")
        self._field("Vocabulary", self.vocabulary)
        self._hint("Proper nouns, tools and jargon the model tends to mangle, separated by commas.")

        self._section("Microphone")
        self.device = self._combo("Device")
        self.device.connect("changed", self._on_device_changed)
        test = Gtk.Box(spacing=10)
        self.test_button = Gtk.Button(label="Test")
        self.test_button.connect("clicked", self._on_test)
        self.meter = Gtk.LevelBar()
        self.meter.set_min_value(0.0)
        self.meter.set_max_value(1.0)
        self.meter.set_valign(Gtk.Align.CENTER)
        self.meter.set_hexpand(True)
        test.pack_start(self.test_button, False, False, 0)
        test.pack_start(self.meter, True, True, 0)
        self._field("", test)
        self.meter_hint = self._hint("Speak after pressing Test: the gauge must move.")

        self._section("Shortcut")
        keys = Gtk.Box(spacing=8)
        self.keycap = Gtk.Label(label="…")
        self.keycap.get_style_context().add_class("keycap")
        change = Gtk.Button(label="Change…")
        change.connect("clicked", self._on_capture)
        reset = Gtk.Button(label="Default")
        reset.set_tooltip_text("The host's default: Super+J, or Ctrl+Alt+J under WSL")
        reset.connect("clicked", lambda _button: send({"action": "hotkey", "accel": "auto"}))
        keys.pack_start(self.keycap, False, False, 0)
        keys.pack_start(change, False, False, 0)
        keys.pack_start(reset, False, False, 0)
        self._field("Start and stop", keys)
        self.hotkey_hint = self._hint(
            "Starts the dictation, stops it, then cuts it off. Change…, then press the new combination."
        )

        self._section("Output")
        self.mode = self._combo("The text")

        self._section("Overlay")
        self.overlay = Gtk.Switch()
        self.overlay.set_halign(Gtk.Align.START)
        self._field("Show the overlay", self.overlay)
        self.position = self._combo("Position")
        self.accent = Gtk.ColorButton()
        self.accent.props.use_alpha = False
        self.accent.set_halign(Gtk.Align.START)
        self._field("Accent colour", self.accent)

        footer = Gtk.Box()
        footer.set_border_width(10)
        layout.pack_end(footer, False, False, 0)
        layout.pack_end(Gtk.Separator(), False, False, 0)
        self.status = Gtk.Label(label="Saved settings apply at once, without restarting.")
        self.status.get_style_context().add_class("hint")
        self.status.set_xalign(0.0)
        self.status.set_line_wrap(True)
        footer.pack_start(self.status, True, True, 0)

        self.connect("key-press-event", self._on_key)
        self.connect("delete-event", self._on_delete)

    # -- layout helpers ----------------------------------------------------
    def _section(self, title: str, first: bool = False) -> None:
        label = Gtk.Label(label=title)
        label.set_xalign(0.0)
        context = label.get_style_context()
        context.add_class("section")
        if first:
            context.add_class("first")
        self.grid.attach(label, 0, self.row, 2, 1)
        self.row += 1

    def _field(self, title: str, widget: Gtk.Widget) -> None:
        label = Gtk.Label(label=title)
        label.set_xalign(1.0)
        label.get_style_context().add_class("dim-label")
        widget.set_hexpand(True)
        self.grid.attach(label, 0, self.row, 1, 1)
        self.grid.attach(widget, 1, self.row, 1, 1)
        self.row += 1

    def _hint(self, text: str) -> Gtk.Label:
        label = Gtk.Label(label=text)
        label.set_xalign(0.0)
        label.set_line_wrap(True)
        label.set_max_width_chars(60)
        label.get_style_context().add_class("hint")
        self.grid.attach(label, 1, self.row, 1, 1)
        self.row += 1
        return label

    def _combo(self, title: str) -> Gtk.ComboBoxText:
        combo = Gtk.ComboBoxText()
        self._field(title, combo)
        return combo

    @staticmethod
    def _fill(combo: Gtk.ComboBoxText, choices: list, current: str) -> None:
        combo.remove_all()
        for value, label in choices:
            combo.append(value, label)
        combo.set_active_id(current)

    # -- orders from the command line --------------------------------------
    def show_state(self, message: dict) -> bool:
        choices = message.get("choices")
        values = message.get("values")
        if choices and values:
            self._fill(self.language, choices["language"], values["language"])
            self._fill(self.model, choices["model"], values["model"])
            self._fill(self.device, choices["device"], values["device"])
            self._fill(self.mode, choices["mode"], values["mode"])
            self._fill(self.position, choices["position"], values["position"])
            self.vocabulary.set_text(values["vocabulary"])
            self.overlay.set_active(bool(values["overlay"]))
            colour = Gdk.RGBA()
            if colour.parse(values["accent"]):
                self.accent.set_rgba(colour)
        if "hotkey" in message:
            self.binding = message["hotkey"]["binding"]
            self.keycap.set_text(message["hotkey"]["label"])
            self._end_capture()
        if "level" in message:
            self.meter.set_value(float(message["level"]))
        if message.get("meter"):
            self.meter_hint.set_text(message["meter"])
            self._stop_test(tell=False)
        if message.get("status"):
            self.status.set_text(message["status"])
            if self.capturing:
                self._end_capture()
        return GLib.SOURCE_REMOVE

    # -- what the user does ------------------------------------------------
    def values(self) -> dict:
        return {
            "language": self.language.get_active_id(),
            "model": self.model.get_active_id(),
            "vocabulary": self.vocabulary.get_text(),
            "device": self.device.get_active_id(),
            "binding": self.binding,
            "mode": self.mode.get_active_id(),
            "overlay": self.overlay.get_active(),
            "position": self.position.get_active_id(),
            "accent": hex_colour(self.accent.get_rgba()),
        }

    def _on_save(self, _button: Gtk.Button) -> None:
        self._stop_test()
        self.status.set_text("Saving…")
        send({"action": "save", "values": self.values()})

    def _on_test(self, _button: Gtk.Button) -> None:
        if self.testing:
            self._stop_test()
            return
        self.testing = True
        self.test_button.set_label("Stop")
        self.meter_hint.set_text("Listening — speak, the gauge must move.")
        send({"action": "test_mic", "device": self.device.get_active_id() or "default"})

    def _stop_test(self, tell: bool = True) -> None:
        if not self.testing:
            return
        self.testing = False
        self.test_button.set_label("Test")
        self.meter.set_value(0.0)
        if tell:
            send({"action": "stop_mic"})

    def _on_device_changed(self, _combo: Gtk.ComboBoxText) -> None:
        if self.testing:
            send({"action": "test_mic", "device": self.device.get_active_id() or "default"})

    def _on_capture(self, _button: Gtk.Button) -> None:
        self.capturing = True
        self.keycap.set_text("Press the combination…")
        self.keycap.get_style_context().add_class("capturing")
        self.hotkey_hint.set_text("Esc to keep the current shortcut.")

    def _end_capture(self) -> None:
        self.capturing = False
        self.keycap.get_style_context().remove_class("capturing")
        self.hotkey_hint.set_text(
            "Starts the dictation, stops it, then cuts it off. Change…, then press the new combination."
        )

    def _on_key(self, _widget: Gtk.Widget, event: Gdk.EventKey) -> bool:
        if not self.capturing:
            return False
        if event.keyval == Gdk.KEY_Escape:
            send({"action": "hotkey", "accel": self.binding})
            return True
        if event.keyval in MODIFIER_KEYS:
            return True
        # X11 reports the Super key as Mod4, which the accelerator mask
        # leaves out: it is named Super before the mask is applied.
        state = event.state
        if state & Gdk.ModifierType.MOD4_MASK:
            state |= Gdk.ModifierType.SUPER_MASK
        modifiers = state & Gtk.accelerator_get_default_mod_mask()
        keyval = Gdk.keyval_to_lower(event.keyval)
        send({"action": "hotkey", "accel": Gtk.accelerator_name(keyval, modifiers)})
        return True

    def _on_delete(self, _widget: Gtk.Widget, _event: Gdk.Event) -> bool:
        self._stop_test()
        return False


def read_orders(window: SettingsWindow) -> None:
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict):
            GLib.idle_add(window.show_state, message)
    GLib.idle_add(Gtk.main_quit)


def main() -> int:
    window = SettingsWindow()
    window.connect("destroy", Gtk.main_quit)
    window.show_all()
    threading.Thread(target=read_orders, args=(window,), daemon=True).start()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
