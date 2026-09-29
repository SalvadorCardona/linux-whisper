#!/usr/bin/env python3
"""History window: find a past dictation, copy it, insert it again, delete it.

Like the overlay, a view launched with the system Python (the one with
PyGObject) and nothing else: it knows neither the history file nor the
daemon. It reads what to show on stdin and says what the user asked for on
stdout, one JSON object per line:

    stdin   {"entries": [{"id", "date", "duration", "model", "text"}, ...],
             "keep_days": 30, "hint": "Super+J", "status": "Copied"}
    stdout  {"action": "copy" | "insert" | "delete", "id": 3, "date": "..."}
            {"action": "keep_days", "days": 30}
"""

from __future__ import annotations

import json
import sys
import threading
import unicodedata
from datetime import date, datetime

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

ACCENT = "#e46212"
# The choices offered for the automatic purge, in days; 0 keeps everything.
KEEP_CHOICES = ((0, "Forever"), (1, "1 day"), (7, "7 days"), (30, "30 days"),
                (90, "90 days"), (365, "1 year"))
# A second click confirms a deletion: no dialog, and no deletion by accident.
CONFIRM_SECONDS = 3

CSS = f"""
.dictation {{ padding: 12px 14px; }}
.meta {{ color: alpha(currentColor, 0.55); font-size: small; }}
.meta .model {{ color: {ACCENT}; }}
.status {{ color: alpha(currentColor, 0.65); }}
.empty {{ color: alpha(currentColor, 0.55); }}
"""


def fold(text: str) -> str:
    """Case and accents set aside, as the command line searches."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def when(stamp: str) -> str:
    """"Today, 14:02" rather than an ISO date: the list is read, not parsed."""
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return stamp
    days = (date.today() - moment.date()).days
    if days == 0:
        return f"Today, {moment:%H:%M}"
    if days == 1:
        return f"Yesterday, {moment:%H:%M}"
    if days < 7:
        return f"{moment:%A, %H:%M}"
    return f"{moment:%d %b %Y, %H:%M}"


def send(message: dict) -> None:
    try:
        print(json.dumps(message, ensure_ascii=False), flush=True)
    except (BrokenPipeError, OSError, ValueError):
        pass


class Row(Gtk.ListBoxRow):
    def __init__(self, entry: dict, window: "HistoryWindow"):
        super().__init__()
        self.entry = entry
        self.folded = fold(entry["text"])
        self.set_activatable(False)

        outer = Gtk.Box(spacing=12)
        outer.get_style_context().add_class("dictation")
        self.add(outer)

        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        outer.pack_start(column, True, True, 0)

        meta = Gtk.Box(spacing=10)
        meta.get_style_context().add_class("meta")
        meta.pack_start(Gtk.Label(label=when(entry["date"])), False, False, 0)
        if entry.get("duration") is not None:
            meta.pack_start(Gtk.Label(label=f"{entry['duration']:.1f} s"), False, False, 0)
        if entry.get("model"):
            model = Gtk.Label(label=entry["model"])
            model.get_style_context().add_class("model")
            meta.pack_start(model, False, False, 0)
        column.pack_start(meta, False, False, 0)

        text = Gtk.Label(label=entry["text"])
        text.set_xalign(0.0)
        text.set_line_wrap(True)
        text.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        text.set_lines(4)
        text.set_ellipsize(Pango.EllipsizeMode.END)
        text.set_selectable(True)
        text.set_can_focus(False)
        text.set_max_width_chars(60)
        column.pack_start(text, False, False, 0)

        buttons = Gtk.Box(spacing=6)
        buttons.set_valign(Gtk.Align.CENTER)
        outer.pack_end(buttons, False, False, 0)
        copy = Gtk.Button(label="Copy")
        copy.set_tooltip_text("Copy to the clipboard")
        copy.connect("clicked", lambda _button: window.act("copy", entry))
        insert = Gtk.Button(label="Insert")
        insert.set_tooltip_text("Type it again where the cursor was")
        insert.connect("clicked", lambda _button: window.insert(entry))
        self.delete = Gtk.Button.new_from_icon_name("user-trash-symbolic", Gtk.IconSize.BUTTON)
        self.delete.set_tooltip_text("Delete this dictation")
        self.delete.connect("clicked", self._on_delete)
        self.confirming = False
        for button in (copy, insert, self.delete):
            buttons.pack_start(button, False, False, 0)
        self.window = window

    def _on_delete(self, _button: Gtk.Button) -> None:
        if self.confirming:
            self.window.act("delete", self.entry)
            return
        self.confirming = True
        self.delete.set_label("Delete?")
        self.delete.set_always_show_image(False)
        self.delete.get_style_context().add_class("destructive-action")
        GLib.timeout_add_seconds(CONFIRM_SECONDS, self._disarm)

    def _disarm(self) -> bool:
        self.confirming = False
        self.delete.set_label("")
        self.delete.set_image(
            Gtk.Image.new_from_icon_name("user-trash-symbolic", Gtk.IconSize.BUTTON)
        )
        self.delete.get_style_context().remove_class("destructive-action")
        return GLib.SOURCE_REMOVE


class HistoryWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="Dictation history")
        self.set_default_size(680, 600)
        self.set_icon_name("audio-input-microphone")
        self.entries: list[dict] = []
        self.hint = "the shortcut"
        self._updating_keep = False

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_screen(
            self.get_screen(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.set_title("Dictation history")
        self.header = header
        self.set_titlebar(header)

        layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(layout)

        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Search the dictations")
        self.search.set_margin_start(12)
        self.search.set_margin_end(12)
        self.search.set_margin_top(10)
        self.search.set_margin_bottom(10)
        self.search.connect("search-changed", lambda _entry: self._refilter())
        layout.pack_start(self.search, False, False, 0)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        layout.pack_start(scroller, True, True, 0)
        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list.set_filter_func(self._visible)
        self.list.set_header_func(self._separator)
        self.empty = Gtk.Label()
        self.empty.get_style_context().add_class("empty")
        self.empty.set_margin_top(48)
        self.empty.show()
        self.list.set_placeholder(self.empty)
        scroller.add(self.list)

        footer = Gtk.Box(spacing=8)
        footer.set_border_width(10)
        layout.pack_end(footer, False, False, 0)
        layout.pack_end(Gtk.Separator(), False, False, 0)
        self.status = Gtk.Label()
        self.status.get_style_context().add_class("status")
        self.status.set_xalign(0.0)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        footer.pack_start(self.status, True, True, 0)
        footer.pack_end(self._keep_selector(), False, False, 0)
        footer.pack_end(Gtk.Label(label="Keep dictations"), False, False, 0)

        self.connect("key-press-event", self._on_key)

    def _keep_selector(self) -> Gtk.ComboBoxText:
        self.keep = Gtk.ComboBoxText()
        for days, label in KEEP_CHOICES:
            self.keep.append(str(days), label)
        self.keep.set_active_id("0")
        self.keep.set_tooltip_text("Older dictations are deleted automatically")
        self.keep.connect("changed", self._on_keep_changed)
        return self.keep

    def _on_keep_changed(self, combo: Gtk.ComboBoxText) -> None:
        if not self._updating_keep and combo.get_active_id() is not None:
            send({"action": "keep_days", "days": int(combo.get_active_id())})

    @staticmethod
    def _separator(row: Gtk.ListBoxRow, before: Gtk.ListBoxRow | None) -> None:
        if before is not None and row.get_header() is None:
            row.set_header(Gtk.Separator())

    def _visible(self, row: Row) -> bool:
        words = fold(self.search.get_text()).split()
        return all(word in row.folded for word in words)

    def _refilter(self) -> None:
        self.list.invalidate_filter()
        self._refresh_empty()

    def _refresh_empty(self) -> None:
        if self.entries:
            self.empty.set_text("No dictation matches this search.")
        else:
            self.empty.set_text(f"No dictation yet — press {self.hint} and speak.")

    def _on_key(self, _widget: Gtk.Widget, event: Gdk.EventKey) -> bool:
        control = event.state & Gdk.ModifierType.CONTROL_MASK
        if control and event.keyval in (Gdk.KEY_f, Gdk.KEY_F):
            self.search.grab_focus()
            return True
        if event.keyval == Gdk.KEY_Escape:
            if self.search.get_text():
                self.search.set_text("")
            else:
                self.close()
            return True
        return False

    # -- orders from the command line --------------------------------------
    def show_state(self, message: dict) -> bool:
        if "hint" in message:
            self.hint = message["hint"]
        if "entries" in message:
            self.entries = message["entries"]
            for child in self.list.get_children():
                self.list.remove(child)
            # Newest first: the dictation just lost is the one looked for.
            for entry in reversed(self.entries):
                self.list.add(Row(entry, self))
            self.list.show_all()
            count = len(self.entries)
            self.header.set_subtitle(f"{count} dictation{'s' if count != 1 else ''}")
            self._refresh_empty()
        if "keep_days" in message:
            self._updating_keep = True
            if not self.keep.set_active_id(str(message["keep_days"])):
                self.keep.append(str(message["keep_days"]), f"{message['keep_days']} days")
                self.keep.set_active_id(str(message["keep_days"]))
            self._updating_keep = False
        if message.get("status"):
            self.status.set_text(message["status"])
        return GLib.SOURCE_REMOVE

    # -- what the user asks ------------------------------------------------
    def act(self, action: str, entry: dict) -> None:
        send({"action": action, "id": entry["id"], "date": entry["date"]})

    def insert(self, entry: dict) -> None:
        """The window gets out of the way, so the text lands where the cursor was."""
        self.act("insert", entry)
        self.hide()
        GLib.timeout_add(200, Gtk.main_quit)


def read_orders(window: HistoryWindow) -> None:
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict):
            GLib.idle_add(window.show_state, message)
    GLib.idle_add(Gtk.main_quit)


def main() -> int:
    window = HistoryWindow()
    window.connect("destroy", Gtk.main_quit)
    window.show_all()
    threading.Thread(target=read_orders, args=(window,), daemon=True).start()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
