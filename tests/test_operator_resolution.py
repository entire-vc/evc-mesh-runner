"""An attempt whose prompt delivery cannot be proven stays in ``unknown`` and keeps its card. The operator
ends it with a token under ``<state_dir>/resolve.d``; the runner ends the attempt the ordinary way."""
import json
import os

from mesh_runner import op_resolve

from fakes import FakeMesh, task
from test_runner import Bench

INTENT = {"version": 1, "token": "t-1", "sha256": "0" * 64, "turn": 2, "kind": "interactive", "harness": "codex",
          "log_path": "/nonexistent/turn2.codex.jsonl", "source_read_at": "", "prior": {}}


class OperatorResolution(Bench):
    def unknown_attempt(self, r, t):
        a = r.store.create(self.agent.name, t["id"], "codex", t["title"])
        wait = {"kind": "card", "target": "abcd1234", "interactive_prompt_intent": dict(INTENT)}
        self.assertTrue(r.store.update(a, expect="starting", state="unknown", wait=wait, turns=2,
                                       wake_text="[runner] end the turn with an outcome",
                                       note="mandatory delivery pending: codex-acceptance-unproven"))
        return r.store.get(a.id)

    def token(self, a, text="Operator: the provider refused the harness, the prompt was never taken\nsecond line"):
        path = op_resolve.path(self.cfg.state_dir, a.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_without_a_token_the_attempt_stays_unknown(self):
        t = task(1, cat="in_progress")
        r = self.runner(FakeMesh([t]))
        a = self.unknown_attempt(r, t)
        r.settle_finished()
        self.assertEqual(r.store.get(a.id).state, "unknown")
        self.assertIn("interactive_prompt_intent", r.store.get(a.id).wait)

    def test_a_token_ends_the_attempt_and_frees_the_card(self):
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t])
        r = self.runner(mesh)
        a = self.unknown_attempt(r, t)
        path = self.token(a)
        r.settle_finished()
        a = r.store.get(a.id)
        self.assertEqual(a.state, "completed")
        self.assertEqual(a.note, "operator resolution: Operator: the provider refused the harness, "
                                 "the prompt was never taken")
        self.assertNotIn("interactive_prompt_intent", a.wait)
        self.assertFalse(path.exists(), "the applied token is removed")
        mark = r.store.mark(self.agent.name, t["id"], "operator_resolved_intent:" + a.id)
        self.assertEqual(json.loads(mark[0])["intent"]["token"], "t-1")
        self.assertIn("operator_resolution", [k for _, k, _ in r.store.events(a.id)])
        self.assertTrue(any("OPERATOR RESOLUTION" in c["body"] for c in mesh.comment_log.get(t["id"], [])))
        self.assertEqual(r.store.terminal_with_intent(self.agent.name), [])
        self.assertEqual(op_resolve.pending(self.cfg.state_dir), [])

    def test_a_token_does_not_end_an_attempt_that_is_not_unknown(self):
        t = task(1, cat="in_progress")
        r = self.runner(FakeMesh([t]))
        a = self.unknown_attempt(r, t)
        self.assertTrue(r.store.update(a, expect="unknown", state="waiting"))
        path = self.token(a)
        r._operator_resolution(r.store.get(a.id))
        self.assertEqual(r.store.get(a.id).state, "waiting")
        self.assertTrue(path.exists())
        self.assertEqual(op_resolve.pending(self.cfg.state_dir), [a.id])

    def test_only_a_regular_file_with_a_reason_is_a_decision(self):
        t = task(1, cat="in_progress")
        r = self.runner(FakeMesh([t]))
        a = self.unknown_attempt(r, t)
        path = self.token(a, "   \n")
        self.assertIsNone(op_resolve.read(self.cfg.state_dir, a.id))
        path.unlink()
        target = path.parent / "elsewhere"
        target.write_text("reason")
        os.symlink(target, path)
        self.assertIsNone(op_resolve.read(self.cfg.state_dir, a.id))
        r.settle_finished()
        self.assertEqual(r.store.get(a.id).state, "unknown")
        self.assertIsNone(op_resolve.read(self.cfg.state_dir, "not-an-attempt-id"))
