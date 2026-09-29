"""Client for the daemon's socket."""

from __future__ import annotations

import json
import socket
import time
from typing import Any

from . import service
from .daemon import is_alive, socket_path


class DaemonUnavailable(RuntimeError):
    pass


class DaemonSilent(DaemonUnavailable):
    """The socket is there, the daemon is not: it took the order and said nothing.

    Told apart from the rest because it is the only case where something is
    left to put down: a daemon stuck on a dictation still holds the microphone
    and keeps its window on screen.
    """


def _connect(timeout: float) -> socket.socket:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    client.connect(str(socket_path()))
    return client


def send(command: str, timeout: float = 300.0, autostart: bool = True,
         **arguments: Any) -> dict[str, Any]:
    """Sends a command to the daemon, starting it if need be."""
    unreachable = DaemonUnavailable(
        f"daemon unreachable — run '{service.hint()}'"
    )
    try:
        client = _connect(timeout)
    except OSError:
        if not autostart or not _start_daemon():
            raise unreachable from None
        try:
            # The socket exists, but the daemon may have died right after.
            client = _connect(timeout)
        except OSError:
            raise unreachable from None

    try:
        client.sendall((json.dumps({"cmd": command, **arguments}) + "\n").encode("utf-8"))
        with client.makefile("rb") as stream:
            raw = stream.readline()
    except OSError as error:      # a timeout is one of these
        raise DaemonSilent(f"the daemon is not answering ({error})") from None
    finally:
        client.close()

    if not raw:
        raise DaemonSilent("the daemon did not answer")
    return json.loads(raw.decode("utf-8"))


def _start_daemon() -> bool:
    """Starts the daemon (systemd, launchd or a detached process) then waits for it.

    The socket left behind by a daemon that was killed is still there: what is
    waited for is an answer, not a file.
    """
    if not service.start():
        return False
    for _ in range(40):
        if socket_path().exists() and is_alive(socket_path()):
            return True
        time.sleep(0.25)
    return False
