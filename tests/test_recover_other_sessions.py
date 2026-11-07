"""The ordinary tick (`AgentRunner._reconcile_interactive_prompt`) reconciles a legacy `session-unbound` codex row
once the later same-cwd rollouts are proven to belong to other attempts.

Real SQLite rows, real journals, stderr and rollouts on disk. The runner object is the real method bound to a
minimal host (state dir, agent name, store, tmux handle, emitter): no internal mocks."""
import hashlib
import json
import os
import tempfile
import types
import unittest
import uuid
from pathlib import Path

from mesh_runner import interactive
from mesh_runner.runner import AgentRunner
from mesh_runner.store import Store

LINE = ("interactive: codex exited during start-up, starting again (2/2) in 30s: Error: account/read failed "
        "during TUI bootstrap: account/read failed: workspace routing discovery unauthorized (401) (code -32603)")


class RecoverOtherSessions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.home = self.root / "codex-home"
        self.home.mkdir()
        self.cwd = self.root / "work"
        self.cwd.mkdir()
        old = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(self.home)
        self.addCleanup(lambda: os.environ.__setitem__("CODEX_HOME", old) if old is not None
                        else os.environ.pop("CODEX_HOME", None))
        self.store = Store(self.state / "runner.sqlite")
        self.addCleanup(self.store.db.close)
        self.events = []
        self.host = types.SimpleNamespace(
            cfg=types.SimpleNamespace(state_dir=self.state), agent=types.SimpleNamespace(name="howard"),
            store=self.store, _tmux=object(), _turn_over=lambda a: True,
            emit=lambda kind, a: self.events.append(kind))
        self.row = self.legacy_row()

    def legacy_row(self):
        a = self.store.create("howard", str(uuid.uuid4()), "codex")
        log = self.root / f"{a.id}.codex.jsonl"
        log.write_text("")
        log.with_suffix(".stderr").write_text(LINE + "\n")
        token, sha = uuid.uuid4().hex, "sha"
        interactive.write_delivery(interactive.agent_dir(self.state, "howard"), a.id, {
            "token": token, "sha256": sha, "phase": "send-intent", "harness": "codex", "session_id": "",
            "cwd": str(self.cwd), "native_base": 0,
            "native_prefix": hashlib.sha256(json.dumps([], sort_keys=True).encode()).hexdigest()})
        intent = {"kind": "interactive", "harness": "codex", "token": token, "sha256": sha, "prompt": "p",
                  "log_path": str(log), "turn": 1, "retryable": False, "resolution": "session-unbound",
                  "prior": {}}
        self.assertTrue(self.store.update(a, state="unknown", wait={"interactive_prompt_intent": intent}))
        return self.store.get(a.id)

    def bind(self, sid):
        other = self.store.create("garfield", str(uuid.uuid4()), "codex")
        self.store.update(other, state="done", session_id=sid)

    def rollout(self, sid, raw=None):
        day = self.home / "sessions" / "2026" / "11" / "07"
        day.mkdir(parents=True, exist_ok=True)
        (day / f"rollout-{sid}.jsonl").write_text(
            raw if raw is not None else json.dumps({"payload": {"id": sid, "cwd": str(self.cwd)}}) + "\n")

    def tick(self):
        return AgentRunner._reconcile_interactive_prompt(self.host, self.store.get(self.row.id))

    def assert_reconciled(self):
        self.assertFalse(self.tick())
        a = self.store.get(self.row.id)
        self.assertEqual(a.state, "ready")
        self.assertTrue(a.wait["interactive_prompt_intent"]["retryable"])
        self.assertIn("prompt_unsent_reconciled", self.events)

    def assert_pending(self):
        self.assertFalse(self.tick())
        self.assertEqual(self.store.get(self.row.id).state, "unknown")
        self.assertNotIn("prompt_unsent_reconciled", self.events)

    def test_later_rollouts_bound_to_other_attempts_reconcile_the_row(self):
        for n in range(3):
            self.bind(f"sid-{n}")
            self.rollout(f"sid-{n}")
        self.assert_reconciled()

    def test_unbound_rollout_keeps_it_pending(self):
        self.bind("sid-0")
        self.rollout("sid-0")
        self.rollout("nobody-owns-this")
        self.assert_pending()

    def test_unreadable_rollout_keeps_it_pending(self):
        self.bind("sid-0")
        self.rollout("sid-0")
        self.rollout("x", raw="{not json\n")
        self.assert_pending()

    def test_rollout_bound_to_the_same_key_keeps_it_pending(self):
        self.store.db.execute("UPDATE attempts SET session_id=? WHERE id=?", ("sid-own", self.row.id))
        self.rollout("sid-own")
        self.assert_pending()

    def test_rollout_bound_to_this_and_another_attempt_keeps_it_pending(self):
        self.bind("sid-shared")
        self.store.db.execute("UPDATE attempts SET session_id=? WHERE id=?", ("sid-shared", self.row.id))
        self.rollout("sid-shared")
        self.assert_pending()


if __name__ == "__main__":
    unittest.main()
