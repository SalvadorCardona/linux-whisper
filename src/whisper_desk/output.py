"""Delivering the transcribed text: insertion at the cursor, clipboard, notifications."""

from __future__ import annotations

import base64
import codecs
import logging
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import config as config_module
from . import host
from .inject import keyboard, resolve_shortcut
from .overlay_proc import system_python

logger = logging.getLogger("whisper-desk.output")

CLIPBOARD_HELPER = Path(__file__).with_name("clipboard_gtk.py")
# Lets the target application claim the selection data before the previous
# clipboard is put back: an X11 paste is asynchronous.
PASTE_SETTLE_SECONDS = 0.25
CLIPBOARD_SETTLE_SECONDS = 0.12


def _run(command: list[str], data: bytes | None = None) -> bool:
    try:
        # No capture_output: wl-copy detaches to serve the selection and would
        # inherit the pipes, which would block the wait until the timeout.
        subprocess.run(
            command,
            input=data,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return True
    except (OSError, subprocess.SubprocessError) as error:
        logger.warning("%s failed: %s", command[0], error)
        return False


def _capture(command: list[str]) -> str | None:
    try:
        result = subprocess.run(command, capture_output=True, timeout=10)
        return result.stdout.decode("utf-8", "replace") if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


# -- clipboard, one path per host --------------------------------------------
#
# Each function returns False if its tool is not there, to let the next one try
# its luck; the readers return None in the same way.


def _set_wayland(text: str) -> bool:
    return bool(shutil.which("wl-copy")) and _run(["wl-copy", "--", text])


def _set_xclip(text: str) -> bool:
    return bool(shutil.which("xclip")) and _run(
        ["xclip", "-selection", "clipboard"], text.encode()
    )


def _set_xsel(text: str) -> bool:
    return bool(shutil.which("xsel")) and _run(
        ["xsel", "--clipboard", "--input"], text.encode()
    )


def _set_macos(text: str) -> bool:
    return bool(shutil.which("pbcopy")) and _run(["pbcopy"], text.encode())


def _set_windows(text: str) -> bool:
    """clip.exe recognises UTF-16LE by its byte order mark: without it, accents are lost."""
    clip = shutil.which("clip.exe")
    if clip and _run([clip], codecs.BOM_UTF16_LE + text.encode("utf-16-le")):
        return True
    if not host.has_windows_interop():
        return False
    # Fallback without clip.exe: the text travels as base64, pure ASCII, hence
    # safe from the Windows console code pages.
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    script = (
        "Set-Clipboard -Value ([Text.Encoding]::UTF8.GetString("
        f"[Convert]::FromBase64String('{payload}')))"
    )
    return host.run_powershell(script) is not None


def _get_wayland() -> str | None:
    return _capture(["wl-paste", "--no-newline"]) if shutil.which("wl-paste") else None


def _get_xclip() -> str | None:
    return _capture(["xclip", "-selection", "clipboard", "-o"]) if shutil.which("xclip") else None


def _get_xsel() -> str | None:
    return _capture(["xsel", "--clipboard", "--output"]) if shutil.which("xsel") else None


def _get_macos() -> str | None:
    return _capture(["pbpaste"]) if shutil.which("pbpaste") else None


def _get_windows() -> str | None:
    text = host.run_powershell("Get-Clipboard -Raw")
    if text is None:
        return None
    # Get-Clipboard adds a line ending, and Windows separates lines with CRLF.
    return text.replace("\r\n", "\n").rstrip("\n")


SETTERS: dict[str, tuple[Callable[[str], bool], ...]] = {
    host.LINUX: (_set_wayland, _set_xclip, _set_xsel),
    host.WSL: (_set_windows, _set_wayland, _set_xclip, _set_xsel),
    host.MACOS: (_set_macos,),
}

GETTERS: dict[str, tuple[Callable[[], str | None], ...]] = {
    host.LINUX: (_get_wayland, _get_xclip, _get_xsel),
    host.WSL: (_get_windows, _get_wayland, _get_xclip, _get_xsel),
    host.MACOS: (_get_macos,),
}

# Tools to install, quoted by the diagnostic and the installer.
CLIPBOARD_TOOLS: dict[str, tuple[str, ...]] = {
    host.LINUX: ("wl-copy", "xclip", "xsel"),
    host.WSL: ("clip.exe", "wl-copy", "xclip", "xsel"),
    host.MACOS: ("pbcopy",),
}


def clipboard_tool() -> str | None:
    """The first clipboard tool present, for the diagnostic."""
    for tool in CLIPBOARD_TOOLS[host.name()]:
        if shutil.which(tool):
            return tool
    return None


class Clipboard:
    """Clipboard, saving and handing back the user's own content.

    From the most reliable to the most resourceful: the host's tools
    (wl-clipboard, xclip, pbcopy, clip.exe), then the overlay window — under
    Wayland only a client with a window can set a selection, and the overlay
    has one.
    """

    def __init__(self, overlay: Any = None):
        self.overlay = overlay
        self._saved: str | None = None
        self._has_saved = False

    def save(self) -> None:
        if self._has_saved:
            return
        self._has_saved = True
        for getter in GETTERS[host.name()]:
            saved = getter()
            if saved is not None:
                self._saved = saved
                return
        if self.overlay is not None and self.overlay.save_clipboard():
            self._saved = None  # remembered on the overlay side
            return
        logger.debug("Clipboard not saved: no reader available.")

    def set(self, text: str) -> bool:
        for setter in SETTERS[host.name()]:
            if setter(text):
                return True
        if self.overlay is not None and self.overlay.copy(text):
            return True
        return _run([system_python(), str(CLIPBOARD_HELPER)], text.encode())

    def restore(self) -> None:
        if not self._has_saved:
            return
        if self._saved is not None:
            self.set(self._saved)
        elif self.overlay is not None:
            self.overlay.restore_clipboard()
        self._has_saved = False
        self._saved = None


class CursorWriter:
    """Inserts the text where the cursor is, through a simulated paste.

    Sending the text key by key would mean knowing the active XKB layout
    (AZERTY, accents, dead keys); the paste shortcut, on the other hand, sits
    on the same physical key on every layout.
    """

    def __init__(self, config: dict[str, Any], overlay: Any = None):
        self.settings = config["output"]
        self.clipboard = Clipboard(overlay)
        self.keyboard = keyboard(str(self.settings["keyboard"]))
        self.shortcut = resolve_shortcut(str(self.settings["paste_shortcut"]))
        # Why the last insertion failed: "clipboard", "keyboard", or "" if it did not.
        self.failure = ""

    def prepare(self) -> None:
        """Creates the virtual keyboard ahead of time: the compositor takes ~0.6 s to see it."""
        self.keyboard.open()
        if self.settings["restore_clipboard"]:
            self.clipboard.save()

    def write(self, text: str) -> bool:
        if self.settings["restore_clipboard"]:
            self.clipboard.save()
        if not self.clipboard.set(text):
            logger.warning("Text not copied: insertion impossible.")
            self.failure = "clipboard"
            return False
        # wl-copy takes ownership of the selection in a detached process:
        # pasting too early would paste the previous content.
        time.sleep(CLIPBOARD_SETTLE_SECONDS)
        if not self.keyboard.press(self.shortcut):
            logger.warning(
                "Virtual keyboard unavailable: the text stays in the clipboard."
            )
            self.failure = "keyboard"
            return False
        time.sleep(PASTE_SETTLE_SECONDS)
        self.failure = ""
        return True

    def close(self) -> None:
        if self.settings["restore_clipboard"]:
            self.clipboard.restore()
        self.keyboard.close()


def copy(text: str, overlay: Any = None) -> bool:
    return Clipboard(overlay).set(text)


def _applescript_string(text: str) -> str:
    """An AppleScript string: only the backslash and the double quote are escaped."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(summary: str, body: str = "") -> None:
    """Desktop notification, when the host knows how to show one."""
    if shutil.which("notify-send"):
        _run([
            "notify-send",
            "--app-name=whisper-desk",
            "--icon=audio-input-microphone-symbolic",
            "--expire-time=3000",
            summary,
            body,
        ])
        return
    if host.is_macos() and shutil.which("osascript"):
        script = (
            f"display notification {_applescript_string(body)} "
            f"with title {_applescript_string(summary)}"
        )
        _run(["osascript", "-e", script])
        return
    logger.debug("Notification skipped: no display method available.")


def log_history(text: str) -> None:
    # Later sentences arrive preceded by a joining space, which makes no sense
    # once the line stands alone in the log.
    entry = text.strip()
    if not entry:
        return
    try:
        config_module.STATE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().isoformat(timespec="seconds")
        with (config_module.STATE_DIR / "history.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp}\t{entry}\n")
    except OSError as error:
        logger.warning("History not written: %s", error)


def modes(config: dict[str, Any]) -> set[str]:
    raw = str(config["output"]["mode"])
    found = {mode.strip() for mode in raw.split("+") if mode.strip()}
    # "type" is still accepted as a historical synonym of "cursor".
    return {"cursor" if mode == "type" else mode for mode in found}


def deliver(text: str, config: dict[str, Any], writer: CursorWriter | None = None,
            overlay: Any = None) -> bool:
    """Applies the configured output modes to the transcribed text.

    False if the text could not be put where it was asked for: the dictation
    then ends on an error the user can see, rather than on nothing at all.
    """
    settings = config["output"]
    selected = modes(config)
    delivered = True

    if "cursor" in selected and writer is not None:
        delivered = writer.write(text) and delivered
    if "clipboard" in selected:
        delivered = copy(text, overlay=overlay) and delivered
    if "stdout" in selected:
        print(text, flush=True)
    if settings["history"]:
        log_history(text)
    if settings["notify"]:
        preview = text if len(text) <= 140 else text[:139] + "…"
        notify("Transcription", preview)
    return delivered
