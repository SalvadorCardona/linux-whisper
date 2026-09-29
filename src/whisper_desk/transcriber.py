"""Offline transcription through faster-whisper (CTranslate2)."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("whisper-desk.transcriber")

GPU_MODEL = "large-v3-turbo"
CPU_MODEL = "small"
# Context handed back to the model from one sentence to the next: enough to keep
# the thread of a dictation split as you go, too short for it to start inventing.
CONTEXT_CHARS = 220
# Above this, the model itself judges that it heard nothing spoken.
NO_SPEECH_LIMIT = 0.6
# Whisper learned on subtitles: when it hears nothing, it spits their credits
# back out. The sentence alone is not damning — "thank you" does get dictated —
# but paired with a high no_speech_prob, it is filler. The list stays in the
# languages Whisper actually emits these in.
FILLERS = frozenset({
    "sous-titres réalisés par la communauté d'amara.org",
    "sous-titres réalisés par l'amara.org",
    "sous-titrage société radio-canada",
    "sous-titrage st' 501",
    "merci d'avoir regardé cette vidéo",
    "merci d'avoir regardé la vidéo",
    "abonnez-vous",
    "n'oubliez pas de vous abonner",
    "à la prochaine",
    "merci",
    "merci beaucoup",
    "au revoir",
    "thank you",
    "thanks for watching",
    "you",
})


# Hugging Face repositories of the models faster-whisper knows by name, for
# when faster-whisper itself cannot be asked (it is imported lazily).
REPOSITORIES = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v1": "Systran/faster-whisper-large-v1",
    "large-v2": "Systran/faster-whisper-large-v2",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "distil-large-v3": "Systran/faster-distil-whisper-large-v3",
}
# Download size of each model, in bytes. The hub announces no total before the
# download starts: an approximate gauge beats a spinner that says nothing.
MODEL_BYTES = {
    "tiny": 75e6,
    "base": 145e6,
    "small": 484e6,
    "medium": 1.53e9,
    "large-v1": 3.09e9,
    "large-v2": 3.09e9,
    "large-v3": 3.09e9,
    "large": 3.09e9,
    "large-v3-turbo": 1.62e9,
    "turbo": 1.62e9,
    "distil-large-v3": 1.51e9,
}


def _hub_cache() -> Path:
    """Where huggingface_hub puts the models, following its own variables."""
    for variable in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        if os.environ.get(variable):
            return Path(os.environ[variable])
    if os.environ.get("HF_HOME"):
        return Path(os.environ["HF_HOME"]) / "hub"
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(cache) / "huggingface" / "hub"


def _model_dir(name: str) -> Path | None:
    repository = REPOSITORIES.get(name.removesuffix(".en")) if "/" not in name else name
    if repository is None:
        return None
    if name.endswith(".en") and "/" not in name:
        repository += ".en"
    return _hub_cache() / ("models--" + repository.replace("/", "--"))


def is_downloaded(name: str) -> bool:
    """Is this model already on disk? A local path always is."""
    if Path(name).is_dir():
        return True
    directory = _model_dir(name)
    if directory is None:
        return True  # unknown to us: nothing to say about its download
    return any((directory / "snapshots").glob("*/model.bin"))


def download_progress(name: str) -> float | None:
    """How far the download of a model has got, 0..1; None if it cannot be told."""
    directory = _model_dir(name)
    total = MODEL_BYTES.get(name.removesuffix(".en"))
    if directory is None or not total:
        return None
    try:
        done = sum(
            blob.stat().st_size for blob in (directory / "blobs").iterdir() if blob.is_file()
        )
    except OSError:
        return 0.0
    # The size is an estimate: the gauge never claims to be finished early.
    return min(done / total, 0.99)


def _normalise(text: str) -> str:
    """The text reduced to what makes it comparable: no case, no punctuation."""
    return text.strip().strip(""" .!?…«»"'-–—""").lower()


def is_filler(text: str, no_speech_prob: float) -> bool:
    """Is this sentence a hallucinated credit line rather than a dictation?"""
    normalised = _normalise(text)
    if "amara.org" in normalised or "sous-titrage" in normalised:
        # Nobody dictates that: no need to wait for the model's opinion.
        return True
    return no_speech_prob >= NO_SPEECH_LIMIT and normalised in FILLERS


def has_nvidia_gpu() -> bool:
    if not shutil.which("nvidia-smi"):
        return False
    try:
        return subprocess.run(
            ["nvidia-smi", "-L"], capture_output=True, timeout=5
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class Transcriber:
    """Loads the model once and for all and keeps it in memory."""

    def __init__(self, config: dict[str, Any]):
        self.config = config["model"]
        self._model = None
        # The preload at start-up and a first dictation may ask at the same time.
        self._loading = threading.Lock()
        # Target values, known even before the model is loaded.
        self.model_name, self.device, self.compute_type = self._resolve()

    def _resolve(self) -> tuple[str, str, str]:
        device = self.config["device"]
        if device == "auto":
            device = "cuda" if has_nvidia_gpu() else "cpu"

        name = self.config["name"]
        if name == "auto":
            name = GPU_MODEL if device == "cuda" else CPU_MODEL

        compute_type = self.config["compute_type"]
        if compute_type == "auto":
            compute_type = "float16" if device == "cuda" else "int8"
        return name, device, compute_type

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    @property
    def is_downloaded(self) -> bool:
        return is_downloaded(self.model_name)

    def download_progress(self) -> float | None:
        return download_progress(self.model_name)

    def load(self) -> None:
        with self._loading:
            self._load()

    def _load(self) -> None:
        if self._model is not None:
            return
        # Xet writes the model file in one go, at the very end: over plain
        # HTTP it grows on disk as it arrives, and the overlay can follow it.
        # Read by huggingface_hub when first imported, hence set before.
        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
        from faster_whisper import WhisperModel

        name, device, compute_type = self._resolve()
        try:
            self._model = WhisperModel(name, device=device, compute_type=compute_type)
        except Exception as error:  # no CUDA, not enough VRAM, broken driver...
            if device != "cuda":
                raise
            logger.warning("Cannot load on CUDA (%s) — falling back to the CPU.", error)
            name = CPU_MODEL if self.config["name"] == "auto" else name
            device, compute_type = "cpu", "int8"
            self._model = WhisperModel(name, device=device, compute_type=compute_type)

        self.model_name, self.device, self.compute_type = name, device, compute_type
        logger.info("Model %s loaded on %s (%s).", name, device, compute_type)

    def prompt(self, context: str = "") -> str | None:
        """The user's vocabulary, extended by what they have just dictated.

        An isolated sentence has no context at all: "some pictures of blocks"
        after "we should add" transcribes far better when the model knows what
        came before.
        """
        parts = [str(self.config["initial_prompt"]).strip()]
        if self.config["context"]:
            parts.append(context.strip()[-CONTEXT_CHARS:])
        return " ".join(part for part in parts if part) or None

    def transcribe(self, pcm: bytes, context: str = "") -> str:
        """s16le 16 kHz mono PCM -> text."""
        if not pcm:
            return ""
        self.load()

        import numpy as np

        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        language = self.config["language"]
        segments, _info = self._model.transcribe(
            audio,
            language=None if language in ("", "auto") else language,
            beam_size=int(self.config["beam_size"]),
            initial_prompt=self.prompt(context),
            # Domain terms, pushed into the decoder prompt: without them,
            # "repos GitHub" comes back as "ripos" and "in Chrome" as "Inchrom".
            hotwords=str(self.config["vocabulary"]).strip() or None,
            vad_filter=True,
            condition_on_previous_text=False,
        )

        kept = []
        for segment in segments:
            text = segment.text.strip()
            if not text:
                continue
            if is_filler(text, segment.no_speech_prob):
                logger.debug("Hallucination dropped: %r (no_speech=%.2f)",
                             text, segment.no_speech_prob)
                continue
            kept.append(text)
        return " ".join(kept).strip()
