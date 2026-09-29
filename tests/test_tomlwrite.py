"""Writing the configuration by hand: the value changes, the comments stay."""

from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from . import context  # noqa: F401

from whisper_desk import config as config_module
from whisper_desk import tomlwrite

DOCUMENT = """\
# whisper-desk configuration

[model]
# "auto": large-v3-turbo with an NVIDIA GPU, small otherwise.
name = "auto"
device = "auto"          # auto | cuda | cpu
beam_size = 5

[output]
mode = "cursor"
notify = false                  # a notification for every transcription
"""


class FormatTest(unittest.TestCase):
    def test_scalars(self):
        self.assertEqual(tomlwrite.format_value(True), "true")
        self.assertEqual(tomlwrite.format_value(0), "0")
        self.assertEqual(tomlwrite.format_value(2.0), "2.0")
        self.assertEqual(tomlwrite.format_value(0.6), "0.6")
        self.assertEqual(tomlwrite.format_value(["a", 1]), '["a", 1]')

    def test_strings_are_escaped(self):
        text = 'a "quoted" \\ path\nwith\ttabs\x01'
        self.assertEqual(tomllib.loads(f"v = {tomlwrite.format_value(text)}")["v"], text)

    def test_what_toml_cannot_hold(self):
        with self.assertRaises(TypeError):
            tomlwrite.format_value(None)


class UpdateTest(unittest.TestCase):
    def test_only_the_value_changes(self):
        edited = tomlwrite.update(DOCUMENT, {"model": {"name": "small"}})
        self.assertEqual(edited, DOCUMENT.replace('name = "auto"', 'name = "small"'))

    def test_the_comment_keeps_its_column(self):
        edited = tomlwrite.update(DOCUMENT, {"model": {"device": "cuda"}})
        self.assertIn('device = "cuda"          # auto | cuda | cpu', edited)

    def test_a_longer_value_pushes_the_comment_rather_than_eat_it(self):
        edited = tomlwrite.update(DOCUMENT, {"output": {"notify": "a very long value, longer"}})
        self.assertIn('notify = "a very long value, longer" # a notification', edited)

    def test_a_value_holding_a_hash_is_not_mistaken_for_a_comment(self):
        document = 'accent = "#e46212"   # the colour\n'
        edited = tomlwrite.update("[overlay]\n" + document, {"overlay": {"accent": "#123456"}})
        self.assertIn('accent = "#123456"   # the colour', edited)

    def test_a_missing_key_joins_its_section(self):
        edited = tomlwrite.update(DOCUMENT, {"model": {"language": "en"}})
        self.assertEqual(tomllib.loads(edited)["model"]["language"], "en")
        lines = edited.splitlines()
        self.assertEqual(lines[lines.index("beam_size = 5") + 1], 'language = "en"')

    def test_a_missing_section_is_added_at_the_end(self):
        edited = tomlwrite.update(DOCUMENT, {"overlay": {"accent": "#ffffff"}})
        self.assertTrue(edited.endswith('\n[overlay]\naccent = "#ffffff"\n'))

    def test_the_same_key_in_another_section_is_left_alone(self):
        document = '[a]\nname = "one"\n\n[b]\nname = "two"\n'
        edited = tomlwrite.update(document, {"b": {"name": "three"}})
        self.assertEqual(tomllib.loads(edited), {"a": {"name": "one"}, "b": {"name": "three"}})

    def test_a_commented_out_key_stays_a_comment(self):
        document = '[model]\n# name = "tiny"\nname = "auto"\n'
        edited = tomlwrite.update(document, {"model": {"name": "base"}})
        self.assertIn('# name = "tiny"', edited)
        self.assertEqual(tomllib.loads(edited)["model"]["name"], "base")


class ApplyTest(unittest.TestCase):
    def test_editing_when_it_can(self):
        edited = tomlwrite.apply(DOCUMENT, {"model": {"beam_size": 1}})
        self.assertIn("# whisper-desk configuration", edited)

    def test_a_clean_rewrite_when_it_cannot(self):
        """A multi-line value is beyond this editor: the values survive, the comments not."""
        document = '[model]\ninitial_prompt = """\nline\n"""\nname = "auto"\n'
        edited = tomlwrite.apply(document, {"model": {"initial_prompt": "one line"}})
        self.assertEqual(tomllib.loads(edited)["model"],
                         {"initial_prompt": "one line", "name": "auto"})

    def test_an_unreadable_file_is_replaced_by_the_values(self):
        edited = tomlwrite.apply("this is [not toml", {"model": {"name": "small"}})
        self.assertEqual(tomllib.loads(edited), {"model": {"name": "small"}})


class ConfigUpdateTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "whisper-desk" / "config.toml"

    def test_a_missing_file_starts_from_the_commented_example(self):
        config_module.update({"model": {"language": "en"}}, self.path)
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# whisper-desk configuration", text)
        self.assertEqual(config_module.load(self.path)["model"]["language"], "en")

    def test_without_the_example_the_file_holds_just_the_values(self):
        with mock.patch.object(config_module, "EXAMPLE_PATH", Path("/nonexistent")):
            config_module.update({"model": {"language": "en"}}, self.path)
        self.assertEqual(tomllib.loads(self.path.read_text()), {"model": {"language": "en"}})

    def test_every_default_value_can_be_written_back(self):
        """What the settings window writes, the loader must read back unchanged."""
        config_module.update(config_module.DEFAULTS, self.path)
        self.assertEqual(config_module.load(self.path), config_module.DEFAULTS)


if __name__ == "__main__":
    unittest.main()
