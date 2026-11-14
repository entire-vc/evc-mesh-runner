"""Native usage records through settle and the real HTTP request encoder."""
import io
import json
import tempfile
import unittest
from pathlib import Path

from mesh_runner.adapters.claude import ClaudeAdapter
from mesh_runner.adapters.codex import CodexAdapter
from mesh_runner.config import AgentConfig, RunnerConfig
from mesh_runner.interactive import Tmux
from mesh_runner.mesh import MeshClient
from mesh_runner.runner import AgentRunner
from mesh_runner.store import Attempt, Store
from mesh_runner.tui_driver import Driver


class Reports(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.sent = []
        self.lose = False
        self.agent = AgentConfig("builder", "test-key", "claude", self.root, [])
        self.cfg = RunnerConfig(mesh_url="https://mesh.example", state_dir=self.root, agents=[self.agent])
        self.attempt = Attempt("a1", "builder", "t1", "claude", "waiting", turns=1)

    def opener(self, req, timeout=None):
        # mock: external boundary — Mesh HTTP, capture the actual encoded body and key.
        self.sent.append(req)
        if self.lose:
            raise TimeoutError("response lost")
        return io.BytesIO(b"{}")

    def runner(self):
        store = Store(self.root / "state.sqlite")
        self.addCleanup(store.db.close)
        return AgentRunner(self.cfg, self.agent, store,
                           mesh=MeshClient(self.cfg.mesh_url, "test-key", opener=self.opener))

    def parse(self, harness, event):
        path = self.root / "turn.jsonl"
        path.write_text(json.dumps(event) + "\n")
        return (ClaudeAdapter() if harness == "claude" else CodexAdapter()).parse(path)

    def report(self, harness, event):
        self.attempt.turns += 1
        self.attempt.harness = harness
        result = self.parse(harness, event)
        self.runner()._report_spend(self.attempt, result)
        return json.loads(self.sent[-1].data)

    def test_observed_zero_is_known_and_missing_measures_are_unknown(self):
        cases = [
            ("claude", {"type": "result", "usage": {"input_tokens": 0, "output_tokens": 0},
                        "total_cost_usd": 0}, {"tokens_in": 0, "tokens_out": 0, "estimated_cost": 0.0}),
            ("codex", {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 8,
                                                                      "output_tokens": 2}},
             {"tokens_in": 10, "tokens_out": 2, "unknown": ["estimated_cost"]}),
            ("claude", {"type": "result", "usage": {"input_tokens": 10, "cache_read_input_tokens": 20,
                                                       "cache_creation_input_tokens": 3}},
             {"tokens_in": 33, "unknown": ["tokens_out", "estimated_cost"]}),
            ("claude", {"type": "result", "usage": {"input_tokens": 10, "cache_read_input_tokens": None,
                                                       "output_tokens": 0}, "total_cost_usd": 0.2},
             {"tokens_out": 0, "estimated_cost": 0.2, "unknown": ["tokens_in"]}),
            ("codex", {"type": "turn.completed"}, {"unknown": ["tokens_in", "tokens_out", "estimated_cost"]}),
            ("claude", {"type": "result", "usage": None},
             {"unknown": ["tokens_in", "tokens_out", "estimated_cost"]}),
        ]
        for harness, event, expected in cases:
            with self.subTest(harness=harness, event=event):
                body = self.report(harness, event)
                self.assertEqual(body, {"task_id": "t1", "model": harness, **expected})

    def test_unknown_report_survives_lost_answer_and_restart_with_same_body_and_key(self):
        event = {"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 3}}
        result = self.parse("codex", event)
        first = self.runner()
        self.lose = True
        first._report_spend(self.attempt, result)
        self.assertIsNone(first.store.mark("builder", "t1", "spend"))
        self.lose = False
        restarted = self.runner()
        restarted._report_spend(self.attempt, self.parse("codex", event))
        count = len(self.sent)
        self.runner()._report_spend(self.attempt, result)
        self.assertEqual(len(self.sent), count)
        self.assertEqual({req.data for req in self.sent}, {self.sent[0].data})
        self.assertEqual(len({req.get_header("Idempotency-key") for req in self.sent}), 1)
        self.assertTrue(self.sent[0].get_header("Idempotency-key"))
        self.assertEqual(json.loads(self.sent[0].data)["unknown"], ["estimated_cost"])

    def test_reports_disabled_still_sends_nothing(self):
        self.cfg.session_reports = False
        self.runner()._report_spend(self.attempt, self.parse("codex", {"type": "turn.completed"}))
        self.assertEqual(self.sent, [])

    def test_complete_report_keeps_wire_body_from_before_upgrade(self):
        MeshClient(self.cfg.mesh_url, "test-key", opener=self.opener).session_report(
            "t1", 100, 5, "m", 0.01, report_id="a1:1")
        self.assertEqual(self.sent[-1].data, json.dumps({"tokens_in": 100, "tokens_out": 5,
                         "model": "m", "estimated_cost": 0.01, "task_id": "t1"}).encode())

    def driver(self, harness):
        out = io.StringIO()
        d = Driver(harness, "builder", self.root, Tmux("tmux"), 1, self.root,
                   "s1", "start", "k", [], out=out, env={})
        return d, out

    def test_interactive_claude_partial_message_usage_does_not_make_a_complete_total(self):
        d, out = self.driver("claude")
        transcript = self.root / "transcript.jsonl"
        messages = [
            {"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": 5, "output_tokens": 3}}},
            {"type": "assistant", "message": {"id": "m2", "usage": {"output_tokens": 2}}},
        ]
        transcript.write_text("".join(json.dumps(e) + "\n" for e in messages))
        d.emit_claude({"event": {"transcript_path": str(transcript)}, "offset": 0})
        body = self.report("claude", json.loads(out.getvalue().splitlines()[-1]))
        self.assertEqual(body, {"task_id": "t1", "model": "claude", "tokens_out": 5,
                                "unknown": ["tokens_in", "estimated_cost"]})

        # A transcript message without an id must not disappear from a known total.
        messages[1]["message"] = {"content": []}
        transcript.write_text("".join(json.dumps(e) + "\n" for e in messages))
        d, out = self.driver("claude")
        d.emit_claude({"event": {"transcript_path": str(transcript)}, "offset": 0})
        body = self.report("claude", json.loads(out.getvalue().splitlines()[-1]))
        self.assertEqual(body["unknown"], ["tokens_in", "tokens_out", "estimated_cost"])

    def test_interactive_codex_partial_counters_stay_unknown(self):
        d, out = self.driver("codex")
        day = self.root / "sessions" / "2026" / "11" / "13"
        day.mkdir(parents=True)
        rollout = day / "rollout-2026-11-13T01-00-00-s1.jsonl"
        rollout.write_text(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 10, "output_tokens": None}}}}) + "\n")
        d.env["CODEX_HOME"] = str(self.root)
        d.emit_codex({"event": {"last-assistant-message": "done"}, "totals": {"input_tokens": 6}})
        body = self.report("codex", json.loads(out.getvalue().splitlines()[-1]))
        self.assertEqual(body, {"task_id": "t1", "model": "codex", "tokens_in": 4,
                                "unknown": ["tokens_out", "estimated_cost"]})

        d, out = self.driver("codex")
        d.env["CODEX_HOME"] = str(self.root)
        d.mode = "resume"
        d.emit_codex({"event": {"last-assistant-message": "done"}, "totals": {}})
        body = self.report("codex", json.loads(out.getvalue().splitlines()[-1]))
        self.assertEqual(body["unknown"], ["tokens_in", "tokens_out", "estimated_cost"])
