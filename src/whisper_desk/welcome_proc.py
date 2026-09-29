"""The first launch: microphone, language and model, download, a first dictation.

The welcome window (welcome_window.py) walks through the steps; this side
does what they need — the same things the settings window does, plus the
watch over the model while it arrives.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import config as config_module
from . import hotkey, window_proc
from .settings_proc import SettingsController
from .transcriber import has_nvidia_gpu

WINDOW_SCRIPT = Path(__file__).with_name("welcome_window.py")
MARKER = "welcomed"
WATCH_SECONDS = 0.5


def marker() -> Path:
    return config_module.STATE_DIR / MARKER


def welcomed() -> bool:
    return marker().exists()


def mark_welcomed() -> None:
    config_module.STATE_DIR.mkdir(parents=True, exist_ok=True)
    marker().touch()


class WelcomeController:
    def __init__(
        self,
        config: dict[str, Any],
        command: str,
        send: Callable[..., dict[str, Any]],
        emit: Callable[[dict[str, Any]], None] = lambda message: None,
    ):
        self.settings = SettingsController(config, command, send, emit=emit)
        self.send = send
        self.emit = emit
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._following = False

    def snapshot(self) -> dict[str, Any]:
        return {
            **self.settings.snapshot(),
            "gpu": has_nvidia_gpu(),
            "shortcut": hotkey.label(hotkey.resolve_binding(self.settings.config)),
        }

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        action = request.get("action")
        if action in ("test_mic", "stop_mic"):
            return self.settings.handle(request)
        if action == "prepare":
            reply = self.settings.save(request.get("values") or {})
            self._watch()
            return {"status": reply.get("status", "")}
        if action == "try":
            try:
                state = self.send("toggle", timeout=10).get("state", "")
            except Exception as error:
                return {"tried": "", "status": f"The service does not answer: {error}"}
            return {"tried": state}
        if action == "finish":
            mark_welcomed()
            self.close()
        return None

    def model_state(self) -> dict[str, Any] | None:
        """What the daemon says of its model — None while it does not answer yet."""
        try:
            status = self.send("status", timeout=5, autostart=False)
        except Exception:
            return None
        return {
            "name": status.get("model", ""),
            "loaded": bool(status.get("loaded")),
            "download": status.get("download"),
        }

    def _watch(self) -> None:
        """Starts the daemon if need be, asks for the model, and follows it in."""
        with self._lock:
            if self._following:
                return
            self._following = True

        def follow() -> None:
            try:
                # Starting it here: the first launch may come before any service.
                self.send("load", timeout=60)
                while not self._stop.is_set():
                    state = self.model_state()
                    if state is not None:
                        self.emit({"model": state})
                        if state["loaded"]:
                            break
                    self._stop.wait(WATCH_SECONDS)
            except Exception as error:
                self.emit({"model": {"error": str(error)}})
            finally:
                with self._lock:
                    self._following = False

        threading.Thread(target=follow, daemon=True).start()

    def close(self) -> None:
        self._stop.set()
        self.settings.close()


def open_window(config: dict[str, Any], command: str,
                send: Callable[..., dict[str, Any]]) -> int:
    window = window_proc.WindowProcess(WINDOW_SCRIPT)
    controller = WelcomeController(config, command, send, emit=window.show)
    try:
        return window_proc.run(WINDOW_SCRIPT, controller.snapshot(), controller.handle, window)
    finally:
        controller.close()
