"""Driving the listening window: the pipe may break, the closing may not."""

from __future__ import annotations

import io
import threading
import time
import unittest

from . import context  # noqa: F401

from whisper_desk import overlay_protocol as protocol
from whisper_desk.overlay_proc import OverlayProcess

CONFIG = {
    "overlay": {
        "enabled": True,
        "accent": "#e46212",
        "width": 232,
        "height": 64,
        "bars": 15,
        "position": "bottom-center",
        "margin": 96,
    }
}


class FakeStdin:
    def __init__(self, broken: bool = False):
        self.broken = broken
        self.lines: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> int:
        if self.broken:
            raise BrokenPipeError("pipe closed")
        self.lines.append(data)
        return len(data)

    def flush(self) -> None:
        if self.broken:
            raise BrokenPipeError("pipe closed")

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(self, stdin: FakeStdin, returncode=None, stdout: bytes = b""):
        self.stdin = stdin
        self.stdout = io.BytesIO(stdout)
        self.returncode = returncode
        self.terminated = False
        self.killed = False
        # A window lingering on its final state: wait() holds until released.
        self.exited = threading.Event()
        self.exited.set()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None) -> int:
        if not self.exited.wait(timeout):
            import subprocess
            raise subprocess.TimeoutExpired("overlay", timeout)
        return 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def attached(stdin: FakeStdin) -> tuple[OverlayProcess, FakeProcess]:
    overlay = OverlayProcess(CONFIG)
    process = FakeProcess(stdin)
    overlay._process = process
    return overlay, process


class SendTest(unittest.TestCase):
    def test_one_command_per_line(self):
        stdin = FakeStdin()
        overlay, _ = attached(stdin)
        overlay.set_state("working")
        overlay.set_level(0.5)
        self.assertEqual(stdin.lines, [b"state working\n", b"level 0.500\n"])

    def test_the_equalizer_travels_on_the_same_line_as_the_level(self):
        """One write per measurement: two lines would interleave on the pipe."""
        stdin = FakeStdin()
        overlay, _ = attached(stdin)
        overlay.set_level(0.5, [0.25, 0.75])
        self.assertEqual(stdin.lines, [b"level 0.500 0.250 0.750\n"])

    def test_the_number_of_bars_comes_from_the_configuration(self):
        self.assertEqual(OverlayProcess(CONFIG).bars, 15)

    def test_overlay_disabled_means_nobody_watches_the_bands(self):
        config = {"overlay": {**CONFIG["overlay"], "enabled": False}}
        self.assertEqual(OverlayProcess(config).bars, 0)

    def test_the_level_is_capped_to_three_decimals(self):
        stdin = FakeStdin()
        overlay, _ = attached(stdin)
        overlay.set_level(1 / 3)
        self.assertEqual(stdin.lines, [b"level 0.333\n"])

    def test_a_final_state_carries_its_words(self):
        stdin = FakeStdin()
        overlay, _ = attached(stdin)
        overlay.set_state("error", "Nothing heard", "Nothing was said within 8 s")
        command, args = protocol.parse(stdin.lines[0].decode())
        self.assertEqual(command, "state")
        self.assertEqual(args, ("error", "Nothing heard", "Nothing was said within 8 s"))

    def test_a_bare_state_stays_a_bare_line(self):
        """What an older window reads must not change for the old states."""
        stdin = FakeStdin()
        overlay, _ = attached(stdin)
        overlay.set_state("listening")
        overlay.set_state("done", "text only")
        self.assertEqual(stdin.lines[0], b"state listening\n")
        self.assertEqual(protocol.parse(stdin.lines[1].decode()), ("state", ("done", "text only", "")))

    def test_the_live_text_and_the_hint_travel_as_base64(self):
        stdin = FakeStdin()
        overlay, _ = attached(stdin)
        overlay.set_text("a sentence, with spaces")
        overlay.set_hint("Super+J to finish")
        self.assertEqual(
            [protocol.parse(line.decode()) for line in stdin.lines],
            [("text", ("a sentence, with spaces",)), ("hint", ("Super+J to finish",))],
        )

    def test_the_progress_is_bounded(self):
        stdin = FakeStdin()
        overlay, _ = attached(stdin)
        overlay.set_progress(0.4219)
        overlay.set_progress(1.7)
        self.assertEqual(stdin.lines, [b"progress 0.422\n", b"progress 1.000\n"])

    def test_the_copied_text_travels_as_base64_on_one_line(self):
        stdin = FakeStdin()
        overlay, _ = attached(stdin)
        self.assertTrue(overlay.copy("sentence with spaces\nand a line break"))
        self.assertEqual(len(stdin.lines), 1)
        self.assertTrue(stdin.lines[0].startswith(b"copy "))
        self.assertEqual(stdin.lines[0].count(b"\n"), 1)


class ProtocolTest(unittest.TestCase):
    """What the window makes of a line: a malformed one is dropped, never guessed."""

    def test_every_state_is_understood(self):
        for state in protocol.STATES:
            self.assertEqual(protocol.parse(f"state {state}"), ("state", (state, "", "")))

    def test_an_unknown_state_is_dropped(self):
        self.assertIsNone(protocol.parse("state dancing"))

    def test_the_level_and_its_bands(self):
        self.assertEqual(protocol.parse("level 0.5 0.25 0.75"), ("level", (0.5, [0.25, 0.75])))

    def test_values_are_brought_back_into_range(self):
        self.assertEqual(protocol.parse("level 3 -1"), ("level", (1.0, [0.0])))
        self.assertEqual(protocol.parse("progress -0.5"), ("progress", (0.0,)))

    def test_garbage_is_dropped(self):
        for line in ("", "   ", "level", "level abc", "progress nan", "text", "text !!notbase64",
                     "state error !!", "copy", "wave 1", "state"):
            self.assertIsNone(protocol.parse(line), line)

    def test_words_survive_the_trip(self):
        text = "Déjà-vu — «quote» \t with\nbreaks"
        self.assertEqual(protocol.parse(f"text {protocol.encode(text)}"), ("text", (text,)))

    def test_bare_commands(self):
        for command in ("quit", "saveclip", "restoreclip"):
            self.assertEqual(protocol.parse(command), (command, ()))


class EventTest(unittest.TestCase):
    """The window answers on its stdout: Esc or a click cancels the dictation."""

    def test_a_cancel_from_the_window_reaches_the_dictation(self):
        events: list[str] = []
        overlay = OverlayProcess(CONFIG, on_event=events.append)
        overlay._read_events(FakeProcess(FakeStdin(), stdout=b"cancel\n"))
        self.assertEqual(events, ["cancel"])

    def test_unknown_words_from_the_window_are_ignored(self):
        events: list[str] = []
        overlay = OverlayProcess(CONFIG, on_event=events.append)
        overlay._read_events(
            FakeProcess(FakeStdin(), stdout=b"Gtk-WARNING something\n\ncancel\n")
        )
        self.assertEqual(events, ["cancel"])

    def test_nobody_listening_is_not_an_error(self):
        OverlayProcess(CONFIG)._read_events(FakeProcess(FakeStdin(), stdout=b"cancel\n"))


class BrokenPipeTest(unittest.TestCase):
    """A broken pipe must not leave the window orphaned on screen."""

    def test_a_failed_write_is_not_fatal(self):
        overlay, _ = attached(FakeStdin(broken=True))
        overlay.set_level(0.5)  # does not raise
        self.assertFalse(overlay.alive)

    def test_the_window_is_closed_anyway(self):
        overlay, process = attached(FakeStdin(broken=True))
        overlay.set_level(0.5)
        overlay.stop()
        self.assertTrue(process.terminated or process.killed)

    def test_nothing_more_is_sent_after_the_break(self):
        stdin = FakeStdin()
        overlay, _ = attached(stdin)
        overlay.set_level(0.1)
        stdin.broken = True
        overlay.set_level(0.2)
        stdin.broken = False
        overlay.set_level(0.3)
        self.assertEqual(stdin.lines, [b"level 0.100\n"])

    def test_the_copy_fails_outright(self):
        overlay, _ = attached(FakeStdin(broken=True))
        overlay.set_level(0.5)
        self.assertFalse(overlay.copy("text"))
        self.assertFalse(overlay.save_clipboard())
        self.assertFalse(overlay.restore_clipboard())


class StopTest(unittest.TestCase):
    def test_stopping_asks_politely_for_the_closing(self):
        stdin = FakeStdin()
        overlay, _ = attached(stdin)
        overlay.stop()
        self.assertEqual(stdin.lines, [b"quit\n"])
        self.assertTrue(stdin.closed)

    def test_stopping_is_idempotent(self):
        overlay, _ = attached(FakeStdin())
        overlay.stop()
        overlay.stop()  # does not raise
        self.assertFalse(overlay.alive)

    def test_stopping_does_not_wait_for_the_final_state_to_be_read(self):
        """The window lingers on "done" for a second: the daemon is free at once."""
        overlay, process = attached(FakeStdin())
        process.exited.clear()
        started = time.monotonic()
        overlay.stop()
        self.assertLess(time.monotonic() - started, 0.5)
        process.exited.set()

    def test_a_new_window_takes_the_place_of_a_lingering_one(self):
        overlay, process = attached(FakeStdin())
        process.exited.clear()
        overlay.stop()
        OverlayProcess._dismiss_lingering()
        self.assertTrue(process.terminated)
        process.exited.set()

    def test_a_dead_process_is_no_longer_alive(self):
        overlay = OverlayProcess(CONFIG)
        overlay._process = FakeProcess(FakeStdin(), returncode=0)
        self.assertFalse(overlay.alive)


class ConcurrencyTest(unittest.TestCase):
    """Levels come from the microphone thread, states from the dictation thread."""

    def test_the_lines_do_not_interleave(self):
        stdin = FakeStdin()
        overlay, _ = attached(stdin)
        start = threading.Barrier(4)

        def spam(send):
            start.wait()
            for _ in range(200):
                send()

        threads = [
            threading.Thread(target=spam, args=(lambda: overlay.set_level(0.25),)),
            threading.Thread(target=spam, args=(lambda: overlay.set_state("listening"),)),
            threading.Thread(target=spam, args=(lambda: overlay.set_state("working"),)),
        ]
        for thread in threads:
            thread.start()
        start.wait()
        for thread in threads:
            thread.join()

        self.assertEqual(len(stdin.lines), 600)
        self.assertEqual(
            set(stdin.lines),
            {b"level 0.250\n", b"state listening\n", b"state working\n"},
        )


if __name__ == "__main__":
    unittest.main()
