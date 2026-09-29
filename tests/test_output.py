"""Output modes, delivery, clipboard per host."""

from __future__ import annotations

import codecs
import unittest
from pathlib import Path
from unittest import mock

from . import context  # noqa: F401
from .context import forced_host

from whisper_desk import config as config_module
from whisper_desk import host
from whisper_desk import output
from whisper_desk.inject import parse_shortcut


def with_mode(mode: str) -> dict:
    return {"output": {"mode": mode}}


class ModesTest(unittest.TestCase):
    def test_single_mode(self):
        self.assertEqual(output.modes(with_mode("cursor")), {"cursor"})

    def test_combined_modes(self):
        self.assertEqual(output.modes(with_mode("cursor+stdout")), {"cursor", "stdout"})

    def test_spaces_are_ignored(self):
        self.assertEqual(output.modes(with_mode(" cursor + stdout ")), {"cursor", "stdout"})

    def test_type_is_still_a_synonym_of_cursor(self):
        self.assertEqual(output.modes(with_mode("type")), {"cursor"})
        self.assertEqual(output.modes(with_mode("type+clipboard")), {"cursor", "clipboard"})

    def test_empty_mode(self):
        self.assertEqual(output.modes(with_mode("")), set())

    def test_the_project_default_inserts_at_the_cursor(self):
        self.assertIn("cursor", output.modes(config_module.load(Path("/nonexistent"))))


class ShortcutTest(unittest.TestCase):
    def test_default_paste(self):
        self.assertEqual(parse_shortcut("ctrl+v"), ["ctrl", "v"])

    def test_terminal_variant(self):
        self.assertEqual(parse_shortcut("shift+insert"), ["shift", "insert"])

    def test_case_and_spaces(self):
        self.assertEqual(parse_shortcut(" Ctrl + V "), ["ctrl", "v"])

    def test_unknown_keys_are_dropped(self):
        self.assertEqual(parse_shortcut("hyper+ctrl+v"), ["ctrl", "v"])

    def test_empty_shortcut(self):
        self.assertEqual(parse_shortcut(""), [])
        self.assertEqual(parse_shortcut("++"), [])


class DeliverTest(unittest.TestCase):
    """The dictation must know when its text went nowhere, to say so."""

    CONFIG = {"output": {"mode": "clipboard", "history": False, "notify": False}}

    def test_a_copied_text_is_delivered(self):
        with mock.patch.object(output, "copy", lambda text, overlay=None: True):
            self.assertTrue(output.deliver("text", self.CONFIG))

    def test_a_clipboard_that_refuses_is_a_failure(self):
        with mock.patch.object(output, "copy", lambda text, overlay=None: False):
            self.assertFalse(output.deliver("text", self.CONFIG))

    def test_the_writer_tells_why_it_failed(self):
        writer = output.CursorWriter.__new__(output.CursorWriter)
        writer.settings = {"restore_clipboard": False}
        writer.clipboard = mock.Mock(set=lambda text: True)
        writer.keyboard = mock.Mock(press=lambda shortcut: False)
        writer.shortcut = ["ctrl", "v"]
        writer.failure = ""
        with mock.patch.object(output.time, "sleep"):
            self.assertFalse(writer.write("text"))
        self.assertEqual(writer.failure, "keyboard")


class ClipboardTest(unittest.TestCase):
    """The clipboard goes through the host's tool, without ever trying them all."""

    def copy_with(self, host_name: str, present: tuple[str, ...], text: str = "l'été"):
        calls: list[tuple[list[str], bytes | None]] = []
        with forced_host(host_name), \
                mock.patch.object(
                    output.shutil, "which",
                    lambda name: f"/usr/bin/{name}" if name in present else None), \
                mock.patch.object(
                    output, "_run",
                    lambda command, data=None: calls.append((command, data)) or True):
            copied = output.Clipboard().set(text)
        return copied, calls

    def test_wayland_before_x11(self):
        _, calls = self.copy_with(host.LINUX, ("wl-copy", "xclip"))
        self.assertEqual(len(calls), 1)
        self.assertIn("wl-copy", calls[0][0][0])

    def test_x11_when_wayland_is_missing(self):
        _, calls = self.copy_with(host.LINUX, ("xclip",))
        self.assertIn("xclip", calls[0][0][0])
        self.assertEqual(calls[0][1], "l'été".encode())

    def test_macos_uses_pbcopy(self):
        _, calls = self.copy_with(host.MACOS, ("pbcopy", "wl-copy"))
        self.assertIn("pbcopy", calls[0][0][0])

    def test_wsl_goes_through_windows_before_the_linux_tools(self):
        _, calls = self.copy_with(host.WSL, ("clip.exe", "wl-copy"))
        self.assertIn("clip.exe", calls[0][0][0])

    def test_clip_exe_receives_utf16_with_its_byte_order_mark(self):
        """Without the mark, clip.exe reads UTF-8 as native text: accents are lost."""
        _, calls = self.copy_with(host.WSL, ("clip.exe",))
        data = calls[0][1]
        self.assertTrue(data.startswith(codecs.BOM_UTF16_LE))
        self.assertEqual(data[len(codecs.BOM_UTF16_LE):].decode("utf-16-le"), "l'été")

    def test_the_overlay_takes_over(self):
        overlay = mock.Mock()
        overlay.copy.return_value = True
        with forced_host(host.LINUX), \
                mock.patch.object(output.shutil, "which", lambda name: None):
            self.assertTrue(output.Clipboard(overlay).set("hello"))
        overlay.copy.assert_called_once_with("hello")

    def test_save_then_restore(self):
        with forced_host(host.MACOS), \
                mock.patch.dict(output.GETTERS, {host.MACOS: (lambda: "before",)}), \
                mock.patch.object(output.Clipboard, "set") as setter:
            clipboard = output.Clipboard()
            clipboard.save()
            clipboard.save()          # two dictations in a row must not overwrite it
            clipboard.restore()
        setter.assert_called_once_with("before")

    def test_the_tool_reported_by_the_diagnostic(self):
        with forced_host(host.MACOS), \
                mock.patch.object(
                    output.shutil, "which", lambda name: "/bin/pbcopy" if name == "pbcopy" else None):
            self.assertEqual(output.clipboard_tool(), "pbcopy")
        with forced_host(host.LINUX), mock.patch.object(output.shutil, "which", lambda name: None):
            self.assertIsNone(output.clipboard_tool())

    def test_every_host_has_readers_and_writers(self):
        for name in (host.LINUX, host.WSL, host.MACOS):
            self.assertTrue(output.SETTERS[name])
            self.assertTrue(output.GETTERS[name])
            self.assertTrue(output.CLIPBOARD_TOOLS[name])


class AppleScriptStringTest(unittest.TestCase):
    def test_quotes_and_backslash(self):
        self.assertEqual(output._applescript_string('a"b\\c'), '"a\\"b\\\\c"')


if __name__ == "__main__":
    unittest.main()
