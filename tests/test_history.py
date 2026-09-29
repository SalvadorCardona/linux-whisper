"""Dictation history: one JSON line per dictation, the old log still read."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from . import context  # noqa: F401

from whisper_desk import __main__ as cli
from whisper_desk import config as config_module
from whisper_desk import history

NOW = datetime(2026, 9, 29, 10, 0, 0)


class HistoryCase(unittest.TestCase):
    """A history of its own, in a temporary state directory."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = Path(directory.name) / "state"
        patcher = mock.patch.object(config_module, "STATE_DIR", state)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config_path = Path(directory.name) / "config.toml"
        patcher = mock.patch.object(config_module, "CONFIG_PATH", self.config_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def legacy(self, *lines: str) -> None:
        config_module.STATE_DIR.mkdir(parents=True, exist_ok=True)
        history.legacy_path().write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


class StorageTest(HistoryCase):
    def test_one_json_line_per_dictation(self):
        history.append("hello there", duration=3.21, model="small", now=NOW)
        lines = history.path().read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            json.loads(lines[0]),
            {"date": "2026-09-29T10:00:00", "text": "hello there", "duration": 3.2, "model": "small"},
        )

    def test_nothing_to_record(self):
        self.assertIsNone(history.append("   "))
        self.assertFalse(history.path().exists())

    def test_the_text_is_kept_whole_without_its_spaces_around(self):
        history.append("  first sentence. second one  ", now=NOW)
        self.assertEqual(history.load()[0].text, "first sentence. second one")

    def test_accents_are_written_as_they_are(self):
        history.append("l'été à Noël", now=NOW)
        self.assertIn("l'été à Noël", history.path().read_text(encoding="utf-8"))

    def test_a_broken_line_costs_only_itself(self):
        history.append("before", now=NOW)
        with history.path().open("a", encoding="utf-8") as handle:
            handle.write('{"date": "2026-09-29T10:01:00", "te\n')
        history.append("after", now=NOW + timedelta(minutes=2))
        self.assertEqual([entry.text for entry in history.load()], ["before", "after"])


class LegacyTest(HistoryCase):
    """The history.log of earlier versions is still there to be found."""

    def test_the_old_log_is_read(self):
        self.legacy("2026-09-20T09:14:02\tan old sentence")
        entry = history.load()[0]
        self.assertEqual((entry.date, entry.text, entry.duration, entry.model),
                         ("2026-09-20T09:14:02", "an old sentence", None, None))

    def test_old_and_new_come_in_date_order(self):
        self.legacy("2026-09-20T09:14:02\told")
        history.append("new", now=NOW)
        self.assertEqual([entry.text for entry in history.load()], ["old", "new"])

    def test_a_rewrite_folds_the_old_log_in_and_keeps_a_copy(self):
        self.legacy("2026-09-20T09:14:02\told", "2026-09-21T09:14:02\tolder not")
        history.append("new", now=NOW)
        history.delete(0)
        self.assertFalse(history.legacy_path().exists())
        self.assertTrue((config_module.STATE_DIR / history.LEGACY_BACKUP).exists())
        self.assertEqual([entry.text for entry in history.load()], ["older not", "new"])


class EditTest(HistoryCase):
    def setUp(self):
        super().setUp()
        for days, text in ((40, "a month ago"), (3, "three days ago"), (0, "today")):
            history.append(text, now=NOW - timedelta(days=days))

    def test_delete(self):
        removed = history.delete(1)
        self.assertEqual(removed.text, "three days ago")
        self.assertEqual([entry.text for entry in history.load()], ["a month ago", "today"])

    def test_clear(self):
        self.assertEqual(history.clear(), 3)
        self.assertEqual(history.load(), [])

    def test_purge_keeps_the_recent_ones(self):
        self.assertEqual(history.purge(30, now=NOW), 1)
        self.assertEqual([entry.text for entry in history.load()], ["three days ago", "today"])

    def test_zero_days_keeps_everything(self):
        self.assertEqual(history.purge(0, now=NOW), 0)
        self.assertEqual(len(history.load()), 3)

    def test_recording_applies_the_limit(self):
        history.append("now", keep_days=7, now=NOW)
        self.assertEqual([entry.text for entry in history.load()],
                         ["three days ago", "today", "now"])


class SearchTest(unittest.TestCase):
    def entry(self, text: str) -> history.Entry:
        return history.Entry(date="2026-09-29T10:00:00", text=text)

    def test_case_and_accents_do_not_matter(self):
        self.assertTrue(history.matches(self.entry("Résumé de la RÉUNION"), "resume reunion"))

    def test_every_word_must_be_there_in_any_order(self):
        entry = self.entry("send the invoice on Thursday")
        self.assertTrue(history.matches(entry, "thursday invoice"))
        self.assertFalse(history.matches(entry, "invoice friday"))

    def test_the_latest_is_number_one(self):
        entries = [self.entry("old"), self.entry("new")]
        self.assertEqual([(number, entry.text) for number, entry in history.numbered(entries)],
                         [(1, "new"), (2, "old")])
        self.assertEqual(history.index_of(1, entries), 1)
        with self.assertRaises(IndexError):
            history.index_of(3, entries)


class CommandTest(HistoryCase):
    """whisper-desk history: list, search, copy, delete."""

    def setUp(self):
        super().setUp()
        history.append("We should add pictures", duration=6.4, model="small",
                       now=NOW - timedelta(hours=1))
        history.append("Résumé de la réunion", duration=2.0, model="small", now=NOW)

    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.main(["history", *argv])
        return code, out.getvalue()

    def test_the_list_ends_on_the_latest(self):
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertTrue(lines[-1].lstrip().startswith("1 "))
        self.assertIn("Résumé de la réunion", lines[-1])
        self.assertIn("6.4 s", lines[0])

    def test_search(self):
        _code, out = self.run_cli("resume")
        self.assertIn("Résumé", out)
        self.assertNotIn("pictures", out)

    def test_copy_takes_the_listed_number(self):
        copied: list[str] = []
        with mock.patch("whisper_desk.output.copy", lambda text, overlay=None: copied.append(text) or True):
            code, _out = self.run_cli("--copy", "2")
        self.assertEqual((code, copied), (0, ["We should add pictures"]))

    def test_an_unknown_number_is_an_error(self):
        code, out = self.run_cli("--copy", "7")
        self.assertEqual(code, 1)
        self.assertIn("there are 2", out)

    def test_delete(self):
        self.run_cli("--delete", "1")
        self.assertEqual([entry.text for entry in history.load()], ["We should add pictures"])

    def test_keep_days_is_written_to_the_configuration(self):
        self.run_cli("--keep-days", "30")
        self.assertEqual(config_module.load(self.config_path)["output"]["history_days"], 30)


if __name__ == "__main__":
    unittest.main()
