"""The GTK windows (history, settings…) as views driven over JSON lines.

Each window runs with the system Python — the one with PyGObject — and knows
nothing of the configuration, the history or the daemon: it shows what it is
sent on stdin and writes what the user asked for on stdout, one JSON object
per line. What it asks is answered here, on the venv side.
"""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from .overlay_proc import gtk_available, system_python

Message = dict[str, Any]


class WindowUnavailable(RuntimeError):
    """No GTK3 for the system Python: the command line remains."""


class WindowProcess:
    def __init__(self, script: Path):
        if not gtk_available():
            raise WindowUnavailable("GTK3 is missing from the system Python")
        self._process = subprocess.Popen(
            [system_python(), str(script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        # Answers come from the main loop, gauges from their own thread.
        self._lock = threading.Lock()

    def show(self, message: Message) -> None:
        stdin = self._process.stdin
        if stdin is None:
            return
        with self._lock:
            try:
                stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass

    def requests(self) -> Iterable[Message]:
        """What the user asks for, until the window closes."""
        assert self._process.stdout is not None
        for line in self._process.stdout:
            try:
                request = json.loads(line)
            except ValueError:
                continue
            if isinstance(request, dict):
                yield request

    def close(self) -> int:
        with self._lock:
            try:
                if self._process.stdin is not None:
                    self._process.stdin.close()
            except OSError:
                pass
        return self._process.wait()


def run(script: Path, first: Message, handle: Callable[[Message], Message | None],
        window: WindowProcess | None = None) -> int:
    """Shows `first`, then answers each request with what handle() returns."""
    window = window or WindowProcess(script)
    window.show(first)
    try:
        for request in window.requests():
            reply = handle(request)
            if reply is not None:
                window.show(reply)
    finally:
        code = window.close()
    return code
