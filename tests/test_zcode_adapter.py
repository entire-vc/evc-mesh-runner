import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from mesh_runner import interactive
from mesh_runner.adapters.zcode import READ_ONLY_DENIED, ZCodeAdapter

SID = "sess_00000000-0000-4000-8000-000000000001"
# Shapes recorded from zcode 0.17.1 (source v3.18.2), payloads trimmed
STARTED = {"type": "turn.started", "sessionId": SID, "payload": {"turnNumber": 1}}
STREAMING = {"type": "model.streaming", "sessionId": SID,
             "payload": {"delta": "PINE", "done": False, "kind": "text_delta"}}
TOOL = {"type": "tool.updated", "sessionId": SID, "payload": {"toolName": "Write", "status": "tool_started"}}
COMPLETED = {"type": "turn.completed", "sessionId": SID,
             "payload": {"response": "↻ NEXT", "resultType": "success", "usage": {"outputTokens": 81}}}
RESULT = {"type": "result", "sessionId": SID, "traceId": "t", "turnId": "turn_1", "response": "↻ NEXT",
          "usage": {"outputTokens": 81}, "eventCount": 4, "projection": {"status": "idle", "turnCount": 1}}
RETRY_502 = {"type": "session.updated", "sessionId": SID, "payload": {
    "type": "model_request_failed", "statusCode": 502, "providerErrorCode": "502",
    "providerErrorMessage": "upstream unavailable", "retryable": True, "reason": "server_error"}}


def failed(code, status, reason, message):
    return [STARTED,
            {"type": "session.updated", "sessionId": SID, "payload": {
                "type": "model_request_failed", "statusCode": status, "providerErrorCode": code,
                "providerErrorMessage": message, "retryable": False, "reason": reason}},
            {"type": "turn.failed", "sessionId": SID, "payload": {"error": {
                "type": "unknown_error", "code": code or "provider_not_configured", "message": message,
                "attribution": {"source": "provider", "reason": reason, "statusCode": status,
                                "providerErrorCode": code, "retryable": False}}}}]


def write(lines, stderr: str = "") -> Path:
    d = Path(tempfile.mkdtemp())
    log = d / "turn.jsonl"
    log.write_bytes(b"".join(json.dumps(x).encode() + b"\n" for x in lines))
    if stderr:
        log.with_suffix(".stderr").write_text(stderr)
    return log


class ZCodeArgv(unittest.TestCase):
    def test_start_and_resume(self):
        a = ZCodeAdapter("zcode", ["--workflow-mode", "disabled"])
        start = a.start_argv("", Path("/p"), [Path("/w1")])
        self.assertEqual(start[:2], ["/bin/sh", "-c"])
        self.assertEqual(start[start.index("--mode") + 1], "yolo")
        self.assertEqual(start[start.index("--cwd") + 1], "/p")
        self.assertEqual(start[start.index("--output-format") + 1], "stream-json")
        self.assertNotIn("--resume", start)
        resume = a.resume_argv(SID, Path("/p"), [])
        self.assertEqual(resume[resume.index("--resume") + 1], SID)
        self.assertEqual(resume[-2:], ["--workflow-mode", "disabled"])

    def test_prompt_reaches_the_cli_as_one_argument(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "zcode"
            fake.write_text("#!/bin/sh\nfor a in \"$@\"; do printf '%s\\0' \"$a\"; done\n")
            fake.chmod(0o755)
            argv = ZCodeAdapter(str(fake)).start_argv("", Path(d), [])
            out = subprocess.run(argv, input="- dash\nи кириллица\n".encode(), capture_output=True, check=True).stdout
            self.assertEqual(out.split(b"\0")[:-1][-1], "--prompt=- dash\nи кириллица".encode())

    def test_read_only_removes_every_writing_tool_once(self):
        a = ZCodeAdapter("zcode", ["--disallowed-tools", "Grep"])
        argv = a.with_read_only(a.start_argv("", Path("/p"), []))
        self.assertEqual(argv.count("--disallowed-tools"), 1)
        denied = argv[argv.index("--disallowed-tools") + 1].split()
        for tool in ("Bash", "Write", "Edit", "ApplyPatch"):
            self.assertIn(tool, denied)
        self.assertEqual(tuple(denied), READ_ONLY_DENIED)
        self.assertEqual(argv[argv.index("--mode") + 1], "yolo")      # MCP stays usable

    def test_headless_in_every_launch_mode(self):
        a = ZCodeAdapter()
        self.assertIs(interactive.wrap(a, "agent", "interactive", Path(tempfile.mkdtemp()), "tmux", 2), a)


class ZCodeParse(unittest.TestCase):
    def test_completed_turn(self):
        r = ZCodeAdapter().parse(write([STARTED, RETRY_502, STREAMING, TOOL, COMPLETED, RESULT]))
        self.assertTrue(r.finished)
        self.assertFalse(r.is_error)                 # a retried 502 did not end the turn
        self.assertEqual(r.session_id, SID)
        self.assertEqual(r.final_text, "↻ NEXT")
        self.assertEqual(r.usage, {"outputTokens": 81, "output_tokens": 81})

    def test_usage_is_read_by_the_completion_line(self):
        # the runner's completion line read 0.0k tok for every ZCode card,
        # it looks for the Anthropic-style keys; the provider's own camelCase keys stay as they are
        usage = {"source": "provider", "modelRequestCount": 7, "inputTokens": 352406, "outputTokens": 3209,
                 "totalTokens": 355615, "cacheReadTokens": 346368, "cacheWriteTokens": 0}
        r = ZCodeAdapter().parse(write([STARTED, COMPLETED, dict(RESULT, usage=usage)]))
        self.assertEqual((r.usage["input_tokens"], r.usage["output_tokens"], r.usage["cache_read_input_tokens"],
                          r.usage["cache_creation_input_tokens"]), (352406, 3209, 346368, 0))
        self.assertEqual(r.usage["inputTokens"], 352406)

    def test_completion_line_counts_zcode_usage(self):
        # the line keeps its long-standing shape (input/output/cache-read); it read 0.0k for ZCode
        import types
        from mesh_runner.runner import AgentRunner
        usage = {"inputTokens": 352406, "outputTokens": 3209, "cacheReadTokens": 346368}
        log = write([STARTED, COMPLETED, dict(RESULT, usage=usage)])
        attempt_id = "a" * 8
        log.rename(log.parent / f"{attempt_id}.turn1.zcode.jsonl")
        fake = types.SimpleNamespace(log_dir=lambda: log.parent, adapter_for=lambda h: ZCodeAdapter(),
                                     launch_mode="interactive")
        a = types.SimpleNamespace(id=attempt_id, harness="zcode", created_at=None, session_id=SID, turns=1)
        line = AgentRunner._completion_line(fake, a)
        self.assertIn("out 3.2k tok, cache_rd 0.3M", line)

    def test_cut_off_turn_keeps_the_session(self):
        r = ZCodeAdapter().parse(write([STARTED, STREAMING]))
        self.assertFalse(r.finished)
        self.assertEqual(r.session_id, SID)

    def test_plan_limit_reads_as_a_quota_error(self):
        from mesh_runner.runner import QUOTA_HINT
        # the business code alone decides: no status, no reason, the provider's Chinese text
        for code in ("1308", "1310"):
            r = ZCodeAdapter().parse(write(failed(code, None, "",
                                                  f"[{code}][您的限额将在 2026-11-14 18:50:42 重置。][rid]"),
                                           "Error: Turn execution failed (traceId: x)\n"))
            self.assertTrue(r.finished and r.is_error)
            self.assertRegex(r.error, QUOTA_HINT)
            self.assertIn(code, r.error)
            self.assertFalse(r.auth_failed)

    def test_other_provider_error_is_not_a_quota(self):
        from mesh_runner.runner import QUOTA_HINT
        r = ZCodeAdapter().parse(write(failed("1211", 400, "invalid_request",
                                              "[1211][Unknown Model, please check the model code.][rid]")))
        self.assertTrue(r.is_error)
        self.assertNotRegex(r.error, QUOTA_HINT)
        self.assertIn("1211", r.error)

    def test_rejected_key_is_an_auth_failure(self):
        r = ZCodeAdapter().parse(write(failed(None, 401, "auth_failed", "Provider authentication failed.")))
        self.assertTrue(r.is_error and r.auth_failed and r.auth_only)

    def test_unknown_session_from_stderr(self):
        from mesh_runner.runner import NO_SESSION_HINT
        r = ZCodeAdapter().parse(write([], "ZCode Built-in skipped (not-due)\n"
                                           "Error: Session not found: sess_x (traceId: t)\n"))
        self.assertTrue(r.finished and r.is_error)
        self.assertRegex(r.error, NO_SESSION_HINT)


if __name__ == "__main__":
    unittest.main()
