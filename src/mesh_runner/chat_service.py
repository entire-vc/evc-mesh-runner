"""The chat loop of one runner: claim turns, admit them to the session pool, run them, obey commands.

Claim asks only for as many turns as the pool can start (a release counts as an attempt in the
protocol, so a turn is not claimed to be handed back). Commands are executed at least once and
acked: ``stop_session`` / ``delete_transcript`` (idempotent, ``not_found`` is a valid answer),
``cancel_turn`` (Stop for a running turn). Everything machine-specific comes in by callback.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from .chat_pool import ChatPool
from .chat_protocol import ChatClient, ChatError
from .chat_token import TokenKeeper
from .chat_turn import ChatTurnRunner, Delivery

log = logging.getLogger("mesh_runner")

# a turn waits this long for its session token; a slower Mesh answer (the 30 s timeout of 26.10, ec202e35) lands later
TOKEN_FIRST_WAIT = 2.0
ORDERS_EVERY = 20.0          # seconds between order fetches while claims come back idle


class ChatService:
    def __init__(self, client: ChatClient, agents: list[str], pool: ChatPool,
                 delivery_for: Callable[[str], Delivery],
                 stop_session: Callable[[str, str], None],
                 delete_transcript: Callable[[str, str], bool],
                 token_path: Callable[[dict], Path | None] = lambda turn: None,
                 spawn: Callable[[Callable[[], None]], None] | None = None):
        self.client, self.agents, self.pool = client, list(agents), pool
        self.delivery_for, self.stop_session, self.delete_transcript = delivery_for, stop_session, delete_transcript
        self.token_path = token_path
        self.spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True, name="chat-turn").start())
        self.cancel: set[str] = set()
        self.running: set[str] = set()
        self.running_on: dict[str, tuple[str, str]] = {}      # turn id -> (agent, thread)
        self.lock = threading.Lock()
        self.outcomes: list[tuple[str, str]] = []
        self._next_orders = 0.0                  # idle rounds fetch orders no more often than ORDERS_EVERY
        self.hold_until = 0.0                    # no claiming until then (a pool refusal must not spin)
        self.claim_s: float | None = None        # how long the last round's claim took (None: no claim sent)

    def free_slots(self) -> int:
        with self.lock:
            return max(0, self.pool.max_per_agent * len(self.agents) - len(self.running))

    def step(self, timeout: int = 30, claim_agents: list[str] | None = None) -> int:
        """One claim round; an optional filter affects claims only, never commands or workers."""
        eligible = self.agents if claim_agents is None else [a for a in self.agents if a in claim_agents]
        free = self.free_slots()
        self.claim_s = None
        if not eligible or free <= 0 or time.monotonic() < self.hold_until:
            # commands (Stop, delete) must not wait for a free slot
            self._fetch_orders()
            return 0
        t0 = time.monotonic()
        try:
            res = self.client.claim(eligible, timeout=timeout, max_turns=min(5, free))
        finally:
            self.claim_s = time.monotonic() - t0
        if res:   # an idle long-poll is not logged; a turn that returned after ~5 s woke on the poll tick, not the hint
            log.info("chat claim: returned in %.1fs, %d turn(s), %d command(s)", time.monotonic() - t0,
                     len(res.get("turns") or []), len(res.get("commands") or []))
        if not res:
            # an idle claim carries no orders (a chat switched off in the project answers 204 for good):
            # stop/delete orders of deleted chats are fetched here, or their transcripts stay on disk.
            # The poll is short, so the fetch is paced separately instead of riding every round.
            if time.monotonic() >= self._next_orders:
                self._next_orders = time.monotonic() + ORDERS_EVERY
                self._fetch_orders()
            return 0
        for cmd in res.get("commands") or []:
            self._command(cmd)
        started = 0
        for turn in res.get("turns") or []:
            started += self._start(turn)
        return started

    # ------------------------------------------------------------------ commands
    def _fetch_orders(self) -> None:
        """A failed order fetch is logged as such and retried on a later round: reported through the
        claim loop it read as a 33 s claim outage and backed the next claim off."""
        t0 = time.monotonic()
        try:
            cmds = self.client.commands(self.agents)
        except ChatError as e:
            if not e.transient:
                raise
            log.warning("chat commands: %s after %.1fs%s", e.error_code, time.monotonic() - t0,
                        f" ({e})" if e.status == 0 else "")
            return
        for cmd in cmds:
            self._command(cmd)

    def _command(self, cmd: dict) -> None:
        kind, cid = cmd.get("kind"), cmd.get("command_id")
        try:
            if kind == "cancel_turn":
                if str(cmd.get("turn_id")) in self.running:
                    self.cancel.add(str(cmd.get("turn_id")))
                return                                   # ephemeral: no ack needed
            agent, thread = str(cmd.get("agent_id") or ""), str(cmd.get("thread_id") or "")
            agent = agent or (self.agents[0] if len(self.agents) == 1 else "")
            if kind in ("stop_session", "delete_transcript") and agent not in self.agents:
                self.client.ack(cid, "not_found")          # not this runner's agent: nothing here to act on
                return
            if kind == "stop_session":
                had = self.pool.release(agent, thread)
                self.stop_session(agent, thread)
                result = "done" if had else "not_found"
            elif kind == "delete_transcript":
                if not self._end_turns_of(agent, thread):
                    raise RuntimeError("turn still running")       # acked failed: Mesh retries the delete
                self.pool.release(agent, thread)
                self.stop_session(agent, thread)
                result = "done" if self.delete_transcript(agent, thread) else "not_found"
            else:
                return                                   # unknown kind: ignored, stays pending
            self.client.ack(cid, result)
        except ChatError as e:
            log.warning("chat command %s not acked: %s", cid, e.error_code)
        except Exception as e:                           # a failed command is acked as failed: retried
            log.warning("chat command %s failed: %s", cid, type(e).__name__)
            try:
                self.client.ack(cid, "failed", type(e).__name__)
            except ChatError:
                pass

    def _end_turns_of(self, agent: str, thread: str, wait: float = 15.0) -> bool:
        """A chat being deleted must not keep a turn (and its token file) alive: stop it first."""
        with self.lock:
            mine = [t for t, at in self.running_on.items() if at == (agent, thread)]
        self.cancel.update(mine)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            with self.lock:
                if not any(t in self.running for t in mine):
                    return True
            time.sleep(0.1)
        return False

    # ------------------------------------------------------------------ turns
    def _start(self, turn: dict) -> int:
        agent, thread = turn["agent_id"], turn["thread_id"]
        adm = self.pool.admit(agent, thread)
        if not adm.ok:
            # handed back, not started: without this line a turn refused on every claim fails as
            # runner_lost with nothing in the log
            log.warning("chat turn %s not admitted: %s", turn["turn_id"][:8], adm.reason or "pool full")
            self.hold_until = time.monotonic() + 5.0
            try:
                self.client.release(turn["turn_id"], turn["claim_token"], adm.reason or "pool full")
            except ChatError:
                pass
            return 0
        if adm.evict is not None:
            self.stop_session(adm.evict.agent, adm.evict.thread)
        with self.lock:
            self.running.add(turn["turn_id"])
            self.running_on[turn["turn_id"]] = (agent, thread)
        self.spawn(lambda: self._run(turn))
        return 1

    def _run(self, turn: dict) -> None:
        tid, agent, thread = turn["turn_id"], turn["agent_id"], turn["thread_id"]
        stuck = False
        keeper: list[TokenKeeper] = []

        def on_started(started: dict) -> None:
            path = self.token_path(turn)
            if path is not None:
                k = TokenKeeper(self.client, turn, path, int(started.get("execution_epoch") or
                                                            turn.get("execution_epoch") or 0))
                k.start(first_wait=TOKEN_FIRST_WAIT)
                keeper.append(k)
        t0 = time.monotonic()
        try:
            out = ChatTurnRunner(self.client, self.delivery_for(agent)).run(
                turn, external_cancel=lambda: tid in self.cancel, on_started=on_started,
                on_end=lambda: [k.stop() for k in keeper])
            self.outcomes.append((tid, out.state))
            # every turn's end is in the log: a Stop or an empty reply is otherwise invisible here
            log.log(logging.WARNING if out.state in ("failed", "dropped", "released") else logging.INFO,
                    "chat turn %s %s: %s (%.1fs)", tid[:8], out.state, out.detail or "-", time.monotonic() - t0)
            stuck = out.detail == "delivery_stuck"
            if out.state in ("failed", "dropped", "released"):
                # a session that did not finish its turn cleanly is not trusted warm
                if out.state != "released":
                    self.pool.release(agent, thread)
                    self.stop_session(agent, thread)
        except Exception as e:
            log.warning("chat turn %s crashed: %s", tid[:8], type(e).__name__)
            self.pool.release(agent, thread)
        finally:
            self.pool.end_turn(agent, thread)
            with self.lock:
                if not stuck:                       # a delivery that will not stop keeps the chat undeletable
                    self.running.discard(tid)
                    self.running_on.pop(tid, None)
            self.cancel.discard(tid)

    def reap_idle(self) -> list[str]:
        """Stop sessions idle past the limit (the process count falls to zero).

        The pool is shared by every served agent's service: this one stops only its own agents'
        sessions, the others' are left to their service (their windows are not known here)."""
        stopped = []
        for s in self.pool.expired():
            if s.agent not in self.agents:
                continue
            self.pool.release(s.agent, s.thread)
            self.stop_session(s.agent, s.thread)
            stopped.append(s.thread)
        return stopped

    def recover(self) -> None:
        """After a restart: a ``claimed`` turn is handed back, a ``streaming`` one fails honestly."""
        for t in self.client.active():
            try:
                if t.get("state") == "streaming":
                    self.client.events(t["turn_id"], t["claim_token"],
                                       [{"seq": int(t.get("last_seq") or 0) + 1, "type": "error",
                                         "code": "runner_restarted", "message": "the runner restarted",
                                         "retryable": False}])
                else:
                    self.client.release(t["turn_id"], t["claim_token"], "runner restarted")
            except ChatError as e:
                log.info("chat recover %s: %s", t.get("turn_id", "")[:8], e.error_code)
