"""A resumed Claude session reports cumulative totals: each turn's report is their increment."""
import io
import json
import tempfile
import unittest
from pathlib import Path

from mesh_runner.adapters.claude import ClaudeAdapter
from mesh_runner.config import AgentConfig, RunnerConfig
from mesh_runner.mesh import MeshClient
from mesh_runner.runner import AgentRunner, _turn_number
from mesh_runner.store import Attempt, Store

MODEL = "claude-opus-5-5"
# result events of three resumed turns of one real session (its headless turn logs):
# total_cost_usd and modelUsage are session totals, usage is the turn's own (no subagents)
TURNS = [
    (4.683041, {"inputTokens": 3169, "outputTokens": 56287, "cacheReadInputTokens": 7655150,
                "cacheCreationInputTokens": 252830},
     {"input_tokens": 118, "cache_creation_input_tokens": 249815, "cache_read_input_tokens": 7452407,
      "output_tokens": 46433}),
    (5.9134444, {"inputTokens": 3215, "outputTokens": 69115, "cacheReadInputTokens": 9923527,
                 "cacheCreationInputTokens": 317828},
     {"input_tokens": 46, "cache_creation_input_tokens": 64998, "cache_read_input_tokens": 2268377,
      "output_tokens": 12828}),
    (6.4044045999999994, {"inputTokens": 3231, "outputTokens": 75314, "cacheReadInputTokens": 11041268,
                          "cacheCreationInputTokens": 335749},
     {"input_tokens": 16, "cache_creation_input_tokens": 17921, "cache_read_input_tokens": 1117741,
      "output_tokens": 6199}),
]


def result(cost, model_usage, usage, sid="s1"):
    ev = {"type": "result", "subtype": "success", "session_id": sid, "usage": usage, "total_cost_usd": cost}
    if model_usage is not None:
        ev["modelUsage"] = {MODEL: {**model_usage, "costUSD": cost, "contextWindow": 1000000}}
    return ev


class SessionSpend(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.sent = []
        self.lose = False
        self.agent = AgentConfig("builder", "test-key", "claude", self.root, [])
        self.cfg = RunnerConfig(mesh_url="https://mesh.example", state_dir=self.root, agents=[self.agent])
        self.attempt = Attempt("a1", "builder", "t1", "claude", "waiting", session_id="s1")

    def opener(self, req, timeout=None):
        # mock: external boundary — Mesh HTTP; the encoded body and its key are what Mesh adds up
        if self.lose:
            raise TimeoutError("response lost")
        self.sent.append(req)
        return io.BytesIO(b"{}")

    def runner(self):
        store = Store(self.root / "state.sqlite")
        self.addCleanup(store.db.close)
        return AgentRunner(self.cfg, self.agent, store,
                           mesh=MeshClient(self.cfg.mesh_url, "test-key", opener=self.opener))

    def turn(self, runner, event, attempt=None):
        a = attempt or self.attempt
        a.turns += 1
        path = runner.log_dir() / f"{a.id}.turn{a.turns}.claude.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        events = event if isinstance(event, list) else [event]
        path.write_text("".join(json.dumps(ev) + "\n" for ev in events))
        runner._report_spend(a, ClaudeAdapter().parse(path))

    def bodies(self):
        return [json.loads(req.data) for req in self.sent]

    def test_three_resumed_turns_report_the_session_total_once(self):
        r = self.runner()
        for t in TURNS:
            self.turn(r, result(*t))
        bodies = self.bodies()
        self.assertEqual(len(bodies), 3)
        self.assertAlmostEqual(sum(b["estimated_cost"] for b in bodies), 6.4044046, delta=0.0001)
        self.assertAlmostEqual(sum(b["estimated_cost"] for b in bodies), 6.4044, places=9)
        self.assertAlmostEqual(bodies[1]["estimated_cost"], 1.2304, places=9)
        # tokens are modelUsage increments (subagents included), summing to the session totals
        self.assertEqual(sum(b["tokens_out"] for b in bodies), 75314)
        self.assertEqual(sum(b["tokens_in"] for b in bodies), 3231 + 11041268 + 335749)
        self.assertEqual(bodies[2]["tokens_out"], 75314 - 69115)
        self.assertEqual(len({req.get_header("Idempotency-key") for req in self.sent}), 3)
        cost_base = json.loads(r.store.mark("builder", "t1", "cost_base")[0])
        self.assertEqual((cost_base["before"], cost_base["total"]), (5.9134444, 6.4044045999999994))

    def test_restart_between_turns_two_and_three_keeps_sum_and_keys(self):
        first = self.runner()
        for t in TURNS[:2]:
            self.turn(first, result(*t))
        restarted = self.runner()
        self.turn(restarted, result(*TURNS[2]))
        self.assertAlmostEqual(sum(b["estimated_cost"] for b in self.bodies()), 6.4044046, delta=0.0001)
        keys = [req.get_header("Idempotency-key") for req in self.sent]
        self.assertEqual(len(set(keys)), 3)
        # the same three turns from scratch in a fresh store carry the very same keys and bodies
        baseline = (self.sent[:], self.root)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root, self.sent, self.attempt.turns = Path(tmp.name), [], 0
        self.cfg.state_dir = self.agent.profile_dir = self.root
        straight = self.runner()
        for t in TURNS:
            self.turn(straight, result(*t))
        self.assertEqual([req.get_header("Idempotency-key") for req in self.sent], keys)
        self.assertEqual([req.data for req in self.sent], [req.data for req in baseline[0]])

    def test_lost_answer_resettled_after_restart_resends_same_body_and_key(self):
        r = self.runner()
        self.turn(r, result(*TURNS[0]))
        self.lose = True
        self.turn(r, result(*TURNS[1]))
        self.assertEqual(r.store.mark("builder", "t1", "spend")[0], "a1:1")    # turn 2 unmarked
        self.lose = False
        path = r.log_dir() / "a1.turn2.claude.jsonl"
        again = self.runner()
        again._report_spend(self.attempt, ClaudeAdapter().parse(path))     # settle of turn 2 again
        again._report_spend(self.attempt, ClaudeAdapter().parse(path))     # and once more: marked, silent
        self.assertEqual(len(self.sent), 2)
        self.assertAlmostEqual(self.bodies()[1]["estimated_cost"], 1.2304, places=9)
        self.turn(again, result(*TURNS[2]))
        self.assertAlmostEqual(sum(b["estimated_cost"] for b in self.bodies()), 6.4044046, delta=0.0001)

    def test_new_session_and_restarted_counter_are_taken_as_they_are(self):
        r = self.runner()
        self.turn(r, result(*TURNS[1]))
        self.turn(r, result(*TURNS[0], sid="s2"))                   # a fresh session: its own totals
        self.turn(r, result(1.0, {"inputTokens": 1, "outputTokens": 2, "cacheReadInputTokens": 3,
                                  "cacheCreationInputTokens": 4}, {}, sid="s1"))   # s1 counter went down
        b = self.bodies()
        self.assertAlmostEqual(b[0]["estimated_cost"], 5.9134, places=9)
        self.assertAlmostEqual(b[1]["estimated_cost"], 4.683, places=9)
        self.assertEqual(b[1]["tokens_out"], 56287)
        self.assertEqual((b[2]["estimated_cost"], b[2]["tokens_in"], b[2]["tokens_out"]), (1.0, 8, 2))

    def test_without_model_usage_the_turn_usage_stands_and_cost_is_still_an_increment(self):
        r = self.runner()
        self.turn(r, result(TURNS[0][0], None, TURNS[0][2]))
        self.turn(r, result(TURNS[1][0], None, TURNS[1][2]))
        b = self.bodies()
        self.assertAlmostEqual(b[1]["estimated_cost"], 1.2304, places=9)
        self.assertEqual(b[1]["tokens_out"], 12828)
        self.assertEqual(b[1]["tokens_in"], 46 + 64998 + 2268377)

    def test_compaction_reports_all_tokens_from_the_session_increment(self):
        r = self.runner()
        self.turn(r, result(*TURNS[0]))
        # After auto-compaction, usage covers only the last context; modelUsage includes
        # the work before the boundary and the compaction itself.
        after_compact = {"input_tokens": 2, "output_tokens": 3,
                         "cache_read_input_tokens": 4, "cache_creation_input_tokens": 5}
        self.turn(r, [{"type": "system", "subtype": "compact_boundary",
                       "compactMetadata": {"trigger": "auto", "preTokens": 180000}},
                      result(TURNS[1][0], TURNS[1][1], after_compact)])
        path = r.log_dir() / "a1.turn2.claude.jsonl"
        self.assertEqual(ClaudeAdapter().parse(path).compactions, 1)
        b = self.bodies()[1]
        self.assertEqual(b["tokens_out"], 69115 - 56287)
        self.assertEqual(b["tokens_in"], (3215 + 9923527 + 317828) - (3169 + 7655150 + 252830))
        self.assertAlmostEqual(b["estimated_cost"], 1.2304, places=9)
        self.assertIn("~$5.91 notional (out 69.1k tok, cache_rd 9.9M)", r._completion_line(self.attempt))

    def test_two_results_in_one_log_report_both_segments_once(self):
        r = self.runner()
        self.turn(r, result(*TURNS[0]))
        # The first result is an intermediate segment; the final cumulative totals
        # include both, whereas its usage contains only the final segment.
        self.turn(r, [result(*TURNS[1]), result(*TURNS[2])])
        b = self.bodies()
        self.assertEqual(len(b), 2)
        self.assertEqual(b[1]["tokens_out"], 75314 - 56287)
        self.assertEqual(b[1]["tokens_in"], (3231 + 11041268 + 335749) - (3169 + 7655150 + 252830))
        self.assertAlmostEqual(sum(body["estimated_cost"] for body in b), 6.4044, places=9)
        self.assertIn("~$6.40 notional (out 75.3k tok, cache_rd 11.0M)", r._completion_line(self.attempt))

    def test_turn_fields_describe_the_same_increment_as_tokens_and_cost(self):
        r = self.runner()
        r.cfg.spend_report_turn_fields = True
        self.turn(r, result(*TURNS[0]))
        self.turn(r, result(*TURNS[1]))
        b = self.bodies()[1]
        self.assertEqual(b["cache_read"], 9923527 - 7655150)
        self.assertEqual(b["cache_write"], 317828 - 252830)
        self.assertEqual(b["tokens_out"], 69115 - 56287)
        self.assertAlmostEqual(b["estimated_cost"], 1.2304, places=9)

    def test_parse_keeps_the_raw_session_total_for_the_api_ledger(self):
        path = self.root / "t.jsonl"
        path.write_text(json.dumps(result(*TURNS[2])) + "\n")
        r = ClaudeAdapter().parse(path)
        self.assertEqual(r.cost_usd, 6.4044045999999994)
        self.assertEqual(r.model_usage[MODEL]["outputTokens"], 75314)

    def test_completion_line_counts_the_attempt_increment_once(self):
        r = self.runner()
        for t in TURNS:
            self.turn(r, result(*t))
        self.assertIn("~$6.40 notional (out 75.3k tok, cache_rd 11.0M)", r._completion_line(self.attempt))
        # a later attempt resuming the same session counts only its own turn
        later = Attempt("a2", "builder", "t1", "claude", "waiting", session_id="s1")
        self.turn(r, result(7.0, {"inputTokens": 3300, "outputTokens": 80314, "cacheReadInputTokens": 12041268,
                                  "cacheCreationInputTokens": 340000}, {}), attempt=later)
        self.assertIn("~$0.60 notional (out 5.0k tok, cache_rd 1.0M)", r._completion_line(later))

    def test_turn_logs_chain_in_numeric_order(self):
        names = ["a.turn10.claude.jsonl", "a.turn9.claude.jsonl", "a.turn1.jsonl"]
        self.assertEqual(sorted((Path(n) for n in names), key=_turn_number),
                         [Path("a.turn1.jsonl"), Path("a.turn9.claude.jsonl"), Path("a.turn10.claude.jsonl")])


if __name__ == "__main__":
    unittest.main()
