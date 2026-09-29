"""Sliding prompt, vocabulary, and sorting out Whisper's hallucinations."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from . import context  # noqa: F401  (adds src/ to the import path)

from whisper_desk import transcriber
from whisper_desk.transcriber import CONTEXT_CHARS, Transcriber, is_filler

try:
    import numpy
except ImportError:  # CI only installs the standard library
    numpy = None

# transcribe() converts the PCM into floats with numpy before calling the
# model: without it, only the pure functions remain testable.
requires_numpy = unittest.skipUnless(numpy is not None, "numpy missing")


def settings(**overrides):
    model = {
        "name": "small",
        "device": "cpu",
        "compute_type": "int8",
        "language": "fr",
        "beam_size": 5,
        "initial_prompt": "",
        "vocabulary": "",
        "context": True,
        "preload": False,
    }
    model.update(overrides)
    return {"model": model}


class Segment:
    """What faster-whisper returns, reduced to what the filter looks at."""

    def __init__(self, text: str, no_speech_prob: float = 0.0):
        self.text = text
        self.no_speech_prob = no_speech_prob


class FillerTest(unittest.TestCase):
    # The sample sentences stay in French: they are the credit lines Whisper
    # itself emits when it hears nothing, and the filter matches them verbatim.

    def test_a_subtitle_credit_never_comes_from_the_microphone(self):
        self.assertTrue(is_filler("Sous-titres réalisés par la communauté d'Amara.org", 0.0))
        self.assertTrue(is_filler("Sous-titrage Société Radio-Canada", 0.1))

    def test_a_thank_you_over_silence_is_a_hallucination(self):
        self.assertTrue(is_filler("Merci.", 0.9))

    def test_a_thank_you_actually_spoken_is_kept(self):
        """The same word, but the model did hear somebody speak."""
        self.assertFalse(is_filler("Merci.", 0.1))

    def test_punctuation_and_case_do_not_save_the_filler(self):
        self.assertTrue(is_filler("  « MERCI ! »  ", 0.8))

    def test_a_real_sentence_stays_even_on_a_high_no_speech(self):
        self.assertFalse(is_filler("we should add some pictures of blocks", 0.99))


class PromptTest(unittest.TestCase):
    def test_with_nothing_the_prompt_stays_empty(self):
        self.assertIsNone(Transcriber(settings()).prompt())

    def test_the_user_vocabulary_opens_the_prompt(self):
        transcriber = Transcriber(settings(initial_prompt="Technical dictation."))
        self.assertEqual(transcriber.prompt(), "Technical dictation.")

    def test_the_context_extends_the_priming_sentence(self):
        transcriber = Transcriber(settings(initial_prompt="Technical dictation."))
        self.assertEqual(
            transcriber.prompt("we should add"),
            "Technical dictation. we should add",
        )

    def test_the_context_is_capped_to_the_last_characters(self):
        """The model must keep the thread, not reread the whole dictation."""
        transcriber = Transcriber(settings())
        prompt = transcriber.prompt("word " * 400)
        self.assertLessEqual(len(prompt), CONTEXT_CHARS)

    def test_with_context_disabled_the_prompt_ignores_what_came_before(self):
        transcriber = Transcriber(settings(context=False))
        self.assertIsNone(transcriber.prompt("we should add"))


class TranscribeTest(unittest.TestCase):
    def transcribe(self, segments, pcm=b"\x01\x02" * 800, context="", **overrides):
        transcriber = Transcriber(settings(**overrides))
        model = mock.Mock()
        model.transcribe.return_value = (iter(segments), None)
        transcriber._model = model
        text = transcriber.transcribe(pcm, context)
        return text, model.transcribe.call_args.kwargs

    @requires_numpy
    def test_the_sentences_are_glued_back_together(self):
        text, _ = self.transcribe([Segment("First sentence."), Segment("And the rest.")])
        self.assertEqual(text, "First sentence. And the rest.")

    @requires_numpy
    def test_the_hallucination_is_dropped_from_the_returned_text(self):
        text, _ = self.transcribe([
            Segment("we should add"),
            Segment("Merci.", no_speech_prob=0.95),
        ])
        self.assertEqual(text, "we should add")

    @requires_numpy
    def test_the_vocabulary_goes_out_as_hotwords(self):
        _, kwargs = self.transcribe([Segment("text")], vocabulary="OpenRouter, GitHub repos")
        self.assertEqual(kwargs["hotwords"], "OpenRouter, GitHub repos")

    @requires_numpy
    def test_without_a_vocabulary_no_hotword_is_imposed(self):
        _, kwargs = self.transcribe([Segment("text")])
        self.assertIsNone(kwargs["hotwords"])

    @requires_numpy
    def test_the_context_does_reach_the_model(self):
        _, kwargs = self.transcribe([Segment("some pictures")], context="we should add")
        self.assertEqual(kwargs["initial_prompt"], "we should add")

    def test_without_sound_the_model_is_left_alone(self):
        transcriber = Transcriber(settings())
        transcriber._model = mock.Mock()
        self.assertEqual(transcriber.transcribe(b""), "")
        transcriber._model.transcribe.assert_not_called()


class DownloadTest(unittest.TestCase):
    """The first dictation waits for the model: the gauge says how long."""

    def setUp(self):
        self.cache = tempfile.TemporaryDirectory()
        self.addCleanup(self.cache.cleanup)
        patcher = mock.patch.dict(os.environ, {"HF_HUB_CACHE": self.cache.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.model = Path(self.cache.name) / "models--Systran--faster-whisper-small"

    def blob(self, name: str, size: int) -> None:
        (self.model / "blobs").mkdir(parents=True, exist_ok=True)
        (self.model / "blobs" / name).write_bytes(b"\0" * size)

    def test_nothing_on_disk_means_to_be_downloaded(self):
        self.assertFalse(transcriber.is_downloaded("small"))
        self.assertEqual(transcriber.download_progress("small"), 0.0)

    def test_the_gauge_follows_the_bytes_received(self):
        self.blob("model.bin.incomplete", 1_000_000)
        with mock.patch.dict(transcriber.MODEL_BYTES, {"small": 4_000_000}):
            self.assertAlmostEqual(transcriber.download_progress("small"), 0.25)

    def test_the_gauge_never_claims_to_be_done_early(self):
        self.blob("model.bin", 9_000_000)
        with mock.patch.dict(transcriber.MODEL_BYTES, {"small": 4_000_000}):
            self.assertLess(transcriber.download_progress("small"), 1.0)

    def test_a_snapshot_with_its_weights_is_downloaded(self):
        snapshot = self.model / "snapshots" / "abc123"
        snapshot.mkdir(parents=True)
        (snapshot / "model.bin").write_bytes(b"")
        self.assertTrue(transcriber.is_downloaded("small"))

    def test_the_english_variants_have_their_own_repository(self):
        self.assertTrue(
            str(transcriber._model_dir("small.en")).endswith("models--Systran--faster-whisper-small.en")
        )

    def test_a_local_model_needs_no_download(self):
        self.assertTrue(transcriber.is_downloaded(self.cache.name))

    def test_an_unknown_model_has_no_gauge(self):
        self.assertIsNone(transcriber.download_progress("my-own-model"))


if __name__ == "__main__":
    unittest.main()
