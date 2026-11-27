"""A headless CLI that exits on its own with an error before its stream opens a conversation.

When the provider withdraws the model agy is configured with, every agy start ends in under a
second with ``error: invalid model selection (...): model ... is not recognized`` on stderr, exit 1, and one
``result`` event naming no conversation, no turn and no usage (the stream below is that recording, only the
model names changed). Recovery read it as ``headless-native-session-mismatch``: the card was held in
``unknown`` for 15 minutes, then started again on the same harness and refused the same way, cycle after
cycle. ZCode shows the same class: ``Error: Select a model before sending a message``,
exit 1, an empty stream.

Such a turn is unsent: the gate recorded the CLI's own exit (journal ``native-exited`` with a positive return
code) and the stream shows nothing past the CLI's start, so the prompt reached no model. The exact prompt is
kept for the next start, without the hold and without an operator alert. The same harness is not started
forever: after ``max_startup_failures`` refusals in a row the card goes up with the CLI's words, as a turn
whose session never began does in ``settle``. A conversation that opened, a process that did not exit on
its own, and the classes with paths of their own (limit, login, egress, missing session) keep their reading.
"""
import hashlib
import json
import subprocess
import sys
import uuid
from pathlib import Path

from fakes import FakeMesh, task
from mesh_runner import interactive, source_review
from mesh_runner.adapters.agy import AgyAdapter
from mesh_runner.adapters.base import proc_stamp
from mesh_runner.adapters.codex import CodexAdapter
from mesh_runner.adapters.kimi import KimiAdapter
from mesh_runner.adapters.zcode import ZCodeAdapter
from mesh_runner.runner import HEADLESS_START_UNSENT, START_REFUSED
from test_runner import Bench

CID = "00000000-0000-4000-8000-0000000000a1"
OTHER = "00000000-1111-2222-3333-444444444444"
MODEL_ERROR = ('invalid model selection (--model "gemini-x-retired" --effort ""): model gemini-x-retired is not '
               'recognized as a known model or custom model in settings\nAvailable models:\n'
               '  Gemini 4 Hydrogen (High)\n  Gemini 3.8 Flash (Low)')
# agy 1.5.x (model names changed)
REFUSED = [{"event": "result", "result": {
    "conversation_id": "", "status": "ERROR", "response": "", "error": MODEL_ERROR, "duration_seconds": 0,
    "num_turns": 0, "usage": {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0,
                              "cache_read_tokens": 0, "total_tokens": 0}, "finished_reason": "error"}}]
REFUSED_STDERR = "error: " + MODEL_ERROR + "\n\n"
INIT = {"event": "init", "conversation_id": CID, "init": {"model": "gemini-3.8-flash-low", "cwd": "/w"}}
FRESH = [INIT, {"event": "step_update", "step_update": {"conversation_id": CID, "step_index": 0, "state": "DONE",
                                                        "step_type": "user_input"}},
         {"event": "result", "result": {"conversation_id": CID, "status": "SUCCESS", "response": "OK\n",
                                        "num_turns": 1, "usage": {"input_tokens": 9, "output_tokens": 1}}}]
ZCODE_STDERR = "Error: Select a model before sending a message (traceId: 00000000-0000-4000-8000-0000000000b2)\n"
CODEX_STDERR = "error: unexpected argument '--no-such-flag' found\n\nUsage: codex exec [OPTIONS] [PROMPT]\n"
CODEX_REFUSED = [{"type": "error", "message": "unknown model \"gpt-x-retired\" in config profile \"lane\""}]
POLICY_STDERR = "error: This request has been flagged for possible cybersecurity risk.\n"
KIMI_VERSION = {"role": "meta", "type": "system.version", "version": "2.6.1"}
KIMI_SERVER = "error: failed to run prompt: provider.api_error: internal server error\n"


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    stamp = proc_stamp(p.pid)
    p.wait(timeout=10)
    return p.pid, stamp


def result_event(**kw):
    base = dict(REFUSED[0]["result"])
    base.update(kw)
    return [{"event": "result", "result": base}]


class HeadlessStartRefused(Bench):
    """Alt-harness headless turns on a claude lane whose process group is gone; a pending CI wake rides on the
    prompt."""

    PROMPT = "EXACT PENDING REQUEST: answer the card."

    def setUp(self):
        super().setUp()
        self.agent.alt = {"agy": {"admission_commands": [["true"]]}, "codex": {}, "kimi": {}, "zcode": {}}
        self.t = task(1, cat="in_progress")
        self.mesh = FakeMesh([self.t])
        self.r = self.runner(self.mesh)
        self.r.me = self.mesh.me()
        self.r.alt_adapters = {"agy": AgyAdapter(binary=str(self.root / "no-agy"), admission_commands=[["true"]]),
                               "codex": CodexAdapter(binary=str(self.root / "no-codex")),
                               "kimi": KimiAdapter(binary=str(self.root / "no-kimi")),
                               "zcode": ZCodeAdapter(binary=str(self.root / "no-zcode"))}
        self.directory = interactive.agent_dir(self.cfg.state_dir, self.agent.name)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.a = None

    def launch(self, stream, stderr, returncode=1, session="", requested=None, harness="agy",
               phase="native-exited", mode="work"):
        """Turn 2 of the attempt (a new one on the first call) ended with ``stream``, ``stderr`` and the
        gate's journal ``phase`` with ``returncode`` (None: not recorded)."""
        if self.a is None:
            self.n = getattr(self, "n", 1) + 1                    # each new attempt on a card of its own
            self.t = task(self.n, cat="in_progress")
            self.mesh.tasks[self.t["id"]] = self.t
            a = self.r.store.create(self.agent.name, self.t["id"], harness, self.t["title"], mode=mode)
            self.workspaces = [str(self.r.pool.lease(a.id, "app"))]
        else:
            a = self.r.store.get(self.a.id)
        log = self.root / f"{a.id}.turn2.{harness}.jsonl"
        log.write_text("".join(json.dumps(e) + "\n" for e in stream))
        log.with_suffix(".stderr").write_text(stderr)
        if harness == "agy":
            log.with_suffix(".agy").write_text(json.dumps({"conversation": session if requested is None
                                                           else requested}))
        self.intent = {"version": 1, "token": uuid.uuid4().hex, "turn": 2, "log_path": str(log),
                       "sha256": hashlib.sha256(self.PROMPT.encode()).hexdigest(), "prompt": self.PROMPT,
                       "kind": "headless", "harness": harness, "session_id": session, "resume": bool(session),
                       "workspaces": self.workspaces, "source_read_at": "pending-source",
                       "prior": {"turns": 1, "session_id": session, "log_path": "", "turn_started_at": "",
                                 "source_read_at": "previous-source"}}
        journal = {"version": 1, "token": self.intent["token"], "sha256": self.intent["sha256"], "phase": phase}
        if returncode is not None:
            journal["returncode"] = returncode
        interactive.write_delivery(self.directory, a.id, journal)
        self.pending = {"ci_delivery": {"key": "fixture-ci-" + self.intent["token"][:8], "value": "failed:1"}}
        pid, stamp = dead_pid()
        self.assertTrue(self.r.store.update(a, state="running", pid=pid, stamp=stamp, turns=2, session_id=session,
                                            log_path=str(log), workspaces=self.workspaces, wake_text="pending wake",
                                            wait={**self.pending, "interactive_prompt_intent": self.intent}))
        self.a = self.r.store.get(a.id)
        return self.a

    def verdict(self):
        return self.r._headless_verdict(self.r.store.get(self.a.id), self.intent)[0]

    def reconcile(self):
        return self.r._reconcile_interactive_prompt(self.r.store.get(self.a.id))

    def details(self, kind):
        return [d for _, k, d in self.r.store.events(self.a.id) if k == kind]

    def bodies(self):
        return " ".join(c["body"] for c in self.mesh.comment_log.get(self.t["id"], []))

    def emitted(self, kind):
        path = self.cfg.state_dir / "events.jsonl"
        rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []
        return [r for r in rows if r["kind"] == kind]

    def alerts(self):
        return self.emitted("operator_alert")

    def assert_kept_unsent(self, count=1):
        self.assertFalse(self.reconcile())
        a = self.r.store.get(self.a.id)
        self.assertEqual(a.state, "ready")
        self.assertEqual(self.details("prompt_delivery_unknown"), [])
        self.assertEqual(self.alerts(), [])
        self.assertNotIn("MANDATORY DELIVERY PENDING", self.bodies())
        # the exact prompt is kept, not acknowledged: the pending wake stays undelivered
        self.assertEqual(a.wait["interactive_prompt_intent"], dict(self.intent, retryable=True))
        self.assertEqual(a.wait["ci_delivery"], self.pending["ci_delivery"])
        self.assertIsNone(self.r.store.mark(self.agent.name, self.t["id"], self.pending["ci_delivery"]["key"]))
        self.assertNotIn("mesh_runner.prompt_ack", Path(self.intent["log_path"]).read_text())
        self.assertEqual((a.turns, a.log_path, a.source_read_at), (1, "", "previous-source"))
        self.assertEqual(self.details("prompt_unsent_reconciled")[-1], self.intent["token"])
        self.assertEqual(json.loads(self.details("headless_start_refused")[-1])["count"], count)
        # not an account limit: no fresh-capacity admission, no on-limit routing
        self.assertFalse(self.r._quota_admission_required(a))
        return a

    def assert_held_unknown(self, reason="headless-native-session-mismatch"):
        self.assertFalse(self.reconcile())
        self.assertEqual(self.r.store.get(self.a.id).state, "unknown")
        self.assertEqual(self.details("prompt_delivery_unknown"), [reason])
        self.assertEqual(self.details("headless_start_refused"), [])

    # -- the refused start --------------------------------------------------------------------------------

    def test_agy_model_refused_before_a_conversation_is_unsent_not_a_session_mismatch(self):
        self.launch(REFUSED, REFUSED_STDERR)
        self.assertEqual(self.verdict(), HEADLESS_START_UNSENT)
        self.assert_kept_unsent()

    def test_a_resume_refused_the_same_way_is_unsent(self):
        self.launch(REFUSED, REFUSED_STDERR, session=CID)
        self.assertEqual(self.verdict(), HEADLESS_START_UNSENT)
        self.assert_kept_unsent()

    def test_an_empty_stream_with_the_cli_error_is_unsent_on_agy_zcode_and_codex(self):
        for harness, stream, stderr in (("agy", [], REFUSED_STDERR), ("zcode", [], ZCODE_STDERR),
                                        ("codex", [], CODEX_STDERR)):
            with self.subTest(harness=harness):
                self.a = None
                self.launch(stream, stderr, harness=harness)
                self.assertEqual(self.verdict(), HEADLESS_START_UNSENT)
                self.assert_kept_unsent()

    def test_a_codex_error_before_any_thread_is_unsent(self):
        # codex wrote its typed error and no thread: it refused before it sent the prompt
        self.launch(CODEX_REFUSED, "", harness="codex")
        self.assertEqual(self.verdict(), HEADLESS_START_UNSENT)
        self.assert_kept_unsent()

    def test_the_second_refused_start_in_a_row_hands_the_card_up_with_the_cli_words(self):
        self.launch(REFUSED, REFUSED_STDERR)
        self.assert_kept_unsent(count=1)
        first = self.intent
        self.launch(REFUSED, REFUSED_STDERR)                  # the next start on the same harness
        self.assertFalse(self.reconcile())
        a = self.r.store.get(self.a.id)
        self.assertIn(a.state, ("failed", "completed"))       # ended: no third start on agy
        self.assertNotEqual(a.state, "unknown")
        self.assertEqual(self.details("prompt_delivery_unknown"), [])
        self.assertEqual(self.alerts(), [])
        bodies = self.bodies()
        self.assertIn("RUNNER GAVE UP", bodies)
        self.assertIn("agy CLI did not start a session in 2 launches", bodies)
        self.assertIn("invalid model selection", bodies)
        self.assertNotIn("MANDATORY DELIVERY PENDING", bodies)
        self.assertNotIn(self.PROMPT, bodies)
        # archived, never consumed or replayed: the pending wake stays undelivered
        archived = self.r.store.mark(self.agent.name, self.t["id"], "start_refused_intent:" + a.id)
        self.assertEqual(json.loads(archived[0])["intent"], self.intent)
        self.assertNotEqual(first["token"], self.intent["token"])
        self.assertNotIn("interactive_prompt_intent", a.wait)
        self.assertIsNone(self.r.store.mark(self.agent.name, self.t["id"], self.pending["ci_delivery"]["key"]))
        self.assertEqual(self.details("prompt_ended_by_start_refusal"), ["agy"])
        # names the launch it cancels, as an unsent prompt does: a turns metric drops that start
        ended = self.emitted("prompt_ended_by_start_refusal")
        self.assertEqual([(e["delivery"], e["delivered"], e["launched_turn"]) for e in ended],
                         [(self.intent["token"], False, 2)])

    def test_the_cli_words_on_the_card_stay_inside_their_code_span(self):
        crafted = "error: unknown flag `--x` [docs](https://example.invalid/d) **bold**\n"
        self.launch([], crafted, harness="zcode")
        self.assert_kept_unsent(count=1)
        self.launch([], crafted, harness="zcode")
        self.assertFalse(self.reconcile())
        line = next(ln for ln in self.bodies().splitlines() if "startup: `" in ln)
        span = line.split("startup: `", 1)[1]
        excerpt = "error: unknown flag '--x' [docs](https://example.invalid/d) **bold**"
        self.assertEqual(span[:len(excerpt) + 1], excerpt + "`")     # the span closes right after it

    def test_reading_the_same_launch_again_counts_it_once(self):
        # an operator recheck may read a launch that was already found unsent: it is still one refused start
        self.launch(REFUSED, REFUSED_STDERR)
        self.assert_kept_unsent(count=1)
        self.assertFalse(self.reconcile())
        a = self.r.store.get(self.a.id)
        self.assertEqual(a.state, "ready")
        self.assertEqual(len(self.details("headless_start_refused")), 1)
        self.assertEqual(self.details("prompt_ended_by_start_refusal"), [])
        self.assertNotIn("RUNNER GAVE UP", self.bodies())

    def test_a_delivered_prompt_ends_the_run_of_refused_starts(self):
        self.launch(REFUSED, REFUSED_STDERR)
        self.assert_kept_unsent(count=1)
        self.launch(FRESH, "", returncode=0)                  # the next start went through
        self.assertTrue(self.reconcile())
        self.assertFalse((self.r.store.mark(self.agent.name, self.t["id"], START_REFUSED) or ("",))[0])
        self.launch(REFUSED, REFUSED_STDERR)                  # a later refusal starts a new run
        self.assert_kept_unsent(count=1)

    # -- controls: these keep today's reading ------------------------------------------------------------

    def test_a_conversation_that_opened_under_another_session_stays_a_mismatch(self):
        # a resume agy silently forked: the conversation opened, then the CLI failed
        self.launch([INIT], REFUSED_STDERR, session=OTHER)
        self.assertEqual(self.verdict(), "headless-native-session-mismatch")
        self.assert_held_unknown()

    def test_a_result_naming_a_conversation_a_turn_or_usage_is_no_refused_start(self):
        for stream in (result_event(conversation_id=CID), result_event(num_turns=1),
                       result_event(usage={"input_tokens": 12, "output_tokens": 0})):
            with self.subTest(stream=stream[0]["result"].get("conversation_id") or stream[0]["result"]["num_turns"]):
                self.a = None
                self.launch(stream, REFUSED_STDERR, session=OTHER)
                self.assertNotEqual(self.verdict(), HEADLESS_START_UNSENT)

    def assert_no_refused_start(self, streams):
        for name, stream in streams:
            with self.subTest(stream=name):
                self.a = None
                self.launch(stream, REFUSED_STDERR, session=OTHER)
                self.assertNotEqual(self.verdict(), HEADLESS_START_UNSENT)
                self.assert_held_unknown()

    def test_a_result_without_a_conversation_that_is_no_error_is_no_refused_start(self):
        # a success, a cancel or an untyped result does not prove the prompt reached no model
        self.assert_no_refused_start((("success", result_event(status="SUCCESS", error="", response="OK\n")),
                                      ("canceled", result_event(status="CANCELED")),
                                      ("no status", result_event(status=None)),
                                      ("no error text", result_event(error=""))))

    def test_a_result_without_a_conversation_that_is_incomplete_is_no_refused_start(self):
        no_usage, no_turns = result_event(), result_event()
        del no_usage[0]["result"]["usage"], no_turns[0]["result"]["num_turns"]
        self.assert_no_refused_start((("no usage", no_usage), ("empty usage", result_event(usage={})),
                                      ("no turn count", no_turns),
                                      ("string usage", result_event(usage={"input_tokens": "0"}))))

    def test_no_recorded_exit_or_a_signal_keeps_the_hold(self):
        for returncode in (None, 0, -15):
            with self.subTest(returncode=returncode):
                self.a = None
                self.launch(REFUSED, REFUSED_STDERR, returncode=returncode)
                self.assert_held_unknown()

    def test_a_process_not_known_to_have_exited_keeps_the_hold(self):
        self.launch(REFUSED, REFUSED_STDERR, returncode=None, phase="native-exec-confirmed")
        self.assertNotEqual(self.verdict(), HEADLESS_START_UNSENT)

    def test_limit_and_login_refusals_before_a_conversation_keep_their_reading(self):
        for error in ("RESOURCE_EXHAUSTED: You have exhausted your capacity on this model.",
                      "authentication required. Run 'agy' to log in."):
            with self.subTest(error=error[:20]):
                self.a = None
                self.launch(result_event(error=error), "error: " + error + "\n")
                self.assertNotEqual(self.verdict(), HEADLESS_START_UNSENT)

    def test_a_policy_refusal_before_any_stream_keeps_its_reading(self):
        for harness in ("codex", "zcode"):
            with self.subTest(harness=harness):
                self.a = None
                self.launch([], POLICY_STDERR, harness=harness)
                self.assertNotEqual(self.verdict(), HEADLESS_START_UNSENT)
                self.assert_held_unknown("headless-native-result-incomplete")   # an empty stream, as before

    def test_a_codex_turn_that_started_or_failed_stays_a_mismatch(self):
        failed = {"type": "turn.failed", "error": {"message": "unknown model"}}
        for name, stream in (("turn failed", [{"type": "turn.started"}, failed]), ("bare turn failed", [failed])):
            with self.subTest(stream=name):
                self.a = None
                self.launch(stream, "", harness="codex")
                self.assert_held_unknown()

    def test_a_source_review_keeps_its_reading(self):
        # a source review ends on its own path and never goes up to a lead
        self.launch(REFUSED, REFUSED_STDERR, mode=source_review.MODE)
        self.assertEqual(self.verdict(), "headless-native-session-mismatch")

    def test_kimi_after_its_version_line_stays_a_mismatch(self):
        # Kimi prints its version before it sends the prompt; a provider error after it is no refused start
        self.launch([KIMI_VERSION], KIMI_SERVER, harness="kimi")
        self.assert_held_unknown()

    def test_agy_stream_opened_reads_only_the_typed_refusal_as_not_opened(self):
        ad = self.r.alt_adapters["agy"]
        self.assertFalse(ad.stream_opened([]))
        self.assertFalse(ad.stream_opened(REFUSED))
        self.assertTrue(ad.stream_opened([INIT]))
        self.assertTrue(ad.stream_opened(FRESH))
        self.assertTrue(ad.stream_opened(REFUSED + [INIT]))
        self.assertTrue(ad.stream_opened(result_event(usage="n/a")))
        self.assertTrue(ad.stream_opened(result_event(status="SUCCESS")))
        self.assertTrue(ad.stream_opened(result_event(usage={})))
        self.assertTrue(self.r.alt_adapters["codex"].stream_opened([{"type": "thread.started", "thread_id": "t"}]))
        self.assertFalse(self.r.alt_adapters["codex"].stream_opened([]))
        codex = self.r.alt_adapters["codex"]
        self.assertFalse(codex.stream_opened(CODEX_REFUSED))
        self.assertFalse(codex.stream_opened([{"type": "error", "error": {"message": "bad flag"}}]))
        self.assertTrue(codex.stream_opened([{"type": "error", "message": ""}]))
        self.assertTrue(codex.stream_opened([{"type": "thread.started", "thread_id": "t"}] + CODEX_REFUSED))
        self.assertTrue(codex.stream_opened([{"type": "turn.failed", "error": {"message": "x"}}]))
