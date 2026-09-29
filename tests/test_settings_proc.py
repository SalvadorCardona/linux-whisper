"""The settings window's requests: what is offered, what is written."""

from __future__ import annotations

import copy
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from . import context  # noqa: F401
from .context import forced_host

from whisper_desk import capture, host, hotkey
from whisper_desk import config as config_module
from whisper_desk import settings_proc
from whisper_desk.settings_proc import SettingsController


class ControllerCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "config.toml"
        self.path.write_text(
            "# my own comment\n[model]\nlanguage = \"fr\"   # the language I speak\n",
            encoding="utf-8",
        )
        patcher = mock.patch.object(config_module, "CONFIG_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in (("has_nvidia_gpu", lambda: False),
                            ("is_downloaded", lambda name: name == "small")):
            patcher = mock.patch.object(settings_proc, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(capture, "devices", lambda backend="auto": [capture.DEFAULT_DEVICE])
        patcher.start()
        self.addCleanup(patcher.stop)

        self.sent: list[str] = []
        self.reply = {"reloaded": True}
        self.installed: list[str] = []
        self.controller = SettingsController(
            config_module.load(),
            "/bin/whisper-desk toggle",
            send=lambda command, **kwargs: self.sent.append(command) or self.reply,
            install_hotkey=lambda config, command: self.installed.append(command)
            or hotkey.resolve_binding(config),
        )

    def save(self, **values):
        return self.controller.save({**self.controller.values(), **values})


class SnapshotTest(ControllerCase):
    def test_the_window_gets_the_values_and_the_choices(self):
        snapshot = self.controller.snapshot()
        self.assertEqual(snapshot["values"]["language"], "fr")
        self.assertEqual(snapshot["values"]["accent"], "#e46212")
        self.assertIn(["en", "English"], snapshot["choices"]["language"])
        self.assertEqual(snapshot["choices"]["device"], [["default", "Default microphone"]])

    def test_each_model_says_its_size_its_speed_and_if_it_is_here(self):
        labels = dict(self.controller.snapshot()["choices"]["model"])
        self.assertEqual(labels["auto"], "Automatic — small on this machine")
        self.assertEqual(labels["small"], "small — 484 MB, fast on a CPU, good · downloaded")
        self.assertEqual(labels["large-v3"], "large-v3 — 3.1 GB, slowest, the most accurate")

    def test_a_value_outside_the_list_is_still_offered(self):
        self.controller.config["recording"]["device"] = "hw:CARD=USB"
        self.assertIn(["hw:CARD=USB", "hw:CARD=USB"], self.controller.snapshot()["choices"]["device"])


class SaveTest(ControllerCase):
    def test_only_what_changed_is_written_and_the_comments_stay(self):
        reply = self.save(language="en", vocabulary="Animalink, Kubernetes")
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# my own comment", text)
        self.assertIn('language = "en"   # the language I speak', text)
        self.assertEqual(tomllib.loads(text)["model"]["vocabulary"], "Animalink, Kubernetes")
        self.assertNotIn("[overlay]", text)
        self.assertTrue(reply["saved"])
        self.assertEqual(reply["status"], "Saved — applied")
        self.assertEqual(self.sent, ["reload"])

    def test_nothing_changed_writes_nothing(self):
        before = self.path.read_text(encoding="utf-8")
        self.assertEqual(self.save()["status"], "Nothing to change")
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        self.assertEqual(self.sent, [])

    def test_a_busy_daemon_applies_it_after_the_dictation(self):
        self.reply = {"error": "busy (recording)"}
        self.assertIn("after the current dictation", self.save(language="en")["status"])

    def test_a_new_shortcut_is_installed(self):
        with forced_host(host.LINUX):
            reply = self.save(binding="<Ctrl><Alt>d")
        self.assertEqual(self.installed, ["/bin/whisper-desk toggle"])
        self.assertIn("shortcut Ctrl+Alt+D installed", reply["status"])

    def test_the_shortcut_is_left_alone_when_it_does_not_change(self):
        self.save(language="en")
        self.assertEqual(self.installed, [])

    def test_a_shortcut_the_desktop_refuses_is_said(self):
        def refuse(config, command):
            raise hotkey.UnsupportedDesktop("unsupported desktop (KDE)")
        self.controller.install_hotkey = refuse
        reply = self.save(binding="<Ctrl><Alt>d")
        self.assertIn("shortcut not installed (unsupported desktop (KDE))", reply["status"])
        self.assertEqual(config_module.load(self.path)["hotkey"]["binding"], "<Ctrl><Alt>d")

    def test_a_model_to_download_is_announced(self):
        self.assertIn("medium downloads in the background (1.5 GB)",
                      self.save(model="medium")["status"])

    def test_a_malformed_colour_is_refused(self):
        reply = self.save(accent="orange")
        self.assertFalse(reply["saved"])
        self.assertEqual(config_module.load(self.path)["overlay"]["accent"], "#e46212")

    def test_the_overlay_switch_is_a_boolean(self):
        self.save(overlay=False)
        self.assertIs(config_module.load(self.path)["overlay"]["enabled"], False)


class ShortcutTest(ControllerCase):
    def test_the_gtk_accelerator_becomes_the_configuration_syntax(self):
        with forced_host(host.LINUX):
            reply = self.controller.shortcut("<Primary><Alt>d")
        self.assertEqual(reply["hotkey"], {"binding": "<Ctrl><Alt>d", "label": "Ctrl+Alt+D"})

    def test_default_is_auto(self):
        with forced_host(host.WSL):
            self.assertEqual(self.controller.shortcut("auto")["hotkey"],
                             {"binding": "auto", "label": "Ctrl+Alt+J"})

    def test_a_bare_letter_is_not_a_shortcut(self):
        self.assertIn("status", self.controller.shortcut("j"))

    def test_a_function_key_alone_is(self):
        self.assertEqual(self.controller.shortcut("F9")["hotkey"]["binding"], "f9")


class MeterTest(unittest.TestCase):
    """The test gauge listens through the same tool as a dictation."""

    def fake_capture(self, samples: bytes):
        script = f"import sys; sys.stdout.buffer.write({samples!r})"
        return mock.patch.object(
            settings_proc.capture, "build",
            lambda device, rate, channels, backend="auto": capture.Capture(
                "fake", [sys.executable, "-c", script]
            ),
        )

    def run_meter(self, samples: bytes) -> list[dict]:
        messages: list[dict] = []
        done = threading.Event()

        def emit(message):
            messages.append(message)
            if "meter" in message:
                done.set()

        with self.fake_capture(samples):
            settings_proc.LevelMeter(emit).start("default", "auto")
            self.assertTrue(done.wait(5))
        return messages

    def test_a_voice_moves_the_gauge(self):
        loud = (b"\x10\x27\xf0\xd8") * 8000       # ±10000, one second
        messages = self.run_meter(loud)
        self.assertTrue(any(message.get("level", 0) > 0.5 for message in messages))
        self.assertEqual(messages[-1]["meter"], "Test over")

    def test_a_mute_microphone_is_named(self):
        messages = self.run_meter(b"\x00\x00" * 16000)
        self.assertIn("mute", messages[-1]["meter"])

    def test_a_missing_tool_is_said(self):
        messages: list[dict] = []
        with mock.patch.object(settings_proc.capture, "build",
                               mock.Mock(side_effect=capture.CaptureUnavailable("no arecord"))):
            settings_proc.LevelMeter(messages.append).start("default", "auto")
        self.assertEqual(messages, [{"level": 0.0, "meter": "Cannot listen: no arecord"}])

    def test_stopping_says_nothing(self):
        messages: list[dict] = []
        endless = "import sys, time\nwhile True:\n    sys.stdout.buffer.write(b'\\0' * 3200); sys.stdout.flush(); time.sleep(0.05)"
        with mock.patch.object(
            settings_proc.capture, "build",
            lambda device, rate, channels, backend="auto": capture.Capture(
                "fake", [sys.executable, "-c", endless]
            ),
        ):
            meter = settings_proc.LevelMeter(messages.append)
            meter.start("default", "auto")
            time.sleep(0.3)
            meter.stop()
            time.sleep(0.2)
        self.assertFalse(any("meter" in message for message in messages))


class DevicesTest(unittest.TestCase):
    """The microphones offered are the capture tool's own."""

    ARECORD = """\
null
    Discard all samples (playback) or generate zero samples (capture)
pipewire
    PipeWire Sound Server
default
    Default ALSA Output (currently PipeWire Media Server)
hw:CARD=Dock,DEV=0
    WD19 Dock, USB Audio
    Direct hardware device without any conversions
plughw:CARD=Dock,DEV=0
    WD19 Dock, USB Audio
    Hardware device with all software conversions
sysdefault:CARD=Dock
    WD19 Dock, USB Audio
    Default Audio Device
dsnoop:CARD=Dock,DEV=0
    WD19 Dock, USB Audio
    Direct sample snooping device
"""

    PACTL = """\
Source #52
\tState: SUSPENDED
\tName: alsa_output.pci-0000_00_1f.3.analog-stereo.monitor
\tDescription: Monitor of Built-in Audio
Source #53
\tName: alsa_input.usb-Blue_Yeti-00.analog-stereo
\tDescription: Yeti Stereo Microphone
"""

    AVFOUNDATION = """\
[AVFoundation indev @ 0x7f] AVFoundation video devices:
[AVFoundation indev @ 0x7f] [0] FaceTime HD Camera
[AVFoundation indev @ 0x7f] AVFoundation audio devices:
[AVFoundation indev @ 0x7f] [0] MacBook Pro Microphone
[AVFoundation indev @ 0x7f] [1] AirPods
"""

    def test_alsa_without_the_plumbing(self):
        self.assertEqual(capture.parse_arecord(self.ARECORD), [
            ("pipewire", "PipeWire Sound Server"),
            ("plughw:CARD=Dock,DEV=0", "WD19 Dock, USB Audio — device 0"),
            ("sysdefault:CARD=Dock", "WD19 Dock, USB Audio"),
        ])

    def test_pulseaudio_without_the_speaker_monitors(self):
        self.assertEqual(capture.parse_pactl(self.PACTL),
                         [("alsa_input.usb-Blue_Yeti-00.analog-stereo", "Yeti Stereo Microphone")])

    def test_avfoundation_audio_only(self):
        self.assertEqual(capture.parse_avfoundation(self.AVFOUNDATION),
                         [(":0", "MacBook Pro Microphone"), (":1", "AirPods")])

    def test_default_comes_first_and_only_once(self):
        with forced_host(host.LINUX), \
                mock.patch.object(capture, "choose", lambda backend="auto": "arecord"), \
                mock.patch.object(capture, "_listing", lambda command, stderr=False: self.ARECORD):
            found = capture.devices()
        self.assertEqual(found[0], capture.DEFAULT_DEVICE)
        self.assertEqual([name for name, _label in found].count("default"), 1)

    def test_no_tool_still_offers_the_default(self):
        with mock.patch.object(capture, "choose", mock.Mock(side_effect=capture.CaptureUnavailable("x"))):
            self.assertEqual(capture.devices(), [capture.DEFAULT_DEVICE])


if __name__ == "__main__":
    unittest.main()
