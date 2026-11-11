"""Controller announcement: the project chat picker lists only agents that a live runner leads
(``agent_controllers``), so a chat-serving runner takes and renews that lease with
``POST /agents/me/controller`` (2 min lease on the server, renewed here every minute).

A chat-serving runner adds the runtime snapshot of its chat sessions to every announcement:
``sessions: [{thread_id, agent, process_alive, busy, slot_held, last_end_at}]``,
``slots: {max_per_agent, used}``, ``reported_at``. Only chat windows of ``ChatPool`` sessions are in it,
never a card (daemon) window. A released slot (idle reap, ``stop_session``, eviction) kicks an announcement
at once instead of waiting for the next minute. A server that refuses the snapshot still gets the lease:
the announcement is sent again without it.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Callable

from . import RUNNER_NAME, __version__
from .mesh import MeshError

log = logging.getLogger("mesh_runner")
ANNOUNCE_EVERY = 60.0
# refusals that are not about the snapshot: the lease would be refused without it as well
_NOT_THE_SNAPSHOT = (401, 403, 409)


def iso(ts: float) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z") if ts else None


def build_snapshot(rows: list[dict], alive: Callable[[str, str], bool], max_per_agent: int,
                   now: float | None = None) -> dict:
    """The runtime snapshot from ``ChatPool.sessions()`` rows; ``alive(agent, thread)`` reads the window."""
    sessions = [{"thread_id": r["thread"], "agent": r["agent"], "process_alive": bool(alive(r["agent"], r["thread"])),
                 "busy": bool(r["busy"]), "slot_held": bool(r["slot_held"]), "last_end_at": iso(r["last_end"])}
                for r in rows]
    return {"sessions": sessions,
            "slots": {"max_per_agent": max_per_agent, "used": sum(1 for r in rows if r["slot_held"])},
            "reported_at": iso(time.time() if now is None else now)}


def announce(mesh, runner_id: str, host: str = "", snapshot: dict | None = None) -> bool:
    """One announcement for the agent behind ``mesh``. False when another runner leads it."""
    body = {"runner_id": runner_id, "name": RUNNER_NAME, "host": host or socket.gethostname(),
            "version": __version__}
    try:
        mesh.request("POST", "/agents/me/controller", dict(body, **snapshot) if snapshot else body)
        return True
    except MeshError as e:
        status = getattr(e, "status", None)
        if not snapshot or not isinstance(status, int) or not 400 <= status < 500 or status in _NOT_THE_SNAPSHOT:
            log.warning("chat announce: %s", status or type(e).__name__)
            return False
        log.warning("chat announce: snapshot refused (%s), announcing without it", status)
    return announce(mesh, runner_id, body["host"])


class Announcer:
    """The announcement loop of one runner. ``snapshot`` (set once the chat service is up) returns the
    runtime snapshot or None; ``kick`` makes the loop announce now."""

    def __init__(self, clients: dict, runner_id: str, every: float = ANNOUNCE_EVERY,
                 snapshot: Callable[[], dict | None] | None = None):
        self.clients, self.runner_id, self.every = clients, runner_id, every
        self.snapshot = snapshot
        self._kicked = threading.Event()

    def kick(self) -> None:
        self._kicked.set()

    def _snapshot(self) -> dict | None:
        if self.snapshot is None:
            return None
        try:
            return self.snapshot()
        except Exception as e:                # an unreadable snapshot never costs the lease
            log.warning("chat announce: no snapshot: %s", type(e).__name__)
            return None

    def once(self) -> None:
        self._kicked.clear()                  # a kick during this round makes another round
        snap = self._snapshot()
        for mesh in list(self.clients.values()):
            announce(mesh, self.runner_id, snapshot=snap)

    def loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.once()
            deadline = time.monotonic() + self.every
            while not stop.is_set() and time.monotonic() < deadline:
                if self._kicked.wait(min(1.0, max(0.0, deadline - time.monotonic()))):
                    break


def announce_loop(clients: dict, runner_id: str, stop: threading.Event, every: float = ANNOUNCE_EVERY,
                  announcer: Announcer | None = None) -> None:
    """``clients``: agent name -> Mesh client. Runs until ``stop``."""
    (announcer or Announcer(clients, runner_id, every)).loop(stop)
