"""Loading of the user configuration (~/.config/whisper-desk/config.toml)."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from . import tomlwrite


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else Path.home() / fallback


def _runtime_dir() -> Path:
    """Socket directory: XDG on Linux, private TMPDIR on macOS, /tmp otherwise."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime)
    tmp = os.environ.get("TMPDIR")
    return Path(tmp) if tmp else Path("/tmp")


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / "whisper-desk"
CONFIG_PATH = CONFIG_DIR / "config.toml"
STATE_DIR = _xdg("XDG_STATE_HOME", ".local/state") / "whisper-desk"
DATA_DIR = Path.home() / ".local/share/whisper-desk"
RUNTIME_DIR = _runtime_dir()
# Next to src/ in the installed application, as in the repository.
EXAMPLE_PATH = Path(__file__).resolve().parents[2] / "config.example.toml"

DEFAULTS: dict[str, Any] = {
    "hotkey": {
        # Global shortcut. GTK syntax: <Super>, <Ctrl>, <Alt>, <Shift>.
        # "auto" -> <Super>j, except under WSL where Windows reserves the Windows
        # key for its own system shortcuts: <Ctrl><Alt>j.
        "binding": "auto",
        "name": "whisper-desk",
        # "toggle": first press = listen, second press = transcribe.
        "action": "toggle",
    },
    "model": {
        # "auto" -> large-v3-turbo with an NVIDIA GPU, small otherwise.
        "name": "auto",
        "device": "auto",          # auto | cuda | cpu
        "compute_type": "auto",    # auto | float16 | int8_float16 | int8
        "language": "fr",          # ISO code, or "auto" for detection
        "beam_size": 5,
        "initial_prompt": "",
        # Terms the model mangles: proper nouns, tools, domain jargon.
        # A plain comma-separated list, pushed into the decoder.
        "vocabulary": "",
        # Feeds the end of what was just dictated back to the model: a sentence
        # isolated by the as-you-go splitting gets its context back.
        "context": True,
        # Loaded when the daemon starts: the first dictation is instant.
        "preload": True,
    },
    "recording": {
        "device": "default",       # device of the capture backend
        # auto | arecord | parec | rec | sox | ffmpeg
        "backend": "auto",
        "max_seconds": 120,
        # Dictation as you go: each sentence is transcribed and inserted as soon
        # as you make a short pause, without stopping listening.
        "streaming": True,
        "segment_silence_seconds": 0.6,
        # Silence that ends the dictation (seconds). 0 = disabled.
        "silence_seconds": 2.0,
        # Give up if nothing is said within this delay after the key press.
        "start_timeout_seconds": 8,
        # "auto" or an integer (RMS 0-32767).
        "threshold": "auto",
    },
    "output": {
        # cursor | clipboard | stdout | none  (combinable: "cursor+stdout")
        "mode": "cursor",
        # Paste shortcut sent to the virtual keyboard to insert the text.
        # "auto" -> cmd+v on macOS, ctrl+v elsewhere; "shift+insert" if you
        # mostly dictate in a terminal.
        "paste_shortcut": "auto",
        # How the paste shortcut keystroke is sent.
        # auto | uinput (Linux) | windows (WSL) | applescript (macOS) | none
        "keyboard": "auto",
        # Hands your original clipboard back at the end of the dictation.
        "restore_clipboard": True,
        "notify": False,
        # Keeps every dictation in ~/.local/state/whisper-desk/history.jsonl
        "history": True,
        # Dictations older than this many days are dropped. 0 = kept forever.
        "history_days": 0,
    },
    "overlay": {
        "enabled": True,
        "accent": "#e46212",
        # Size of the pill, in pixels.
        "width": 232,
        "height": 64,
        # Number of equalizer bars.
        "bars": 15,
        # bottom-center | top-center | center
        "position": "bottom-center",
        # Distance to the screen edge, in pixels.
        "margin": 96,
    },
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = {k: (v.copy() if isinstance(v, dict) else v) for k, v in base.items()}
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load(path: Path | None = None) -> dict[str, Any]:
    """Returns the configuration merged with the default values."""
    path = path or CONFIG_PATH
    if not path.exists():
        return _merge(DEFAULTS, {})
    with path.open("rb") as handle:
        user = tomllib.load(handle)
    return _merge(DEFAULTS, user)


def update(changes: dict[str, dict[str, Any]], path: Path | None = None) -> None:
    """Writes values into config.toml, its comments and layout kept.

    A missing file starts from the commented example rather than from an
    empty page: the user who opens it later still finds the explanations.
    """
    path = path or CONFIG_PATH
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        try:
            text = EXAMPLE_PATH.read_text(encoding="utf-8")
        except OSError:
            text = ""
    edited = tomlwrite.apply(text, changes)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".toml.tmp")
    temporary.write_text(edited, encoding="utf-8")
    # Swapped in whole: the daemon never reads half a file.
    os.replace(temporary, path)
