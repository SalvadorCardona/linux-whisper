"""The shortcut as a real toggle: it starts, it stops, it cuts off, it gives up.

In a shared office, a neighbour's voice keeps the microphone busy: the silence
that ends a dictation never comes. Pressing the shortcut again must always
answer — first by stopping the listening, then by dropping what the model was
still chewing on, rather than typing the room's conversation at the cursor.

And when even that is not enough — a model stuck on a segment, a frozen window,
a microphone that no longer sends anything — one more press gives the dictation
up and hands back a daemon ready for the next one: no press may ever be lost.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
import unittest
from unittest import mock

from . import context  # noqa: F401  (adds src/ to the import path)

from whisper_desk import daemon

# Long enough for a thread to get going, short enough for a stuck test to fail.
TIMEOUT = 5.0

# Giving a dictation up is worth a warning in a journal, not in a test report.
logging.getLogger("whisper-desk.daemon").setLevel(logging.CRITICAL)

CONFIG = {
    # "clipboard" keeps the virtual keyboard out of the way: what is under test
    # is what reaches the output, not how it is typed.
    "output": {
        "mode": "clipboard", "history": False, "notify": False, "paste_shortcut": "ctrl+v",
    },
    "recording": {"device": "default", "start_timeout_seconds": 8},
    "overlay": {"enabled": False},
    "hotkey": {"binding": "<Super>j"},
    "model": {},
}


def wait_until(predicate, timeout: float = TIMEOUT) -> bool:
    """Waits for a state reached by another thread."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class FakeOverlay:
    """The listening window, reduced to what the dictation asks of it."""

    def __init__(self, _config=None, on_event=None):
        self.bars = 0
        self.on_event = on_event
        self.states: list[str] = []
        # The words of the states that carry some: (state, title, detail).
        self.messages: list[tuple[str, str, str]] = []
        self.texts: list[str] = []
        self.progress: list[float] = []
        self.hint = ""
        self.stopped = False

    alive = True

    def start(self, state: str = "listening", hint: str = "") -> None:
        self.hint = hint
        self.states.append(state)

    def set_state(self, state: str, title: str = "", detail: str = "") -> None:
        if self.stopped:
            return  # a closed window hears nothing more
        self.states.append(state)
        if title or detail:
            self.messages.append((state, title, detail))

    def set_text(self, text: str) -> None:
        self.texts.append(text)

    def set_progress(self, progress: float) -> None:
        self.progress.append(progress)

    def set_level(self, level: float, bands=()) -> None:
        pass

    def stop(self) -> None:
        self.stopped = True

    @property
    def final(self) -> tuple[str, str, str] | None:
        """The last message the window showed before closing."""
        return self.messages[-1] if self.messages else None


class FakeRecorder:
    """Hands over the sentences the test gives it, then waits for the shortcut."""

    def __init__(self, segments=(), tail: bytes = b""):
        self.segments = list(segments)
        self.tail = tail
        self.on_segment = None
        self.listening = threading.Event()
        self.stopped = threading.Event()
        self.aborted = threading.Event()
        self.peak = 1000.0
        self.reason = "stopped"
        self.backend = "fake"

    def stop(self) -> None:
        self.stopped.set()

    def abort(self) -> None:
        self.aborted.set()
        self.stopped.set()

    def record(self) -> bytes:
        for segment in self.segments:
            self.on_segment(segment)
        self.listening.set()
        self.stopped.wait(TIMEOUT)
        return self.tail


class DeafRecorder(FakeRecorder):
    """A capture gone quiet: it hears nothing but the killing of the tool."""

    def stop(self) -> None:
        pass


class FakeTranscriber:
    """One text per segment, held inside the model as long as the test wants."""

    def __init__(self):
        self.model_name, self.device, self.compute_type = "fake", "cpu", "int8"
        self.is_loaded = True
        self.entered = threading.Event()    # a segment has reached the model
        self.release = threading.Event()    # ... and may leave it
        self.release.set()
        self.seen: list[bytes] = []

    def transcribe(self, pcm: bytes, context: str = "") -> str:
        self.seen.append(pcm)
        self.entered.set()
        self.release.wait(TIMEOUT)
        return pcm.decode()


class ColdTranscriber(FakeTranscriber):
    """A model still to be downloaded: it arrives when the test says so."""

    def __init__(self, failure: Exception | None = None):
        super().__init__()
        self.is_loaded = False
        self.is_downloaded = False
        self.failure = failure
        self.arrived = threading.Event()

    def download_progress(self) -> float | None:
        return 0.5

    def load(self) -> None:
        self.arrived.wait(TIMEOUT)
        if self.failure is not None:
            raise self.failure
        self.is_loaded = True


class DictationCase(unittest.TestCase):
    """A daemon whose microphone, model and window belong to the test."""

    def setUp(self):
        self.recorder = FakeRecorder([b"a sentence"])
        self.transcriber = FakeTranscriber()
        self.delivered: list[str] = []
        self.delivery_works = True

    @contextlib.contextmanager
    def service(self):
        """A daemon whose microphone, model and window belong to the test."""
        def build_recorder(*_args, **kwargs):
            self.recorder.on_segment = kwargs["on_segment"]
            return self.recorder

        with mock.patch.object(daemon, "Recorder", build_recorder), \
                mock.patch.object(daemon, "OverlayProcess", FakeOverlay), \
                mock.patch.object(daemon, "Transcriber", lambda *a, **k: self.transcriber), \
                mock.patch.object(
                    daemon.output, "deliver",
                    lambda text, *a, **k: self.delivered.append(text) or self.delivery_works,
                ), \
                mock.patch.object(daemon.output, "notify", lambda *a, **k: None):
            service = daemon.Service(CONFIG)
            try:
                yield service
            finally:
                # Whatever the test did, no thread is left hanging.
                self.recorder.abort()
                self.transcriber.release.set()
                session = service.session
                if session is not None:
                    session.done.wait(TIMEOUT)

    def listening(self, service) -> None:
        """Starts a dictation and waits for the microphone to be open."""
        service.toggle()
        self.assertTrue(self.recorder.listening.wait(TIMEOUT))

    def transcribing(self, service) -> None:
        """Stops the listening while a sentence is still inside the model."""
        self.transcriber.release.clear()
        self.listening(service)
        self.assertTrue(self.transcriber.entered.wait(TIMEOUT))
        service.toggle()
        self.assertTrue(wait_until(lambda: service.state == "working"))


class ToggleTest(DictationCase):
    # -- the two first presses, unchanged -----------------------------------
    def test_the_first_press_starts_the_listening(self):
        with self.service() as service:
            self.assertEqual(service.toggle(), {"state": "recording"})
            self.assertEqual(service.state, "recording")

    def test_the_second_press_stops_the_listening(self):
        with self.service() as service:
            self.listening(service)
            self.assertEqual(service.toggle(), {"state": "working"})
            self.assertTrue(self.recorder.stopped.is_set())

    def test_an_ordinary_dictation_still_delivers_its_text(self):
        with self.service() as service:
            self.listening(service)
            service.toggle()
            self.assertTrue(wait_until(lambda: service.session is None))
        self.assertEqual(self.delivered, ["a sentence"])

    # -- the press that cuts the transcription off ---------------------------
    def test_a_press_while_transcribing_cuts_the_dictation_off(self):
        with self.service() as service:
            self.transcribing(service)
            self.assertEqual(service.toggle(), {"state": "cancelled"})
            self.transcriber.release.set()
            self.assertTrue(wait_until(lambda: service.session is None))
        self.assertEqual(self.delivered, [])

    def test_the_sentences_still_queued_are_dropped(self):
        self.recorder.segments[:] = [b"first", b"second", b"third"]
        with self.service() as service:
            self.transcribing(service)
            service.toggle()
            self.transcriber.release.set()
            self.assertTrue(wait_until(lambda: service.session is None))
        # The one already inside the model could not be interrupted; the
        # others never reached it, and none of them was typed.
        self.assertEqual(self.transcriber.seen, [b"first"])
        self.assertEqual(self.delivered, [])

    def test_the_window_closes_without_waiting_for_the_model(self):
        """The shortcut must be seen to answer, model busy or not."""
        with self.service() as service:
            self.transcribing(service)
            overlay = service.session.overlay
            service.toggle()
            self.assertTrue(overlay.stopped)

    def test_the_shortcut_is_never_ignored(self):
        with self.service() as service:
            self.transcribing(service)
            self.assertNotIn("ignored", service.toggle())

    def test_a_new_dictation_can_start_right_after(self):
        with self.service() as service:
            self.transcribing(service)
            service.toggle()
            self.transcriber.release.set()
            self.assertTrue(wait_until(lambda: service.state == "idle"))
            self.assertEqual(service.toggle(), {"state": "recording"})

    def test_a_capture_that_no_longer_answers_is_killed(self):
        """Asking nicely is not enough when the microphone has gone quiet."""
        self.recorder = DeafRecorder([b"a sentence"])
        with self.service() as service:
            self.listening(service)
            service.toggle()            # asks the listening to stop: unheard
            self.assertFalse(self.recorder.aborted.is_set())
            service.toggle()            # cuts off: the tool is killed
            self.assertTrue(self.recorder.aborted.is_set())
            self.assertTrue(wait_until(lambda: service.session is None))

    # -- the press that gives the dictation up -------------------------------
    def test_a_press_on_a_wedged_dictation_gives_it_up(self):
        """The model does not let go of its segment: nobody waits for it."""
        with self.service() as service:
            self.transcribing(service)
            service.toggle()
            self.assertEqual(service.toggle(), {"state": "idle", "given_up": True})
            self.assertIsNone(service.session)
            self.assertEqual(service.state, "idle")

    def test_the_shortcut_stops_waiting_for_the_model(self):
        with self.service() as service:
            self.transcribing(service)
            session = service.session
            service.toggle()
            service.toggle()
            self.assertTrue(session.done.is_set())
            # ... and the sentence is still inside the model.
            self.assertFalse(self.transcriber.release.is_set())

    def test_a_new_dictation_starts_over_a_wedged_one(self):
        with self.service() as service:
            self.transcribing(service)
            service.toggle()
            service.toggle()
            self.assertEqual(service.toggle(), {"state": "recording"})

    def test_a_dictation_given_up_does_not_take_the_next_one_down(self):
        """It may come back long afterwards, when someone else has the microphone."""
        with self.service() as service:
            self.transcribing(service)
            service.toggle()
            abandoned = service.session
            service.toggle()
            service.toggle()
            service.finish(abandoned)       # the abandoned dictation ends at last
            self.assertIsNotNone(service.session)
            self.assertNotEqual(service.state, "idle")

    def test_a_cut_dictation_says_so_on_the_window(self):
        with self.service() as service:
            self.transcribing(service)
            overlay = service.session.overlay
            service.toggle()
            self.assertEqual(overlay.final[0], "cancelled")

    def test_a_cut_dictation_says_nothing_about_the_microphone(self):
        """No sentence is not the same as no sound: no mute-microphone warning."""
        with self.service() as service, \
                mock.patch.object(daemon.output, "notify") as notify:
            self.transcribing(service)
            service.toggle()
            self.transcriber.release.set()
            self.assertTrue(wait_until(lambda: service.session is None))
            notify.assert_not_called()



class FeedbackTest(DictationCase):
    """The window says how every dictation ended: nobody is left guessing."""

    def finished(self, service) -> FakeOverlay:
        """Runs one dictation to its end and returns its window."""
        self.listening(service)
        overlay = service.session.overlay
        service.toggle()
        self.assertTrue(wait_until(lambda: service.session is None))
        return overlay

    def test_a_dictation_ends_on_the_text_it_inserted(self):
        with self.service() as service:
            overlay = self.finished(service)
        self.assertEqual(overlay.final, ("done", "a sentence", "Copied to the clipboard"))

    def test_each_sentence_is_shown_as_it_comes(self):
        self.recorder.segments[:] = [b"first", b"second"]
        with self.service() as service:
            overlay = self.finished(service)
        self.assertEqual(overlay.texts, ["first", "second"])

    def test_the_window_says_how_to_stop(self):
        with self.service() as service:
            self.listening(service)
            self.assertEqual(service.session.overlay.hint, "Super+J to finish")

    def test_nothing_said_is_an_error_worth_showing(self):
        self.recorder.segments[:] = []
        self.recorder.reason = "no-speech"
        with self.service() as service:
            overlay = self.finished(service)
        self.assertEqual(overlay.final, ("error", "Nothing heard", "Nothing was said within 8 s"))

    def test_a_mute_microphone_is_told_apart_from_silence(self):
        self.recorder.segments[:] = []
        self.recorder.peak = 0.0
        with self.service() as service:
            overlay = self.finished(service)
        self.assertEqual(overlay.final[:2], ("error", "The microphone is silent"))

    def test_a_failed_insertion_is_an_error(self):
        self.delivery_works = False
        with self.service() as service:
            overlay = self.finished(service)
        self.assertEqual(overlay.final[:2], ("error", "The text could not be inserted"))

    def test_without_a_window_the_error_becomes_a_notification(self):
        self.recorder.segments[:] = []
        with self.service() as service, \
                mock.patch.object(daemon.output, "notify") as notify, \
                mock.patch.object(FakeOverlay, "alive", False):
            self.finished(service)
        notify.assert_called_once()
        self.assertIn("nothing heard", notify.call_args.args[0])

    def test_the_window_can_cut_the_dictation_off(self):
        """Esc or a click on the overlay: the same as the shortcut's second press."""
        self.transcriber.release.clear()
        with self.service() as service:
            self.listening(service)
            overlay = service.session.overlay
            self.assertTrue(self.transcriber.entered.wait(TIMEOUT))
            overlay.on_event("cancel")
            self.assertTrue(wait_until(lambda: self.recorder.aborted.is_set()))
            self.transcriber.release.set()
            self.assertTrue(wait_until(lambda: service.session is None))
        self.assertEqual(overlay.final[0], "cancelled")
        self.assertEqual(self.delivered, [])


class HistoryTest(DictationCase):
    """One entry per dictation, whatever the number of sentences it took."""

    def setUp(self):
        super().setUp()
        self.recorded: list[tuple[str, dict]] = []
        patcher = mock.patch.object(
            daemon.history, "append",
            lambda text, **kwargs: self.recorded.append((text, kwargs)),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(
            CONFIG, {"output": {**CONFIG["output"], "history": True, "history_days": 30}}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def dictate(self) -> None:
        with self.service() as service:
            self.listening(service)
            service.toggle()
            self.assertTrue(wait_until(lambda: service.session is None))

    def test_the_sentences_make_one_entry(self):
        self.recorder.segments[:] = [b"first", b"second"]
        self.dictate()
        self.assertEqual(len(self.recorded), 1)
        text, details = self.recorded[0]
        self.assertEqual(text, "first second")
        self.assertEqual(details["model"], "fake")
        self.assertEqual(details["keep_days"], 30)
        self.assertGreaterEqual(details["duration"], 0.0)

    def test_nothing_said_leaves_no_entry(self):
        self.recorder.segments[:] = []
        self.dictate()
        self.assertEqual(self.recorded, [])


class InsertTest(DictationCase):
    """A dictation brought back from the history is typed by the daemon."""

    def test_the_daemon_types_the_text_again(self):
        written: list[str] = []

        class Writer:
            def __init__(self, *_args, **_kwargs):
                pass

            def prepare(self):
                pass

            def write(self, text):
                written.append(text)
                return True

            def close(self):
                pass

        with self.service() as service, \
                mock.patch.object(daemon.output, "CursorWriter", Writer), \
                mock.patch.object(daemon, "INSERT_DELAY_SECONDS", 0):
            self.assertEqual(service.insert("hello again"), {"inserting": True})
            self.assertTrue(wait_until(lambda: written == ["hello again"]))

    def test_not_over_a_dictation(self):
        with self.service() as service:
            self.listening(service)
            self.assertIn("error", service.insert("hello again"))

    def test_nothing_to_insert(self):
        with self.service() as service:
            self.assertIn("error", service.insert("  "))


class LoadingTest(DictationCase):
    """A model still on its way is shown, and the microphone waits for it."""

    def test_the_microphone_opens_once_the_model_is_there(self):
        self.transcriber = ColdTranscriber()
        with self.service() as service:
            service.toggle()
            overlay = service.session.overlay
            self.assertTrue(wait_until(lambda: overlay.progress))
            self.assertEqual(service.state, "loading")
            self.assertFalse(self.recorder.listening.is_set())
            self.transcriber.arrived.set()
            self.assertTrue(self.recorder.listening.wait(TIMEOUT))
            self.assertEqual(overlay.states[:2], ["loading", "loading"])
            self.assertIn("listening", overlay.states)
            self.assertEqual(overlay.messages[0], ("loading", "Downloading the fake model", ""))

    def test_the_shortcut_cancels_the_wait(self):
        self.transcriber = ColdTranscriber()
        with self.service() as service:
            service.toggle()
            overlay = service.session.overlay
            self.assertTrue(wait_until(lambda: service.state == "loading"))
            self.assertEqual(service.toggle(), {"state": "cancelled"})
            self.assertTrue(wait_until(lambda: service.session is None))
            self.transcriber.arrived.set()
        self.assertFalse(self.recorder.listening.is_set())
        self.assertEqual(overlay.final[0], "cancelled")

    def test_a_model_that_cannot_load_is_an_error(self):
        self.transcriber = ColdTranscriber(failure=RuntimeError("no space left on device"))
        self.transcriber.arrived.set()
        logging.getLogger("whisper-desk.daemon").disabled = True
        try:
            with self.service() as service:
                service.toggle()
                overlay = service.session.overlay
                self.assertTrue(wait_until(lambda: service.session is None))
        finally:
            logging.getLogger("whisper-desk.daemon").disabled = False
        self.assertEqual(
            overlay.final, ("error", "The model could not be loaded", "no space left on device")
        )


if __name__ == "__main__":
    unittest.main()
