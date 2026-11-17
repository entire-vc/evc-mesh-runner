"""A finished turn is not an unknown delivery. Such turns ended as `prompt_delivery_unknown`,
each one +15 min of a held card and an operator alert saying "the session did not confirm it took the prompt":

- `cursor-mismatch`: the Runner itself stopped the turn for a lost lock (reassigned, released or expired) with
  the driver's journal at `accepted`. The stop closes the window and `interactive.forget` drops its hook events, so
  recovery read an empty native stream. The end of such a turn is known: it ends as a lock-lost turn, the prompt is
  archived, never replayed, no hold, no alert.
- `headless-native-terminal-mismatch`: headless claude wrote 2-3 `result` of the same session (a background
  task's notification adds its own, num_turns 0). All bound to the session: the turn is acknowledged.
- a `cursor-mismatch` the Runner did not cause, with the journal at `accepted`, still holds the card, but it no
  longer tells anyone the session did not confirm it took the prompt.
"""
import hashlib
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

from fakes import AGENT_ID, FakeMesh, task
from mesh_runner import interactive
from mesh_runner.adapters.base import group_alive, proc_stamp
from test_runner import Bench

REASSIGNED = "reassigned: the card was given to another assignee while the turn ran"
RELEASED = "released or expired: nobody holds the lock this turn worked under"


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    stamp = proc_stamp(p.pid)
    p.wait(timeout=10)
    return p.pid, stamp


class InteractiveTurn(Bench):
    """An interactive claude turn 2 whose driver journal is at ``accepted`` (the session took the prompt and worked),
    with a pending CI wake riding on its prompt. The window's hook events once held one event (native_base=1)."""

    PHASE = "accepted"

    def setUp(self):
        super().setUp()
        self.t = task(1, cat="in_progress")
        self.mesh = FakeMesh([self.t])
        self.r = self.runner(self.mesh)
        self.r.refresh_me()
        self.assertEqual(self.r.agent_id, AGENT_ID)
        self.children = []
        a = self.r.store.create(self.agent.name, self.t["id"], "claude", self.t["title"])
        self.directory = interactive.agent_dir(self.cfg.state_dir, self.agent.name)
        self.directory.mkdir(parents=True, exist_ok=True)
        log = self.root / f"{a.id}.turn2.claude.jsonl"
        log.write_text("")                       # an interactive turn log stays empty until its driver ends it
        self.intent = {"version": 1, "token": uuid.uuid4().hex, "sha256": "2" * 64, "turn": 2,
                       "kind": "interactive", "harness": "claude", "log_path": str(log), "prompt": "PROMPT",
                       "session_id": str(uuid.uuid4()), "source_read_at": "", "resume": True,
                       "prior": {"turns": 1, "session_id": "", "log_path": "", "turn_started_at": "",
                                 "source_read_at": ""}}
        start = [{"hook_event_name": "SessionStart", "session_id": self.intent["session_id"]}]
        self.events = self.directory / f"{a.id}.events.jsonl"
        self.events.write_text(json.dumps(start[0]) + "\n")
        interactive.write_delivery(self.directory, a.id, {
            "version": 1, "token": self.intent["token"], "sha256": self.intent["sha256"], "phase": self.PHASE,
            "harness": "claude", "session_id": self.intent["session_id"], "cwd": str(self.root),
            "native_base": 1, "native_prefix": hashlib.sha256(json.dumps(start, sort_keys=True).encode()).hexdigest(),
            "offset": 0, "meta": {}})
        self.pending = {"ci_delivery": {"key": "fixture-ci", "value": "failed:1"}}
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        self.children.append(p)
        self.r.procs[a.id] = p
        self.assertTrue(self.r.store.update(a, expect="starting", state="running", pid=p.pid,
                                            stamp=proc_stamp(p.pid), turns=2, log_path=str(log),
                                            checkout={"token": "tok-1", "session_id": a.id},
                                            wait={**self.pending, "interactive_prompt_intent": self.intent}))
        self.a = self.r.store.get(a.id)

    def tearDown(self):
        for p in self.children:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=5)
        super().tearDown()

    def alerts(self):
        path = self.cfg.state_dir / "events.jsonl"
        if not path.exists():
            return []
        rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
        return [r for r in rows if r["kind"] == "operator_alert"]

    def held(self):
        return [c["body"] for c in self.mesh.comment_log.get(self.t["id"], [])
                if "MANDATORY DELIVERY PENDING" in c["body"]]

    def kinds(self, attempt_id=None):
        return [k for _, k, _ in self.r.store.events(attempt_id or self.a.id)]

    def details(self, kind):
        return [d for _, k, d in self.r.store.events(self.a.id) if k == kind]

    def stop_for_lost_lock(self):
        """The heartbeat finds the lock gone: the Runner's own stop, as the incident's ``kill`` events show."""
        why = self.r._lock_loss_reason(self.a, self.mesh.tasks[self.t["id"]])
        self.r._lock_lost(self.r.store.get(self.a.id), why)
        self.assertIn("kill", self.kinds())
        deadline = time.monotonic() + 10
        while group_alive(self.a.pid, self.a.stamp):
            self.assertLess(time.monotonic(), deadline, "the stopped turn's group outlived the kill")
            time.sleep(0.05)
        # the stop closes an interactive window, and with it ``interactive.forget`` drops the hook events
        interactive.forget(self.directory, self.a.id)
        return why

    def process_ends_by_itself(self):
        self.children[0].kill()
        self.children[0].wait(timeout=5)
        self.events.unlink()                     # the same empty native stream, without any stop of the Runner


class OwnLockStopEndsTheTurn(InteractiveTurn):

    def assert_ended_as_lock_lost(self, why):
        for _ in range(3):
            self.r.settle_finished()
        a = self.r.store.get(self.a.id)
        self.assertEqual(self.details("prompt_delivery_unknown"), [], "the end of the turn is known")
        self.assertEqual(self.alerts(), [])
        self.assertEqual(self.held(), [])
        self.assertEqual(a.state, "failed")
        self.assertEqual(a.note.split("; worktrees:")[0], "checkout lost: " + why)
        self.assertEqual(self.details("prompt_ended_by_lock_loss"), ["cursor-mismatch"])
        # the prompt is archived, never consumed or replayed; the pending wake stays undelivered
        self.assertNotIn("interactive_prompt_intent", a.wait)
        archived = json.loads(self.r.store.mark(self.agent.name, self.t["id"], "lock_lost_intent:" + a.id)[0])
        self.assertEqual(archived["intent"]["token"], self.intent["token"])
        self.assertEqual(archived["reason"], "checkout lost: " + why)
        self.assertIsNone(self.r.store.mark(self.agent.name, self.t["id"], "fixture-ci"))
        self.assertNotIn("prompt_unsent_reconciled", self.kinds())
        self.assertNotIn("prompt_native_result_recovered", self.kinds())
        self.assertEqual(self.r.store.terminal_with_intent(self.agent.name), [])
        for other in self.r.store.by_state(self.agent.name, ("starting", "running", "ready", "waiting", "unknown")):
            self.assertNotEqual((other.wait or {}).get("interactive_prompt_intent", {}).get("token"),
                                self.intent["token"])

    def test_reassigned_card_turn_ends_as_lock_lost_not_unknown(self):
        self.mesh.tasks[self.t["id"]]["assignee_id"] = "agent-lead"
        why = self.stop_for_lost_lock()
        self.assertEqual(why, REASSIGNED)
        self.assert_ended_as_lock_lost(why)

    def test_released_or_expired_lock_turn_ends_as_lock_lost_not_unknown(self):
        self.mesh.tasks[self.t["id"]].update(assignee_id=AGENT_ID, checked_out_by=None)
        why = self.stop_for_lost_lock()
        self.assertEqual(why, RELEASED)
        self.assert_ended_as_lock_lost(why)

    def test_a_stop_of_an_earlier_turn_is_not_this_turns_end(self):
        # positive control: the lock_lost_stop is keyed by turn; a stop of turn 1 says nothing about turn 2
        self.r.store.event(self.a.id, "lock_lost_stop", json.dumps({"turn": 1, "why": REASSIGNED}))
        self.process_ends_by_itself()
        self.r.settle_finished()
        a = self.r.store.get(self.a.id)
        self.assertEqual(a.state, "unknown")
        self.assertEqual(self.details("prompt_delivery_unknown"), ["cursor-mismatch"])
        self.assertIsNone(self.r.store.mark(self.agent.name, self.t["id"], "lock_lost_intent:" + a.id))


class CursorMismatchWithoutOwnStop(InteractiveTurn):
    """No stop of the Runner: the hold stays (nothing proves the end), but the driver's journal shows the session took
    the prompt, so the alert and the comment never say it did not confirm that."""

    def test_accepted_journal_is_held_but_never_called_unconfirmed(self):
        self.process_ends_by_itself()
        for _ in range(3):
            self.r.settle_finished()
        a = self.r.store.get(self.a.id)
        self.assertEqual(a.state, "unknown")
        self.assertEqual(self.details("prompt_delivery_unknown"), ["cursor-mismatch"])
        self.assertEqual(len(self.alerts()), 1)
        alert = self.alerts()[0]["text"]
        self.assertIn("took the prompt; the end of its turn could not be read", alert)
        self.assertNotIn("did not confirm", alert)
        held = self.held()
        self.assertEqual(len(held), 1)
        self.assertIn("The session took the last prompt, but the end of the turn could not be read", held[0])
        self.assertNotIn("did not confirm", held[0])
        self.assertIn("is not sent again", held[0])
        self.assertIn("MANDATORY DELIVERY PENDING: cursor-mismatch", held[0])


class CursorMismatchBeforeAcceptance(InteractiveTurn):
    """Positive control: a journal that never reached ``accepted`` keeps the old words."""

    PHASE = "send-intent"

    def test_unaccepted_journal_keeps_did_not_confirm(self):
        self.process_ends_by_itself()
        self.r.settle_finished()
        self.assertEqual(self.r.store.get(self.a.id).state, "unknown")
        self.assertIn("did not confirm it took the prompt", self.alerts()[0]["text"])
        self.assertIn("did not confirm that it received the last prompt", self.held()[0])


class HeadlessSeveralResults(Bench):
    """A headless claude turn whose process ended (journal ``native-exited``) with its terminal results in the log."""

    def setUp(self):
        super().setUp()
        self.t = task(2, cat="in_progress")
        self.mesh = FakeMesh([self.t])
        self.r = self.runner(self.mesh)
        a = self.r.store.create(self.agent.name, self.t["id"], "claude", self.t["title"])
        self.directory = interactive.agent_dir(self.cfg.state_dir, self.agent.name)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.log = self.root / f"{a.id}.turn3.claude.jsonl"
        self.sid = str(uuid.uuid4())
        self.intent = {"token": uuid.uuid4().hex, "sha256": "3" * 64, "turn": 3, "log_path": str(self.log),
                       "kind": "headless", "harness": "claude", "session_id": self.sid, "prompt": "PROMPT",
                       "source_read_at": "", "prior": {"turns": 2, "session_id": "", "log_path": "",
                                                       "turn_started_at": "", "source_read_at": ""}}
        interactive.write_delivery(self.directory, a.id, {"version": 1, "token": self.intent["token"],
                                                          "sha256": self.intent["sha256"], "phase": "native-exited"})
        pid, stamp = dead_pid()
        self.assertTrue(self.r.store.update(a, expect="starting", state="running", pid=pid, stamp=stamp, turns=3,
                                            log_path=str(self.log), wait={"interactive_prompt_intent": self.intent}))
        self.a = self.r.store.get(a.id)

    def stream(self, *results):
        """`(session, num_turns)` per result, each after its own init, as in the incident's turn logs."""
        rows = []
        for sid, turns in results:
            rows.append({"type": "system", "subtype": "init", "session_id": sid})
            rows.append({"type": "result", "subtype": "success", "is_error": False, "num_turns": turns,
                         "result": "done" if turns else "", "session_id": sid})
        self.log.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def reconcile(self):
        return self.r._reconcile_interactive_prompt(self.r.store.get(self.a.id))

    def details(self, kind):
        return [d for _, k, d in self.r.store.events(self.a.id) if k == kind]

    def assert_acknowledged(self):
        self.assertTrue(self.reconcile())
        a = self.r.store.get(self.a.id)
        self.assertEqual(a.state, "running")
        self.assertNotIn("interactive_prompt_intent", a.wait)
        self.assertEqual(self.details("prompt_native_result_recovered"), ["headless.bound-terminal"])
        self.assertEqual(self.details("prompt_delivery_unknown"), [])

    def test_two_results_of_this_session_acknowledge_the_turn(self):
        self.stream((self.sid, 0), (self.sid, 3))
        self.assert_acknowledged()

    def test_three_results_of_this_session_acknowledge_the_turn(self):
        self.stream((self.sid, 20), (self.sid, 0), (self.sid, 0))
        self.assert_acknowledged()

    def test_one_result_of_this_session_still_acknowledges(self):
        self.stream((self.sid, 4))
        self.assert_acknowledged()

    def test_a_result_of_another_session_stays_a_mismatch(self):
        # negative control: a terminal of any other session is ambiguous, whatever the others say
        self.stream((str(uuid.uuid4()), 0), (self.sid, 3))
        self.assertFalse(self.reconcile())
        a = self.r.store.get(self.a.id)
        self.assertEqual(a.state, "unknown")
        self.assertEqual(self.details("prompt_delivery_unknown"), ["headless-native-terminal-mismatch"])
        self.assertIn("interactive_prompt_intent", a.wait)


class OwnTurnCutEndsTheTurn(InteractiveTurn):
    """The Runner itself cut the turn at its time limit (``turn exceeded N min``) after the session took the prompt
    (journal ``accepted``). The cut window looks idle, so the next start on the host closes it to make room and
    ``interactive.forget`` drops its hook events: recovery read ``cursor-mismatch`` and held the card 15 min with an
    operator alert. The end of such a turn is known: no hold, no alert; the prompt is archived, never replayed."""

    def launch(self, turn):
        """The ``launch`` record the Runner writes when it starts a turn (``_record_launch``)."""
        self.r.store.event(self.a.id, "launch", json.dumps({"task": self.t["id"], "lane": self.agent.name,
                                                            "attempt": self.a.id, "turn": turn, "intent": "resume"}))

    def cut_at_turn_limit(self):
        """The real time limit: the turn's wall clock is past ``max_turn_minutes``, ``_turn_over`` kills its group."""
        self.r.cfg.max_turn_minutes = 0
        self.r.store.event(self.a.id, "state", "running")
        time.sleep(0.01)
        self.assertFalse(self.r._turn_over(self.r.store.get(self.a.id)))
        self.assertEqual(self.details("kill"), ["turn exceeded 0 min"])
        deadline = time.monotonic() + 10
        while group_alive(self.a.pid, self.a.stamp):
            self.assertLess(time.monotonic(), deadline, "the cut turn's group outlived the kill")
            time.sleep(0.05)

    def own_cut_comments(self):
        return [c["body"] for c in self.mesh.comment_log.get(self.t["id"], [])
                if "The turn was stopped by the Runner" in c["body"]]

    def assert_ended_by_own_cut(self, observed):
        for _ in range(3):
            self.r.settle_finished()
        a = self.r.store.get(self.a.id)
        self.assertEqual(self.details("prompt_delivery_unknown"), [], "the end of the turn is known")
        self.assertEqual(self.alerts(), [])
        self.assertEqual(self.held(), [])
        self.assertEqual(a.state, "completed")
        self.assertEqual(a.note.split("; worktrees:")[0],
                         "turn exceeded 0 min; the session had taken the prompt; not sent again")
        self.assertEqual(self.details("prompt_ended_by_own_cut"), [observed])
        comments = self.own_cut_comments()
        self.assertEqual(len(comments), 1)
        self.assertIn("(turn exceeded 0 min)", comments[0])
        self.assertIn("is not sent again", comments[0])
        self.assertNotIn("did not confirm", comments[0])
        # the prompt is archived, never consumed or replayed; the pending wake stays undelivered
        self.assertNotIn("interactive_prompt_intent", a.wait)
        archived = json.loads(self.r.store.mark(self.agent.name, self.t["id"], "own_cut_intent:" + a.id)[0])
        self.assertEqual(archived["intent"]["token"], self.intent["token"])
        self.assertEqual(archived["reason"], "turn exceeded 0 min")
        self.assertIsNone(self.r.store.mark(self.agent.name, self.t["id"], "fixture-ci"))
        self.assertNotIn("prompt_unsent_reconciled", self.kinds())
        self.assertNotIn("prompt_native_result_recovered", self.kinds())
        self.assertEqual(self.r.store.terminal_with_intent(self.agent.name), [])
        for other in self.r.store.by_state(self.agent.name, ("starting", "running", "ready", "waiting", "unknown")):
            self.assertNotEqual((other.wait or {}).get("interactive_prompt_intent", {}).get("token"),
                                self.intent["token"])

    def test_cut_turn_whose_window_another_start_closed_is_not_unknown(self):
        self.launch(2)
        self.cut_at_turn_limit()
        # another lane's start makes room: the cut window is idle, it is closed and its hook events forgotten
        interactive.forget(self.directory, self.a.id)
        self.assert_ended_by_own_cut("cursor-mismatch")

    def test_cut_turn_whose_window_is_still_open_is_not_unknown(self):
        # the hook stream holds the session's submit of this prompt and no end of its turn: it was cut mid-work
        with self.events.open("a") as fh:
            fh.write(json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": self.intent["session_id"],
                                 "prompt_id": "p-1", "prompt": self.intent["prompt"]}) + "\n")
        self.launch(2)
        self.cut_at_turn_limit()
        self.assertTrue(self.events.exists())
        self.assert_ended_by_own_cut("native-running-or-incomplete")

    def test_a_cut_of_an_earlier_turn_is_not_this_turns_end(self):
        # positive control: the cut is keyed by the turn's launch; a cut of turn 1 says nothing about turn 2
        self.launch(1)
        self.r.store.event(self.a.id, "kill", "turn exceeded 180 min")
        self.launch(2)
        self.process_ends_by_itself()
        self.r.settle_finished()
        a = self.r.store.get(self.a.id)
        self.assertEqual(a.state, "unknown")
        self.assertEqual(self.details("prompt_delivery_unknown"), ["cursor-mismatch"])
        self.assertIsNone(self.r.store.mark(self.agent.name, self.t["id"], "own_cut_intent:" + a.id))
        self.assertEqual(self.own_cut_comments(), [])


class OwnTurnCutBeforeAcceptance(OwnTurnCutEndsTheTurn):
    """Positive control: a cut turn whose journal never reached ``accepted`` stays ``unknown``: the delivery itself
    is unproven, so the hold and the old words stay."""

    PHASE = "send-intent"

    def test_cut_turn_whose_window_another_start_closed_is_not_unknown(self):
        self.launch(2)
        self.cut_at_turn_limit()
        interactive.forget(self.directory, self.a.id)
        self.r.settle_finished()
        a = self.r.store.get(self.a.id)
        self.assertEqual(a.state, "unknown")
        self.assertEqual(self.details("prompt_delivery_unknown"), ["cursor-mismatch"])
        self.assertIn("did not confirm it took the prompt", self.alerts()[0]["text"])
        self.assertIsNone(self.r.store.mark(self.agent.name, self.t["id"], "own_cut_intent:" + a.id))

    test_cut_turn_whose_window_is_still_open_is_not_unknown = None
