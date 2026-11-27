"""agy context control: an agy session whose one model call carried more context than
``agy_context_rotate_tokens`` is not resumed again; its next turn starts a fresh session with the card's
context, and the overheat is an event in runner.sqlite. The agy start prompt tells the session to search
large files instead of paging through them."""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

from fakes import FakeAdapter, FakeMesh, ask_agent, calls, task, write_replies
from mesh_runner import config as config_mod
from mesh_runner import prompt as prompts
from test_runner import Bench

_TURN = re.compile(r"\.turn(\d+)\.")


class AgyShaped(FakeAdapter):
    """The fixture agent on the lane's agy route. The harness's own report is the boundary: each turn
    reports the context its largest single model call carried (``peaks``, by turn number) and a turn
    total (``usage``) far above it, as a multi-step agy turn does."""

    def __init__(self, script, peaks):
        super().__init__(script)
        self.peaks = peaks

    def parse(self, log_path):
        r = super().parse(log_path)
        m = _TURN.search(Path(log_path).name)
        if m and int(m.group(1)) in self.peaks:
            r.peak_context = self.peaks[int(m.group(1))]
            r.usage = {**r.usage, "input_tokens": 60_000, "cache_read_input_tokens": 420_000}
        return r


class Rotation(Bench):
    def run_two_turns(self, first_peak, limit=None):
        """Turn 1 on the agy route reports ``first_peak`` and waits on an answer; the answer wakes the
        card for turn 2. Returns the task, the runner and the attempt after turn 2."""
        if limit is not None:
            self.cfg.agy_context_rotate_tokens = limit
        t = task(1)
        self.agent.env_command = [sys.executable, "-c", "print('MESH_RUNNER_HARNESS=agy')"]
        self.agent.alt = {"agy": {}}
        write_replies(self.replies, {t["id"][:8]: ask_agent("lead", "approve the plan?"),
                                     t["id"][:8] + ":resume": ask_agent("lead", "and the second step?")})
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.alt_adapters = {"agy": AgyShaped(r.adapter.script, {1: first_peak, 2: 1_000})}
        r.tick()
        self.settle_all(r)
        a = r.store.by_state(self.agent.name, ("waiting",))[0]
        self.assertEqual(a.harness, "agy")
        mesh.add_comment(t["id"], "UNIQUE-ANSWER", author="lead")
        r.tick()
        self.settle_all(r)
        return t, r, r.store.by_state(self.agent.name, ("waiting",))[0]

    def test_a_call_over_the_limit_starts_the_next_turn_in_a_fresh_session(self):
        t, r, a = self.run_two_turns(230_000)
        done = calls(self.calls)
        self.assertEqual([c["resume"] for c in done], [False, False])     # the overheated session is not resumed
        self.assertNotEqual(done[0]["sid"], done[1]["sid"])
        self.assertEqual(a.session_id, done[1]["sid"])
        self.assertEqual(a.no_outcome, 0)                                 # a rotation is no failed turn
        prompt = done[1]["prompt"]
        self.assertIn("Task #" + t["id"][:8], prompt)                     # the card's context of a fresh start
        self.assertIn("UNIQUE-ANSWER", prompt)                            # its latest comments
        self.assertIn("Resumed on task #" + t["id"][:8], prompt)          # and the event that woke it
        self.assertIn(r.FRESH_START_REASONS["context_rotation"], prompt)  # why the session is new
        self.assertIn("Reading large files", prompt)                      # the agy rule rides on the fresh start
        events = r.store.events(a.id)
        overheat = [json.loads(d) for _, k, d in events if k == "context_overheat"]
        self.assertEqual(len(overheat), 1)
        self.assertEqual((overheat[0]["peak"], overheat[0]["limit"], overheat[0]["turn"],
                          overheat[0]["session"]), (230_000, 200_000, 1, done[0]["sid"]))
        launches = [json.loads(d) for _, k, d in events if k == "launch"]
        self.assertEqual([(l["intent"], l["restart_reason"]) for l in launches],
                         [("start", "first_start"), ("start", "context_rotation")])
        rotated = [json.loads(d) for _, k, d in events if k == "context_rotation"]
        self.assertEqual([x["session"] for x in rotated], [done[0]["sid"]])
        self.assertEqual(r.store.mark(self.agent.name, t["id"], "context_rotate")[0], "")   # spent once

    def test_a_turn_whose_calls_stay_under_the_limit_keeps_its_session(self):
        # the turn's summed input (480k) is far over the limit; no single call came near it
        t, r, a = self.run_two_turns(150_000)
        done = calls(self.calls)
        self.assertEqual([c["resume"] for c in done], [False, True])
        self.assertEqual(done[0]["sid"], done[1]["sid"])
        kinds = [k for _, k, _ in r.store.events(a.id)]
        self.assertNotIn("context_overheat", kinds)
        self.assertNotIn("context_rotation", kinds)

    def test_the_limit_itself_rotates(self):
        t, r, a = self.run_two_turns(200_000)
        self.assertEqual([c["resume"] for c in calls(self.calls)], [False, False])

    def test_zero_turns_the_rotation_off(self):
        t, r, a = self.run_two_turns(400_000, limit=0)
        self.assertEqual([c["resume"] for c in calls(self.calls)], [False, True])
        self.assertNotIn("context_overheat", [k for _, k, _ in r.store.events(a.id)])

    def test_other_harnesses_are_not_rotated(self):
        t = task(1)
        write_replies(self.replies, {t["id"][:8]: ask_agent("lead", "approve the plan?"),
                                     t["id"][:8] + ":resume": ask_agent("lead", "and the second step?")})
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.adapter = AgyShaped(r.adapter.script, {1: 400_000})            # a claude lane reporting a big call
        r.tick()
        self.settle_all(r)
        mesh.add_comment(t["id"], "answer", author="lead")
        r.tick()
        self.settle_all(r)
        self.assertEqual([c["resume"] for c in calls(self.calls)], [False, True])


class Config(unittest.TestCase):
    def load(self, extra=""):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        key = Path(d.name) / "k"
        key.write_text("secret")
        key.chmod(0o600)
        p = Path(d.name) / "runner.toml"
        p.write_text(f'[runner]\nmesh_url = "http://mesh"\nstate_dir = "{d.name}/s"\n{extra}\n'
                     f'[[agents]]\nname = "a"\nkey_file = "{key}"\nprofile_dir = "{d.name}"\n')
        return config_mod.load(p)

    def test_the_default_is_below_250k(self):
        self.assertEqual(self.load().agy_context_rotate_tokens, 200_000)
        self.assertLess(config_mod.RunnerConfig.agy_context_rotate_tokens, 250_000)

    def test_an_explicit_limit_and_off(self):
        self.assertEqual(self.load("agy_context_rotate_tokens = 220000").agy_context_rotate_tokens, 220_000)
        self.assertEqual(self.load("agy_context_rotate_tokens = 0").agy_context_rotate_tokens, 0)

    def test_a_limit_at_or_over_250k_or_negative_is_refused(self):
        for bad in ("250000", "501000", "-1", '"200000"', "true"):
            with self.subTest(bad=bad), self.assertRaises(config_mod.ConfigError):
                self.load(f"agy_context_rotate_tokens = {bad}")


class LargeFileRule(unittest.TestCase):
    def test_agy_start_and_review_prompts_carry_the_rule(self):
        t = task(1)
        for text in (prompts.start_prompt(t, "builder", ["/w"], [], harness="agy"),
                     prompts.review_prompt(t, "reviewer", ["/w"], [], harness="agy")):
            self.assertIn(prompts.AGY_LARGE_FILE_RULE.strip(), text)
            self.assertIn(".d.ts", text)
            self.assertIn("1000 lines", text)
            self.assertIn("rg -n", text)
            self.assertIn("grep -n", text)

    def test_other_harnesses_get_the_prompt_unchanged(self):
        t = task(1)
        for harness in ("", "claude", "codex", "kimi", "zcode"):
            with self.subTest(harness=harness):
                self.assertNotIn("Reading large files", prompts.start_prompt(t, "b", ["/w"], [], harness=harness))
                self.assertNotIn("Reading large files", prompts.review_prompt(t, "r", ["/w"], [], harness=harness))


if __name__ == "__main__":
    unittest.main()
