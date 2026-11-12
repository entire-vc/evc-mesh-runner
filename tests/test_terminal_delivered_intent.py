"""An ended attempt whose prompt was provably delivered does not keep its outbox (and the drain guard) forever.

A Runner without the intent protocol that ran over this store during a restart closed attempts as completed
and left their consumed `interactive_prompt_intent` behind. The supported retire drops such an intent only on
the exact native proof (driver `prompt_ack` token + sha256 in the intent's log, same turn and log, journal at
`accepted`, no live group), by compare-and-set, with event `prompt_intent_retired`, without a state change or
a re-send. Ambiguous and live intents keep blocking.

Real temporary SQLite store, real delivery journals and logs on disk, the real guard `pending()` and the real
runner methods bound to a minimal host: no internal mocks."""
import json
import os
import tempfile
import types
import unittest
import uuid
from pathlib import Path

import fakes  # noqa: F401  (the suite's STRICT lifecycle: every state write here must be a listed one)
from mesh_runner import interactive, launch_guard as guard, queue_wait
from mesh_runner.runner import AgentRunner
from mesh_runner.store import Store

AGENT = "steward"


class TerminalDeliveredIntent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.store = Store(self.state / "runner.sqlite")
        self.addCleanup(lambda: self.store.db.close())
        self.emitted = []
        self.host = self.make_host(self.store)

    def make_host(self, store):
        host = types.SimpleNamespace(
            cfg=types.SimpleNamespace(state_dir=self.state), agent=types.SimpleNamespace(name=AGENT),
            store=store, emit=lambda kind, a, **kw: self.emitted.append((kind, kw.get("reason"))),
            _guard=lambda a, name, fn: fn(a))
        for name in ("_retire_proven_unsent_intent", "_retire_proven_delivered_intent", "_retire_terminal_intent"):
            setattr(host, name, (lambda n: lambda a: getattr(AgentRunner, n)(host, a))(name))
        return host

    def settle(self, host=None):
        host = host or self.host
        host._terminal_intent_next = 0
        AgentRunner.settle_terminal_intents(host)

    def launch(self, *, phase="accepted", ack="exact", wait_extra=None, state="running"):
        """Write what the launch and driver leave on disk: row with intent, journal, turn log with the ack."""
        a = self.store.create(AGENT, str(uuid.uuid4()), "claude")
        log = self.root / f"{a.id}.claude.jsonl"
        token, sha = uuid.uuid4().hex, uuid.uuid4().hex
        interactive.write_delivery(interactive.agent_dir(self.state, AGENT), a.id, {
            "token": token, "sha256": sha, "phase": phase, "harness": "claude", "session_id": "s-" + a.id[:8]})
        lines = [{"type": "assistant", "text": "done"}]
        if ack == "exact":
            lines.append({"type": "mesh_runner.prompt_ack", "token": token, "sha256": sha})
        elif ack == "token":
            lines.append({"type": "mesh_runner.prompt_ack", "token": "other", "sha256": sha})
        elif ack == "sha":
            lines.append({"type": "mesh_runner.prompt_ack", "token": token, "sha256": "other"})
        log.write_text("".join(json.dumps(x) + "\n" for x in lines))
        intent = {"kind": "interactive", "harness": "claude", "token": token, "sha256": sha, "prompt": "p",
                  "log_path": str(log), "turn": 1, "prior": {}, "source_read_at": ""}
        wait = {"interactive_prompt_intent": intent, **(wait_extra or {})}
        self.assertTrue(self.store.update(a, state=state, pid=os.getpgrp(), stamp="", turns=1,
                                          log_path=str(log), wait=wait))
        self.store.db.commit()
        return a.id

    def close_without_protocol(self, key, state="completed", **changes):
        """What the protocol-less Runner did: set the end state, keep `wait` untouched."""
        self.assertTrue(self.store.update(self.store.get(key), state=state, pid=0, stamp="", **changes))
        self.store.db.commit()

    def kinds(self, key):
        return [k for _, k, _ in self.store.events(key)]

    def test_restart_during_launch_leaves_no_eternal_intent(self):
        key = self.launch()
        self.assertTrue(guard.pending(self.state))                # live turn blocks
        self.close_without_protocol(key)
        self.assertTrue(guard.pending(self.state))                # the bug: completed row blocks forever
        self.store.db.close()
        again = Store(self.state / "runner.sqlite")               # restart onto the fixed Runner
        self.addCleanup(again.db.close)
        host = self.make_host(again)
        self.settle(host)
        a = again.get(key)
        self.assertNotIn("interactive_prompt_intent", a.wait)
        self.assertEqual((a.state, a.turns, a.pid), ("completed", 1, 0))   # no state change, no new turn
        self.assertEqual([k for _, k, _ in again.events(key)].count("prompt_intent_retired"), 1)
        self.assertIn(("prompt_intent_retired", "proven-delivered"), self.emitted)
        self.assertFalse(guard.pending(self.state))
        self.assertEqual(guard.pending_agents(self.state), frozenset())

    def test_every_terminal_state_is_retired_on_proof(self):
        for state in ("completed", "failed", "aborted"):
            with self.subTest(state=state):
                key = self.launch(state="starting" if state == "aborted" else "running")
                self.close_without_protocol(key, state=state)
                self.settle()
                self.assertNotIn("interactive_prompt_intent", self.store.get(key).wait)
                self.assertEqual(self.store.get(key).state, state)

    def test_ambiguous_or_mismatched_keeps_blocking(self):
        cases = ({"phase": "send-intent", "ack": "none"},            # the Prober row: sent, never acknowledged
                 {"phase": "accepted", "ack": "none"},
                 {"phase": "accepted", "ack": "token"},
                 {"phase": "accepted", "ack": "sha"},
                 {"phase": "send-intent", "ack": "exact"},
                 {"phase": "bootstrap-failed", "ack": "exact"})
        for kw in cases:
            with self.subTest(kw=kw):
                self.store.db.execute("DELETE FROM attempts")
                key = self.launch(**kw)
                self.close_without_protocol(key)
                self.settle()
                self.assertIn("interactive_prompt_intent", self.store.get(key).wait)
                self.assertNotIn("prompt_intent_retired", self.kinds(key))
                self.assertTrue(guard.pending(self.state))

    def test_turn_or_log_mismatch_keeps_blocking(self):
        for changes in ({"turns": 2}, {"log_path": str(self.root / "elsewhere.jsonl")}):
            with self.subTest(changes=changes):
                self.store.db.execute("DELETE FROM attempts")
                key = self.launch()
                self.close_without_protocol(key, **changes)
                self.settle()
                self.assertIn("interactive_prompt_intent", self.store.get(key).wait)
                self.assertTrue(guard.pending(self.state))

    def test_missing_journal_or_log_keeps_blocking(self):
        key = self.launch()
        self.close_without_protocol(key)
        interactive.delivery_path(interactive.agent_dir(self.state, AGENT), key).unlink()
        self.settle()
        self.assertIn("interactive_prompt_intent", self.store.get(key).wait)
        key2 = self.launch()
        self.close_without_protocol(key2)
        Path(self.store.get(key2).log_path).unlink()
        self.settle()
        self.assertIn("interactive_prompt_intent", self.store.get(key2).wait)
        self.assertTrue(guard.pending(self.state))

    def test_live_or_open_rows_are_not_retired(self):
        key = self.launch()
        self.close_without_protocol(key)
        self.store.update(self.store.get(key), pid=os.getpgrp())     # live group still owns the turn
        self.store.db.commit()
        self.assertFalse(AgentRunner._retire_proven_delivered_intent(self.host, self.store.get(key)))
        for state in ("running", "unknown", "waiting", "ready", "starting"):
            with self.subTest(state=state):
                other = self.launch()
                self.store.update(self.store.get(other), state=state, pid=0)
                self.store.db.commit()
                self.assertFalse(AgentRunner._retire_proven_delivered_intent(self.host, self.store.get(other)))
                self.assertIn("interactive_prompt_intent", self.store.get(other).wait)

    def test_retire_is_compare_and_set(self):
        key = self.launch()
        self.close_without_protocol(key)
        stale = self.store.get(key)
        self.store.update(self.store.get(key), wait={**stale.wait, "x": 1})
        self.store.db.commit()
        self.assertFalse(AgentRunner._retire_proven_delivered_intent(self.host, stale))
        self.assertIn("interactive_prompt_intent", self.store.get(key).wait)
        self.assertNotIn("prompt_intent_retired", self.kinds(key))

    def test_delivered_wakes_become_marks_and_are_not_delivered_again(self):
        extra = {"ci_delivery": {"key": "ci:seen", "value": "pipe-7"},
                 "queue_delivery": {"key": "queue:seen", "value": "q-3", "floor": "f-9"}}
        key = self.launch(wait_extra=extra)
        task = self.store.get(key).task_id
        self.close_without_protocol(key)
        self.settle()
        a = self.store.get(key)
        self.assertFalse({"interactive_prompt_intent", "ci_delivery", "queue_delivery"} & set(a.wait))
        self.assertEqual(self.store.mark(AGENT, task, "ci:seen")[0], "pipe-7")
        self.assertEqual(self.store.mark(AGENT, task, "queue:seen")[0], "q-3")
        self.assertEqual(self.store.mark(AGENT, task, queue_wait.FLOOR_KEY)[0], "f-9")
        self.settle()                                               # second tick is a no-op
        self.assertEqual(self.kinds(key).count("prompt_intent_retired"), 1)

    def test_proven_unsent_path_is_unchanged(self):
        key = self.launch(phase="reserved", ack="none")
        self.close_without_protocol(key)
        self.settle()
        self.assertNotIn("interactive_prompt_intent", self.store.get(key).wait)
        self.assertIn(("prompt_intent_retired", "proven-unsent"), self.emitted)


if __name__ == "__main__":
    unittest.main()
