"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from . import config as config_module
from . import host

BIN = str(Path.home() / ".local/bin/whisper-desk")
# Beyond this, the daemon is considered lost: the longest a press may take is
# the closing of the overlay, a couple of seconds at most.
TOGGLE_TIMEOUT = 8.0
# "update --check" is made to be hung on a cron or a status bar: 0 nothing to
# do, 1 something new upstream, 2 the question could not be answered.
CANNOT_TELL = 2


def _print_error(message: str) -> int:
    print(f"whisper-desk: {message}", file=sys.stderr)
    return 1


def cmd_daemon(_args: argparse.Namespace) -> int:
    from .daemon import serve

    return serve()


def cmd_toggle(_args: argparse.Namespace) -> int:
    """The shortcut. It answers whatever happens — by force if it has to.

    A press left unanswered is the worst thing that can happen to the user:
    the overlay stays on screen, the microphone stays open, and there is
    nothing left to press. A daemon that no longer answers is therefore put
    down, and the next press starts a fresh one.
    """
    from . import service
    from .client import DaemonSilent, DaemonUnavailable, send

    try:
        reply = send("toggle", timeout=TOGGLE_TIMEOUT)
    except DaemonSilent as error:
        if not service.force_stop():
            return _print_error(f"{error} — stop it by hand: {service.hint()}")
        print("killed")
        return 0
    except DaemonUnavailable as error:
        return _print_error(str(error))
    print(reply.get("state", reply))
    return 0


def cmd_record(_args: argparse.Namespace) -> int:
    """Synchronous dictation: the transcribed text goes to stdout."""
    from .client import DaemonUnavailable, send

    try:
        reply = send("record")
    except DaemonUnavailable as error:
        return _print_error(str(error))
    if "error" in reply:
        return _print_error(reply["error"])
    text = reply.get("text", "")
    if text:
        print(text)
    return 0


def cmd_simple(command: str):
    def run(_args: argparse.Namespace) -> int:
        from .client import DaemonUnavailable, send

        try:
            # Querying or stopping the daemon must not bring it to life.
            reply = send(command, timeout=30, autostart=command not in ("status", "quit"))
        except DaemonUnavailable as error:
            return _print_error(str(error))
        print(json.dumps(reply, ensure_ascii=False))
        return 0

    return run


def cmd_hotkey(args: argparse.Namespace) -> int:
    from . import hotkey

    config = config_module.load()
    try:
        if args.hotkey_action == "remove":
            hotkey.remove()
            print("Shortcut removed.")
            return 0
        if args.hotkey_action == "show":
            current = hotkey.show()
            print(json.dumps(current, ensure_ascii=False, indent=2) if current else "No shortcut installed.")
            return 0
        binding = hotkey.install(config, f"{BIN} toggle")
        print(f"Shortcut installed: {binding} → {BIN} toggle")
        return 0
    except hotkey.UnsupportedDesktop as error:
        return _print_error(
            f"{error}. Create the shortcut by hand on the command '{BIN} toggle'."
        )
    except (OSError, subprocess.SubprocessError) as error:
        return _print_error(f"installing the shortcut failed: {error}")


def cmd_config(args: argparse.Namespace) -> int:
    path = config_module.CONFIG_PATH
    if args.config_action == "path":
        print(path)
        return 0
    if args.config_action == "show":
        print(json.dumps(config_module.load(), ensure_ascii=False, indent=2))
        return 0
    if not path.exists():
        return _print_error(f"{path} is missing — run 'whisper-desk update' to put it back.")
    editor = os.environ.get("EDITOR") or shutil.which("nano") or "vi"
    subprocess.run([editor, str(path)], check=False)
    print("Remember to run 'whisper-desk reload' to apply the changes.")
    return 0


def _history_line(number: int, entry, width: int | None) -> str:
    """One dictation on one line: number, date, duration, model, then the text."""
    stamp = entry.date.replace("T", " ")[:16]
    duration = f"{entry.duration:5.1f} s" if entry.duration is not None else " " * 7
    head = f"{number:>4}  {stamp}  {duration}  {(entry.model or '-'):<14}  "
    text = " ".join(entry.text.split())
    if width and len(head) + len(text) > width:
        text = text[: max(width - len(head) - 1, 10)] + "…"
    return head + text


def cmd_history(args: argparse.Namespace) -> int:
    """Past dictations: list, search, copy, delete — or the window that does it all."""
    from . import history, output

    config = config_module.load()
    if args.window:
        from . import history_proc, window_proc
        from .client import send

        controller = history_proc.HistoryController(
            config, copy=lambda text: output.copy(text), send=send
        )
        try:
            history_proc.open_window(controller)
        except window_proc.WindowUnavailable as error:
            return _print_error(f"{error} — 'whisper-desk history' lists them here instead")
        return 0

    if args.keep_days is not None:
        days = max(args.keep_days, 0)
        config_module.update({"output": {"history_days": days}})
        removed = history.purge(days)
        print("Dictations are kept forever." if days == 0 else
              f"Dictations older than {days} days are deleted ({removed} removed now).")
        print("Run 'whisper-desk reload' for the daemon to apply it.")
        return 0
    if args.clear:
        print(f"{history.clear()} dictation(s) deleted.")
        return 0

    entries = history.load()
    for number in (args.copy, args.delete):
        if number is not None and not 1 <= number <= len(entries):
            return _print_error(f"no dictation number {number} — there are {len(entries)}")
    if args.copy is not None:
        entry = entries[history.index_of(args.copy, entries)]
        if not output.copy(entry.text):
            return _print_error("no clipboard tool answered — see 'whisper-desk doctor'")
        print(f"Copied: {entry.text}")
        return 0
    if args.delete is not None:
        removed = history.delete(history.index_of(args.delete, entries))
        print(f"Deleted: {removed.text}")
        return 0

    query = " ".join(args.query)
    shown = [
        (number, entry) for number, entry in history.numbered(entries)
        if not query or history.matches(entry, query)
    ]
    if args.limit > 0:
        shown = shown[: args.limit]
    if not shown:
        print("No dictation matches." if query else "No dictation yet.")
        return 0
    width = shutil.get_terminal_size().columns if sys.stdout.isatty() else None
    # The oldest at the top, the latest just above the prompt.
    for number, entry in reversed(shown):
        print(_history_line(number, entry, width))
    return 0


def cmd_settings(_args: argparse.Namespace) -> int:
    """The settings window: the essentials of config.toml without opening it."""
    from . import settings_proc, window_proc
    from .client import send

    try:
        settings_proc.open_window(config_module.load(), f"{BIN} toggle", send)
    except window_proc.WindowUnavailable as error:
        return _print_error(f"{error} — 'whisper-desk config' opens the file instead")
    return 0


def _config_bytes() -> bytes | None:
    try:
        return config_module.CONFIG_PATH.read_bytes()
    except OSError:
        return None


def cmd_update(args: argparse.Namespace) -> int:
    """Brings the installation up to date by replaying install.sh.

    The script is the one that knows how to install: it fetches the sources,
    brings the environment up to date and rewrites the command and the
    service. All that is done here is asking it, from a place it will not
    delete, and saying what changed.
    """
    from . import service, update
    from .client import DaemonUnavailable, send

    fingerprint = update.read_fingerprint()
    local = update.commit(fingerprint)
    reference = update.ref(fingerprint)
    source = update.local_source(fingerprint)

    if args.check:
        try:
            latest = update.latest_commit(update.repo(fingerprint), reference)
        except update.UpstreamUnreachable as error:
            _print_error(str(error))
            return CANNOT_TELL
        if not local:
            print(
                "unknown version — this installation predates the version fingerprint;"
                f" {reference} is at {update.short(latest['commit'])}: run 'whisper-desk update'"
            )
            return 1
        if local == latest["commit"]:
            print(f"up to date — {update.describe(fingerprint)}")
            return 0
        when = f" ({latest['date'][:10]})" if latest["date"] else ""
        print(
            f"an update is available — installed {update.short(local)},"
            f" {reference} @ {update.short(latest['commit'])}{when}"
        )
        return 1

    # Restarting the daemon under a dictation would cut someone off mid-sentence.
    try:
        state = str(send("status", timeout=10, autostart=False).get("state", "idle"))
    except DaemonUnavailable:
        state = "idle"      # no daemon answering: no dictation to interrupt
    if state != "idle":
        return _print_error(
            f"a dictation is under way ({state}) — run the update once it is over"
        )

    try:
        latest = update.latest_commit(update.repo(fingerprint), reference)
    except update.UpstreamUnreachable as error:
        if source is None:
            _print_error(str(error))
            return CANNOT_TELL
        latest = None       # a local copy needs no network
    target = update.source_commit(source) if source else (latest or {}).get("commit", "")
    if local and target and local == target:
        print(f"already up to date — {update.describe(fingerprint)}")
        return 0

    print(
        f"Updating {update.repo(fingerprint)}@{reference}:"
        f" {update.short(local) or 'unknown version'} → {update.short(target) or 'upstream'}"
    )
    if source is not None:
        print(f"  from the local copy {source}")
        if latest and target and latest["commit"] != target:
            print(f"  (upstream is at {update.short(latest['commit'])} — 'git pull' there first)")

    keep_hotkey = update.hotkey_installed()
    config_before = _config_bytes()
    try:
        code = update.run_install(fingerprint, keep_hotkey)
    except update.UpstreamUnreachable as error:
        _print_error(str(error))
        return CANNOT_TELL
    if code != 0:
        return _print_error(
            f"install.sh failed (exit code {code}) — the service was left alone"
        )

    installed = update.read_fingerprint()
    restarted = service.restart()
    print("\nSummary")
    print(f"  version   {update.short(local) or 'unknown'} → {update.describe(installed)}")
    print(f"  service   {'restarted' if restarted else 'not restarted — ' + service.hint()}")
    print(f"  shortcut  {'untouched' if keep_hotkey else 'installed'}")
    print(
        f"  config    {config_module.CONFIG_PATH}"
        f" {'kept' if _config_bytes() == config_before else 'rewritten'}"
    )
    return 0


def _measure_microphone(config: dict, seconds: int = 2) -> tuple[float, float]:
    """Listens to the microphone briefly and returns (average level, peak).

    It goes through the same capture backend as a dictation: what the
    diagnostic measures is exactly what the daemon will hear.
    """
    import threading

    from . import capture
    from .recorder import CHANNELS, CHUNK_BYTES, RATE, SAMPLE_WIDTH, _rms

    settings = config["recording"]
    try:
        source = capture.build(
            str(settings["device"]), RATE, CHANNELS, str(settings["backend"])
        )
    except capture.CaptureUnavailable:
        return 0.0, 0.0

    try:
        process = subprocess.Popen(
            source.command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            env={**os.environ, **source.env} if source.env else None,
        )
    except OSError:
        return 0.0, 0.0

    # A missing microphone returns nothing at all: the read would block without this guard.
    watchdog = threading.Timer(seconds + 5, process.kill)
    watchdog.start()
    wanted = int(RATE * SAMPLE_WIDTH * seconds)
    raw = b""
    try:
        assert process.stdout is not None
        while len(raw) < wanted:
            chunk = process.stdout.read(CHUNK_BYTES)
            if not chunk:
                break
            raw += chunk
    except OSError:
        pass
    finally:
        watchdog.cancel()
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
        if process.stdout:
            process.stdout.close()

    if not raw:
        return 0.0, 0.0
    levels = [
        _rms(raw[start:start + CHUNK_BYTES])
        for start in range(0, len(raw), CHUNK_BYTES)
    ] or [0.0]
    return sum(levels) / len(levels), max(levels)


def cmd_doctor(_args: argparse.Namespace) -> int:
    from . import capture, hotkey, inject, output, service, update
    from .daemon import socket_path
    from .overlay_proc import system_python
    from .recorder import SILENT_INPUT_PEAK
    from .transcriber import has_nvidia_gpu

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'✓' if ok else '✗'} {label}{f' — {detail}' if detail else ''}")

    config = config_module.load()
    settings = config["recording"]

    print("Version")
    fingerprint = update.read_fingerprint()
    local = update.commit(fingerprint)
    check(
        f"whisper-desk {__version__}",
        bool(local),
        update.describe(fingerprint) if local
        else "unknown commit, installed before the fingerprint — 'whisper-desk update' records it",
    )
    # Asking upstream is a courtesy, never a condition: offline, the
    # diagnostic goes on without a word about it.
    try:
        latest = update.latest_commit(
            update.repo(fingerprint), update.ref(fingerprint), timeout=update.DOCTOR_TIMEOUT
        )
    except update.UpstreamUnreachable:
        latest = None
    if latest and local and latest["commit"] != local:
        print(
            f"  ⚠ {update.ref(fingerprint)} @ {update.short(latest['commit'])}"
            " is newer — run 'whisper-desk update'"
        )

    print(f"System — {host.label()}")
    usable = capture.available()
    check(
        "microphone capture",
        bool(usable),
        ", ".join(usable) if usable
        else f"install {capture.PACKAGES[capture.recommended()]}",
    )
    level, peak = _measure_microphone(config)
    audible = peak > SILENT_INPUT_PEAK
    check(
        f"the microphone picks up sound ('{settings['device']}')",
        audible,
        f"average level {level:.0f}, peak {peak:.0f}"
        + ("" if audible else f" — {_microphone_hint()}"),
    )
    tool = output.clipboard_tool()
    check(
        "clipboard",
        bool(tool),
        tool or f"install {' or '.join(output.CLIPBOARD_TOOLS[host.name()])}",
    )
    keyboard = inject.keyboard(str(config["output"]["keyboard"]))
    check(
        f"paste keystroke ({keyboard.name})",
        keyboard.available,
        keyboard.hint if not keyboard.available else "",
    )
    check(
        "notifications",
        bool(shutil.which("notify-send"))
        or (host.is_macos() and bool(shutil.which("osascript"))),
        "osascript" if host.is_macos() else "libnotify-bin package",
    )
    if host.is_macos():
        check("NVIDIA GPU", False, "transcription on the CPU (int8) — normal on macOS")
    else:
        check("NVIDIA GPU", has_nvidia_gpu(), "otherwise transcription on the CPU")

    if config["overlay"]["enabled"]:
        check(
            "overlay without focus stealing (X11/Xwayland)",
            bool(os.environ.get("DISPLAY")),
            "otherwise the overlay would catch the paste",
        )
        overlay_ok = subprocess.run(
            [system_python(), "-c",
             "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk"],
            capture_output=True,
        ).returncode == 0
        check("GTK3 overlay", overlay_ok, _overlay_hint())

    print("Configuration")
    check(f"file {config_module.CONFIG_PATH}", config_module.CONFIG_PATH.exists())
    print(
        f"    model={config['model']['name']} language={config['model']['language']}"
        f" output={config['output']['mode']}"
        f" paste={'+'.join(inject.resolve_shortcut(str(config['output']['paste_shortcut'])))}"
        f" streaming={'yes' if settings['streaming'] else 'no'}"
    )

    print("Service")
    state = service.status()
    check(f"daemon ({service.manager()})", state == "active", state)
    check(f"socket {socket_path()}", socket_path().exists())

    print("Shortcut")
    try:
        current = hotkey.show()
        check(
            f"{hotkey.backend().name} shortcut",
            bool(current),
            current.get("binding", "missing") if current else "missing",
        )
    except hotkey.UnsupportedDesktop as error:
        check("global shortcut", False, str(error))
    return 0


def _microphone_hint() -> str:
    if host.is_macos():
        return (
            "check the default input (System Settings → Sound) and the "
            "microphone permission of the terminal"
        )
    if host.is_wsl():
        return "WSLg must be up to date, and the microphone allowed on the Windows side"
    return "check the default source: wpctl status"


def _overlay_hint() -> str:
    if host.is_macos():
        return "brew install pygobject3 gtk+3, or overlay.enabled = false"
    return "python3-gi + gir1.2-gtk-3.0 packages"


def _version_line() -> str:
    """The version, with the commit that is actually running when it is known.

    The program is distributed by `main`: the number alone would say nothing
    about the code in place.
    """
    from . import update

    fingerprint = update.read_fingerprint()
    local = update.commit(fingerprint)
    if not local:
        return f"whisper-desk {__version__}"
    return f"whisper-desk {__version__} ({update.ref(fingerprint)} @ {update.short(local)})"


class _Version(argparse.Action):
    """--version, read from the installation only when it is asked for.

    Every press of the shortcut goes through this parser: the fingerprint is
    not worth a file read on the way to a dictation.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        parser.exit(message=_version_line() + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whisper-desk",
        description="Offline voice dictation: one shortcut, you speak, the text is copied.",
    )
    parser.add_argument(
        "--version", action=_Version, nargs=0,
        help="shows the version and the commit installed",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("daemon", help="runs the service (managed by systemd)").set_defaults(func=cmd_daemon)
    sub.add_parser("toggle", help="starts listening, stops it, then cuts the dictation off").set_defaults(func=cmd_toggle)
    sub.add_parser("record", help="synchronous dictation, text on stdout").set_defaults(func=cmd_record)
    sub.add_parser("stop", help="stops the current listening").set_defaults(func=cmd_simple("stop"))
    sub.add_parser("status", help="daemon state").set_defaults(func=cmd_simple("status"))
    sub.add_parser("reload", help="reloads the configuration").set_defaults(func=cmd_simple("reload"))
    sub.add_parser("quit", help="stops the daemon").set_defaults(func=cmd_simple("quit"))
    sub.add_parser("doctor", help="diagnostic of the installation").set_defaults(func=cmd_doctor)

    update_parser = sub.add_parser(
        "update",
        help="updates the installation from the source repository",
        description="Updates whisper-desk: sources, environment, command, service.",
    )
    update_parser.add_argument(
        "--check", action="store_true",
        help="only compares the versions: exit 0 up to date, 1 something new, 2 cannot tell",
    )
    update_parser.set_defaults(func=cmd_update)

    history_parser = sub.add_parser(
        "history",
        help="past dictations: list, search, copy, delete",
        description="Past dictations, numbered from 1 (the latest).",
    )
    history_parser.add_argument("query", nargs="*", help="words to look for (case and accents ignored)")
    history_parser.add_argument(
        "-n", "--limit", type=int, default=20, help="how many to show (0: all; default 20)"
    )
    history_parser.add_argument("--copy", type=int, metavar="N", help="copies dictation N")
    history_parser.add_argument("--delete", type=int, metavar="N", help="deletes dictation N")
    history_parser.add_argument("--clear", action="store_true", help="deletes every dictation")
    history_parser.add_argument(
        "--keep-days", type=int, metavar="DAYS",
        help="deletes dictations older than DAYS from now on (0: keep them all)",
    )
    history_parser.add_argument(
        "--window", action="store_true", help="opens the history window (GTK)"
    )
    history_parser.set_defaults(func=cmd_history)

    hotkey_parser = sub.add_parser("hotkey", help="manages the global shortcut")
    hotkey_parser.add_argument(
        "hotkey_action", nargs="?", default="install", choices=("install", "remove", "show")
    )
    hotkey_parser.set_defaults(func=cmd_hotkey)

    sub.add_parser(
        "settings", help="settings window: language, model, microphone, shortcut…"
    ).set_defaults(func=cmd_settings)

    config_parser = sub.add_parser("config", help="user configuration")
    config_parser.add_argument(
        "config_action", nargs="?", default="edit", choices=("edit", "path", "show")
    )
    config_parser.set_defaults(func=cmd_config)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
