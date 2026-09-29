"""The settings window's side in the venv: what to offer, and what to write.

The window (settings_window.py) only shows values and choices. Here the
configuration is read and written back — comments kept —, the microphone
is listened to for the test gauge, the shortcut is installed, and the daemon
is asked to reload.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import capture, hotkey, window_proc
from . import config as config_module
from .recorder import CHANNELS, CHUNK_BYTES, MIN_THRESHOLD, RATE, _rms
from .spectrum import visual_level
from .transcriber import CPU_MODEL, GPU_MODEL, MODEL_BYTES, has_nvidia_gpu, is_downloaded

WINDOW_SCRIPT = Path(__file__).with_name("settings_window.py")
# The test gauge closes the microphone by itself: a window forgotten open
# must not keep listening to the room.
METER_SECONDS = 30.0

LANGUAGES = (
    ("auto", "Detect automatically"),
    ("fr", "French"), ("en", "English"), ("es", "Spanish"), ("de", "German"),
    ("it", "Italian"), ("pt", "Portuguese"), ("nl", "Dutch"), ("pl", "Polish"),
    ("ca", "Catalan"), ("ro", "Romanian"), ("sv", "Swedish"), ("da", "Danish"),
    ("no", "Norwegian"), ("fi", "Finnish"), ("cs", "Czech"), ("hu", "Hungarian"),
    ("el", "Greek"), ("tr", "Turkish"), ("ru", "Russian"), ("uk", "Ukrainian"),
    ("ar", "Arabic"), ("he", "Hebrew"), ("hi", "Hindi"), ("zh", "Chinese"),
    ("ja", "Japanese"), ("ko", "Korean"), ("vi", "Vietnamese"), ("id", "Indonesian"),
)
# Name, then what to expect of it: an indication, measured on no particular machine.
MODELS = (
    ("tiny", "fastest, rough"),
    ("base", "very fast, approximate"),
    ("small", "fast on a CPU, good"),
    ("medium", "slow on a CPU, better"),
    ("large-v3-turbo", "fast on a GPU, excellent"),
    ("large-v3", "slowest, the most accurate"),
    ("distil-large-v3", "fast on a GPU, English only"),
)
MODES = (
    ("cursor", "Insert at the cursor"),
    ("clipboard", "Copy to the clipboard"),
    ("cursor+clipboard", "Insert, and keep in the clipboard"),
)
POSITIONS = (
    ("bottom-center", "Bottom of the screen"),
    ("top-center", "Top of the screen"),
    ("center", "Centre of the screen"),
)
# Where each value of the window lives in config.toml.
FIELDS = {
    "language": ("model", "language"),
    "model": ("model", "name"),
    "vocabulary": ("model", "vocabulary"),
    "device": ("recording", "device"),
    "binding": ("hotkey", "binding"),
    "mode": ("output", "mode"),
    "overlay": ("overlay", "enabled"),
    "position": ("overlay", "position"),
    "accent": ("overlay", "accent"),
}
COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")


def size_label(size: float) -> str:
    return f"{size / 1e9:.1f} GB" if size >= 1e9 else f"{size / 1e6:.0f} MB"


def model_choices() -> list[tuple[str, str]]:
    """Each model with its download size and speed — and whether it is already here."""
    automatic = GPU_MODEL if has_nvidia_gpu() else CPU_MODEL
    choices = [("auto", f"Automatic — {automatic} on this machine")]
    for name, speed in MODELS:
        label = f"{name} — {size_label(MODEL_BYTES[name])}, {speed}"
        if is_downloaded(name):
            label += " · downloaded"
        choices.append((name, label))
    return choices


def with_current(choices: list[tuple[str, str]] | tuple, current: str) -> list[list[str]]:
    """The choices, plus the configured value when it is not one of them."""
    listed = [[value, label] for value, label in choices]
    if current not in {value for value, _label in listed}:
        listed.append([current, current])
    return listed


class LevelMeter:
    """Listens to one microphone and reports its level, for the test gauge."""

    def __init__(self, emit: Callable[[dict[str, Any]], None]):
        self.emit = emit
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

    def start(self, device: str, backend: str) -> None:
        self.stop()
        try:
            source = capture.build(device, RATE, CHANNELS, backend)
            process = subprocess.Popen(
                source.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                env={**os.environ, **source.env} if source.env else None,
            )
        except (capture.CaptureUnavailable, OSError) as error:
            self.emit({"level": 0.0, "meter": f"Cannot listen: {error}"})
            return
        with self._lock:
            self._process = process
        threading.Thread(target=self._listen, args=(process,), daemon=True).start()

    def _listen(self, process: subprocess.Popen[bytes]) -> None:
        assert process.stdout is not None
        deadline = time.monotonic() + METER_SECONDS
        peak = 0.0
        # The pipe is this thread's: it closes it, whoever stopped the capture.
        with process.stdout:
            while time.monotonic() < deadline:
                chunk = process.stdout.read(CHUNK_BYTES)
                if not chunk:
                    break
                level = _rms(chunk)
                peak = max(peak, level)
                self.emit({"level": round(visual_level(level, MIN_THRESHOLD), 3)})
        stopped = self._release(process)
        if stopped:
            return  # asked for: nothing to report
        if peak <= 0.0:
            self.emit({"level": 0.0, "meter": "No sound at all: this microphone is mute or busy"})
        else:
            self.emit({"level": 0.0, "meter": "Test over"})

    def _release(self, process: subprocess.Popen[bytes]) -> bool:
        """Closes the capture; True if stop() had already done it."""
        with self._lock:
            ours = self._process is process
            if ours:
                self._process = None
        if ours:
            process.kill()
            process.wait()
        return not ours

    def stop(self) -> None:
        with self._lock:
            process, self._process = self._process, None
        if process is not None:
            process.kill()
            process.wait()


class SettingsController:
    def __init__(
        self,
        config: dict[str, Any],
        command: str,
        send: Callable[..., dict[str, Any]],
        install_hotkey: Callable[[dict[str, Any], str], str] = hotkey.install,
        emit: Callable[[dict[str, Any]], None] = lambda message: None,
    ):
        self.config = config
        self.command = command
        self.send = send
        self.install_hotkey = install_hotkey
        self.meter = LevelMeter(emit)

    def values(self) -> dict[str, Any]:
        return {
            field: self.config[section][key] for field, (section, key) in FIELDS.items()
        }

    def snapshot(self) -> dict[str, Any]:
        values = self.values()
        binding = hotkey.resolve_binding(self.config)
        return {
            "values": values,
            "hotkey": {"binding": values["binding"], "label": hotkey.label(binding)},
            "choices": {
                "language": with_current(LANGUAGES, values["language"]),
                "model": with_current(model_choices(), values["model"]),
                "device": with_current(
                    capture.devices(str(self.config["recording"]["backend"])), values["device"]
                ),
                "mode": with_current(MODES, values["mode"]),
                "position": with_current(POSITIONS, values["position"]),
            },
        }

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        action = request.get("action")
        if action == "test_mic":
            self.meter.start(
                str(request.get("device") or "default"), str(self.config["recording"]["backend"])
            )
            return None
        if action == "stop_mic":
            self.meter.stop()
            return None
        if action == "hotkey":
            return self.shortcut(str(request.get("accel", "")))
        if action == "save":
            return self.save(request.get("values") or {})
        return None

    def shortcut(self, accel: str) -> dict[str, Any]:
        """A combination pressed in the window, in the configuration's syntax."""
        if accel == "auto":
            binding = "auto"
        else:
            modifiers, key = hotkey.parse_binding(accel)
            if not key:
                return {"status": "Press a key along with the modifiers"}
            # Shift alone would take a capital letter away from every application.
            if not set(modifiers) - {"shift"} and not re.fullmatch(r"f\d{1,2}", key):
                return {"status": "A shortcut needs Ctrl, Alt or Super — or a function key"}
            binding = hotkey.format_gtk(modifiers, key)
        resolved = hotkey.resolve_binding({"hotkey": {"binding": binding}})
        return {"hotkey": {"binding": binding, "label": hotkey.label(resolved)}}

    def changes(self, values: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Only what differs from the file: the rest is left exactly as written."""
        changes: dict[str, dict[str, Any]] = {}
        for field, value in values.items():
            if field not in FIELDS:
                continue
            section, key = FIELDS[field]
            current = self.config[section][key]
            if isinstance(current, bool):
                value = bool(value)
            else:
                value = str(value).strip()
            if value != current:
                changes.setdefault(section, {})[key] = value
        return changes

    def save(self, values: dict[str, Any]) -> dict[str, Any]:
        accent = values.get("accent", self.config["overlay"]["accent"])
        if not COLOUR.match(str(accent)):
            return {"status": f"The colour {accent} is not of the form #rrggbb", "saved": False}
        changes = self.changes(values)
        if not changes:
            return {"status": "Nothing to change", "saved": True}
        self.meter.stop()
        try:
            config_module.update(changes)
        except OSError as error:
            return {"status": f"Could not write the configuration: {error}", "saved": False}
        self.config = config_module.load()

        notes = []
        if "binding" in changes.get("hotkey", {}):
            try:
                installed = self.install_hotkey(self.config, self.command)
                notes.append(f"shortcut {hotkey.label(installed)} installed")
            except (hotkey.UnsupportedDesktop, OSError, subprocess.SubprocessError) as error:
                notes.append(f"shortcut not installed ({error})")
        notes.append(self._reload())
        model = str(self.config["model"]["name"])
        if "name" in changes.get("model", {}) and model in MODEL_BYTES and not is_downloaded(model):
            notes.append(f"{model} downloads in the background ({size_label(MODEL_BYTES[model])})")
        return {**self.snapshot(), "status": "Saved — " + "; ".join(notes), "saved": True}

    def _reload(self) -> str:
        try:
            reply = self.send("reload", timeout=10, autostart=False)
        except Exception:
            return "applies when the service starts"
        if "error" in reply:
            return "applies after the current dictation ('whisper-desk reload')"
        return "applied"

    def close(self) -> None:
        self.meter.stop()


def open_window(config: dict[str, Any], command: str,
                send: Callable[..., dict[str, Any]]) -> int:
    """Shows the settings until the window is closed."""
    window = window_proc.WindowProcess(WINDOW_SCRIPT)
    controller = SettingsController(config, command, send, emit=window.show)
    try:
        return window_proc.run(WINDOW_SCRIPT, controller.snapshot(), controller.handle, window)
    finally:
        controller.close()
