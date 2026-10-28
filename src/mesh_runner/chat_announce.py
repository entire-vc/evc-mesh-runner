"""Controller announcement: the project chat picker lists only agents that a live runner leads
(``agent_controllers``), so a chat-serving runner takes and renews that lease with
``POST /agents/me/controller`` (2 min lease on the server, renewed here every minute)."""
from __future__ import annotations

import logging
import socket
import threading

from . import RUNNER_NAME, __version__
from .mesh import MeshError

log = logging.getLogger("mesh_runner")
ANNOUNCE_EVERY = 60.0


def announce(mesh, runner_id: str, host: str = "") -> bool:
    """One announcement for the agent behind ``mesh``. False when another runner leads it."""
    try:
        mesh.request("POST", "/agents/me/controller", {
            "runner_id": runner_id, "name": RUNNER_NAME, "host": host or socket.gethostname(),
            "version": __version__})
        return True
    except MeshError as e:
        log.warning("chat announce: %s", getattr(e, "status", type(e).__name__))
        return False


def announce_loop(clients: dict, runner_id: str, stop: threading.Event, every: float = ANNOUNCE_EVERY) -> None:
    """``clients``: agent name -> Mesh client. Runs until ``stop``."""
    while not stop.is_set():
        for mesh in clients.values():
            announce(mesh, runner_id)
        stop.wait(every)
