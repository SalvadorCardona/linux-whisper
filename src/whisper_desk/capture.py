"""Choice of the microphone capture tool, depending on the host.

The rest of the program only wants one thing: a mono s16le PCM stream on the
standard output of a process. Several tools can produce it, none of them is
present everywhere — arecord comes from ALSA (Linux), parec from PulseAudio
(the only audio path in WSLg), rec/sox and ffmpeg cover macOS.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable

from . import host


class CaptureUnavailable(RuntimeError):
    """No capture tool installed for this host."""


@dataclass(frozen=True)
class Capture:
    """A command ready to launch, and the environment variables it requires."""

    name: str
    command: list[str]
    env: dict[str, str] = field(default_factory=dict)


def _default_device(device: str) -> bool:
    return device.strip().lower() in ("", "default", "auto")


def _arecord(device: str, rate: int, channels: int) -> Capture:
    return Capture("arecord", [
        "arecord",
        "-D", "default" if _default_device(device) else device,
        "-f", "S16_LE",
        "-r", str(rate),
        "-c", str(channels),
        "-t", "raw",
        "-q",
        "-",
    ])


def _parec(device: str, rate: int, channels: int) -> Capture:
    command = [
        "parec",
        "--format=s16le",
        f"--rate={rate}",
        f"--channels={channels}",
        "--latency-msec=100",
    ]
    if not _default_device(device):
        command += ["-d", device]
    return Capture("parec", command)


def _ffmpeg(device: str, rate: int, channels: int) -> Capture:
    if host.is_macos():
        # avfoundation names inputs by index: ":0" is the default microphone.
        # `ffmpeg -f avfoundation -list_devices true -i ""` lists them.
        source = ":0" if _default_device(device) else (
            device if device.startswith(":") else f":{device}"
        )
        input_args = ["-f", "avfoundation", "-i", source]
    else:
        input_args = ["-f", "pulse", "-i", "default" if _default_device(device) else device]
    return Capture("ffmpeg", [
        "ffmpeg", "-hide_banner", "-loglevel", "quiet", "-nostdin",
        *input_args,
        "-ar", str(rate), "-ac", str(channels), "-f", "s16le", "-",
    ])


def _sox(binary: str) -> Callable[[str, int, int], Capture]:
    def build(device: str, rate: int, channels: int) -> Capture:
        command = [binary, "-q"]
        if binary == "sox":
            command.append("-d")          # "-d": input = the audio device
        command += [
            "-t", "raw", "-b", "16", "-e", "signed-integer", "-L",
            "-r", str(rate), "-c", str(channels), "-",
        ]
        # sox does not take the device as an argument: it reads AUDIODEV.
        env = {} if _default_device(device) else {"AUDIODEV": device}
        return Capture(binary, command, env)

    return build


BUILDERS: dict[str, Callable[[str, int, int], Capture]] = {
    "arecord": _arecord,
    "parec": _parec,
    "ffmpeg": _ffmpeg,
    "rec": _sox("rec"),
    "sox": _sox("sox"),
}

# From the most suitable to the last resort, for each host.
PREFERENCES: dict[str, tuple[str, ...]] = {
    host.LINUX: ("arecord", "parec", "ffmpeg", "rec", "sox"),
    # WSLg only provides PulseAudio: arecord exists only with the ALSA plugin.
    host.WSL: ("parec", "arecord", "ffmpeg", "rec", "sox"),
    host.MACOS: ("rec", "sox", "ffmpeg"),
}

# Packages to install, quoted by the diagnostic and the installer.
PACKAGES: dict[str, str] = {
    "arecord": "alsa-utils",
    "parec": "pulseaudio-utils",
    "ffmpeg": "ffmpeg",
    "rec": "sox",
    "sox": "sox",
}


def preferences() -> tuple[str, ...]:
    return PREFERENCES[host.name()]


def installed(name: str) -> bool:
    return bool(shutil.which(name))


def available() -> list[str]:
    """The backends usable here, in the host's order of preference."""
    return [name for name in preferences() if installed(name)]


def recommended() -> str:
    """The backend to install first on this host."""
    return preferences()[0]


def choose(preferred: str = "auto") -> str:
    """Name of the backend to use. Raises CaptureUnavailable if nothing is installed."""
    preferred = (preferred or "auto").strip().lower()
    if preferred not in ("", "auto"):
        if preferred not in BUILDERS:
            raise CaptureUnavailable(
                f"unknown capture backend: '{preferred}' "
                f"(known: {', '.join(sorted(BUILDERS))})"
            )
        if not installed(preferred):
            raise CaptureUnavailable(f"'{preferred}' is not installed")
        return preferred

    usable = available()
    if usable:
        return usable[0]
    wanted = recommended()
    raise CaptureUnavailable(
        f"no capture tool found on {host.label()} — install "
        f"{PACKAGES[wanted]} ('{wanted}')"
    )


def build(device: str, rate: int, channels: int, backend: str = "auto") -> Capture:
    """The capture command, ready for Popen."""
    return BUILDERS[choose(backend)](device, rate, channels)


# -- listing the microphones ----------------------------------------------------
#
# A device name only means something to the tool that captures through it: an
# ALSA name for arecord, a PulseAudio source for parec, an index for
# avfoundation. The list offered is therefore the chosen tool's own.

DEFAULT_DEVICE = ("default", "Default microphone")
# ALSA names worth offering: the others (dmix, dsnoop, surround…) are plumbing.
ALSA_KEPT = ("pipewire", "pulse", "sysdefault:", "plughw:")


def parse_arecord(listing: str) -> list[tuple[str, str]]:
    """`arecord -L` → [(name, description)], the plumbing left out."""
    devices: list[tuple[str, str]] = []
    name, lines = "", []
    for raw in [*listing.splitlines(), ""]:
        if raw and raw[0].isspace():
            lines.append(raw.strip().rstrip(","))
            continue
        if name and name.startswith(ALSA_KEPT):
            description = lines[0] if lines else name
            device = re.search(r"DEV=(\d+)", name)
            if name.startswith("plughw:") and device:
                description = f"{description} — device {device.group(1)}"
            devices.append((name, description))
        name, lines = raw.strip(), []
    return devices


def parse_pactl(listing: str) -> list[tuple[str, str]]:
    """`pactl list sources` → [(name, description)], without the speaker monitors."""
    devices: list[tuple[str, str]] = []
    name = ""
    for raw in listing.splitlines():
        line = raw.strip()
        if line.startswith("Name:"):
            name = line.partition(":")[2].strip()
        elif line.startswith("Description:") and name:
            if not name.endswith(".monitor"):
                devices.append((name, line.partition(":")[2].strip()))
            name = ""
    return devices


def parse_avfoundation(listing: str) -> list[tuple[str, str]]:
    """`ffmpeg -f avfoundation -list_devices true -i ""` → [(":index", name)]."""
    devices: list[tuple[str, str]] = []
    audio = False
    for line in listing.splitlines():
        if "AVFoundation audio devices" in line:
            audio = True
            continue
        if "AVFoundation video devices" in line:
            audio = False
            continue
        match = re.search(r"\]\s*\[(\d+)\]\s*(.+)$", line)
        if audio and match:
            devices.append((f":{match.group(1)}", match.group(2).strip()))
    return devices


def _listing(command: list[str], stderr: bool = False) -> str:
    try:
        result = subprocess.run(command, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (result.stderr if stderr else result.stdout).decode("utf-8", "replace")


def devices(backend: str = "auto") -> list[tuple[str, str]]:
    """The microphones the capture tool can open, "default" first."""
    try:
        chosen = choose(backend)
    except CaptureUnavailable:
        return [DEFAULT_DEVICE]
    found: list[tuple[str, str]] = []
    if chosen == "arecord":
        found = parse_arecord(_listing(["arecord", "-L"]))
    elif chosen == "parec" or (chosen == "ffmpeg" and not host.is_macos()):
        if shutil.which("pactl"):
            found = parse_pactl(_listing(["pactl", "list", "sources"]))
    elif chosen == "ffmpeg":
        found = parse_avfoundation(_listing(
            ["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            stderr=True,
        ))
    return [DEFAULT_DEVICE, *[device for device in found if device[0] != "default"]]
