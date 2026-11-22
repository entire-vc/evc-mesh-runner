"""A turn without a provider price is reported at the model's list tariff."""
import io
import json
import tempfile
import unittest
from pathlib import Path

from mesh_runner.adapters.base import TurnResult
from mesh_runner.adapters.claude import ClaudeAdapter
from mesh_runner.config import AgentConfig, ConfigError, RunnerConfig, load
from mesh_runner.interactive import Tmux
from mesh_runner.mesh import MeshClient
from mesh_runner.runner import AgentRunner
from mesh_runner.store import Attempt, Store
from mesh_runner.tui_driver import Driver

# A real interactive turn of claude-opus-5-5 that reached Mesh without a cost (no total_cost_usd).
TURN = {"input_tokens": 128, "output_tokens": 43935, "cache_read_input_tokens": 6789009,
         "cache_creation_input_tokens": 165280}
# claude-opus-5-5 list tariff; Claude Code's interactive session writes its cache with the 1h lifetime
TURN_COST = 128 * 4e-6 + 43935 * 20e-6 + 6789009 * 0.2e-6 + 165280 * 8e-6


class Spend(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.sent = []
        self.agent = AgentConfig("builder", "test-key", "claude", self.root, [])
        self.cfg = RunnerConfig(mesh_url="https://mesh.example", state_dir=self.root, agents=[self.agent])
        self.attempt = Attempt("a1", "builder", "t1", "claude", "waiting", turns=1)

    def opener(self, req, timeout=None):
        # mock: external boundary — Mesh HTTP, the encoded body is what Mesh receives
        self.sent.append(req)
        return io.BytesIO(b"{}")

    def send(self, result, harness="claude"):
        store = Store(self.root / "state.sqlite")
        self.addCleanup(store.db.close)
        runner = AgentRunner(self.cfg, self.agent, store,
                             mesh=MeshClient(self.cfg.mesh_url, "test-key", opener=self.opener))
        self.attempt.turns += 1
        self.attempt.harness = harness
        runner._report_spend(self.attempt, result)
        return json.loads(self.sent[-1].data)

    def interactive_claude(self, messages):
        """The interactive driver's result for a transcript, parsed by the Claude adapter."""
        out = io.StringIO()
        d = Driver("claude", "builder", self.root, Tmux("tmux"), 1, self.root, "s1", "start", "k", [],
                   out=out, env={})
        transcript = self.root / "transcript.jsonl"
        transcript.write_text("".join(json.dumps(e) + "\n" for e in messages))
        d.emit_claude({"event": {"transcript_path": str(transcript)}, "offset": 0})
        log = self.root / "turn.jsonl"
        log.write_text(out.getvalue())
        return ClaudeAdapter().parse(log)

    @staticmethod
    def message(mid, usage, model="claude-opus-5-5"):
        return {"type": "assistant", "message": {"id": mid, "model": model, "usage": usage,
                                                 "content": [{"type": "text", "text": "ok"}]}}

    def turn_transcript(self, model="claude-opus-5-5"):
        """That turn split over two API calls, as the transcript records them."""
        first = {"input_tokens": 100, "output_tokens": 40000, "cache_read_input_tokens": 6000000,
                 "cache_creation_input_tokens": 165000,
                 "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 165000}}
        second = {"input_tokens": 28, "output_tokens": 3935, "cache_read_input_tokens": 789009,
                  "cache_creation_input_tokens": 280,
                  "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 280}}
        return [self.message("m1", first, model), self.message("m2", second, model)]

    def test_interactive_claude_turn_reports_its_list_cost(self):
        result = self.interactive_claude(self.turn_transcript())
        self.assertIsNone(result.cost_usd)                      # the provider gave no price
        body = self.send(result)
        self.assertEqual(body, {"task_id": "t1", "model": "claude-opus-5-5",
                                "tokens_in": 128 + 6789009 + 165280, "tokens_out": 43935,
                                "estimated_cost": round(TURN_COST, 4)})
        self.assertEqual(body["estimated_cost"], 3.5593)

    def test_model_without_tariff_stays_unknown(self):
        result = self.interactive_claude(self.turn_transcript(model="glm-5.3"))
        body = self.send(result)
        self.assertEqual(body["model"], "glm-5.3")
        self.assertEqual(body["unknown"], ["estimated_cost"])
        self.assertNotIn("estimated_cost", body)
        # no model at all: the harness name is no tariff either
        body = self.send(TurnResult(usage=dict(TURN)))
        self.assertEqual((body["model"], body["unknown"]), ("claude", ["estimated_cost"]))

    def test_cache_write_of_unknown_lifetime_is_not_guessed(self):
        # the shape the driver emitted before this change: the 5m/1h split lost, prices differ
        body = self.send(TurnResult(model="claude-opus-5-5", usage={**TURN, "cache_creation": None}))
        self.assertEqual(body["unknown"], ["estimated_cost"])
        # without cache writes nothing is ambiguous
        body = self.send(TurnResult(model="claude-opus-5-5", usage={**TURN, "cache_creation_input_tokens": 0}))
        self.assertEqual(body["estimated_cost"], round(TURN_COST - 165280 * 8e-6, 4))

    def test_codex_turn_prices_fresh_and_cached_input_apart(self):
        usage = {"input_tokens": 1489062, "cached_input_tokens": 1300000, "output_tokens": 8385}
        body = self.send(TurnResult(model="gpt-6.1-sol", usage=usage), harness="codex")
        expected = (1489062 - 1300000) * 2e-6 + 1300000 * 0.1e-6 + 8385 * 10e-6
        self.assertEqual(body, {"task_id": "t1", "model": "gpt-6.1-sol", "tokens_in": 1489062,
                                "tokens_out": 8385, "estimated_cost": round(expected, 4)})
        self.assertEqual(body["estimated_cost"], 0.592)
        # counters that cannot be true are no price
        bad = {"input_tokens": 10, "cached_input_tokens": 11, "output_tokens": 1}
        body = self.send(TurnResult(model="gpt-6.1-sol", usage=bad), harness="codex")
        self.assertEqual(body["unknown"], ["estimated_cost"])

    def test_provider_price_wins_over_the_tariff(self):
        body = self.send(TurnResult(model="claude-opus-5-5", usage=dict(TURN), cost_usd=1.25))
        self.assertEqual(body["estimated_cost"], 1.25)
        body = self.send(TurnResult(model="claude-opus-5-5", usage=dict(TURN), cost_usd=0.0))
        self.assertEqual(body["estimated_cost"], 0.0)

    def test_missing_counter_leaves_cost_unknown(self):
        body = self.send(TurnResult(model="claude-opus-5-5", usage={"input_tokens": 5}))
        self.assertEqual(body["unknown"], ["tokens_out", "estimated_cost"])

    def test_variant_suffix_uses_the_base_tariff(self):
        body = self.send(TurnResult(model="claude-opus-5-5[1m]", usage={"input_tokens": 1000000,
                                                                        "output_tokens": 0}))
        self.assertEqual(body["estimated_cost"], 4.0)


class Config(unittest.TestCase):
    def load(self, text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = Path(tmp.name) / "runner.toml"
        p.write_text(f'[runner]\nmesh_url = "https://mesh.example"\nstate_dir = "{tmp.name}"\n' + text)
        return load(p)

    def test_builtin_table_without_a_section(self):
        prices = self.load("").prices
        self.assertEqual(prices["claude-opus-5-5"]["cache_write_1h"], 8.0)
        self.assertTrue(prices["gpt-6.1-sol"]["source"].startswith("https://"))

    def test_section_adds_and_replaces_tariffs(self):
        prices = self.load('[prices."glm-5.3"]\ninput = 0.6\noutput = 2.2\ncache_read = 0.11\n'
                           'source = "https://docs.z.ai/guides/overview/pricing"\n'
                           '[prices."claude-opus-5-5"]\ninput = 1\noutput = 2\nsource = "test"\n').prices
        self.assertEqual(prices["glm-5.3"]["output"], 2.2)
        self.assertEqual(prices["claude-opus-5-5"], {"input": 1.0, "output": 2.0, "source": "test"})
        self.assertIn("claude-sonnet-5-5", prices)
        prices = self.load('[prices]\nbuiltin = false\n[prices.m]\ninput = 1\noutput = 2\nsource = "x"\n').prices
        self.assertEqual(list(prices), ["m"])

    def test_bad_entries_are_refused(self):
        for text, words in (('[prices.m]\ninput = 1\noutput = 2\n', "source"),
                            ('[prices.m]\ninput = -1\noutput = 2\nsource = "x"\n', "non-negative"),
                            ('[prices.m]\ninput = "1"\noutput = 2\nsource = "x"\n', "non-negative"),
                            ('[prices.m]\noutput = 2\nsource = "x"\n', "input and output"),
                            ('[prices.m]\ninput = 1\noutput = 2\ncache = 1\nsource = "x"\n', "unknown keys"),
                            ('[prices]\nbuiltin = "no"\n', "builtin")):
            with self.subTest(text=text), self.assertRaisesRegex(ConfigError, words):
                self.load(text)


if __name__ == "__main__":
    unittest.main()
