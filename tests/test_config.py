"""Merging the user configuration with the default values."""

from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path

from . import context  # noqa: F401

from whisper_desk import config as config_module


class MergeTest(unittest.TestCase):
    def test_missing_keys_keep_the_default(self):
        merged = config_module._merge({"a": 1, "b": 2}, {"b": 3})
        self.assertEqual(merged, {"a": 1, "b": 3})

    def test_deep_merge(self):
        merged = config_module._merge(
            {"model": {"name": "auto", "language": "fr"}}, {"model": {"language": "en"}}
        )
        self.assertEqual(merged, {"model": {"name": "auto", "language": "en"}})

    def test_the_default_is_not_modified(self):
        base = {"model": {"language": "fr"}}
        config_module._merge(base, {"model": {"language": "en"}})
        self.assertEqual(base, {"model": {"language": "fr"}})


class LoadTest(unittest.TestCase):
    def write(self, content: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.toml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_missing_file(self):
        config = config_module.load(Path("/nonexistent/config.toml"))
        self.assertEqual(config, config_module.DEFAULTS)
        self.assertIsNot(config["model"], config_module.DEFAULTS["model"])

    def test_partial_override(self):
        config = config_module.load(self.write('[model]\nlanguage = "en"\n'))
        self.assertEqual(config["model"]["language"], "en")
        self.assertEqual(config["model"]["name"], config_module.DEFAULTS["model"]["name"])
        self.assertEqual(config["overlay"], config_module.DEFAULTS["overlay"])

    def test_every_expected_section_is_there(self):
        config = config_module.load(Path("/nonexistent"))
        self.assertEqual(
            set(config), {"hotkey", "model", "recording", "output", "overlay"}
        )

    def test_the_keys_read_by_the_code_exist(self):
        """The code indexes the config without .get(): a missing key would crash."""
        config = config_module.load(Path("/nonexistent"))
        expected = {
            "recording": {
                "device", "backend", "max_seconds", "streaming",
                "segment_silence_seconds", "silence_seconds",
                "start_timeout_seconds", "threshold",
            },
            "output": {
                "mode", "paste_shortcut", "keyboard", "restore_clipboard",
                "notify", "history", "history_days",
            },
            "overlay": {"enabled", "accent", "width", "height", "position", "margin"},
            "model": {
                "name", "device", "compute_type", "language", "beam_size",
                "initial_prompt", "preload",
            },
            "hotkey": {"binding", "name", "action"},
        }
        for section, keys in expected.items():
            self.assertLessEqual(keys, set(config[section]), section)


class ExampleTest(unittest.TestCase):
    def test_the_example_mirrors_the_defaults(self):
        """A default changed without its example would go unnoticed by every user."""
        example = config_module.load(config_module.EXAMPLE_PATH)
        with config_module.EXAMPLE_PATH.open("rb") as handle:
            written = tomllib.load(handle)
        self.assertEqual(written, config_module.DEFAULTS)
        self.assertEqual(example, config_module.DEFAULTS)


if __name__ == "__main__":
    unittest.main()
