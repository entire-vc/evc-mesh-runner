"""A drain is not blocked forever by an intent that is PROVEN never sent (READY retryable, or left on an ended
attempt), while live, ambiguous and unproven intents keep blocking.

Real temporary SQLite store, real delivery journals on disk, the real guard `pending()` and the real runner
methods bound to a minimal host: no internal mocks."""
import hashlib
import json
import os
import tempfile
import types
import unittest
import uuid
from pathlib import Path

from mesh_runner import interactive, launch_guard as guard
from mesh_runner.runner import AgentRunner
from mesh_runner.store import Store


class DrainIntent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.cwd = self.root / "work"
        self.cwd.mkdir()
        self.store = Store(self.state / "runner.sqlite")
        self.addCleanup(lambda: self.store.db.close())
        self.events = []
        host = types.SimpleNamespace(
            cfg=types.SimpleNamespace(state_dir=self.state), agent=types.SimpleNamespace(name="planner"),
            store=self.store, emit=lambda kind, a, **kw: self.events.append(kind),
            _guard=lambda a, name, fn: fn(a))
        host._retire_proven_unsent_intent = lambda a: AgentRunner._retire_proven_unsent_intent(host, a)
        host._retire_proven_delivered_intent = lambda a: AgentRunner._retire_proven_delivered_intent(host, a)
        host._retire_terminal_intent = lambda a: AgentRunner._retire_terminal_intent(host, a)
        self.host = host

    def row(self, *, state, phase="reserved", retryable=True, pid=0, token_ok=True, kind="interactive"):
        a = self.store.create("planner", str(uuid.uuid4()), "codex")
        log = self.root / f"{a.id}.codex.jsonl"
        log.write_text("")
        token, sha = uuid.uuid4().hex, "sha"
        interactive.write_delivery(interactive.agent_dir(self.state, "planner"), a.id, {
            "token": token if token_ok else "other", "sha256": sha, "phase": phase, "harness": "codex",
            "session_id": "", "cwd": str(self.cwd), "native_base": 0,
            "native_prefix": hashlib.sha256(json.dumps([], sort_keys=True).encode()).hexdigest()})
        intent = {"kind": kind, "harness": "codex", "token": token, "sha256": sha, "prompt": "p",
                  "log_path": str(log), "turn": 1, "retryable": retryable, "resolution": "unsent", "prior": {}}
        self.assertTrue(self.store.update(a, state=state, pid=pid, wait={"interactive_prompt_intent": intent}))
        self.store.db.commit()
        return a.id

    def pending(self):
        return guard.pending(self.state)

    def test_ready_proven_unsent_does_not_block_and_survives_restart_without_redelivery(self):
        key = self.row(state="ready")
        self.assertFalse(self.pending())
        journal = interactive.read_delivery(interactive.agent_dir(self.state, "planner"), key)
        self.store.db.close()
        again = Store(self.state / "runner.sqlite")                # restart
        self.addCleanup(again.db.close)
        a = again.get(key)
        self.assertEqual(a.state, "ready")
        self.assertTrue(a.wait["interactive_prompt_intent"]["retryable"])   # accepted command kept for resume
        self.assertEqual(interactive.read_delivery(interactive.agent_dir(self.state, "planner"), key), journal)
        self.assertFalse(self.pending())

    def test_ready_unproven_variants_keep_blocking(self):
        for kw in ({"phase": "send-intent"}, {"phase": "sent"}, {"retryable": False}, {"token_ok": False},
                   {"pid": os.getpid()}, {"kind": "headless"}):
            with self.subTest(kw=kw):
                self.store.db.execute("DELETE FROM attempts")
                self.row(state="ready", **kw)
                self.assertTrue(self.pending())

    def test_running_and_unknown_always_block(self):
        for state in ("running", "unknown", "starting", "waiting"):
            with self.subTest(state=state):
                self.store.db.execute("DELETE FROM attempts")
                self.row(state=state)
                self.assertTrue(self.pending())

    def test_terminal_with_proof_does_not_block_without_proof_does(self):
        self.row(state="completed")
        self.assertFalse(self.pending())
        self.row(state="completed", phase="send-intent")           # terminal alone proves nothing
        self.assertTrue(self.pending())

    def test_one_live_row_still_blocks_next_to_settled_rows(self):
        self.row(state="ready")
        self.row(state="completed")
        self.assertFalse(self.pending())
        self.row(state="running", pid=0)
        self.assertTrue(self.pending())

    def test_rows_an_older_guard_counted_are_not_pending_once_proven_unsent(self):
        self.row(state="ready")
        self.row(state="completed")
        # A guard from before the journal proof counted every nonempty outbox, even these rows.
        rows = self.store.db.execute("SELECT state, wait FROM attempts").fetchall()
        old_pending = any(state == "unknown" or "interactive_prompt_intent" in json.loads(wait)
                          for state, wait in rows)
        self.assertTrue(old_pending)
        self.assertFalse(guard.pending(self.state))
        self.assertEqual(guard.pending_agents(self.state), frozenset())
        self.assertEqual(len(rows), 2)  # The proof never consumes the preserved requests.

    def test_one_unproven_or_live_row_keeps_the_state_pending_next_to_settled_rows(self):
        for extra in ({"state": "unknown"}, {"state": "running"}, {"state": "ready", "pid": os.getpid()},
                      {"state": "ready", "phase": "send-intent"}, {"state": "ready", "token_ok": False},
                      {"state": "completed", "phase": "sent"}):
            with self.subTest(extra=extra):
                self.store.db.execute("DELETE FROM attempts")
                self.row(state="ready")
                self.row(**extra)
                self.assertTrue(guard.pending(self.state))
                self.assertTrue(guard.pending_agents(self.state))
        self.store.db.execute("DELETE FROM attempts")
        self.store.db.commit()
        self.assertFalse(guard.pending(self.state))

    def test_runner_retires_proven_terminal_intent_only(self):
        proven = self.row(state="completed")
        unproven = self.row(state="completed", phase="send-intent")
        AgentRunner.settle_terminal_intents(self.host)
        self.assertNotIn("interactive_prompt_intent", self.store.get(proven).wait)
        self.assertIn("interactive_prompt_intent", self.store.get(unproven).wait)
        self.assertIn("prompt_intent_retired", self.events)
        self.assertTrue(self.pending())                            # the unproven one still blocks

    def test_retire_is_compare_and_set(self):
        key = self.row(state="completed")
        stale = self.store.get(key)
        self.store.update(self.store.get(key), wait={"interactive_prompt_intent": stale.wait["interactive_prompt_intent"],
                                                      "x": 1})
        self.assertFalse(AgentRunner._retire_proven_unsent_intent(self.host, stale))
        self.assertIn("interactive_prompt_intent", self.store.get(key).wait)


if __name__ == "__main__":
    unittest.main()
