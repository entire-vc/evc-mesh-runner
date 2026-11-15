import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from mesh_runner import interactive
from mesh_runner.adapters.kimi import KimiAdapter

STREAM = [
    {"role": "meta", "type": "system.version", "version": "2.6.1"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"type": "function", "id": "c1", "function": {"name": "Bash", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    {"role": "assistant", "content": "⏳ WAIT 2h"},
    {"role": "meta", "type": "session.resume_hint", "session_id": "ses_abc", "command": "kimi -r ses_abc",
     "content": "To resume this session: kimi -r ses_abc"},
]


def write(lines, stderr: str = "") -> Path:
    d = Path(tempfile.mkdtemp())
    log = d / "turn.jsonl"
    log.write_bytes(b"".join(json.dumps(x).encode() + b"\n" for x in lines))
    if stderr:
        log.with_suffix(".stderr").write_text(stderr)
    return log


class KimiArgv(unittest.TestCase):
    def test_start_reads_the_prompt_from_stdin_into_one_argument(self):
        a = KimiAdapter("kimi", ["--model", "k"])
        argv = a.start_argv("", Path("/p"), [Path("/w1")])
        self.assertEqual(argv[:2], ["/bin/sh", "-c"])
        self.assertEqual(argv[3], "kimi")
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        self.assertEqual(argv[argv.index("--add-dir") + 1], "/w1")
        self.assertFalse(any(x.startswith("--session") for x in argv))

    def test_resume_names_the_session_in_one_token(self):
        argv = KimiAdapter().resume_argv("ses_abc", Path("/p"), [])
        self.assertIn("--session=ses_abc", argv)

    def test_the_shell_passes_a_dash_prompt_and_newlines_intact(self):
        """The real shell line: a prompt starting with a dash stays the --prompt value."""
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "kimi"
            fake.write_text("#!/bin/sh\nfor a in \"$@\"; do printf '%s\\0' \"$a\"; done\n")
            fake.chmod(0o755)
            argv = KimiAdapter(str(fake)).resume_argv("ses_1", Path(d), [])
            out = subprocess.run(argv, input=b"-x first\nsecond line\n", capture_output=True, check=True).stdout
            args = out.split(b"\0")[:-1]
            self.assertEqual(args[-1], b"--prompt=-x first\nsecond line")
            self.assertIn(b"--session=ses_1", args)

    def test_model_override_replaces_the_configured_one(self):
        a = KimiAdapter("kimi", ["-m", "old", "--model=older"])
        argv = a.with_model(a.start_argv("", Path("/p"), []), "k3.1")
        self.assertEqual([x for x in argv if x.startswith(("--model", "-m"))], ["--model"])
        self.assertEqual(argv[-2:], ["--model", "k3.1"])

    def test_no_per_turn_read_only_and_no_interactive_wrapper(self):
        a = KimiAdapter()
        with self.assertRaises(NotImplementedError):
            a.with_read_only(a.start_argv("", Path("/p"), []))
        wrapped = interactive.wrap(a, "agent", "interactive", Path(tempfile.mkdtemp()), "tmux", 2)
        self.assertIs(wrapped, a)       # print mode only: same subscription, no terminal needed


class KimiParse(unittest.TestCase):
    def test_finished_turn_has_session_and_last_text(self):
        r = KimiAdapter().parse(write(STREAM))
        self.assertTrue(r.finished)
        self.assertFalse(r.is_error)
        self.assertEqual(r.session_id, "ses_abc")
        self.assertEqual(r.final_text, "⏳ WAIT 2h")

    def test_cut_off_turn_is_not_finished(self):
        r = KimiAdapter().parse(write(STREAM[:-1]))
        self.assertFalse(r.finished)
        self.assertEqual(r.session_id, "")
        self.assertEqual(r.final_text, "⏳ WAIT 2h")

    def test_failed_turn_reads_the_stderr_line(self):
        r = KimiAdapter().parse(write(STREAM[:1], "error: failed to run prompt: provider.connection_error: reset\n"))
        self.assertTrue(r.finished and r.is_error)
        self.assertEqual(r.error, "provider.connection_error: reset")
        self.assertFalse(r.auth_failed)

    def test_provider_limit_is_named_in_the_runners_words(self):
        from mesh_runner.runner import QUOTA_HINT
        for line in ("provider.api_error: Your membership allowance for this month is used up",
                     "provider.rate_limit: slow down", "provider.api_error: HTTP 429"):
            r = KimiAdapter().parse(write([], f"error: failed to run prompt: {line}\n"))
            self.assertTrue(r.is_error)
            self.assertRegex(r.error, QUOTA_HINT, line)

    def test_missing_login_is_an_auth_failure(self):
        r = KimiAdapter().parse(write(STREAM[:1], "error: failed to run prompt: No model configured. Run `kimi` "
                                                    "and use /login to sign in, then retry\n"))
        self.assertTrue(r.auth_failed and r.auth_only)

    def test_unknown_session_reads_as_a_missing_session(self):
        from mesh_runner.runner import NO_SESSION_HINT
        r = KimiAdapter().parse(write([], 'error: failed to run prompt: Session "ses_x" not found.\n'))
        self.assertRegex(r.error, NO_SESSION_HINT)

    def test_session_occupied_envelope_is_an_error(self):
        r = KimiAdapter().parse(write([{"type": "error", "code": "session.occupied", "message": "busy"}]))
        self.assertTrue(r.is_error)
        self.assertIn("session.occupied", r.error)


class KimiSessionKnown(unittest.TestCase):
    def test_reads_the_session_directory_of_the_kimi_home(self):
        with tempfile.TemporaryDirectory() as d:
            env = {"KIMI_CODE_HOME": d}
            a = KimiAdapter()
            self.assertTrue(a.session_known("ses_1", Path("/p"), env))      # no sessions dir: cannot tell
            (Path(d) / "sessions" / "wd_x_1234").mkdir(parents=True)
            self.assertFalse(a.session_known("ses_1", Path("/p"), env))
            (Path(d) / "sessions" / "wd_x_1234" / "ses_1").mkdir()
            self.assertTrue(a.session_known("ses_1", Path("/p"), env))
            self.assertTrue(a.session_known("../x", Path("/p"), env))       # never a path outside the home


# A recorded Kimi 2.6.1 journal (agents/main/wire.jsonl) of one session with two turns, reduced to an
# allowlist of fields: event types, turn ids, usage counters; prompt and tool text replaced.
WIRE = Path(__file__).parent / "fixtures" / "kimi_wire_two_turns.jsonl"
SESSION = "session_68b69566-19dc-428b-9eb9-5376c55e44d0"
TURN0 = {"input_tokens": 11608, "output_tokens": 3926, "cache_read_input_tokens": 103168,
         "cache_creation_input_tokens": 0}
TURN1 = {"input_tokens": 57838, "output_tokens": 4439, "cache_read_input_tokens": 297728,
         "cache_creation_input_tokens": 0}
HINT = {"role": "meta", "type": "session.resume_hint", "session_id": SESSION}


def wire_parts() -> tuple[bytes, bytes]:
    """The journal as each of its two invocations wrote it: turn 0 (with the session header), turn 1."""
    lines = WIRE.read_bytes().splitlines(keepends=True)
    second = next(i for i, raw in enumerate(lines)
                  if json.loads(raw)["type"] == "turn.prompt" and json.loads(raw)["turnId"] == 1)
    return b"".join(lines[:second]), b"".join(lines[second:])


class KimiUsage(unittest.TestCase):
    """Print mode streams no usage: the adapter reads its own turn from the session's journal."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.home = self.dir / "kimi-home"
        self.journal = self.home / "sessions" / "wd_lane_508d14361961" / SESSION / "agents" / "main" / "wire.jsonl"
        self.logs = self.dir / "logs"
        # the CLI as far as the runner sees it: appends its turn to the journal, prints its stream
        self.kimi = self.dir / "kimi"
        self.kimi.write_text('#!/bin/sh\nmkdir -p "$(dirname "$FAKE_JOURNAL")"\n'
                             'cat "$FAKE_TURN" >> "$FAKE_JOURNAL"\ncat "$FAKE_STREAM"\n')
        self.kimi.chmod(0o755)

    def run_turn(self, n: int, journal_part: bytes, stream: list, session: str = "", attempt: str = "a" * 8) -> Path:
        from mesh_runner.adapters.base import Launch
        part, out = self.dir / f"part{n}", self.dir / f"stream{n}"
        part.write_bytes(journal_part)
        out.write_bytes(b"".join(json.dumps(x).encode() + b"\n" for x in stream))
        a = KimiAdapter(str(self.kimi))
        argv = a.resume_argv(session, self.dir, []) if session else a.start_argv("", self.dir, [])
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "KIMI_CODE_HOME": str(self.home),
               "FAKE_JOURNAL": str(self.journal), "FAKE_TURN": str(part), "FAKE_STREAM": str(out)}
        log = self.logs / f"{attempt}.turn{n}.kimi.jsonl"
        a.spawn(Launch(argv, self.dir, env, "prompt"), log).wait(timeout=30)
        return log

    def two_turns(self) -> tuple[Path, Path]:
        first, second = wire_parts()
        log1 = self.run_turn(1, first, STREAM[1:-1] + [HINT])
        log2 = self.run_turn(2, second, STREAM[1:-1] + [HINT], session=SESSION)
        return log1, log2

    def test_each_turn_reads_only_its_own_kimi_turn(self):
        log1, log2 = self.two_turns()
        # read after both turns ran: the journal already holds the next turn, each log still gets its own
        self.assertEqual(KimiAdapter().parse(log1).usage, TURN0)
        self.assertEqual(KimiAdapter().parse(log2).usage, TURN1)

    def test_completion_line_counts_the_session_once(self):
        import types
        from mesh_runner.runner import AgentRunner
        self.two_turns()
        fake = types.SimpleNamespace(log_dir=lambda: self.logs, adapter_for=lambda h: KimiAdapter(),
                                     launch_mode="interactive")
        a = types.SimpleNamespace(id="a" * 8, harness="kimi", created_at=None, session_id=SESSION, turns=2)
        line = AgentRunner._completion_line(fake, a)
        out = TURN0["output_tokens"] + TURN1["output_tokens"]                    # 8365
        cache = TURN0["cache_read_input_tokens"] + TURN1["cache_read_input_tokens"]  # 400896
        self.assertIn(f"out {out / 1000:.1f}k tok, cache_rd {cache / 1e6:.1f}M", line)
        self.assertIn("out 8.4k tok, cache_rd 0.4M", line)

    def test_a_later_attempt_on_the_same_session_counts_only_its_own_turns(self):
        # a card returned to the lane continues its earlier session in a new attempt: that attempt's
        # completion line holds its own turn, not the whole session again
        import types
        from mesh_runner.runner import AgentRunner
        first, second = wire_parts()
        self.run_turn(1, first, STREAM[1:-1] + [HINT], attempt="a" * 8)
        self.run_turn(1, second, STREAM[1:-1] + [HINT], session=SESSION, attempt="b" * 8)
        fake = types.SimpleNamespace(log_dir=lambda: self.logs, adapter_for=lambda h: KimiAdapter(),
                                     launch_mode="interactive", env={"KIMI_CODE_HOME": str(self.home)})
        later = types.SimpleNamespace(id="b" * 8, harness="kimi", created_at=None, session_id=SESSION, turns=1)
        self.assertIn("out 4.4k tok, cache_rd 0.3M", AgentRunner._completion_line(fake, later))

    def test_an_id_that_is_not_a_kimi_session_id_finds_no_journal(self):
        from mesh_runner.adapters.kimi import _journal
        self.two_turns()
        self.assertEqual(_journal(self.home, SESSION), self.journal)
        for sid in ("", "session_other", "../x", ".hidden", "*", "session_*", "**/" + SESSION,
                    "[s]" + SESSION[1:], SESSION[:-1] + "?"):
            self.assertIsNone(_journal(self.home, sid), sid)

    def test_a_turn_refused_before_its_prompt_takes_nothing(self):
        # session busy: the CLI wrote no turn; the next invocation's turn lies after the same mark
        first, second = wire_parts()
        self.run_turn(1, first, STREAM[1:-1] + [HINT])
        refused = self.run_turn(2, b"", [{"type": "error", "code": "session.occupied", "message": "busy"}],
                                session=SESSION)
        self.run_turn(3, second, STREAM[1:-1] + [HINT], session=SESSION)
        r = KimiAdapter().parse(refused)
        self.assertTrue(r.is_error)
        self.assertEqual(r.usage, {})

    def test_cut_off_turn_counts_what_its_journal_recorded(self):
        first, _ = wire_parts()
        lines = first.splitlines(keepends=True)
        ended = next(i for i, raw in enumerate(lines) if json.loads(raw)["type"] == "turn.ended")
        log = self.run_turn(1, b"".join(lines[:ended]), STREAM[1:-1])     # killed: no turn.ended, no hint
        r = KimiAdapter().parse(log)
        self.assertFalse(r.finished)
        self.assertEqual(r.usage, {})       # a new session whose id never reached the stream: unknown

    def test_cut_off_resumed_turn_counts_what_its_journal_recorded(self):
        first, second = wire_parts()
        self.run_turn(1, first, STREAM[1:-1] + [HINT])
        lines = second.splitlines(keepends=True)
        ended = next(i for i, raw in enumerate(lines) if json.loads(raw)["type"] == "turn.ended")
        log = self.run_turn(2, b"".join(lines[:ended]), STREAM[1:-1], session=SESSION)
        r = KimiAdapter().parse(log)
        self.assertFalse(r.finished)
        self.assertEqual(r.usage, TURN1)    # every step it recorded before the cut

    def test_unknown_without_a_mark_or_a_journal(self):
        self.assertEqual(KimiAdapter().parse(write(STREAM)).usage, {})        # no mark (a log from before)
        log1, log2 = self.two_turns()
        self.journal.unlink()
        self.assertEqual(KimiAdapter().parse(log2).usage, {})
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        self.journal.write_bytes(b"")                                        # replaced, shorter than the mark
        self.assertEqual(KimiAdapter().parse(log2).usage, {})

    def test_a_journal_replaced_by_a_longer_one_is_not_read(self):
        # another journal under the same path, at least as long as the mark: its turns are not ours
        _, log2 = self.two_turns()
        whole = self.journal.read_bytes()
        other = self.journal.with_name("other.jsonl")
        other.write_bytes(whole.replace(b'"inputOther":', b'"inputOther" :') + whole)   # longer, other bytes
        other.replace(self.journal)                                                      # new inode
        self.assertEqual(KimiAdapter().parse(log2).usage, {})

    def test_a_journal_rewritten_in_place_is_not_read(self):
        # same file (same inode), rewritten with other bytes before the mark and as long or longer
        _, log2 = self.two_turns()
        whole = self.journal.read_bytes()
        mark = json.loads(log2.with_suffix(".wire").read_text())
        rewritten = whole[:mark["offset"] - 2].replace(b'"agentId":"main"', b'"agentId":"mAin"') \
            + whole[mark["offset"] - 2:]
        self.assertEqual(len(rewritten), len(whole))
        with open(self.journal, "r+b") as fh:
            fh.write(rewritten)
        self.assertEqual(KimiAdapter().parse(log2).usage, {})
        with open(self.journal, "r+b") as fh:          # the original bytes back: read again
            fh.write(whole)
        self.assertEqual(KimiAdapter().parse(log2).usage, TURN1)

    def test_a_mark_of_another_session_is_not_read(self):
        _, log2 = self.two_turns()
        mark = json.loads(log2.with_suffix(".wire").read_text())
        self.assertEqual((mark["home"], mark["session"]), (str(self.home), SESSION))
        log2.with_suffix(".wire").write_text(json.dumps(dict(mark, session="session_other")))
        self.assertEqual(KimiAdapter().parse(log2).usage, {})


if __name__ == "__main__":
    unittest.main()
