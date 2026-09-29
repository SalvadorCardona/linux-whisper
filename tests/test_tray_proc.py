"""The tray: the daemon's state in a menu, and the menu's orders carried out."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from . import context  # noqa: F401
from .context import forced_host

from whisper_desk import config as config_module
from whisper_desk import host, tray_proc
from whisper_desk.tray_proc import TrayController

READY = {"state": "idle", "model": "small", "device": "cpu", "loaded": True,
         "microphone": "default", "paused": False, "download": None}


class MenuTest(unittest.TestCase):
    def setUp(self):
        self.replies: dict[str, dict] = {"status": dict(READY)}
        self.sent: list[tuple[str, dict]] = []
        self.spawned: list[list[str]] = []
        self.controller = TrayController(
            copy.deepcopy(config_module.DEFAULTS), "/bin/whisper-desk",
            send=self.send, spawn=self.spawned.append,
        )

    def send(self, command: str, **kwargs):
        self.sent.append((command, kwargs))
        reply = self.replies.get(command, {})
        if isinstance(reply, Exception):
            raise reply
        return reply

    def items(self, status) -> dict[str, dict]:
        with forced_host(host.LINUX):
            menu = self.controller.menu(status)
        return {item["id"]: item for item in menu["items"] if "id" in item}

    def test_ready_says_which_model_and_where(self):
        items = self.items(READY)
        self.assertEqual(items["status"]["label"], "Ready — small on the CPU")
        self.assertEqual(items["microphone"]["label"], "Microphone: default")
        self.assertEqual(items["toggle"]["label"], "Start a dictation (Super+J)")

    def test_listening_offers_to_finish(self):
        items = self.items({**READY, "state": "recording"})
        self.assertEqual(items["status"]["label"], "● Listening…")
        self.assertEqual(items["toggle"]["label"], "Finish the dictation")

    def test_a_download_shows_its_progress(self):
        items = self.items({**READY, "loaded": False, "loading": True, "download": 0.42,
                            "model": "large-v3"})
        self.assertEqual(items["status"]["label"], "Downloading large-v3 — 42 %")

    def test_a_model_not_preloaded_is_not_forever_loading(self):
        items = self.items({**READY, "loaded": False, "loading": False})
        self.assertEqual(items["status"]["label"], "Ready — small loads at the first dictation")

    def test_a_failed_load_is_said(self):
        items = self.items({**READY, "loaded": False, "loading": False, "load_error": "disk full"})
        self.assertIn("could not be loaded", items["status"]["label"])

    def test_paused_greys_the_dictation_out(self):
        items = self.items({**READY, "paused": True})
        self.assertFalse(items["toggle"]["enabled"])
        self.assertTrue(items["pause"]["active"])

    def test_a_stopped_service_is_said(self):
        with forced_host(host.LINUX):
            menu = self.controller.menu(None)
        self.assertEqual(menu["icon"], "action-unavailable-symbolic")
        self.assertIn("Service stopped", menu["items"][0]["label"])

    def test_the_status_is_asked_without_starting_the_daemon(self):
        self.controller.refresh()
        self.assertEqual(self.sent, [("status", {"timeout": 5, "autostart": False})])

    def test_an_unreachable_daemon_is_a_stopped_one(self):
        self.replies["status"] = OSError("no socket")
        self.assertIsNone(self.controller.status())

    def test_the_dictation_item_toggles(self):
        self.controller.handle({"action": "toggle"})
        self.assertEqual(self.sent[0][0], "toggle")

    def test_pause_and_resume(self):
        self.controller.handle({"action": "pause", "active": True})
        self.controller.handle({"action": "pause", "active": False})
        self.assertEqual([command for command, _ in self.sent if command != "status"],
                         ["pause", "resume"])

    def test_history_and_settings_open_their_windows(self):
        self.controller.handle({"action": "history"})
        self.controller.handle({"action": "settings"})
        self.assertEqual(self.spawned, [["/bin/whisper-desk", "history", "--window"],
                                        ["/bin/whisper-desk", "settings"]])

    def test_quit_stops_the_daemon_and_the_tray(self):
        self.assertEqual(self.controller.handle({"action": "quit"}), {"quit": True})
        self.assertEqual(self.sent, [("quit", {"timeout": 5, "autostart": False})])

    def test_a_missing_indicator_is_remembered(self):
        self.controller.handle({"event": "unavailable", "reason": "no AppIndicator"})
        self.assertEqual(self.controller.unavailable, "no AppIndicator")


class SupportTest(unittest.TestCase):
    """GNOME without its extension shows no icon: say so, and where to go instead."""

    def test_no_library(self):
        with mock.patch.object(tray_proc, "library", lambda: None):
            support = tray_proc.support()
        self.assertFalse(support.ok)
        self.assertIn("gir1.2-ayatanaappindicator3", support.fix)

    def test_gnome_without_the_extension(self):
        with mock.patch.object(tray_proc, "library", lambda: "AyatanaAppIndicator3"), \
                mock.patch.object(tray_proc, "has_watcher", lambda: False), \
                mock.patch.object(tray_proc.hotkey, "is_gnome", lambda: True):
            support = tray_proc.support()
        self.assertFalse(support.ok)
        self.assertIn(tray_proc.EXTENSION, support.fix)
        self.assertIn("applications menu", support.fix)

    def test_a_desktop_that_hosts_icons(self):
        with mock.patch.object(tray_proc, "library", lambda: "AyatanaAppIndicator3"), \
                mock.patch.object(tray_proc, "has_watcher", lambda: True):
            self.assertTrue(tray_proc.support().ok)


class LockTest(unittest.TestCase):
    def test_one_tray_per_session(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(config_module, "RUNTIME_DIR", Path(directory)):
            first = tray_proc.lock()
            self.assertIsNotNone(first)
            self.assertIsNone(tray_proc.lock())
            first.close()
            again = tray_proc.lock()
            self.assertIsNotNone(again)
            again.close()


if __name__ == "__main__":
    unittest.main()
