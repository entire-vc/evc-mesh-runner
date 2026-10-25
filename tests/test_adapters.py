import json
import tempfile
import unittest
from pathlib import Path

from mesh_runner.adapters.claude import ClaudeAdapter
from mesh_runner.adapters.codex import CodexAdapter

CLAUDE_STREAM = [
    {"type": "system", "subtype": "hook_started"},
    {"type": "system", "subtype": "init", "session_id": "s-1", "model": "m"},
    {"type": "assistant", "session_id": "s-1", "message": {"content": [{"type": "text", "text": "working"}]}},
    {"type": "result", "subtype": "success", "is_error": False, "result": "⏳ WAIT 2h", "session_id": "s-1",
     "total_cost_usd": 0.01, "usage": {"output_tokens": 5}},
]
CODEX_STREAM = [
    {"type": "thread.started", "thread_id": "t-1"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "↻ NEXT"}},
    {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}},
]


def write(lines, extra=b""):
    f = tempfile.NamedTemporaryFile("wb", suffix=".jsonl", delete=False)
    f.write(b"".join(json.dumps(x).encode() + b"\n" for x in lines) + extra)
    f.close()
    return Path(f.name)


class Adapters(unittest.TestCase):
    def test_claude_argv_start_and_resume(self):
        a = ClaudeAdapter(extra_args=["--model", "sonnet"])
        start = a.start_argv("sid", Path("/p"), [Path("/w1")])
        self.assertEqual(start[:3], ["claude", "--session-id", "sid"])
        self.assertIn("stream-json", start)
        self.assertEqual(start[start.index("--add-dir") + 1], "/w1")
        self.assertEqual(a.resume_argv("sid", Path("/p"), [])[:3], ["claude", "--resume", "sid"])

    def test_model_override_replaces_or_appends(self):
        a = ClaudeAdapter("claude", ["--model", "sonnet", "--x"])
        argv = a.with_model(a.start_argv("s", Path("/p"), []), "opus")
        self.assertEqual(argv.count("--model"), 1)
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        b = ClaudeAdapter("claude", [])
        self.assertEqual(b.with_model(b.start_argv("s", Path("/p"), []), "opus")[-2:], ["--model", "opus"])
        d = ClaudeAdapter("claude", ["--model=sonnet", "--x", "--model"])
        argv = d.with_model(d.start_argv("s", Path("/p"), []), "opus")
        self.assertEqual([x for x in argv if x.startswith("--model")], ["--model"])
        self.assertEqual(argv[-2:], ["--model", "opus"])
        self.assertIn("--x", argv)
        c = CodexAdapter("codex", [], "prof")
        argv = c.start_argv("", Path("/p"), [])
        self.assertEqual(c.with_model(argv, "opus"), argv)

    def test_claude_parse(self):
        r = ClaudeAdapter().parse(write(CLAUDE_STREAM, b"not json\n"))
        self.assertEqual((r.session_id, r.final_text, r.finished, r.is_error), ("s-1", "⏳ WAIT 2h", True, False))
        self.assertEqual(r.cost_usd, 0.01)

    def test_claude_parse_without_result_keeps_last_text(self):
        r = ClaudeAdapter().parse(write(CLAUDE_STREAM[:3]))
        self.assertEqual((r.finished, r.final_text), (False, "working"))

    def test_claude_error_result(self):
        err = dict(CLAUDE_STREAM[-1], subtype="error_max_turns", is_error=True, result="")
        r = ClaudeAdapter().parse(write(CLAUDE_STREAM[:3] + [err]))
        self.assertTrue(r.is_error)
        self.assertEqual(r.error, "error_max_turns")

    def test_claude_success_envelope_with_error_preserves_the_real_reason(self):
        err = dict(CLAUDE_STREAM[-1], is_error=True, result="Usage limit reached")
        r = ClaudeAdapter().parse(write(CLAUDE_STREAM[:3] + [err]))
        self.assertTrue(r.is_error)
        self.assertEqual(r.error, "Usage limit reached")

    def test_claude_success_envelope_without_error_reason_remains_an_error(self):
        err = dict(CLAUDE_STREAM[-1], is_error=True, result="")
        r = ClaudeAdapter().parse(write(CLAUDE_STREAM[:3] + [err]))
        self.assertTrue(r.is_error)
        self.assertEqual(r.error, "result flagged is_error")

    def test_claude_error_preserves_subtype_result_and_errors(self):
        err = dict(CLAUDE_STREAM[-1], subtype="error_during_execution", is_error=True,
                   result="Request failed", errors=["connection reset"])
        r = ClaudeAdapter().parse(write(CLAUDE_STREAM[:3] + [err]))
        self.assertEqual(r.error, "error_during_execution: Request failed: connection reset")

    def test_claude_full_error_details_keep_signals_beyond_the_summary(self):
        for signal in ("Usage limit reached", "No conversation found",
                       "flagged for possible cybersecurity risk"):
            with self.subTest(signal=signal):
                err = dict(CLAUDE_STREAM[-1], subtype="error_during_execution", is_error=True,
                           result="Long harmless provider context. " * 20,
                           errors=["Long harmless error context. " * 20, signal])
                r = ClaudeAdapter().parse(write([err]))
                self.assertEqual(len(r.error), 300)
                self.assertNotIn(signal, r.error)
                self.assertTrue(r.error_details.endswith(signal))

    def test_codex_argv_puts_root_options_before_exec(self):
        a = CodexAdapter(profile="p1")
        start = a.start_argv("", Path("/cwd"), [Path("/w")])
        self.assertEqual(start[:5], ["codex", "--profile", "p1", "-C", "/cwd"])
        self.assertEqual(start[-4:], ["exec", "--json", "--skip-git-repo-check", "-"])
        resume = a.resume_argv("t-1", Path("/cwd"), [])
        self.assertEqual(resume[resume.index("exec"):],
                         ["exec", "resume", "t-1", "--json", "--skip-git-repo-check", "-"])

    def test_codex_parse(self):
        r = CodexAdapter().parse(write(CODEX_STREAM))
        self.assertEqual((r.session_id, r.final_text, r.finished), ("t-1", "↻ NEXT", True))

    def test_codex_transient_error_then_completed_is_not_an_error(self):
        r = CodexAdapter().parse(write(CODEX_STREAM[:2] + [{"type": "error", "message": "reconnecting"}]
                                       + CODEX_STREAM[2:]))
        self.assertFalse(r.is_error)
        self.assertEqual(r.final_text, "↻ NEXT")

    def test_codex_failed_turn(self):
        r = CodexAdapter().parse(write(CODEX_STREAM[:2] + [{"type": "turn.failed", "error": {"message": "quota"}}]))
        self.assertTrue(r.is_error)
        self.assertEqual(r.error, "quota")


class CarrySession(unittest.TestCase):
    def test_a_session_started_under_another_account_is_carried_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            accounts = Path(tmp) / "accounts"
            old_proj = accounts / "team-a" / "projects" / "-work-agent"
            old_proj.mkdir(parents=True)
            (old_proj / "s-1.jsonl").write_text("{}\n")
            (old_proj / "s-1").mkdir()
            (old_proj / "s-1" / "sub.jsonl").write_text("{}\n")
            (accounts / "team-b").mkdir()
            env = {"HOME": tmp, "CLAUDE_CONFIG_DIR": str(accounts / "team-b")}
            a = ClaudeAdapter()
            note = a.prepare_resume("s-1", Path("/work/agent"), env)
            self.assertIn("team-a", note)
            self.assertTrue((accounts / "team-b/projects/-work-agent/s-1.jsonl").is_file())
            self.assertTrue((accounts / "team-b/projects/-work-agent/s-1/sub.jsonl").is_file())
            self.assertEqual(a.prepare_resume("s-1", Path("/work/agent"), env), "")   # already there
            self.assertEqual(a.prepare_resume("s-unknown", Path("/work/agent"), env), "")

    def test_the_newest_copy_wins_and_a_stale_one_is_replaced(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            accounts = Path(tmp) / "accounts"
            for acct, body, t in (("team-a", "old", 1000), ("team-b", "new", 2000)):
                d = accounts / acct / "projects" / "-w"
                d.mkdir(parents=True)
                f = d / "s-2.jsonl"
                f.write_text(body)
                os.utime(f, (t, t))
            a = ClaudeAdapter()
            env_a = {"HOME": tmp, "CLAUDE_CONFIG_DIR": str(accounts / "team-a")}
            self.assertIn("team-b", a.prepare_resume("s-2", Path("/w"), env_a))   # back on A: newer B wins
            self.assertEqual((accounts / "team-a/projects/-w/s-2.jsonl").read_text(), "new")
            env_b = {"HOME": tmp, "CLAUDE_CONFIG_DIR": str(accounts / "team-b")}
            self.assertEqual(a.prepare_resume("s-2", Path("/w"), env_b), "")      # B is not older


if __name__ == "__main__":
    unittest.main()


class EffortAndModel(unittest.TestCase):
    def test_codex_effort_is_a_root_override_and_model_comes_from_the_profile(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "lane.config.toml").write_text('model = "gpt-x"\nmodel_reasoning_effort = "high"\n')
            os.environ["CODEX_HOME"] = tmp
            try:
                a = CodexAdapter(profile="lane")
                argv = a.with_effort(a.start_argv("", Path("/w"), []), "low")
                self.assertEqual(argv[1:3], ["-c", 'model_reasoning_effort="low"'])
                self.assertEqual(a.profile_model(), "gpt-x")
            finally:
                del os.environ["CODEX_HOME"]

    def test_claude_effort_replaces_a_configured_one(self):
        a = ClaudeAdapter(extra_args=["--effort", "high"])
        argv = a.with_effort(a.start_argv("s", Path("/w"), []), "low")
        self.assertEqual(argv.count("--effort"), 1)
        self.assertEqual(argv[-2:], ["--effort", "low"])
