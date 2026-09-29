#!/usr/bin/env python3
"""Tray indicator: whisper-desk's state at a glance, and its menu.

Launched with the system Python, where AppIndicator lives; tray_proc.py
drives it over JSON lines:

    stdin   {"icon": "...", "items": [{"id", "label", "enabled", "check", "active"}
             | {"separator": true}, ...]}      {"quit": true}
    stdout  {"action": "toggle" | "history" | "settings" | "quit"}
            {"action": "pause", "active": true}
            {"event": "unavailable", "reason": "..."}
"""

from __future__ import annotations

import importlib
import json
import sys
import threading

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

# Ayatana on today's distributions, the original Canonical name on older ones.
AppIndicator = None
for _name in ("AyatanaAppIndicator3", "AppIndicator3"):
    try:
        gi.require_version(_name, "0.1")
        AppIndicator = importlib.import_module(f"gi.repository.{_name}")
        break
    except (ValueError, ImportError):
        continue


def send(message: dict) -> None:
    try:
        print(json.dumps(message), flush=True)
    except (BrokenPipeError, OSError, ValueError):
        pass


class Tray:
    def __init__(self):
        self.indicator = AppIndicator.Indicator.new(
            "whisper-desk",
            "audio-input-microphone-symbolic",
            AppIndicator.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_title("whisper-desk")
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.menu = Gtk.Menu()
        self.items: dict[str, Gtk.MenuItem] = {}
        # Setting a check item from the daemon's state must not read as a click.
        self.quiet = False
        self.indicator.set_menu(self.menu)

    def _build(self, entries: list[dict]) -> None:
        for entry in entries:
            if entry.get("separator"):
                self.menu.append(Gtk.SeparatorMenuItem())
                continue
            if entry.get("check"):
                item = Gtk.CheckMenuItem(label=entry["label"])
                item.connect("toggled", self._on_check, entry["id"])
            else:
                item = Gtk.MenuItem(label=entry["label"])
                item.connect("activate", self._on_activate, entry["id"])
            self.items[entry["id"]] = item
            self.menu.append(item)
        self.menu.show_all()
        # A middle click on the icon starts or ends a dictation: the one thing
        # the tray is there for.
        if "toggle" in self.items:
            self.indicator.set_secondary_activate_target(self.items["toggle"])

    def show_state(self, message: dict) -> bool:
        if message.get("quit"):
            Gtk.main_quit()
            return GLib.SOURCE_REMOVE
        entries = message.get("items") or []
        if entries and not self.items:
            self._build(entries)
        self.quiet = True
        for entry in entries:
            item = self.items.get(entry.get("id", ""))
            if item is None:
                continue
            item.set_label(entry["label"])
            item.set_sensitive(entry.get("enabled", True))
            if isinstance(item, Gtk.CheckMenuItem):
                item.set_active(bool(entry.get("active")))
        self.quiet = False
        if message.get("icon"):
            self.indicator.set_icon_full(message["icon"], "whisper-desk")
        return GLib.SOURCE_REMOVE

    def _on_activate(self, _item: Gtk.MenuItem, action: str) -> None:
        send({"action": action})

    def _on_check(self, item: Gtk.CheckMenuItem, action: str) -> None:
        if not self.quiet:
            send({"action": action, "active": item.get_active()})


def read_orders(tray: Tray) -> None:
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict):
            GLib.idle_add(tray.show_state, message)
    GLib.idle_add(Gtk.main_quit)


def main() -> int:
    if AppIndicator is None:
        send({"event": "unavailable", "reason": "no AppIndicator library for the system Python"})
        return 2
    tray = Tray()
    threading.Thread(target=read_orders, args=(tray,), daemon=True).start()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
