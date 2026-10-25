"""Interactive launch mode: the driver feeds a live interactive session in tmux.

The harnesses here are fakes that behave like the interactive programs where it matters:
a raw terminal with bracketed paste, Claude Code's command hooks from --settings
(SessionStart, UserPromptSubmit, Stop) and transcript file, Codex's ``notify`` program.
Everything runs on a private tmux server (-L), never the host's sessions.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from mesh_runner import interactive
from mesh_runner.adapters.base import Adapter, Launch
from mesh_runner.adapters.claude import ClaudeAdapter
from mesh_runner.adapters.codex import CodexAdapter
from mesh_runner.config import ConfigError, load
from mesh_runner.store import ACTIVE, OPEN, Store
from mesh_runner.tui_driver import Driver

TMUX = shutil.which("tmux")

FAKE_HARNESS = r'''
import json, os, subprocess, sys, termios, tty, time, uuid
args = sys.argv[1:]
kind = os.environ["FAKE_KIND"]
root = os.environ["FAKE_ROOT"]
with open(os.path.join(root, "starts"), "a") as fh:
    fh.write(json.dumps(args) + "\n")
with open(os.path.join(root, "envs"), "a") as fh:
    fh.write(json.dumps(sorted(k for k in os.environ if k.startswith("CLAUDE"))) + "\n")
if os.environ.get("FAKE_DIE"):
    print("fatal: cannot start"); sys.stdout.flush(); sys.exit(3)
hooks, notify, sid = {}, None, ""
for i, a in enumerate(args):
    if a == "--settings":
        hooks = json.load(open(args[i + 1])).get("hooks", {})
    if a in ("--session-id", "--resume"):
        sid = args[i + 1]
    if a == "-c" and args[i + 1].startswith("notify="):
        notify = json.loads(args[i + 1][len("notify="):])
    if a == "resume":
        sid = args[i + 1]
sid = sid or str(uuid.uuid4())
said = []
transcript = os.path.join(root, f"{sid}.jsonl")

def hook(name, **payload):
    for entry in hooks.get(name, []):
        for h in entry.get("hooks", []):
            subprocess.run(h["command"], shell=True, input=json.dumps(
                {"hook_event_name": name, "session_id": sid, "transcript_path": transcript, **payload}), text=True)

rollout = None
if kind == "codex" and os.environ.get("FAKE_ROLLOUT"):     # write the session's rollout the way Codex does
    day = os.path.join(os.environ["CODEX_HOME"], "sessions", "2026", "10", "23")
    os.makedirs(day, exist_ok=True)
    rollout = os.path.join(day, "rollout-2026-10-23T09-00-00-%s.jsonl" % sid)

def note(rec):
    with open(rollout, "a") as fh:
        fh.write(json.dumps(rec) + "\n")

if rollout:                                                  # an earlier turn of this thread, already ended
    note({"type": "session_meta", "payload": {"id": sid}})
    note({"type": "response_item", "payload": {"type": "message", "role": "assistant"}})
    note({"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "old"}})

def say(prompt):
    reply = os.environ.get("FAKE_REPLY", "⏳ WAIT 2h: fake") + f" [{prompt}]"
    if kind == "claude":
        pid = "" if os.environ.get("FAKE_NO_IDS") else str(uuid.uuid4())
        recorded = '\n\n<pasted_content id="8d5d">\n' + prompt + '\n</pasted_content>'   # how Claude Code records a paste
        hook("UserPromptSubmit", prompt=recorded, prompt_id=pid)
        if os.environ.get("FAKE_FAIL"):          # an API failure ends the turn, without prompt_id
            sys.stdout.write("API Error: 429 rate_limit_error\r\n"); sys.stdout.flush()
            hook("StopFailure", error="rate_limit_error: usage limit reached")
            return
        if os.environ.get("FAKE_BANNER"):        # a limit banner and no hook at all
            sys.stdout.write("\x1b[2J\x1b[HYou've hit your usage limit · resets 5pm\r\n> \r\n"); sys.stdout.flush()
            return
        if os.environ.get("FAKE_BLOCKED"):       # a Stop hook blocks the stop: Claude goes on
            with open(transcript, "a") as fh:
                fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n")
            hook("Stop", last_assistant_message="partial", prompt_id=pid)
            sys.stdout.write("✻ Working… (esc to interrupt)\r\n"); sys.stdout.flush()
            time.sleep(3)
            with open(transcript, "a") as fh:
                fh.write(json.dumps({"type": "assistant", "message": {"id": "late", "model": "fake",
                    "content": [{"type": "text", "text": "final answer"}], "usage": {"output_tokens": 4}}}) + "\n")
            sys.stdout.write("\x1b[2J\x1b[H> \r\n"); sys.stdout.flush()
            hook("Stop", last_assistant_message="final answer", prompt_id=pid)
            return
        if os.environ.get("FAKE_SLOW"):          # a long think / tool: the screen is busy, the transcript silent
            sys.stdout.write("\x1b[2J\x1b[H✻ Working… (esc to interrupt)\r\n"); sys.stdout.flush()
            time.sleep(float(os.environ["FAKE_SLOW"]))
            sys.stdout.write("\x1b[2J\x1b[H> \r\n"); sys.stdout.flush()
        with open(transcript, "a") as fh:
            fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": recorded}}) + "\n")
            for n in (1, 2):
                fh.write(json.dumps({"type": "assistant", "message": {"id": "m%d-%s" % (n, len(prompt)), "model": "fake",
                    "content": [{"type": "text", "text": reply}],
                    "usage": {"input_tokens": 3, "cache_read_input_tokens": 100, "output_tokens": 7}}}) + "\n")
        if os.environ.get("FAKE_STALE_STOP"):     # a cut-off earlier turn reports late
            hook("Stop", last_assistant_message="OLD TURN", prompt_id="" if os.environ.get("FAKE_NO_IDS") else "old-prompt")
        hook("Stop", last_assistant_message=reply, prompt_id=pid)
        if os.environ.get("FAKE_BG"):            # a background task finishes: Claude goes on alone
            sys.stdout.write("\x1b[2J\x1b[H✽ Forging… (3s · ↓ 12 tokens)\r\n"); sys.stdout.flush()
            time.sleep(float(os.environ["FAKE_BG"]))
            with open(transcript, "a") as fh:
                fh.write(json.dumps({"type": "assistant", "message": {"id": "bg-%s" % pid, "model": "fake",
                    "content": [{"type": "text", "text": "background result read"}], "usage": {"output_tokens": 2}}}) + "\n")
            sys.stdout.write("\x1b[2J\x1b[H✻ Sautéed for 3s\r\n> \r\n"); sys.stdout.flush()
    else:
        print("Working (1s • esc to interrupt)"); sys.stdout.flush()
        if rollout and os.environ["FAKE_ROLLOUT"] == "silent":   # busy, writes nothing (the test writes the end)
            time.sleep(1.5)
            sys.stdout.write("\x1b[2J\x1b[H› Ask Codex to do anything\r\n"); sys.stdout.flush()
            return
        turn = "turn-%d" % (len(said) + 1)

        def quoted_title():
            # Codex titles the thread in a side task of its own thread and turn: its one input message
            # quotes our prompt, and it notifies within seconds, while our turn is still running
            if os.environ.get("FAKE_TITLE") != "quoted":
                return
            subprocess.run([*notify, json.dumps({"type": "agent-turn-complete", "thread-id": "title-thread",
                            "turn-id": "title-turn", "input-messages": [
                                "Generate a concise, single-line task title. Do not answer the request.\n\n"
                                "User prompt:\n" + prompt],
                            "last-assistant-message": '{"title":"Fix the lint"}'})])
            time.sleep(2.0)
        if not rollout:
            quoted_title()
        if rollout:
            time.sleep(1.0)
            note({"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}})
            note({"type": "response_item", "payload": {"type": "message", "role": "user"}})
            note({"type": "event_msg", "payload": {"type": "token_count"}})
            quoted_title()
            if os.environ["FAKE_ROLLOUT"] == "empty":      # the task ends with no model record and no notify
                note({"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": None,
                      "error": {"message": "stream disconnected", "codex_error_info": "other"}}})
                sys.stdout.write("\x1b[2J\x1b[H› Ask Codex to do anything\r\n"); sys.stdout.flush()
                return
            note({"type": "response_item", "payload": {"type": "reasoning"}})
            note({"type": "response_item", "payload": {"type": "message", "role": "assistant"}})
            if os.environ["FAKE_ROLLOUT"] == "errored":     # model work, then the API fails: no final message, no notify
                note({"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": None,
                      "error": {"message": "at capacity", "codex_error_info": "server_overloaded"}}})
                sys.stdout.write("\x1b[2J\x1b[H› Ask Codex to do anything\r\n"); sys.stdout.flush()
                return
            note({"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": reply}})
            if os.environ["FAKE_ROLLOUT"] == "nonotify":    # the end is in the rollout only
                sys.stdout.write("\x1b[2J\x1b[H› Ask Codex to do anything\r\n"); sys.stdout.flush()
                return
            time.sleep(1.0)
        if os.environ.get("FAKE_TITLE") == "1":  # a side task in its own thread notifies first
            subprocess.run([*notify, json.dumps({"type": "agent-turn-complete", "thread-id": "title-thread",
                            "input-messages": ["Generate a concise title"], "last-assistant-message": "{}"})])
        said.append(prompt)                      # Codex lists every user message of the thread
        subprocess.run([*notify, json.dumps({"type": "agent-turn-complete", "thread-id": sid, "turn-id": turn,
                        "input-messages": [] if os.environ.get("FAKE_BARE") else list(said),
                        "last-assistant-message": reply})])
        sys.stdout.write("\x1b[2J\x1b[H› Ask Codex to do anything\r\n"); sys.stdout.flush()
    print("done:", reply); sys.stdout.flush()

fd = sys.stdin.fileno()
tty.setraw(fd)
if os.environ.get("FAKE_TRUST"):                # Claude's first-run question, "No" preselected
    choice = 0
    while True:
        opts = ["No, exit", "Yes, I trust this folder"]
        sys.stdout.write("\x1b[2J\x1b[HQuick safety check: Is this a project you created or one you trust?\r\n")
        for i, o in enumerate(opts):
            sys.stdout.write(("❯ " if i == choice else "  ") + o + "\r\n")
        sys.stdout.flush()
        key = os.read(fd, 8)
        if key in (b"\r", b"\n"):
            if choice == 0:
                sys.exit(1)
            break
        if key.endswith(b"B"):
            choice = 1
sys.stdout.write("\x1b[?2004h\r\n" + ("› Ask Codex to do anything\r\n" if kind == "codex" else "> \r\n"))
sys.stdout.flush()
if kind == "codex" and os.environ.get("FAKE_UPDATE"):   # Codex's startup update menu
    choice = 0
    while True:
        opts = ["1. Update now", "2. Skip", "3. Skip until next version"]
        sys.stdout.write("\x1b[2J\x1b[HUpdate available! 0.159 -> 0.163\r\n")
        for i, o in enumerate(opts):
            sys.stdout.write(("› " if i == choice else "  ") + o + "\r\n")
        sys.stdout.flush()
        key = os.read(fd, 8)
        if key in (b"\r", b"\n"):
            if choice == 0:
                print("updating..."); sys.stdout.flush(); time.sleep(30); sys.exit(0)
            break
        if key.endswith(b"B"):
            choice = min(choice + 1, 2)
    sys.stdout.write("\x1b[2J\x1b[H› Ask Codex to do anything\r\n"); sys.stdout.flush()
if kind == "codex" and os.environ.get("FAKE_BOOTSTRAP_FAIL"):   # Codex's account check timing out
    if len(open(os.path.join(root, "starts")).read().splitlines()) <= int(os.environ["FAKE_BOOTSTRAP_FAIL"]):
        time.sleep(1.5)
        sys.stdout.write("\r\nError: account/read failed during TUI bootstrap: account/read failed: "
                         "workspace routing discovery timed out (code -32603)\r\n"); sys.stdout.flush()
        sys.exit(1)
if kind == "claude":
    hook("SessionStart", source="resume" if "--resume" in args else "startup")
buf, pasting, data = "", False, b""
while True:
    ch = os.read(fd, 1)
    if not ch:
        break
    data += ch
    if data.startswith(b"\x1b"):
        if data == b"\x1b[200~":
            pasting, data = True, b""
        elif data == b"\x1b[201~":
            pasting, data = False, b""
            sys.stdout.write("[Pasted text #1 +%d lines]\r\n" % buf.count("\r")); sys.stdout.flush()   # the TUI's paste chip
        elif len(data) >= 6 or (len(data) > 1 and data[1:2] != b"["):
            data = b""
        continue
    if pasting:
        buf += data.decode(errors="replace")
        data = b""
        continue
    if data in (b"\r", b"\n"):
        if buf and os.environ.get("FAKE_DIE_ON_PROMPT"):   # took the prompt, started, then died
            print("Working (1s • esc to interrupt)"); sys.stdout.flush(); time.sleep(1.5)
            print(buf.strip().splitlines()[-1]); sys.stdout.flush(); sys.exit(1)
        if buf:
            say(buf)
        buf = ""
    else:
        buf += data.decode(errors="replace")
    data = b""
'''


class FakeStore:
    def __init__(self, attempts):
        self.attempts = attempts

    def get(self, key):
        return self.attempts.get(key)


class A:
    def __init__(self, state, wait=None):
        self.state, self.wait = state, wait or {}


@unittest.skipUnless(TMUX, "tmux not installed")
class DriverTurns(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sock = f"mr-itest-{os.getpid()}-{id(self)}"
        self.tmux = interactive.Tmux([TMUX, "-L", self.sock, "-f", "/dev/null"])
        self.fake = self.root / "harness.py"
        self.fake.write_text(f"#!{sys.executable}\n" + FAKE_HARNESS)
        self.fake.chmod(0o755)
        self.state = self.root / "state" / "interactive" / "builder"
        self.cwd = self.root / "profile"
        self.cwd.mkdir()

    def tearDown(self):
        subprocess.run([TMUX, "-L", self.sock, "kill-server"], capture_output=True)
        self.tmp.cleanup()

    def driver(self, kind, key="att-1", sid="", mode="start", **env):
        argv = [str(self.fake)]
        if kind == "claude":
            argv += ["--resume" if mode == "resume" else "--session-id", sid]
        elif mode == "resume" and sid:
            argv += ["resume", sid]
        out = io.StringIO()
        d = Driver(harness=kind, agent="builder", state=self.state, tmux=self.tmux, max_sessions=10, cwd=self.cwd,
                   session_id=sid, mode=mode, key=key, argv=argv, out=out,
                   env={"PATH": os.environ.get("PATH", ""), "FAKE_KIND": kind, "FAKE_ROOT": str(self.root),
                        "HOME": os.environ.get("HOME", ""), **env},
                   timeouts={"ready": 15, "accept": 8, "poll": 0.1, "scan": 2, "interrupt": 2})
        return d, out

    def events(self, out):
        return [json.loads(x) for x in out.getvalue().splitlines() if x.strip()]

    def starts(self):
        p = self.root / "starts"
        return p.read_text().splitlines() if p.exists() else []

    def test_claude_turn_lands_in_a_live_session_and_parses_like_headless(self):
        d, out = self.driver("claude", sid="s-1")
        self.assertEqual(d.run("Investigate the failing build\nand report."), 0)
        evs = self.events(out)
        self.assertEqual(evs[0]["type"], "system")
        self.assertEqual(evs[-1]["type"], "result")
        log = self.root / "turn.jsonl"
        log.write_text(out.getvalue())
        r = ClaudeAdapter().parse(log)
        self.assertTrue(r.finished)
        self.assertFalse(r.is_error)
        self.assertEqual(r.session_id, "s-1")
        self.assertIn("⏳ WAIT 2h", r.final_text)
        # the whole paste arrived as one prompt (tmux sends the line break as CR inside the paste)
        self.assertRegex(r.final_text, r"Investigate the failing build[\r\n]and report\.")
        self.assertIsNone(r.cost_usd)                                      # a subscription turn has no price
        self.assertEqual(r.usage["output_tokens"], 14)                     # both messages, once each
        self.assertEqual(self.tmux.windows(interactive.session_name("builder")), {"att-1": True})
        # the next turn goes into the same live session: no second start
        d2, out2 = self.driver("claude", sid="s-1", mode="resume")
        self.assertEqual(d2.run("Second turn"), 0)
        self.assertEqual(len(self.starts()), 1)
        log.write_text(out2.getvalue())
        r2 = ClaudeAdapter().parse(log)
        self.assertIn("Second turn", r2.final_text)
        self.assertEqual(r2.usage["output_tokens"], 14)                    # only this turn's messages
        self.assertIn("--settings", json.loads(self.starts()[0]))

    def test_a_start_never_reuses_a_window_and_a_changed_command_restarts_it(self):
        d, _ = self.driver("claude", sid="s-1")
        self.assertEqual(d.run("one"), 0)
        d, _ = self.driver("claude", sid="s-2")                            # a new session for the attempt
        self.assertEqual(d.run("two"), 0)
        self.assertEqual(len(self.starts()), 2)
        d, _ = self.driver("claude", sid="s-2", mode="resume", CLAUDE_CONFIG_DIR="/other/account")
        self.assertEqual(d.run("three"), 0)                                # account switch: new process
        self.assertEqual(len(self.starts()), 3)
        self.assertIn("--resume", self.starts()[-1])

    def test_codex_turn_takes_the_thread_from_notify(self):
        d, out = self.driver("codex")
        self.assertEqual(d.run("Fix the lint"), 0)
        log = self.root / "turn.jsonl"
        log.write_text(out.getvalue())
        r = CodexAdapter().parse(log)
        self.assertTrue(r.finished)
        self.assertFalse(r.is_error)
        self.assertTrue(r.session_id)
        self.assertIn("Fix the lint", r.final_text)
        start = json.loads(self.starts()[0])
        self.assertTrue(any(x.startswith("notify=") for x in start))
        self.assertTrue(any("trust_level" in x for x in start))
        d2, _ = self.driver("codex", sid=r.session_id, mode="resume")
        self.assertEqual(d2.run("again"), 0)
        self.assertEqual(len(self.starts()), 1)

    def test_codex_start_up_exit_is_started_again_and_the_turn_lands(self):
        d, out = self.driver("codex", FAKE_BOOTSTRAP_FAIL="1")
        self.assertEqual(d.run("Fix the lint"), 0, out.getvalue()[-400:])
        log = self.root / "turn.jsonl"
        log.write_text(out.getvalue())
        r = CodexAdapter().parse(log)
        self.assertTrue(r.finished)
        self.assertFalse(r.is_error)
        self.assertIn("Fix the lint", r.final_text)
        self.assertEqual(len(self.starts()), 2)                              # the dead start, then one that lives
        self.assertEqual(self.tmux.windows(interactive.session_name("builder")), {"att-1": True})

    def test_codex_start_up_exits_give_up_after_the_retries(self):
        d, out = self.driver("codex", FAKE_BOOTSTRAP_FAIL="9")
        self.assertEqual(d.run("Fix the lint"), 1)
        self.assertEqual(len(self.starts()), 3)                              # first start + two retries
        last = self.events(out)[-1]
        self.assertEqual(last["type"], "turn.failed")
        self.assertIn("TUI bootstrap", last["error"]["message"])
        self.assertEqual(self.tmux.windows(interactive.session_name("builder")), {})

    def test_a_prompt_quoting_the_start_up_error_is_never_sent_twice(self):
        # the phrase is the prompt's last line and the process exits non-zero, but only after
        # it took the prompt: a new window could deliver it twice, so the turn fails instead
        d, out = self.driver("codex", FAKE_DIE_ON_PROMPT="1")
        prompt = "Investigate this log line:\nError: account/read failed during TUI bootstrap: timed out"
        self.assertEqual(d.run(prompt), 1)
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(self.events(out)[-1]["type"], "turn.failed")

    def test_a_harness_dying_for_another_reason_is_not_started_again(self):
        d, out = self.driver("codex", FAKE_DIE="1")
        self.assertEqual(d.run("x"), 1)
        self.assertEqual(len(self.starts()), 1)
        last = self.events(out)[-1]
        self.assertEqual(last["type"], "turn.failed")
        self.assertIn("fatal: cannot start", last["error"]["message"])

    def test_claude_folder_trust_question_is_answered_yes(self):
        d, out = self.driver("claude", sid="s-1", FAKE_TRUST="1")
        self.assertEqual(d.run("hello"), 0, out.getvalue()[-400:])
        self.assertFalse(self.events(out)[-1]["is_error"])

    def test_codex_side_task_notification_is_not_the_turn(self):
        d, out = self.driver("codex", FAKE_TITLE="1")
        self.assertEqual(d.run("Fix the lint"), 0)
        texts = [e["item"]["text"] for e in self.events(out) if e.get("type") == "item.completed"]
        self.assertEqual(len(texts), 1)
        self.assertIn("Fix the lint", texts[0])
        thread = [e["thread_id"] for e in self.events(out) if e.get("type") == "thread.started"][0]
        self.assertNotEqual(thread, "title-thread")

    def assert_title_task_is_not_the_turn(self, out, thread=None):
        evs = self.events(out)
        texts = [e["item"]["text"] for e in evs if e.get("type") == "item.completed"]
        self.assertEqual(len(texts), 1, out.getvalue()[-400:])
        self.assertNotIn('"title"', texts[0])                     # never the side task's title as the reply
        self.assertIn("WAIT 2h: fake [Fix the lint", texts[0])
        threads = [e["thread_id"] for e in evs if e.get("type") == "thread.started"]
        self.assertNotIn("title-thread", threads)
        if thread:
            self.assertEqual(threads, [thread])

    def test_codex_title_task_quoting_the_prompt_does_not_end_a_new_sessions_turn(self):
        d, out = self.driver("codex", FAKE_TITLE="quoted")
        self.assertEqual(d.run("Fix the lint"), 0)
        self.assert_title_task_is_not_the_turn(out)
        self.assertNotEqual(d.meta.get("session_id"), "title-thread")   # the next turn resumes our thread

    def test_codex_title_task_quoting_the_prompt_does_not_end_a_resumed_turn(self):
        d, out = self.driver("codex", sid=self.THREAD, mode="resume", FAKE_ROLLOUT="full", FAKE_TITLE="quoted",
                             CODEX_HOME=str(self.root / "codexhome"))
        self.assertEqual(d.run("Fix the lint"), 0)
        self.assert_title_task_is_not_the_turn(out, thread=self.THREAD)
        self.assertEqual(d.meta.get("session_id"), self.THREAD)

    def test_codex_notify_of_another_turn_or_thread_is_not_ours(self):
        d, _ = self.driver("codex", sid=self.THREAD, mode="resume")
        d.prompt = "Fix the lint\nand report."
        mine = {"type": "agent-turn-complete", "thread-id": self.THREAD, "turn-id": "t-1",
                "input-messages": ["earlier", "Fix the lint\rand report."]}
        self.assertTrue(d.ours(mine))
        # the title side task quotes our prompt after its own instructions
        self.assertFalse(d.ours({**mine, "thread-id": "title-thread", "turn-id": "t-9",
                                 "input-messages": ["Generate a title.\n\nUser prompt:\nFix the lint\nand report."]}))
        d.turn_id = "t-1"                                        # the rollout named the turn our prompt started
        self.assertTrue(d.ours(mine))
        self.assertFalse(d.ours({**mine, "turn-id": "t-2"}))
        self.assertTrue(d.ours({k: v for k, v in mine.items() if k != "turn-id"}))

    def test_codex_notify_must_carry_the_whole_prompt_not_only_its_opening(self):
        d, _ = self.driver("codex", sid=self.THREAD, mode="resume")
        opening = "[fleet] shared preamble of every runner prompt. " * 8          # > 200 characters
        d.prompt = opening + "Task A: fix the lint\nand report."
        ev = {"type": "agent-turn-complete", "thread-id": self.THREAD, "turn-id": "t-1"}
        self.assertFalse(d.ours({**ev, "input-messages": [opening + "Task B: something else"]}))
        self.assertFalse(d.ours({**ev, "input-messages": [opening]}))                 # cut short
        self.assertTrue(d.ours({**ev, "input-messages": ["earlier", d.prompt]}))
        # whitespace as the TUI records a paste (CR for the line break, re-wrapped spaces) still matches
        self.assertTrue(d.ours({**ev, "input-messages": [d.prompt.replace("\n", "\r").replace(". ", ".  ")]}))

    def test_codex_notify_without_input_messages_ends_the_turn_it_names(self):
        d, out = self.driver("codex", sid=self.THREAD, mode="resume", FAKE_ROLLOUT="full", FAKE_BARE="1",
                             CODEX_HOME=str(self.root / "codexhome"))
        d.t["beat"] = 0.1
        self.assertEqual(d.run("Fix the lint"), 0)
        self.assertEqual(d.turn_id, "turn-1")                    # read from the rollout's task_started
        texts = [e["item"]["text"] for e in self.events(out) if e.get("type") == "item.completed"]
        self.assertEqual(len(texts), 1)
        self.assertIn("WAIT 2h: fake [Fix the lint", texts[0])

    def test_codex_notify_without_input_messages_needs_our_turn_id(self):
        d, _ = self.driver("codex", sid=self.THREAD, mode="resume")
        d.prompt = "Fix the lint"
        bare = {"type": "agent-turn-complete", "thread-id": self.THREAD, "last-assistant-message": '{"title":"x"}'}
        # our thread alone is no evidence of our prompt: not before the rollout named our turn
        self.assertFalse(d.ours(bare))
        self.assertFalse(d.ours({**bare, "turn-id": "t-1"}))
        self.assertFalse(d.ours({**bare, "input-messages": []}))
        d.turn_id = "t-1"
        self.assertFalse(d.ours(bare))                           # names no turn: still not ours
        self.assertFalse(d.ours({**bare, "turn-id": "title-turn"}))
        self.assertTrue(d.ours({**bare, "turn-id": "t-1"}))
        self.assertTrue(d.ours({**bare, "thread-id": "", "turn-id": "t-1", "input-messages": [" "]}))

    THREAD = "11111111-2222-3333-4444-555555555555"

    def codex_rollout_turn(self, how):
        d, out = self.driver("codex", sid=self.THREAD, mode="resume", FAKE_ROLLOUT=how,
                             CODEX_HOME=str(self.root / "codexhome"))
        d.t["beat"] = 0.1
        return d, out

    def assert_no_outcome_turn(self, d, out):
        start = time.time()
        self.assertEqual(d.run("Fix the lint"), 0, out.getvalue()[-300:])
        self.assertLess(time.time() - start, 12)                  # not the 40 min watchdog
        evs = self.events(out)
        self.assertEqual([e["thread_id"] for e in evs if e["type"] == "thread.started"], [self.THREAD])
        self.assertEqual([e["item"]["text"] for e in evs if e["type"] == "item.completed"], [""])
        self.assertEqual(evs[-1]["type"], "turn.completed")
        log = self.root / "turn.jsonl"
        log.write_text(out.getvalue())
        self.assertEqual(CodexAdapter().parse(log).final_text, "")  # no outcome declared
        self.assertEqual(interactive.read_progress(self.state, "att-1")["phase"], "done")

    def test_codex_task_that_ends_with_no_model_record_ends_the_turn_as_no_output(self):
        self.assert_no_outcome_turn(*self.codex_rollout_turn("empty"))

    def test_codex_task_that_ends_with_an_error_after_model_work_ends_the_turn_as_no_output(self):
        d, out = self.codex_rollout_turn("errored")
        self.assert_no_outcome_turn(d, out)
        self.assertTrue(d.turn_end["output"])
        self.assertIn("server_overloaded", d.turn_end["error"])

    def test_codex_task_complete_with_a_message_ends_the_turn_even_without_a_notify(self):
        d, out = self.codex_rollout_turn("nonotify")
        self.assertEqual(d.run("Fix the lint"), 0, out.getvalue()[-300:])
        texts = [e["item"]["text"] for e in self.events(out) if e.get("type") == "item.completed"]
        self.assertEqual(len(texts), 1)
        self.assertIn("Fix the lint", texts[0])                    # the rollout's final message

    def test_codex_end_written_before_the_prompt_is_accepted_by_the_driver_is_not_missed(self):
        """A fast failure: the task's end is in the rollout by the time the driver has seen the prompt
        accepted, so a cursor taken at that moment would stand behind it."""
        d, out = self.codex_rollout_turn("silent")
        real, written = d.accepted, []

        def accepted(base, busy_before):
            ok = real(base, busy_before)
            if ok and not written:
                written.append(1)
                with open(d.codex_rollout(self.THREAD), "a") as fh:
                    for rec in ({"type": "response_item", "payload": {"type": "message", "role": "user"}},
                                {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": None,
                                 "error": {"message": "at capacity", "codex_error_info": "server_overloaded"}}}):
                        fh.write(json.dumps(rec) + "\n")
            return ok
        d.accepted = accepted
        self.assert_no_outcome_turn(d, out)
        self.assertEqual(written, [1])

    def test_codex_task_that_ends_after_model_records_is_the_normal_end(self):
        d, out = self.codex_rollout_turn("normal")
        self.assertEqual(d.run("Fix the lint"), 0, out.getvalue()[-300:])
        texts = [e["item"]["text"] for e in self.events(out) if e.get("type") == "item.completed"]
        self.assertEqual(len(texts), 1)
        self.assertIn("Fix the lint", texts[0])

    def test_a_late_stop_of_another_prompt_is_not_this_turn(self):
        d, out = self.driver("claude", sid="s-1", FAKE_STALE_STOP="1")
        self.assertEqual(d.run("the real question"), 0)
        last = self.events(out)[-1]
        self.assertIn("the real question", last["result"])
        self.assertNotIn("OLD TURN", last["result"])

    def test_codex_update_menu_is_skipped_not_taken(self):
        d, out = self.driver("codex", FAKE_UPDATE="1")
        self.assertEqual(d.run("Fix the lint"), 0, out.getvalue()[-300:])
        start = json.loads(self.starts()[0])
        self.assertIn("check_for_update_on_startup=false", start)

    def test_parent_session_markers_do_not_reach_the_harness(self):
        d, _ = self.driver("claude", sid="s-1", CLAUDECODE="1", CLAUDE_CODE_ENTRYPOINT="cli",
                           CLAUDE_CONFIG_DIR=str(self.root / "acct"))
        self.assertEqual(d.run("hi"), 0)
        seen = json.loads((self.root / "envs").read_text().splitlines()[0])
        self.assertEqual(seen, ["CLAUDE_CONFIG_DIR"])

    def test_parallel_first_starts_of_one_agent_all_get_a_window(self):
        import threading
        results = {}

        def go(key):
            d, _ = self.driver("claude", key=key, sid=key)
            results[key] = d.run("hi " + key)
        threads = [threading.Thread(target=go, args=(f"p{i}",)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results, {f"p{i}": 0 for i in range(5)})
        self.assertEqual(len(self.tmux.windows(interactive.session_name("builder"))), 5)

    def test_parallel_starts_never_exceed_the_host_cap(self):
        # the cap closes idle windows (a window running a turn is never closed); parallel
        # starts evict under one host lock, so they cannot all count the same free room
        import threading
        for key in ("i1", "i2", "i3"):
            d, _ = self.driver("claude", key=key, sid=key)
            self.assertEqual(d.run("hi"), 0)
        results = {}

        def go(key):
            d, _ = self.driver("claude", key=key, sid=key)
            d.max_sessions = 3
            results[key] = d.run("hi " + key)
        import mesh_runner.tui_driver as drv
        orig = drv.pid_alive
        drv.pid_alive = lambda pid: False                  # threads share one pid: count windows as idle
        try:
            threads = [threading.Thread(target=go, args=(f"c{i}",)) for i in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            drv.pid_alive = orig
        self.assertEqual(set(results.values()), {0})
        self.assertLessEqual(len(self.tmux.windows(interactive.session_name("builder"))), 3)

    def test_stop_failure_without_prompt_id_ends_the_turn_as_an_error(self):
        d, out = self.driver("claude", sid="s-1", FAKE_FAIL="1")
        started = time.time()
        self.assertEqual(d.run("hello"), 1)
        self.assertLess(time.time() - started, 20)
        last = self.events(out)[-1]
        self.assertTrue(last["is_error"])
        self.assertIn("rate_limit_error", last["result"])
        self.assertEqual(self.tmux.windows(interactive.session_name("builder")), {})   # closed

    def test_without_prompt_ids_a_named_stale_stop_is_still_not_this_turn(self):
        d, out = self.driver("claude", sid="s-1", FAKE_STALE_STOP="1", FAKE_NO_IDS="1")
        self.assertEqual(d.run("the real question"), 0)
        self.assertNotIn("OLD TURN", self.events(out)[-1]["result"])

    def test_at_the_cap_with_every_window_busy_a_start_waits_then_fails(self):
        d, _ = self.driver("claude", key="b1", sid="b1")
        self.assertEqual(d.run("hi"), 0)
        meta = interactive.read_meta(self.state, "b1")
        meta["busy_pid"] = os.getpid()                                    # running a turn
        interactive.write_meta(self.state, "b1", meta)
        d, out = self.driver("claude", key="b2", sid="b2")
        d.max_sessions = 1
        d.t.update(cap_wait=1, cap_poll=0.2)
        self.assertEqual(d.run("hi"), 1)
        self.assertIn("host session cap reached", self.events(out)[-1]["result"])
        self.assertEqual(set(self.tmux.windows(interactive.session_name("builder"))), {"b1"})

    def test_a_window_is_claimed_busy_from_its_creation(self):
        d, _ = self.driver("claude", key="w1", sid="w1")
        seen = {}
        orig = d.new_window

        def new_window(command):
            orig(command)
            seen["after_create"] = interactive.read_meta(self.state, "w1").get("busy_pid")
        d.new_window = new_window
        orig_ready = d.wait_ready

        def wait_ready():
            seen["at_ready"] = interactive.read_meta(self.state, "w1").get("busy_pid")
            orig_ready()
        d.wait_ready = wait_ready
        self.assertEqual(d.run("hi"), 0)
        self.assertEqual(seen["at_ready"], os.getpid())               # saved under the lock already

    def test_the_state_directory_is_made_private_even_when_it_existed(self):
        self.state.mkdir(parents=True, mode=0o755)
        os.chmod(self.state, 0o755)
        d, _ = self.driver("claude", sid="s-1")
        self.assertEqual(d.run("hi"), 0)
        self.assertEqual(os.stat(self.state).st_mode & 0o777, 0o700)

    def test_a_limit_banner_without_hook_ends_the_turn_and_closes_the_window(self):
        d, out = self.driver("claude", sid="s-1", FAKE_BANNER="1")
        self.assertEqual(d.run("hello"), 1)
        self.assertIn("hit your usage limit", self.events(out)[-1]["result"])
        self.assertEqual(self.tmux.windows(interactive.session_name("builder")), {})

    def test_a_blocked_stop_waits_for_the_real_end_of_the_turn(self):
        d, out = self.driver("claude", sid="s-1", FAKE_BLOCKED="1")
        self.assertEqual(d.run("hello"), 0)
        last = self.events(out)[-1]
        self.assertEqual(last["result"], "final answer")

    def test_a_session_still_busy_after_its_turn_gets_the_next_prompt_when_idle(self):
        d, _ = self.driver("claude", sid="s-1", FAKE_BG="10")
        self.assertEqual(d.run("start the tests in the background"), 0)
        d2, out = self.driver("claude", sid="s-1", mode="resume", FAKE_BG="10")
        self.assertEqual(d2.run("read the result"), 0, out.getvalue()[-300:])   # accept is 8 s, busy 10 s
        self.assertIn("read the result", self.events(out)[-1]["result"])
        self.assertEqual(len(self.starts()), 1)                                 # same live session

    def test_a_busy_session_is_reused_and_waited_for_not_interrupted(self):
        d, _ = self.driver("claude", sid="s-1")
        self.assertEqual(d.run("first"), 0)
        d2, out = self.driver("claude", sid="s-1", mode="resume")
        calls, orig_busy = {"n": 0}, d2.busy

        def busy():                                   # still working for its first checks
            calls["n"] += 1
            return calls["n"] <= 5 or orig_busy()
        d2.busy = busy
        sent = []
        orig_run = d2.tmux.run

        def run(*args, **kw):
            if args[:1] == ("send-keys",) and "Escape" in args:
                sent.append(args)
            return orig_run(*args, **kw)
        d2.tmux.run = run
        self.assertEqual(d2.run("second"), 0, out.getvalue()[-300:])
        self.assertEqual(sent, [])                                           # never interrupted
        self.assertEqual(len(self.starts()), 1)                              # not rebuilt

    def test_a_harness_that_dies_is_a_turn_error_with_its_screen(self):
        d, out = self.driver("claude", sid="s-1", FAKE_DIE="1")
        self.assertEqual(d.run("x"), 1)
        last = self.events(out)[-1]
        self.assertTrue(last["is_error"])
        self.assertIn("fatal: cannot start", last["result"])
        self.assertEqual(self.tmux.windows(interactive.session_name("builder")), {})

    def test_cancel_interrupts_the_turn_without_a_result_and_keeps_the_session(self):
        d, out = self.driver("claude", sid="s-1", FAKE_REPLY="⏳ WAIT 1h: x")
        d.done = lambda base: None                                         # the hook never comes
        orig = d.accepted

        def accepted(base, busy_before):
            d.cancelled = True                                             # SIGTERM during the turn
            return orig(base, busy_before)
        d.accepted = accepted
        self.assertEqual(d.run("long job"), 1)
        evs = self.events(out)
        self.assertFalse(any(e.get("type") == "result" for e in evs))     # cut off: no result
        self.assertTrue(self.tmux.windows(interactive.session_name("builder")).get("att-1"))

    def test_sessions_on_the_host_are_capped_by_closing_the_longest_idle(self):
        for key in ("k1", "k2"):
            d, _ = self.driver("claude", key=key, sid=key)
            self.assertEqual(d.run("hi"), 0)
        meta = interactive.read_meta(self.state, "k1")
        meta["last_end"] = 1.0
        interactive.write_meta(self.state, "k1", meta)
        d, _ = self.driver("claude", key="k3", sid="k3")
        d.max_sessions = 2
        self.assertEqual(d.run("hi"), 0)
        self.assertEqual(set(self.tmux.windows(interactive.session_name("builder"))), {"k2", "k3"})

    def phases(self, d):
        """Every progress report the driver writes during one turn, in order."""
        import mesh_runner.tui_driver as td
        seen = []
        real = td.write_progress

        def record(directory, key, progress):
            seen.append(dict(progress))
            real(directory, key, progress)
        td.write_progress = record
        self.addCleanup(setattr, td, "write_progress", real)
        return seen

    def test_the_driver_reports_where_the_turn_stands(self):
        d, out = self.driver("claude", sid="s-1", FAKE_SLOW="2")
        d.t["beat"] = 0.2
        seen = self.phases(d)
        self.assertEqual(d.run("think hard"), 0)
        order = [p["phase"] for p in seen]
        self.assertEqual(order[0], "launcher")
        self.assertEqual(order[-1], "done")
        self.assertLess(order.index("ready"), order.index("sent"))
        self.assertLess(order.index("sent"), order.index("output"))
        self.assertTrue(all(p["pid"] == os.getpid() for p in seen))
        sent = [p for p in seen if p["phase"] == "sent"]
        self.assertTrue(any(p["external_wait"] for p in sent))             # the busy screen is the agent's own wait
        first_out = next(p for p in seen if p["phase"] == "output")
        self.assertGreater(first_out["progress_at"], sent[0]["progress_at"])   # first output is progress
        self.assertFalse(interactive.read_progress(self.state, "att-1")["external_wait"])
        self.assertEqual(interactive.read_progress(self.state, "att-1")["phase"], "done")
        # silence is not progress: while the reply is awaited progress_at stays where the prompt was accepted
        self.assertEqual({p["progress_at"] for p in sent}, {sent[0]["progress_at"]})

    def test_first_output_is_a_reply_not_the_echo_of_our_prompt(self):
        d, _ = self.driver("claude", sid="s-1")
        d.meta["transcript"] = str(self.root / "t.jsonl")
        t = Path(d.meta["transcript"])
        t.write_text("")
        d.sent_size = d.scan_from = 0

        def more(text):
            with open(t, "a") as fh:
                fh.write(text)
            return d.reply_started(t.stat().st_size)
        self.assertFalse(more(json.dumps({"type": "user", "message": {"content": "p"}}) + "\n"))
        self.assertFalse(more(json.dumps({"type": "attachment"}) + "\n"))
        # a user record that quotes the marker (compact or spaced) is still a user record
        quoted = {"type": "user", "message": {"content": 'see {"type":"assistant"} and {"type": "assistant"}'}}
        self.assertFalse(more(json.dumps(quoted) + "\n"))
        self.assertFalse(more(json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": '{"type":"assistant","message":{}}'}]}}) + "\n"))
        # a record cut in the middle waits for its newline, then counts
        line = '{"type":"assistant","message":{"content":[]}}\n'
        held = d.scan_from
        self.assertFalse(more(line[:15]))
        self.assertEqual(d.scan_from, held)                              # nothing consumed yet
        self.assertFalse(more(line[15:-1]))
        self.assertEqual(d.scan_from, held)
        self.assertTrue(more(line[-1:]))

    def reader(self, kind="claude", cap=64):
        """A driver with a tiny read cap over an empty transcript, and a spy on the lines it parses."""
        import mesh_runner.tui_driver as td
        old = td.SCAN_CHUNK
        td.SCAN_CHUNK = cap
        self.addCleanup(setattr, td, "SCAN_CHUNK", old)
        d, _ = self.driver(kind, sid="s-1")
        path = self.root / f"{kind}.jsonl"
        path.write_text("")
        if kind == "claude":
            d.meta["transcript"] = str(path)
        else:
            d.rollout = str(path)
        d.sent_size = d.scan_from = 0
        parsed = []
        real = d.claude_reply if kind == "claude" else d.codex_reply
        setattr(d, "claude_reply" if kind == "claude" else "codex_reply", lambda line: parsed.append(line) or real(line))

        def more(text):
            with open(path, "a") as fh:
                fh.write(text)
            return d.reply_started(path.stat().st_size)
        return d, more, parsed

    def test_an_over_long_line_is_output_and_its_fragments_are_never_parsed(self):
        d, more, parsed = self.reader()
        long_line = '{"type":"assistant","message":{"content":"' + "x" * 150 + '"}}\n'
        self.assertFalse(more(long_line[:40]))                           # under the cap: a partial line waits
        self.assertEqual(d.scan_from, 0)
        self.assertTrue(more(long_line[40:100]))                         # over the cap with no newline: output
        self.assertTrue(d.skipping)
        self.assertEqual(parsed, [])                                     # the fragment was not parsed
        self.assertFalse(more(long_line[100:-3]))                        # still inside the line: skipped
        self.assertEqual(parsed, [])
        self.assertTrue(d.skipping)
        self.assertFalse(more(long_line[-3:]))                           # the newline ends it
        self.assertFalse(d.skipping)
        self.assertEqual(parsed, [])
        self.assertEqual(d.scan_from, len(long_line))

    def test_reading_resumes_at_the_line_after_an_over_long_one(self):
        d, more, parsed = self.reader()
        user = json.dumps({"type": "user", "message": {"content": "q" * 150}}) + "\n"
        self.assertTrue(more(user))                                      # larger than the cap: counted as output
        self.assertTrue(d.skipping)
        nxt = json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n"
        self.assertLess(len(nxt), 64)
        self.assertFalse(more(nxt))                                      # the rest of the long line is skipped
        self.assertFalse(d.skipping)
        self.assertEqual(parsed, [nxt.encode().strip()])                 # the next whole line is parsed, alone
        self.assertTrue(more('{"type":"assistant"}\n'))                 # and the one after it
        self.assertEqual(len(parsed), 2)

    def test_an_over_long_codex_line_is_skipped_the_same_way(self):
        d, more, parsed = self.reader("codex", cap=256)
        long_line = self.rollout_line("response_item", "message", "user", pad="x" * 600)
        self.assertTrue(more(long_line[:300]))
        self.assertTrue(d.skipping)
        self.assertFalse(more(long_line[300:]))
        self.assertFalse(d.skipping)
        self.assertEqual(parsed, [])
        self.assertFalse(more(self.rollout_line("response_item", "message", "user")))
        self.assertTrue(more(self.rollout_line("response_item", "reasoning")))

    def rollout_line(self, kind, sub=None, role=None, **extra):
        payload = {k: v for k, v in (("type", sub), ("role", role)) if v}
        return json.dumps({"timestamp": "t", "type": kind, **({"payload": payload} if payload or kind else {}), **extra}) + "\n"

    def test_codex_first_output_is_a_model_record_not_meta_or_the_echoed_prompt(self):
        d, _ = self.driver("codex")
        roll = self.root / "rollout.jsonl"
        roll.write_text("")
        d.rollout, d.sent_size, d.scan_from = str(roll), 0, 0

        def more(*lines):
            with open(roll, "a") as fh:
                fh.write("".join(lines))
            return d.reply_started(roll.stat().st_size)
        self.assertFalse(more(self.rollout_line("session_meta"), self.rollout_line("event_msg", "task_started"),
                              self.rollout_line("response_item", "message", "developer"),
                              self.rollout_line("response_item", "message", "user"),     # the echoed prompt
                              self.rollout_line("turn_context"), self.rollout_line("event_msg", "token_count"),
                              self.rollout_line("event_msg", "item_completed")))
        self.assertGreater(d.scan_from, 0)                              # read once, not again
        before = d.scan_from
        self.assertFalse(more(self.rollout_line("response_item", "message", "user")))
        self.assertGreater(d.scan_from, before)
        # a record cut in the middle waits for its newline, then counts
        part = self.rollout_line("response_item", "message", "assistant")
        with open(roll, "a") as fh:
            fh.write(part[:20])
        mark = d.scan_from
        self.assertFalse(d.reply_started(roll.stat().st_size))
        self.assertEqual(d.scan_from, mark)
        with open(roll, "a") as fh:
            fh.write(part[20:])
        self.assertTrue(d.reply_started(roll.stat().st_size))

    def codex_reader(self):
        d, _ = self.driver("codex")
        roll = self.root / "rollout.jsonl"
        roll.write_text("")
        d.rollout, d.sent_size, d.scan_from = str(roll), 0, 0

        def more(*lines):
            with open(roll, "a") as fh:
                fh.write("".join(lines))
            return d.reply_started(roll.stat().st_size)
        return d, roll, more

    def task_complete(self, message=None, error=None):
        return json.dumps({"timestamp": "t", "type": "event_msg", "payload": {
            "type": "task_complete", "last_agent_message": message, "error": error}}) + "\n"

    def test_codex_task_complete_right_after_our_prompt_is_an_empty_turn(self):
        d, roll, more = self.codex_reader()
        self.assertFalse(more(self.rollout_line("event_msg", "task_started"),
                              self.rollout_line("response_item", "message", "user"),
                              self.rollout_line("event_msg", "token_count")))
        self.assertIsNone(d.turn_end)                                   # not over yet
        self.assertFalse(more(self.task_complete()))
        self.assertEqual(d.turn_end, {"message": "", "error": "", "output": False})
        d2, _, more2 = self.codex_reader()
        more2(self.rollout_line("response_item", "message", "user"),
              self.task_complete(error={"message": "stream disconnected", "codex_error_info": "other"}))
        self.assertEqual(d2.turn_end, {"message": "", "error": "other: stream disconnected", "output": False})

    def test_codex_task_complete_after_model_records_ends_the_turn_with_or_without_a_message(self):
        for model in ("reasoning", "function_call"):
            d, roll, more = self.codex_reader()
            self.assertTrue(more(self.rollout_line("response_item", "message", "user"),
                                 self.rollout_line("response_item", model), self.task_complete("done")))
            self.assertEqual(d.turn_end, {"message": "done", "error": "", "output": True})
        # models in one read and the end (an API error, no final message) in a later one
        d, roll, more = self.codex_reader()
        self.assertTrue(more(self.rollout_line("response_item", "reasoning")))
        self.assertIsNone(d.turn_end)
        self.assertFalse(more(self.task_complete(error={"message": "at capacity",
                                                        "codex_error_info": "server_overloaded"})))
        self.assertEqual(d.turn_end, {"message": "", "error": "server_overloaded: at capacity", "output": True})

    def test_codex_task_complete_of_an_earlier_turn_is_ignored(self):
        d, roll, more = self.codex_reader()
        roll.write_text(self.rollout_line("response_item", "message", "user") + self.task_complete())
        d.sent_size = d.scan_from = roll.stat().st_size                 # as at acceptance of the prompt
        self.assertFalse(d.reply_started(roll.stat().st_size))
        self.assertIsNone(d.turn_end)
        self.assertFalse(more(self.rollout_line("response_item", "message", "user")))
        self.assertIsNone(d.turn_end)                                   # and our own turn is still running

    def test_the_end_of_a_codex_turn_moves_the_phase_to_done_for_the_watchdog(self):
        for phase, lines in (("sent", ""), ("output", self.rollout_line("response_item", "reasoning"))):
            d, roll, more = self.codex_reader()
            d.progress["phase"] = phase
            roll.write_text(lines + self.task_complete())
            d.beat(force=True)
            self.assertEqual(d.progress["phase"], "done", phase)
            self.assertIsNotNone(d.turn_end)

    def test_codex_reasoning_and_tool_records_are_first_output_too(self):
        for sub in ("reasoning", "function_call", "custom_tool_call", "agent_message"):
            d, _ = self.driver("codex")
            roll = self.root / f"r-{sub}.jsonl"
            roll.write_text(self.rollout_line("response_item", sub))
            d.rollout, d.scan_from = str(roll), 0
            self.assertTrue(d.reply_started(roll.stat().st_size), sub)

    def test_codex_records_before_the_prompt_was_accepted_never_count(self):
        d, _ = self.driver("codex")
        roll = self.root / "old.jsonl"
        roll.write_text(self.rollout_line("response_item", "message", "assistant") * 3)
        d.rollout = str(roll)
        d.sent_size = d.scan_from = roll.stat().st_size                 # as at acceptance of the prompt
        self.assertFalse(d.reply_started(roll.stat().st_size))

    def test_a_new_codex_session_is_found_by_its_directory(self):
        d, _ = self.driver("codex")
        home = self.root / "codexhome"
        day = home / "sessions" / "2026" / "10" / "23"
        day.mkdir(parents=True)
        d.env["CODEX_HOME"] = str(home)
        mine, other = day / "rollout-a-1.jsonl", day / "rollout-b-2.jsonl"
        other.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": "/somewhere/else"}}) + "\n")
        self.assertEqual(d.fresh_rollout(), "")                         # not ours
        mine.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": str(self.cwd)}}) + "\n")
        self.assertEqual(d.fresh_rollout(), str(mine))
        self.assertEqual(d.progress_size(), mine.stat().st_size)        # no thread id needed

    def test_a_progress_file_is_dropped_with_its_window(self):
        d, _ = self.driver("claude", sid="s-1")
        self.assertEqual(d.run("hi"), 0)
        self.assertTrue(interactive.progress_path(self.state, "att-1").exists())
        store = FakeStore({"att-1": A("completed")})
        interactive.write_progress(self.state, "orphan", {"pid": 0, "phase": "ready"})   # its driver is gone
        interactive.write_progress(self.state, "starting", {"pid": os.getpid(), "phase": "launcher"})  # no window yet
        interactive.reap(self.tmux, store, self.root / "state", "builder", 60, OPEN, ACTIVE)
        self.assertFalse(interactive.progress_path(self.state, "att-1").exists())
        self.assertFalse(interactive.progress_path(self.state, "orphan").exists())
        self.assertTrue(interactive.progress_path(self.state, "starting").exists())


    def test_reaper_closes_what_will_not_be_used_warm(self):
        for key in ("closed", "idle", "running", "longwait", "cimr"):
            d, _ = self.driver("claude", key=key, sid=key)
            self.assertEqual(d.run("hi"), 0)
        meta = interactive.read_meta(self.state, "idle")
        meta["last_end"] = time.time() - 7200
        interactive.write_meta(self.state, "idle", meta)
        later = (time.time() + 86400)
        store = FakeStore({"closed": A("completed"), "idle": A("waiting", {"kind": "card"}),
                           "running": A("running"),
                           "longwait": A("waiting", {"kind": "time",
                                                     "until": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(later))}),
                           "cimr": A("waiting", {"kind": "pipeline"})})
        closed = dict(interactive.reap(self.tmux, store, self.root / "state", "builder", 60, OPEN, ACTIVE))
        self.assertEqual(set(closed), {"closed", "idle", "longwait"})
        self.assertEqual(set(self.tmux.windows(interactive.session_name("builder"))), {"running", "cimr"})
        self.assertFalse(interactive.meta_path(self.state, "closed").exists())


class AdapterAndConfig(unittest.TestCase):
    def test_interactive_argv_has_no_print_mode(self):
        c = ClaudeAdapter(extra_args=["--model", "sonnet"])
        argv = c.interactive_argv("sid", Path("/p"), [Path("/w")], resume=True)
        self.assertEqual(argv[:3], ["claude", "--resume", "sid"])
        self.assertNotIn("-p", argv)
        self.assertNotIn("stream-json", argv)
        x = CodexAdapter(profile="mario")
        argv = x.interactive_argv("t-1", Path("/p"), [], resume=True)
        self.assertNotIn("exec", argv)
        self.assertEqual(argv[-2:], ["resume", "t-1"])

    def test_wrapped_adapter_runs_the_driver_and_overrides_reach_the_harness(self):
        w = interactive.wrap(ClaudeAdapter(extra_args=["--model", "sonnet"]), "builder", "interactive",
                             Path("/state"), "tmux", 20)
        argv = w.resume_argv("sid", Path("/p"), [])
        self.assertTrue(argv[1].endswith("tui_driver.py"))
        tail = argv[argv.index("--") + 1:]
        self.assertEqual(tail[:3], ["claude", "--resume", "sid"])
        argv = w.with_effort(w.with_model(argv, "opus"), "high")
        tail = argv[argv.index("--") + 1:]
        self.assertEqual(tail[tail.index("--model") + 1], "opus")
        self.assertEqual(tail[tail.index("--effort") + 1], "high")
        self.assertIs(interactive.wrap(ClaudeAdapter(), "b", "headless", Path("/s"), "tmux", 20).__class__,
                      ClaudeAdapter)

    def test_a_recorded_prompt_must_hold_ours(self):
        d = Driver(harness="claude", agent="a", state=Path("/tmp/x"), tmux=interactive.Tmux("tmux"), max_sessions=1,
                   cwd=Path("/tmp"), session_id="s", mode="start", key="k", argv=["claude"])
        d.prompt = "[runner] Task #1 - fix the build\nand report the result."
        self.assertTrue(d.said_ours('\n\n<pasted_content id="8d5d">\n' + d.prompt + '\n</pasted_content>'))
        self.assertTrue(d.said_ours(d.prompt.replace("\n", "\r")))
        self.assertFalse(d.said_ours("y"))                                 # inside our text, but not ours
        self.assertFalse(d.said_ours("Generate a concise title"))

    def test_profile_routing_reaches_the_wrapped_codex_adapter(self):
        w = interactive.wrap(CodexAdapter(profile="base"), "builder", "interactive", Path("/state"), "tmux", 20)
        if not hasattr(w, "_base_profile"):
            w._base_profile = w.profile
        w.profile = "routed-acct2"
        argv = w.start_argv("", Path("/p"), [])
        tail = argv[argv.index("--") + 1:]
        self.assertEqual(tail[tail.index("--profile") + 1], "routed-acct2")
        w.profile = "" or w._base_profile
        tail = w.start_argv("", Path("/p"), [])
        self.assertIn("base", tail)

    def test_spawn_names_the_window_after_the_attempt(self):
        seen = {}
        w = interactive.wrap(ClaudeAdapter(), "builder", "interactive", Path("/state"), "tmux", 20)
        orig = Adapter.spawn
        Adapter.spawn = lambda self, launch, log_path: seen.setdefault("argv", launch.argv)
        try:
            w.spawn(Launch(w.start_argv("sid", Path("/p"), []), Path("/p"), {}, "x"),
                    Path("/logs/att-42.turn3.claude.jsonl"))
        finally:
            Adapter.spawn = orig
        argv = seen["argv"]
        self.assertEqual(argv[argv.index("--key") + 1], "att-42")
        self.assertLess(argv.index("--key"), argv.index("--"))

    def test_launch_mode_defaults_to_interactive_and_can_be_overridden(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "k"
            key.write_text("x")
            key.chmod(0o600)
            base = (f'[runner]\nmesh_url = "http://m"\nstate_dir = "{tmp}"\n{{runner}}'
                    f'[[agents]]\nname = "a"\nkey_file = "{key}"\nprofile_dir = "{tmp}"\n{{agent}}')
            path = Path(tmp) / "r.toml"
            path.write_text(base.format(runner="", agent=""))
            cfg = load(path)
            self.assertEqual(cfg.launch_mode, "interactive")
            self.assertEqual(cfg.agents[0].launch_mode, "")
            path.write_text(base.format(runner='launch_mode = "headless"\n', agent='launch_mode = "interactive"\n'))
            cfg = load(path)
            self.assertEqual((cfg.launch_mode, cfg.agents[0].launch_mode), ("headless", "interactive"))
            path.write_text(base.format(runner='launch_mode = "tui"\n', agent=""))
            with self.assertRaises(ConfigError):
                load(path)


if __name__ == "__main__":
    unittest.main()
