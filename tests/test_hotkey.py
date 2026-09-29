"""Global shortcut: syntax of the combinations, and translation for each host."""

from __future__ import annotations

import unittest

from . import context  # noqa: F401
from .context import forced_host

from whisper_desk import host, hotkey


class ParseTest(unittest.TestCase):
    def test_gtk_syntax(self):
        self.assertEqual(hotkey.parse_binding("<Super>j"), (["super"], "j"))

    def test_several_modifiers(self):
        self.assertEqual(hotkey.parse_binding("<Ctrl><Alt>j"), (["ctrl", "alt"], "j"))

    def test_plus_syntax(self):
        self.assertEqual(hotkey.parse_binding("ctrl+alt+j"), (["ctrl", "alt"], "j"))

    def test_synonyms(self):
        self.assertEqual(hotkey.parse_binding("cmd+j"), (["super"], "j"))
        self.assertEqual(hotkey.parse_binding("<Primary><Option>j"), (["ctrl", "alt"], "j"))

    def test_named_key(self):
        self.assertEqual(hotkey.parse_binding("<Ctrl><Alt>space"), (["ctrl", "alt"], "space"))

    def test_the_modifier_order_is_normalised(self):
        self.assertEqual(
            hotkey.parse_binding("<Alt><Ctrl>j"), hotkey.parse_binding("<Ctrl><Alt>j")
        )

    def test_gtk_round_trip(self):
        self.assertEqual(hotkey.format_gtk(*hotkey.parse_binding("<Ctrl><Alt>j")), "<Ctrl><Alt>j")


class LabelTest(unittest.TestCase):
    """The shortcut as the overlay tells it: the way it reads on the keyboard."""

    def test_gtk_syntax_becomes_keycaps(self):
        with forced_host(host.LINUX):
            self.assertEqual(hotkey.label("<Super>j"), "Super+J")
            self.assertEqual(hotkey.label("<Ctrl><Alt>space"), "Ctrl+Alt+Space")

    def test_a_mac_keyboard_says_cmd(self):
        with forced_host(host.MACOS):
            self.assertEqual(hotkey.label("<Super><Alt>d"), "Option+Cmd+D")


class DefaultBindingTest(unittest.TestCase):
    def test_wsl_avoids_the_windows_key(self):
        with forced_host(host.WSL):
            self.assertEqual(hotkey.default_binding(), "<Ctrl><Alt>j")

    def test_elsewhere_it_is_super(self):
        for name in (host.LINUX, host.MACOS):
            with forced_host(name):
                self.assertEqual(hotkey.default_binding(), "<Super>j")

    def test_auto_is_resolved(self):
        with forced_host(host.LINUX):
            self.assertEqual(hotkey.resolve_binding({"hotkey": {"binding": "auto"}}), "<Super>j")

    def test_an_explicit_shortcut_is_honoured(self):
        with forced_host(host.WSL):
            self.assertEqual(
                hotkey.resolve_binding({"hotkey": {"binding": "<Super><Shift>d"}}),
                "<Super><Shift>d",
            )


class WindowsFormatTest(unittest.TestCase):
    def test_accepted_combination(self):
        self.assertEqual(hotkey.format_windows(["ctrl", "alt"], "j"), "CTRL+ALT+J")

    def test_ctrl_shift_too(self):
        self.assertEqual(hotkey.format_windows(["ctrl", "shift"], "k"), "CTRL+SHIFT+K")

    def test_named_key(self):
        self.assertEqual(hotkey.format_windows(["ctrl", "alt"], "space"), "CTRL+ALT+SPACE")

    def test_the_windows_key_is_refused(self):
        with self.assertRaises(hotkey.UnsupportedDesktop) as raised:
            hotkey.format_windows(["super"], "j")
        self.assertIn("Ctrl+Alt", str(raised.exception))

    def test_ctrl_alone_is_refused(self):
        with self.assertRaises(hotkey.UnsupportedDesktop):
            hotkey.format_windows(["ctrl"], "j")

    def test_without_a_final_key(self):
        with self.assertRaises(hotkey.UnsupportedDesktop):
            hotkey.format_windows(["ctrl", "alt"], "")


class SkhdFormatTest(unittest.TestCase):
    def test_command_alone(self):
        self.assertEqual(hotkey.format_skhd(["super"], "j"), "cmd - j")

    def test_several_modifiers(self):
        self.assertEqual(hotkey.format_skhd(["ctrl", "alt"], "j"), "ctrl + alt - j")

    def test_without_a_modifier(self):
        self.assertEqual(hotkey.format_skhd([], "f13"), "f13")

    def test_without_a_final_key(self):
        with self.assertRaises(hotkey.UnsupportedDesktop):
            hotkey.format_skhd(["super"], "")


class BackendTest(unittest.TestCase):
    def test_each_host_has_its_plugin(self):
        expectations = {host.LINUX: "GNOME", host.WSL: "Windows", host.MACOS: "skhd"}
        for name, expected in expectations.items():
            with forced_host(name):
                self.assertEqual(hotkey.backend().name, expected)

    def test_outside_wsl_the_windows_plugin_refuses(self):
        with forced_host(host.LINUX):
            with self.assertRaises(hotkey.UnsupportedDesktop):
                hotkey.WindowsHotkey().check()


if __name__ == "__main__":
    unittest.main()
