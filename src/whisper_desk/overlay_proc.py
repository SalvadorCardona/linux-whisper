"""Driving the overlay process (GTK3, system Python)."""

from __future__ import annotations

import functools
import logging
import os
import shutil
import subprocess
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Callable

from . import overlay_protocol as protocol

logger = logging.getLogger("whisper-desk.overlay")

OVERLAY_SCRIPT = Path(__file__).with_name("overlay.py")
# The longest a final state stays on screen, plus its fade: past that, a
# window that has not closed is no longer lingering, it is stuck.
LINGER_SECONDS = 5.0


def system_python() -> str:
    """The system Python: it is the one with PyGObject, not the venv's."""
    return os.environ.get("WD_SYSTEM_PYTHON") or shutil.which("python3") or "/usr/bin/python3"


@functools.cache
def gtk_available() -> bool:
    """Does the system Python have PyGObject + GTK3?

    On macOS, and on a Linux without python3-gi, the answer is no: better to
    know it once than to launch a doomed process for every dictation.
    """
    try:
        return subprocess.run(
            [system_python(), "-c",
             "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk"],
            capture_output=True,
            timeout=20,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class OverlayProcess:
    """Listening window launched on demand, driven by lines on stdin.

    It answers on stdout: `on_event` receives what the user asked for from the
    window itself (see overlay_protocol.EVENTS), in the reading thread.
    """

    # Windows still showing their final state: a new dictation takes their
    # place on screen rather than piling up on top of them.
    _lingering: list[subprocess.Popen[bytes]] = []
    _lingering_lock = threading.Lock()

    def __init__(self, config: dict[str, Any], on_event: Callable[[str], None] | None = None):
        self.config = config["overlay"]
        self.on_event = on_event
        self._process: subprocess.Popen[bytes] | None = None
        # Levels come from the recording thread, states from the dictation
        # thread: without a lock, two lines would interleave on the same pipe.
        self._lock = threading.Lock()
        self._broken = False

    def start(self, state: str = "listening", hint: str = "") -> None:
        if not self.config["enabled"] or self._process is not None:
            return
        if not gtk_available():
            logger.debug("Overlay skipped: GTK3 missing from the system Python.")
            return
        command = [
            system_python(),
            str(OVERLAY_SCRIPT),
            str(self.config["width"]),
            str(self.config["height"]),
            str(self.config["accent"]),
            str(self.config["position"]),
            str(self.config["margin"]),
            str(self.config["bars"]),
        ]
        self._dismiss_lingering()
        try:
            self._process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
        except OSError as error:
            logger.warning("Overlay unavailable: %s", error)
            self._process = None
            return
        threading.Thread(target=self._read_events, args=(self._process,), daemon=True).start()
        if hint:
            self.set_hint(hint)
        self.set_state(state)

    def set_state(self, state: str, title: str = "", detail: str = "") -> None:
        """A state, with the words that go with it for the final ones."""
        texts = [title, detail] if detail else [title] if title else []
        self._send(" ".join(["state", state, *(protocol.encode(text) for text in texts)]))

    def set_text(self, text: str) -> None:
        """The last sentence transcribed, shown under the equalizer."""
        self._send(f"text {protocol.encode(text)}")

    def set_hint(self, hint: str) -> None:
        self._send(f"hint {protocol.encode(hint)}")

    def set_progress(self, progress: float) -> None:
        self._send(f"progress {max(0.0, min(progress, 1.0)):.3f}")

    def set_level(self, level: float, bands: Sequence[float] = ()) -> None:
        """Overall volume, followed by the energy per band when it is known."""
        spectrum = "".join(f" {band:.3f}" for band in bands)
        self._send(f"level {level:.3f}{spectrum}")

    @property
    def bars(self) -> int:
        return int(self.config["bars"]) if self.config["enabled"] else 0

    @property
    def alive(self) -> bool:
        return (
            self._process is not None
            and not self._broken
            and self._process.poll() is None
        )

    def copy(self, text: str) -> bool:
        """Copies through the overlay window, which owns an X11 selection."""
        if not self.alive:
            return False
        self._send(f"copy {protocol.encode(text)}")
        return self.alive

    def save_clipboard(self) -> bool:
        if not self.alive:
            return False
        self._send("saveclip")
        return self.alive

    def restore_clipboard(self) -> bool:
        if not self.alive:
            return False
        self._send("restoreclip")
        return self.alive

    def stop(self) -> None:
        """Asks the window to close, without waiting for it.

        A final state — done, error, cancelled — stays up a moment to be read:
        the dictation is over, the daemon is free, and only the window is
        still on its way out.
        """
        with self._lock:
            process, self._process = self._process, None
            broken, self._broken = self._broken, False
        if process is None:
            return
        try:
            if broken or process.stdin is None:
                # Without a pipe, we cannot ask it to close: we force it.
                process.terminate()
            else:
                process.stdin.write(b"quit\n")
                process.stdin.flush()
                process.stdin.close()
        except (OSError, ValueError):
            process.kill()
        with self._lingering_lock:
            self._lingering.append(process)
        threading.Thread(target=self._reap, args=(process,), daemon=True).start()

    @classmethod
    def _reap(cls, process: subprocess.Popen[bytes]) -> None:
        try:
            process.wait(timeout=LINGER_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:  # SIGKILL pending: nothing more to do
                logger.warning("The overlay did not exit.")
        with cls._lingering_lock:
            if process in cls._lingering:
                cls._lingering.remove(process)

    @classmethod
    def _dismiss_lingering(cls) -> None:
        with cls._lingering_lock:
            lingering = list(cls._lingering)
        for process in lingering:
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass

    def _read_events(self, process: subprocess.Popen[bytes]) -> None:
        if process.stdout is None:
            return
        try:
            for raw in process.stdout:
                event = raw.decode("utf-8", "replace").strip()
                if event in protocol.EVENTS and self.on_event is not None:
                    self.on_event(event)
        except (OSError, ValueError):
            pass
        finally:
            try:
                process.stdout.close()
            except OSError:
                pass

    def _send(self, line: str) -> None:
        with self._lock:
            process = self._process
            if process is None or process.stdin is None or self._broken:
                return
            try:
                process.stdin.write(f"{line}\n".encode())
                process.stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                # The pipe is dead, but not necessarily the window: the process
                # is kept around so that stop() can still close it.
                self._broken = True
