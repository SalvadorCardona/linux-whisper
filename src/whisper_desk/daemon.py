"""Daemon: keeps the model in memory and runs dictations on demand.

Protocol: one JSON request per line on a Unix socket, one JSON response per
line. Commands: toggle, record, stop, status, reload, quit.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import socket
import socketserver
import threading
from pathlib import Path
from typing import Any

from . import config as config_module
from . import host, hotkey, inject
from . import output
from .capture import CaptureUnavailable
from .overlay_proc import OverlayProcess
from .recorder import SILENT_INPUT_PEAK, Recorder
from .transcriber import Transcriber

logger = logging.getLogger("whisper-desk.daemon")

# How often the download gauge is refreshed while the model arrives.
PROGRESS_SECONDS = 0.25


class ModelUnavailable(RuntimeError):
    """The model could neither be downloaded nor loaded."""


def socket_path() -> Path:
    """The daemon's socket, in the host's runtime directory.

    XDG_RUNTIME_DIR on Linux, TMPDIR (private) on macOS; failing that /tmp,
    which is shared — so the user id is appended there.
    """
    base = config_module.RUNTIME_DIR
    if base == Path("/tmp"):
        return base / f"whisper-desk-{os.getuid()}.sock"
    return base / "whisper-desk.sock"


class Session:
    """One dictation: listening, sentence-by-sentence transcription, insertion.

    Listening and transcription run in two separate threads: the model works on
    the previous sentence while the microphone keeps recording.
    """

    def __init__(self, service: "Service", capture: bool):
        self.service = service
        self.capture = capture          # True -> the text is returned to the client
        self.done = threading.Event()
        self.recording_over = threading.Event()
        # The steps the shortcut goes through, in order: asked to stop, then
        # cut off — after which only giving up is left.
        self.stopping = threading.Event()
        self.cancelled = threading.Event()
        self.text = ""
        self.error: str | None = None
        # Why the text did not reach the cursor: CursorWriter.failure.
        self.delivery_failure = ""
        self.parts: list[str] = []
        self.queue: queue.Queue[bytes | None] = queue.Queue()
        self.overlay = OverlayProcess(service.config, on_event=self._on_overlay_event)
        self.recorder = Recorder(
            service.config,
            on_level=self._on_level,
            on_segment=self.queue.put,
            bands=self.overlay.bars,
        )
        self.writer: output.CursorWriter | None = None

    def _on_level(self, level: float, bands: list[float]) -> None:
        self.overlay.set_level(level, bands)

    def _on_overlay_event(self, event: str) -> None:
        if event == "cancel":
            # Out of the window's reading thread: cancelling closes that window.
            threading.Thread(target=self.service.cancel, args=(self,), daemon=True).start()

    def _hint(self) -> str:
        """How to end the dictation, in the words of the user's keyboard."""
        binding = hotkey.label(hotkey.resolve_binding(self.service.config))
        return f"{binding} to finish"

    def run(self) -> None:
        try:
            ready = self.service.transcriber.is_loaded
            self.overlay.start("listening" if ready else "loading", hint=self._hint())
            if not self.capture and "cursor" in output.modes(self.service.config):
                self.writer = output.CursorWriter(self.service.config, self.overlay)
                # While the user speaks, the virtual keyboard is being prepared.
                threading.Thread(target=self.writer.prepare, daemon=True).start()
            if not ready and not self._wait_for_model():
                return
            worker = threading.Thread(target=self._transcribe_loop, daemon=True)
            worker.start()

            tail = self.recorder.record()
            self.recording_over.set()
            if tail:
                self.queue.put(tail)
            self.queue.put(None)

            self.service.state = "working"
            self.overlay.set_state("working")
            worker.join()
            self.text = " ".join(self.parts)
            self._conclude()
        except CaptureUnavailable as error:
            # A tool to install, not a bug: no need to spread out a traceback.
            self.error = str(error)
            logger.error("Capture impossible: %s", error)
            self._fail("Microphone unavailable", str(error))
        except ModelUnavailable as error:
            self.error = str(error)
            self._fail("The model could not be loaded", str(error))
        except Exception as error:  # the daemon must never die on a dictation
            self.error = str(error)
            logger.exception("Dictation failed")
            self._fail("Something went wrong", str(error))
        finally:
            if self.writer:
                self.writer.close()
            self.overlay.stop()
            self.service.finish(self)
            self.done.set()

    def _wait_for_model(self) -> bool:
        """Shows the model arriving, and only opens the microphone once it is there.

        Listening while the model downloads would ask the user to speak into
        a void for minutes: better to say plainly what is being waited for.
        False if the dictation was dropped in the meantime.
        """
        self.service.state = "loading"
        transcriber = self.service.transcriber
        downloading = not transcriber.is_downloaded
        verb = "Downloading" if downloading else "Loading"
        self.overlay.set_state("loading", f"{verb} the {transcriber.model_name} model")
        if downloading and not self.overlay.alive:
            # Without a window, minutes of silence would pass for a failure.
            output.notify(
                f"whisper-desk: downloading the {transcriber.model_name} model",
                "Listening starts once it is there.",
            )
        failure: list[BaseException] = []

        def load() -> None:
            try:
                transcriber.load()
            except Exception as error:
                logger.exception("Cannot load the model")
                failure.append(error)

        loader = threading.Thread(target=load, daemon=True)
        loader.start()
        while loader.is_alive():
            if self.stopping.is_set() or self.cancelled.is_set():
                # The model keeps loading in the background, for the next time.
                self._cancelled_display()
                return False
            if downloading:
                progress = transcriber.download_progress()
                if progress is not None:
                    self.overlay.set_progress(progress)
            loader.join(PROGRESS_SECONDS)
        if failure:
            raise ModelUnavailable(str(failure[0]))
        self.service.state = "recording"
        self.overlay.set_state("listening")
        return True

    def _conclude(self) -> None:
        """The last word of the window: what became of the dictation."""
        if self.cancelled.is_set():
            return  # already said by cancel()
        if self.error:
            self._fail("Transcription failed", self.error)
        elif not self.parts:
            self._report_silence()
        elif self.delivery_failure == "keyboard":
            paste = "+".join(key.capitalize() for key in inject.resolve_shortcut(
                str(self.service.config["output"]["paste_shortcut"])
            ))
            self._fail("The text could not be typed", f"It is in the clipboard: paste it with {paste}")
        elif self.delivery_failure:
            self._fail("The text could not be inserted", "No clipboard answered — see whisper-desk doctor")
        else:
            self.overlay.set_state("done", self.text, self._done_detail())

    def _done_detail(self) -> str:
        if self.capture:
            return "Transcribed"
        selected = output.modes(self.service.config)
        if "cursor" in selected:
            return "Inserted at the cursor"
        if "clipboard" in selected:
            return "Copied to the clipboard"
        return "Transcribed"

    def _fail(self, title: str, detail: str) -> None:
        """An error the user reads on the window — or in a notification, without one."""
        if self.overlay.alive:
            self.overlay.set_state("error", title, detail)
        else:
            output.notify(f"whisper-desk: {title[0].lower()}{title[1:]}", detail)

    def _cancelled_display(self) -> None:
        detail = "What was already inserted stays" if self.parts else "Nothing was inserted"
        self.overlay.set_state("cancelled", "Dictation cancelled", detail)

    def _report_silence(self) -> None:
        """An empty dictation: tell the user's silence apart from a mute microphone."""
        peak = self.recorder.peak
        device = self.service.config["recording"]["device"]
        if peak < SILENT_INPUT_PEAK:
            logger.warning(
                "No sound captured on microphone '%s' through %s (peak %.0f) — mute "
                "device or wrong default source; see 'whisper-desk doctor'.",
                device, self.recorder.backend or "?", peak,
            )
            self._fail("The microphone is silent", f"No sound from device '{device}' — see whisper-desk doctor")
        else:
            logger.info(
                "No speech detected (%s, peak %.0f).", self.recorder.reason, peak
            )
            timeout = float(self.service.config["recording"]["start_timeout_seconds"])
            detail = (
                f"Nothing was said within {timeout:g} s"
                if self.recorder.reason == "no-speech" else "No speech was recognised"
            )
            self._fail("Nothing heard", detail)

    def _transcribe_loop(self) -> None:
        """Transcribes the sentences in order, as they come in."""
        while True:
            segment = self.queue.get()
            if segment is None or self.cancelled.is_set():
                return
            if not self.recording_over.is_set():
                self.overlay.set_state("working")
            try:
                text = self.service.transcriber.transcribe(segment, " ".join(self.parts))
            except Exception as error:
                self.error = str(error)
                logger.exception("Transcription failed")
                continue
            finally:
                if not self.recording_over.is_set():
                    self.overlay.set_state("listening")
            if self.cancelled.is_set():
                # The shortcut was pressed while this sentence was being
                # transcribed: it goes no further than here.
                return
            if not text:
                continue
            # Later sentences are separated from the previous insertion by a space.
            self.parts.append(text)
            self.overlay.set_text(text)
            if not self.capture:
                delivered = output.deliver(
                    text if len(self.parts) == 1 else f" {text}",
                    self.service.config,
                    writer=self.writer,
                    overlay=self.overlay,
                )
                if not delivered and not self.delivery_failure:
                    self.delivery_failure = (
                        self.writer.failure if self.writer and self.writer.failure else "clipboard"
                    )

    def stop(self) -> None:
        self.stopping.set()
        self.recorder.stop()

    def cancel(self) -> None:
        """Cuts the dictation short: nothing more is transcribed nor inserted.

        The way out when the microphone has caught a neighbour's conversation:
        the sentences already queued are dropped rather than typed at the cursor.
        """
        logger.info("Dictation cut short by the user.")
        self.cancelled.set()
        # Nothing more will be listened to: the tool is killed rather than
        # asked politely, so that a capture gone quiet cannot hold on.
        self.recorder.abort()
        self.queue.put(None)      # wakes the transcription up at once
        # The sentence in progress cannot be interrupted inside the model: the
        # window is closed here so the shortcut is seen to have answered.
        self._cancelled_display()
        self.overlay.stop()

    def give_up(self) -> None:
        """Gives the dictation up for lost, wedged threads and all.

        Cutting off is not always enough: a model stuck on a segment cannot be
        interrupted from the outside, and the dictation would hold the daemon
        for minutes. What it owns is released here — in the background, so the
        shortcut answers at once — and the threads still hanging on are left to
        finish into the void.
        """
        logger.warning("Dictation given up: the daemon takes the hand back by force.")
        self.cancelled.set()
        self.queue.put(None)
        writer, self.writer = self.writer, None
        threading.Thread(target=self._release, args=(writer,), daemon=True).start()
        self.done.set()

    def _release(self, writer: output.CursorWriter | None) -> None:
        """Hands back the microphone, the window and the virtual keyboard.

        Each of them may take its time, or never let go at all: this is why it
        runs beside the shortcut rather than under it.
        """
        try:
            self.recorder.abort()
            self.overlay.stop()
            if writer is not None:
                writer.close()
        except Exception:
            logger.exception("The abandoned dictation could not be released")


class Service:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.transcriber = Transcriber(config)
        self.state = "idle"
        self.session: Session | None = None
        self.lock = threading.Lock()

    # -- commands ----------------------------------------------------------
    def toggle(self) -> dict[str, Any]:
        """The shortcut answers in every state: it never leaves the user stuck.

        Each press goes one step further than the one before: it stops the
        listening, then it cuts the dictation off — the only way to shut the
        microphone up when the room is the one talking — then, if what is left
        still does not answer, it gives the dictation up altogether and hands
        back a free daemon rather than a window that never closes.
        """
        with self.lock:
            session = self.session
            if session is None:
                self._start(capture=False)
                return {"state": "recording"}
            if self.state == "recording" and not session.stopping.is_set():
                session.stop()
                return {"state": "working"}
            if not session.cancelled.is_set():
                session.cancel()
                return {"state": "cancelled"}
            session.give_up()
            self._forget(session)
            return {"state": "idle", "given_up": True}

    def cancel(self, session: Session) -> None:
        """Esc or a click on the window: the same cut as the shortcut's second step."""
        with self.lock:
            if self.session is session and not session.cancelled.is_set():
                session.cancel()

    def record(self) -> dict[str, Any]:
        with self.lock:
            if self.state != "idle":
                return {"error": f"busy ({self.state})"}
            session = self._start(capture=True)
        session.done.wait()
        if session.error:
            return {"error": session.error}
        return {"text": session.text}

    def stop(self) -> dict[str, Any]:
        with self.lock:
            if self.session:
                self.session.stop()
                return {"state": "working"}
            return {"state": self.state}

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "host": host.name(),
            "model": self.transcriber.model_name,
            "device": self.transcriber.device,
            "compute_type": self.transcriber.compute_type,
            "loaded": self.transcriber.is_loaded,
        }

    def reload(self) -> dict[str, Any]:
        with self.lock:
            if self.state != "idle":
                return {"error": f"busy ({self.state})"}
            self.config = config_module.load()
            self.transcriber = Transcriber(self.config)
            if self.config["model"]["preload"]:
                threading.Thread(target=self._preload, daemon=True).start()
            return {"reloaded": True}

    # -- internals ---------------------------------------------------------
    def _start(self, capture: bool) -> Session:
        session = Session(self, capture)
        self.session = session
        self.state = "recording"
        threading.Thread(target=session.run, daemon=True).start()
        return session

    def finish(self, session: Session) -> None:
        """Makes the service available again, under the same lock as the commands."""
        with self.lock:
            self._forget(session)

    def _forget(self, session: Session) -> None:
        """Drops a finished dictation — the lock is already held.

        A dictation given up may come back long afterwards, when a new one is
        already under way: it must not take that one down with it.
        """
        if self.session is session:
            self.session = None
            self.state = "idle"

    def _preload(self) -> None:
        try:
            self.transcriber.load()
        except Exception:
            logger.exception("Cannot preload the model")


class Handler(socketserver.StreamRequestHandler):
    service: Service

    def handle(self) -> None:
        for raw in self.rfile:
            try:
                request = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                self._reply({"error": "invalid JSON"})
                continue
            command = request.get("cmd", "")
            handlers = {
                "toggle": self.service.toggle,
                "record": self.service.record,
                "stop": self.service.stop,
                "status": self.service.status,
                "reload": self.service.reload,
                "ping": lambda: {"pong": True},
            }
            if command == "quit":
                self._reply({"bye": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            handler = handlers.get(command)
            self._reply(handler() if handler else {"error": f"unknown command: {command}"})

    def _reply(self, payload: dict[str, Any]) -> None:
        self.wfile.write((json.dumps(payload) + "\n").encode("utf-8"))
        self.wfile.flush()


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def is_alive(path: Path) -> bool:
    """True if an existing socket still answers.

    A socket file outlives a daemon that was killed: its mere presence proves
    nothing, only an answer does.
    """
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(1.0)
    try:
        client.connect(str(path))
        client.sendall(b'{"cmd": "ping"}\n')
        return bool(client.recv(64))
    except OSError:
        return False
    finally:
        client.close()


def serve() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # Downloading the model is chatty: only warnings are kept.
    for noisy in ("httpx", "httpcore", "huggingface_hub", "urllib3", "filelock"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    config = config_module.load()
    path = socket_path()

    if path.exists():
        if is_alive(path):
            logger.error("A daemon is already running on %s", path)
            return 1
        path.unlink()

    service = Service(config)
    handler = type("BoundHandler", (Handler,), {"service": service})
    server = Server(str(path), handler)
    os.chmod(path, 0o600)

    if config["model"]["preload"]:
        threading.Thread(target=service._preload, daemon=True).start()

    logger.info("Listening on %s (%s)", path, host.label())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        session = service.session
        if session is not None:
            # Leaving now would abandon the window on screen and the microphone
            # open, with nothing left to close them.
            session.cancel()
        path.unlink(missing_ok=True)
    return 0
