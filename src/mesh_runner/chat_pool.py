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
        self.lock = threading.Lock()

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
                del self.slots[(evict.agent, evict.thread)]       # the caller stops its session
            self.slots[key] = Slot(agent, thread, busy=True, started=self.clock())
            self._unqueue(key)
            return Admission(True, reason, evict)

    def end_turn(self, agent: str, thread: str) -> None:
        with self.lock:
            s = self.slots.get((agent, thread))
            if s:
                s.busy, s.last_end = False, self.clock()

    def release(self, agent: str, thread: str) -> bool:
        with self.lock:
            return self.slots.pop((agent, thread), None) is not None

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
