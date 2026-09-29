"""The tray indicator's side in the venv: what the menu says, what it does.

The indicator itself (tray_window.py) runs with the system Python, where
AppIndicator lives. This side asks the daemon for its state every couple of
seconds, turns it into menu entries, and carries out what is clicked.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from . import config as config_module
from . import hotkey, window_proc
from .overlay_proc import system_python

TRAY_SCRIPT = Path(__file__).with_name("tray_window.py")
POLL_SECONDS = 2.0
# The two names the library goes by: Ayatana (Debian, Ubuntu, Fedora) and
# the original Canonical one, still found on older systems.
LIBRARIES = ("AyatanaAppIndicator3", "AppIndicator3")
EXTENSION = "gnome-shell-extension-appindicator"


@dataclass(frozen=True)
class Support:
    ok: bool
    detail: str
    fix: str = ""


def library() -> str | None:
    """The AppIndicator binding the system Python can load, if any."""
    probe = (
        "import gi\n"
        f"for name in {LIBRARIES!r}:\n"
        "    try:\n"
        "        gi.require_version(name, '0.1'); print(name); break\n"
        "    except ValueError:\n"
        "        pass\n"
    )
    try:
        result = subprocess.run([system_python(), "-c", probe], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    name = result.stdout.decode().strip()
    return name or None


def has_watcher() -> bool:
    """Does the desktop show tray icons? It says so by owning a D-Bus name."""
    if not shutil.which("gdbus"):
        return False
    try:
        result = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus",
             "--object-path", "/org/freedesktop/DBus",
             "--method", "org.freedesktop.DBus.NameHasOwner", "org.kde.StatusNotifierWatcher"],
            capture_output=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return b"true" in result.stdout


def support() -> Support:
    """Can a tray icon be shown here — and if not, what to do about it."""
    name = library()
    if name is None:
        return Support(
            False, "no AppIndicator library for the system Python",
            "install the gir1.2-ayatanaappindicator3-0.1 package",
        )
    if not has_watcher():
        if hotkey.is_gnome():
            return Support(
                False, "GNOME shows no tray icon without an extension",
                f"install and enable {EXTENSION} — meanwhile, Settings and History "
                "are in the applications menu",
            )
        return Support(False, "this desktop shows no tray icon",
                       "the applications menu holds whisper-desk Settings and History")
    return Support(True, name)


def lock() -> IO[str] | None:
    """One tray per session: the login starts one, an installation may start another."""
    name = "whisper-desk-tray.lock"
    if config_module.RUNTIME_DIR == Path("/tmp"):
        name = f"whisper-desk-tray-{os.getuid()}.lock"
    handle = (config_module.RUNTIME_DIR / name).open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _speed(status: dict[str, Any]) -> str:
    return "GPU" if status.get("device") == "cuda" else "CPU"


class TrayController:
    def __init__(
        self,
        config: dict[str, Any],
        command: str,
        send: Callable[..., dict[str, Any]],
        spawn: Callable[[list[str]], Any] | None = None,
    ):
        self.config = config
        self.command = command
        self.send = send
        self.spawn = spawn or (lambda argv: subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        ))
        self.unavailable = ""

    def status(self) -> dict[str, Any] | None:
        try:
            return self.send("status", timeout=5, autostart=False)
        except Exception:
            return None  # stopped, or not answering: the menu says so

    def menu(self, status: dict[str, Any] | None) -> dict[str, Any]:
        """The menu for a daemon state — None when the daemon is not running."""
        shortcut = hotkey.label(hotkey.resolve_binding(self.config))
        state = status.get("state") if status else None
        paused = bool(status and status.get("paused"))
        model = status.get("model", "") if status else ""

        if status is None:
            line, icon = "○ Service stopped — a dictation starts it", "action-unavailable-symbolic"
        elif paused:
            line, icon = "Paused — the shortcut is ignored", "microphone-disabled-symbolic"
        elif state == "recording":
            line, icon = "● Listening…", "media-record-symbolic"
        elif state == "working":
            line, icon = "Transcribing…", "audio-input-microphone-symbolic"
        elif state == "loading" or not status.get("loaded"):
            download = status.get("download")
            line = (f"Downloading {model} — {round(download * 100)} %" if download is not None
                    else f"Loading {model}…")
            icon = "folder-download-symbolic" if download is not None else "audio-input-microphone-symbolic"
        else:
            line, icon = f"Ready — {model} on the {_speed(status)}", "audio-input-microphone-symbolic"

        if state == "recording":
            toggle = "Finish the dictation"
        elif state in ("working", "loading"):
            toggle = "Cancel the dictation"
        else:
            toggle = f"Start a dictation ({shortcut})"
        microphone = status.get("microphone", "default") if status else self.config["recording"]["device"]
        return {
            "icon": icon,
            "items": [
                {"id": "status", "label": line, "enabled": False},
                {"id": "microphone", "label": f"Microphone: {microphone}", "enabled": False},
                {"separator": True},
                {"id": "toggle", "label": toggle, "enabled": not paused},
                {"id": "history", "label": "History…"},
                {"id": "settings", "label": "Settings…"},
                {"separator": True},
                {"id": "pause", "label": "Pause dictation", "check": True, "active": paused,
                 "enabled": status is not None},
                {"id": "quit", "label": "Quit whisper-desk"},
            ],
        }

    def refresh(self) -> dict[str, Any]:
        return self.menu(self.status())

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        if request.get("event") == "unavailable":
            self.unavailable = str(request.get("reason", ""))
            return None
        action = request.get("action")
        if action == "toggle":
            try:
                self.send("toggle", timeout=10)
            except Exception:
                pass
        elif action == "pause":
            try:
                self.send("pause" if request.get("active") else "resume", timeout=5)
            except Exception:
                pass
        elif action in ("history", "settings"):
            argv = [self.command, "history", "--window"] if action == "history" \
                else [self.command, "settings"]
            self.spawn(argv)
            return None
        elif action == "quit":
            try:
                self.send("quit", timeout=5, autostart=False)
            except Exception:
                pass
            return {"quit": True}
        else:
            return None
        return self.refresh()


def run(controller: TrayController) -> str:
    """Shows the indicator until quit; returns why it could not, if it could not."""
    window = window_proc.WindowProcess(TRAY_SCRIPT)
    stop = threading.Event()

    def poll() -> None:
        while not stop.wait(POLL_SECONDS):
            window.show(controller.refresh())

    threading.Thread(target=poll, daemon=True).start()
    try:
        window_proc.run(TRAY_SCRIPT, controller.refresh(), controller.handle, window)
    finally:
        stop.set()
    return controller.unavailable
