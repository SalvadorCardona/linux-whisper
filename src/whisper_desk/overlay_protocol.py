"""The line protocol spoken between the daemon and the overlay window.

Stdlib only, and no relative import: the overlay runs this module as a plain
script neighbour, with the system Python, while the daemon imports it from the
venv. Both sides therefore read the same definition of a line.

Daemon → overlay, one command per line on stdin:

    state <name> [<b64 title> [<b64 detail>]]   see STATES
    level 0.42 [0.10 0.31 ...]                  overall volume, then the bands
    text <b64>                                  last sentence transcribed
    hint <b64>                                  how to stop, shown while listening
    progress 0.42                               model download, 0..1
    copy <b64>                                  puts the text in the clipboard
    saveclip | restoreclip                      saves / hands back the clipboard
    quit                                        close, once the last state was read

Overlay → daemon, one event per line on stdout:

    cancel                                      Esc or a click: drop the dictation

Free text travels as base64: it is the only way to keep spaces and line
breaks on a protocol that splits on whitespace.
"""

from __future__ import annotations

import base64
import binascii

STATES = ("loading", "listening", "working", "done", "error", "cancelled")
# States that end a dictation: the window stays up a moment to be read.
FINAL_STATES = ("done", "error", "cancelled")
EVENTS = ("cancel",)


def encode(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def decode(token: str) -> str | None:
    try:
        return base64.b64decode(token, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None


def _unit(token: str) -> float | None:
    try:
        value = float(token)
    except ValueError:
        return None
    if value != value:  # NaN
        return None
    return max(0.0, min(value, 1.0))


def parse(line: str) -> tuple[str, tuple] | None:
    """A line read by the overlay → (command, arguments), None if it is unreadable.

    A malformed line is dropped rather than guessed at: the daemon and the
    window may come from two different versions during an update.
    """
    parts = line.split()
    if not parts:
        return None
    command, rest = parts[0], parts[1:]
    if command in ("quit", "saveclip", "restoreclip"):
        return command, ()
    if not rest:
        return None
    if command == "state":
        if rest[0] not in STATES:
            return None
        texts = [decode(token) for token in rest[1:3]]
        if any(text is None for text in texts):
            return None
        title, detail = (texts + ["", ""])[:2]
        return command, (rest[0], title, detail)
    if command == "level":
        values = [_unit(token) for token in rest]
        if any(value is None for value in values):
            return None
        return command, (values[0], values[1:])
    if command == "progress":
        value = _unit(rest[0])
        return None if value is None else (command, (value,))
    if command in ("text", "hint", "copy"):
        text = decode(rest[0])
        return None if text is None else (command, (text,))
    return None
