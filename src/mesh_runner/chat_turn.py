"""Executes one claimed chat turn against the protocol (Mesh Doc ``agent-chat-runner-protocol``).

The turn's message reaches the model only after Mesh accepted ``start`` (§5.1, at-most-once).
A ``Delivery`` produces the runner-side events (session / text_delta / tool_status / usage /
final / error) from the live session; this module numbers them, sends them in batches
(~300 ms), keeps the lease alive, relays Stop (``cancel_requested``) to the delivery and stops
at once when Mesh no longer holds the turn for us.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable, Iterable, Protocol

from .chat_protocol import ChatClient, ChatError

log = logging.getLogger("mesh_runner")

BATCH_SECONDS = 0.3
BATCH_MAX_EVENTS = 200
START_RETRIES = 3
TERMINAL = ("final", "error")


class Delivery(Protocol):
    def run(self, turn: dict, cancelled: Callable[[], bool]) -> Iterable[dict]:
        """Hand the message to the session and yield events (without ``seq``). The last one is a
        ``final`` or an ``error``. Must return promptly after ``cancelled()`` turns true, ending
        with ``final(stop_reason=cancelled)``; the session itself stays alive."""


class TurnOutcome:
    def __init__(self, state: str, detail: str = ""):
        self.state, self.detail = state, detail      # done/cancelled/failed/released/dropped

    def __repr__(self) -> str:
        return f"TurnOutcome({self.state!r}, {self.detail!r})"


class ChatTurnRunner:
    def __init__(self, client: ChatClient, delivery: Delivery, clock=time.monotonic,
                 sleep=time.sleep, batch_seconds: float = BATCH_SECONDS):
        self.client, self.delivery = client, delivery
        self.clock, self.sleep, self.batch_seconds = clock, sleep, batch_seconds

    # ------------------------------------------------------------------ start
    def _start(self, turn: dict) -> dict | None:
        """Accepted start answer, or None when the turn must be dropped / handed back."""
        tid, tok = turn["turn_id"], turn["claim_token"]
        for attempt in range(START_RETRIES):
            try:
                return self.client.start(tid, tok)       # idempotent for the same claim_token
            except ChatError as e:
                if e.drop_turn:
                    return None
                if not e.transient:
                    raise
                self.sleep(min(2 ** attempt, 5))
        return None

    def run(self, turn: dict, external_cancel: Callable[[], bool] = lambda: False,
            on_started: Callable[[dict], None] | None = None,
            on_end: Callable[[], None] | None = None) -> TurnOutcome:
        """``external_cancel``: the claim loop saw a ``cancel_turn`` command for this turn."""
        tid, tok = turn["turn_id"], turn["claim_token"]
        try:
            started = self._start(turn)
        except ChatError as e:
            return TurnOutcome("dropped", e.error_code)
        if started is None:
            # the model never saw the message: hand it back (best effort), nothing else to do
            try:
                self.client.release(tid, tok, "start not accepted")
            except ChatError:
                pass
            return TurnOutcome("released", "start not accepted")
        try:
            return self._run_started(turn, started, external_cancel, on_started)
        finally:
            if on_end:
                on_end()

    def _run_started(self, turn, started, external_cancel, on_started) -> TurnOutcome:
        tid, tok = turn["turn_id"], turn["claim_token"]
        state = {"cancel": bool(started.get("cancel_requested")), "dropped": False, "terminal": "", "broken": ""}
        seq = int(started.get("last_seq") or 0)
        # renew at a third of the lease; a silent turn also learns about Stop from the lease answer
        lease_every = max(1.0, min(5.0, float(turn.get("lease_seconds") or 60) / 3))
        if state["cancel"]:
            return self._send_cancelled(tid, tok, seq + 1)
        if on_started:
            try:
                on_started(started)
            except Exception:                           # the token file is optional; the turn goes on without it
                log.warning("chat turn %s: on_started failed", tid[:8])

        feed: "queue.Queue[dict | None]" = queue.Queue()

        def produce() -> None:
            try:
                for ev in self.delivery.run(turn, lambda: state["cancel"] or state["dropped"] or external_cancel()):
                    feed.put(ev)
            except Exception as e:                      # never leave a turn without a terminal event
                # a refusal with a stable code (e.g. headless_forbidden) keeps it; messages stay user-safe
                code = getattr(e, "code", "") or "delivery_failed"
                feed.put({"type": "error", "code": code, "retryable": False,
                          "message": (str(e) if hasattr(e, "code") else type(e).__name__)[:120]})
            finally:
                feed.put(None)
        worker = threading.Thread(target=produce, daemon=True, name=f"chat-turn-{tid[:8]}")
        worker.start()

        pending: list[dict] = []
        last_flush = last_lease = self.clock()
        finished = False

        def flush() -> bool:
            """Send the pending batch. False = Mesh dropped the turn."""
            nonlocal pending, last_flush, last_lease
            if not pending:
                return True
            batch, pending = pending, []
            last_flush = last_lease = self.clock()
            try:
                res = self._post(tid, tok, batch)
            except ChatError as e:
                if e.drop_turn:
                    state["dropped"] = True
                    state["terminal"] = e.error_code
                    return False
                state["broken"] = e.error_code
                state["broken_seq"] = batch[0]["seq"]   # the error replaces the refused batch: no seq gap
                # Mesh stays reachable but refuses: end the turn honestly
                return False
            if res.get("cancel_requested"):
                state["cancel"] = True
            if res.get("state") in ("done", "failed", "cancelled", "expired"):
                state["terminal"] = res["state"]
            return True

        try:
            while not finished:
                try:
                    ev = feed.get(timeout=self.batch_seconds)
                except queue.Empty:
                    ev = ...                            # idle tick: flush / renew only
                if ev is not ... and ev is None:
                    finished = True
                elif ev is not ...:
                    seq += 1
                    pending.append({**ev, "seq": seq})
                    if ev.get("type") in TERMINAL:
                        finished = True
                now = self.clock()
                if pending and (finished or len(pending) >= BATCH_MAX_EVENTS
                                or now - last_flush >= self.batch_seconds):
                    if not flush():
                        break
                elif not pending and now - last_lease >= lease_every:
                    last_lease = now
                    try:
                        res = self.client.lease(tid, tok)
                    except ChatError as e:
                        if e.drop_turn:
                            state["dropped"] = True
                            state["terminal"] = e.error_code
                            break
                        if not e.transient:
                            state["broken"] = e.error_code
                            break
                        continue
                    if res.get("cancel_requested"):
                        state["cancel"] = True
        finally:
            if state["dropped"] or state["broken"]:
                state["cancel"] = True                  # the delivery interrupts the model's turn
            for _ in range(5):                          # the delivery ends its driver within ~25 s of a cancel
                worker.join(timeout=30)
                if not worker.is_alive():
                    break
            stuck = worker.is_alive()
        if stuck:
            return TurnOutcome("failed", "delivery_stuck")
        if state["dropped"]:
            return TurnOutcome("dropped", state["terminal"])
        if state["broken"]:
            try:
                self.client.events(tid, tok, [{"seq": state.get("broken_seq") or seq + 1, "type": "error", "code": "delivery_failed",
                                               "message": state["broken"][:120], "retryable": False}])
            except ChatError:
                pass
            return TurnOutcome("failed", state["broken"])
        if not finished or not state["terminal"]:
            return TurnOutcome("failed", "no terminal event")
        return TurnOutcome(state["terminal"])

    # ------------------------------------------------------------------ wire helpers
    def _post(self, tid: str, tok: str, batch: list[dict]) -> dict:
        """Events are replay-safe (same seqs): a transient failure is retried with the same batch."""
        for attempt in range(4):
            try:
                return self.client.events(tid, tok, batch)
            except ChatError as e:
                if e.error_code == "chat_seq_gap":
                    raise
                if not e.transient or attempt == 3:
                    raise
                self.sleep(min(2 ** attempt, 5))
        raise AssertionError("unreachable")

    def _send_cancelled(self, tid: str, tok: str, seq: int) -> TurnOutcome:
        try:
            res = self._post(tid, tok, [{"seq": seq, "type": "final", "stop_reason": "cancelled"}])
        except ChatError as e:
            return TurnOutcome("dropped", e.error_code)
        return TurnOutcome(res.get("state") or "cancelled")
