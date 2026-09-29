"""The first launch: the model followed in, a first dictation, never twice."""

from __future__ import annotations

import contextlib
import io
import threading
import unittest
from unittest import mock

from . import context  # noqa: F401
from .test_history import HistoryCase

from whisper_desk import __main__ as cli
from whisper_desk import capture, welcome_proc
from whisper_desk.welcome_proc import WelcomeController


class WelcomeTest(HistoryCase):
    def setUp(self):
        super().setUp()
        for target, name, value in (
            (welcome_proc, "has_nvidia_gpu", lambda: False),
            (capture, "devices", lambda backend="auto": [capture.DEFAULT_DEVICE]),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.statuses = [
            {"model": "small", "loaded": False, "download": 0.3},
            {"model": "small", "loaded": False, "download": None},
            {"model": "small", "loaded": True, "download": None},
        ]
        self.sent: list[str] = []
        self.emitted: list[dict] = []
        self.done = threading.Event()
        self.controller = WelcomeController(
            welcome_proc.config_module.load(), "/bin/whisper-desk toggle",
            send=self.send, emit=self.emit,
        )
        self.addCleanup(self.controller.close)

    def send(self, command: str, **kwargs):
        self.sent.append(command)
        if command == "status":
            return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if command == "toggle":
            return {"state": "recording"}
        return {}

    def emit(self, message: dict) -> None:
        self.emitted.append(message)
        if message.get("model", {}).get("loaded") or message.get("model", {}).get("error"):
            self.done.set()

    def test_the_window_learns_the_shortcut_and_the_hardware(self):
        snapshot = self.controller.snapshot()
        self.assertIn("+", snapshot["shortcut"])
        self.assertFalse(snapshot["gpu"])
        self.assertIn("model", snapshot["choices"])

    def test_the_model_is_followed_until_it_is_loaded(self):
        with mock.patch.object(welcome_proc, "WATCH_SECONDS", 0.01):
            reply = self.controller.handle({"action": "prepare", "values": {"language": "en"}})
            self.assertTrue(self.done.wait(5))
        self.assertIn("Saved", reply["status"])
        self.assertIn("load", self.sent)
        models = [message["model"] for message in self.emitted if "model" in message]
        self.assertEqual(models[0], {"name": "small", "loaded": False, "download": 0.3})
        self.assertTrue(models[-1]["loaded"])
        self.assertEqual(welcome_proc.config_module.load(self.config_path)["model"]["language"], "en")

    def test_a_silent_service_is_said(self):
        def refuse(command, **kwargs):
            raise OSError("daemon unreachable")
        self.controller.send = refuse
        self.controller._watch()
        self.assertTrue(self.done.wait(5))
        self.assertIn("unreachable", self.emitted[-1]["model"]["error"])

    def test_the_test_dictation_goes_through_the_shortcut(self):
        self.assertEqual(self.controller.handle({"action": "try"}), {"tried": "recording"})

    def test_finishing_is_remembered(self):
        self.assertFalse(welcome_proc.welcomed())
        self.controller.handle({"action": "finish"})
        self.assertTrue(welcome_proc.welcomed())

    def test_the_first_launch_happens_once(self):
        welcome_proc.mark_welcomed()
        with mock.patch.object(welcome_proc, "open_window") as window:
            self.assertEqual(cli.main(["welcome", "--if-first"]), 0)
        window.assert_not_called()

    def test_without_gtk_the_terminal_says_where_things_are(self):
        err = io.StringIO()
        with mock.patch.object(welcome_proc.window_proc, "gtk_available", lambda: False), \
                contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["welcome"]), 1)
        self.assertIn("doctor", err.getvalue())
        self.assertTrue(welcome_proc.welcomed())


if __name__ == "__main__":
    unittest.main()
