"""Capacity of live chat sessions: per agent (`max_chat_sessions`) and per host, shared with cards.

A chat holds a slot from its first turn until its session is stopped (idle for
`session_idle_minutes`, a `stop_session` command, thread deletion). Over the limit the longest
idle session of the agent is stopped to make room; when every slot is busy with a turn the
message waits in the queue and reports its position. Pure bookkeeping: tmux and Mesh calls
are made by the service through the callbacks it passes.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

# a stopped session stays in the runtime snapshot (process_alive=false) this long, so Mesh sees the stop
# itself and not only an absence; past it an absent thread reads as stopped anyway
STOPPED_KEEP = 600.0
STOPPED_MAX = 200


@dataclass
class Slot:
    agent: str
    thread: str
    busy: bool = False
    last_end: float = 0.0
    started: float = 0.0


@dataclass
class Admission:
    ok: bool
    reason: str = ""            # "" / agent_limit / host_limit
    evict: Slot | None = None   # idle session to stop first
    position: int = 0           # place in the queue when not ok


class ChatPool:
    def __init__(self, max_per_agent: int = 3, max_host: int = 6, host_used=lambda: 0,
                 idle_minutes: float = 10.0, clock=time.time):
        self.max_per_agent, self.max_host = max_per_agent, max_host
        self.host_used = host_used        # sessions of cards on this host that count against the host cap
        self.idle_seconds = idle_minutes * 60
        self.clock = clock
        self.slots: dict[tuple[str, str], Slot] = {}
        self.queue: list[tuple[str, str]] = []
        # agent id -> Stop of the service that owns that agent's sessions (windows are known only to it):
        # an eviction caused by another agent's host is stopped through the owner
        self.owners: dict[str, object] = {}
        self.stopped: dict[tuple[str, str], tuple[float, float]] = {}   # key -> (last_end, stopped at)
        self.lock = threading.Lock()

    def _stopped(self, slot: Slot) -> None:
        self.stopped[(slot.agent, slot.thread)] = (slot.last_end, self.clock())
        while len(self.stopped) > STOPPED_MAX:
            self.stopped.pop(min(self.stopped, key=lambda k: self.stopped[k][1]))

    def _count(self, agent: str | None = None) -> int:
        return sum(1 for s in self.slots.values() if agent is None or s.agent == agent)

    def _idlest(self, agent: str | None) -> Slot | None:
        idle = [s for s in self.slots.values() if not s.busy and (agent is None or s.agent == agent)]
        return min(idle, key=lambda s: s.last_end) if idle else None

    def admit(self, agent: str, thread: str) -> Admission:
        """Reserve a slot for a turn of (agent, thread). A thread that already has a live session
        keeps it. Call ``end_turn`` when the turn ends and ``release`` when the session stops."""
        key = (agent, thread)
        with self.lock:
            if key in self.slots:
                s = self.slots[key]
                if s.busy:
                    return Admission(False, "thread_busy", position=self._pos(key))
                s.busy = True
                self._unqueue(key)
                return Admission(True)
            evict = None
            reason = ""
            if self._count(agent) >= self.max_per_agent:
                evict, reason = self._idlest(agent), "agent_limit"
            elif self._count() + self.host_used() >= self.max_host:
                evict, reason = self._idlest(None), "host_limit"
            if reason and evict is None:
                self._enqueue(key)
                return Admission(False, reason, position=self._pos(key))
            if evict is not None:
                self._stopped(self.slots.pop((evict.agent, evict.thread)))   # the caller stops its session
            self.slots[key] = Slot(agent, thread, busy=True, started=self.clock())
            self.stopped.pop(key, None)
            self._unqueue(key)
            return Admission(True, reason, evict)

    def end_turn(self, agent: str, thread: str) -> None:
        with self.lock:
            s = self.slots.get((agent, thread))
            if s:
                s.busy, s.last_end = False, self.clock()

    def restore(self, slot: Slot) -> None:
        """Put back an idle slot whose session could not be stopped, so it is stopped again later."""
        with self.lock:
            self.slots.setdefault((slot.agent, slot.thread), slot)
            self.stopped.pop((slot.agent, slot.thread), None)

    def release(self, agent: str, thread: str) -> bool:
        with self.lock:
            slot = self.slots.pop((agent, thread), None)
            if slot is not None:
                self._stopped(slot)
            return slot is not None

    def sessions(self) -> list[dict]:
        """Every session holding a slot plus those stopped within ``STOPPED_KEEP``: the rows of the runtime
        snapshot (``slot_held`` false for a stopped one). Liveness of the process is not known here."""
        now = self.clock()
        with self.lock:
            for k in [k for k, (_, at) in self.stopped.items() if now - at > STOPPED_KEEP]:
                del self.stopped[k]
            rows = [{"agent": s.agent, "thread": s.thread, "busy": s.busy, "last_end": s.last_end,
                     "slot_held": True} for s in self.slots.values()]
            rows += [{"agent": a, "thread": t, "busy": False, "last_end": end, "slot_held": False}
                     for (a, t), (end, _) in self.stopped.items() if (a, t) not in self.slots]
            return rows

    def expired(self) -> list[Slot]:
        """Idle longer than the limit: the caller stops these sessions and calls ``release``."""
        now = self.clock()
        with self.lock:
            return [s for s in self.slots.values() if not s.busy and now - s.last_end > self.idle_seconds]

    def _enqueue(self, key):
        if key not in self.queue:
            self.queue.append(key)

    def _unqueue(self, key):
        if key in self.queue:
            self.queue.remove(key)

    def _pos(self, key) -> int:
        return self.queue.index(key) + 1 if key in self.queue else 0
