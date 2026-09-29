"""The history window's requests: copy, insert again, delete, purge."""

from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta
from unittest import mock

from . import context  # noqa: F401
from .test_history import HistoryCase

from whisper_desk import config as config_module
from whisper_desk import history
from whisper_desk.history_proc import HistoryController

NOW = datetime.now().replace(microsecond=0)


class ControllerTest(HistoryCase):
    def setUp(self):
        super().setUp()
        history.append("an old dictation", duration=1.0, model="small", now=NOW - timedelta(days=60))
        history.append("the one that got lost", duration=4.2, model="small", now=NOW)
        self.copied: list[str] = []
        self.sent: list[tuple[str, dict]] = []
        self.reply: dict = {"inserting": True}
        self.config = copy.deepcopy(config_module.DEFAULTS)
        self.controller = HistoryController(
            self.config,
            copy=lambda text: self.copied.append(text) or True,
            send=lambda command, **kwargs: self.sent.append((command, kwargs)) or self.reply,
        )

    def request(self, action: str, index: int) -> dict:
        entry = history.load()[index]
        return {"action": action, "id": index, "date": entry.date}

    def test_the_window_sees_every_dictation(self):
        snapshot = self.controller.snapshot()
        self.assertEqual([entry["text"] for entry in snapshot["entries"]],
                         ["an old dictation", "the one that got lost"])
        self.assertEqual(snapshot["entries"][1]["duration"], 4.2)
        self.assertEqual(snapshot["keep_days"], 0)
        self.assertIn("+", snapshot["hint"])

    def test_copy(self):
        reply = self.controller.handle(self.request("copy", 1))
        self.assertEqual(self.copied, ["the one that got lost"])
        self.assertEqual(reply["status"], "Copied to the clipboard")

    def test_insert_goes_through_the_daemon(self):
        self.assertIsNone(self.controller.handle(self.request("insert", 1)))
        self.assertEqual(self.sent, [("insert", {"timeout": 10, "text": "the one that got lost"})])
        self.assertEqual(self.copied, [])

    def test_a_busy_daemon_leaves_the_text_in_the_clipboard(self):
        self.reply = {"error": "busy (recording)"}
        with mock.patch("whisper_desk.output.notify"):
            self.controller.handle(self.request("insert", 1))
        self.assertEqual(self.copied, ["the one that got lost"])

    def test_delete(self):
        reply = self.controller.handle(self.request("delete", 0))
        self.assertEqual([entry["text"] for entry in reply["entries"]], ["the one that got lost"])

    def test_a_dictation_that_moved_is_found_by_its_date(self):
        request = self.request("delete", 1)
        request["id"] = 0
        self.controller.handle(request)
        self.assertEqual([entry.text for entry in history.load()], ["an old dictation"])

    def test_a_dictation_already_gone_is_not_someone_else(self):
        reply = self.controller.handle({"action": "delete", "id": 0, "date": "1999-01-01T00:00:00"})
        self.assertEqual(len(history.load()), 2)
        self.assertIn("no longer", reply["status"])

    def test_keep_days_purges_writes_and_reloads(self):
        reply = self.controller.handle({"action": "keep_days", "days": 30})
        self.assertEqual([entry["text"] for entry in reply["entries"]], ["the one that got lost"])
        self.assertEqual(reply["keep_days"], 30)
        self.assertEqual(config_module.load(self.config_path)["output"]["history_days"], 30)
        self.assertIn(("reload", {"timeout": 10, "autostart": False}), self.sent)


if __name__ == "__main__":
    unittest.main()
