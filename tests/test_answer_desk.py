"""The answer desk: a question to another lane of this runner is answered without a request card, and the card
is filed after all whenever the desk cannot answer (never a lost question)."""
import datetime as dt
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from mesh_runner import answer_desk
from mesh_runner.answer_desk import AnswerDesk, desk_plan, thread_of
from mesh_runner.chat_delivery import window_key
from mesh_runner.interactive import write_meta
from mesh_runner.mesh import MeshError
from mesh_runner.store import Store

from fakes import AGENT_ID, AgentView, FakeMesh, calls, task
from test_typed_outcome import TypedTurn

REQUEST = "6f1c2a90-1b2c-4d5e-8f90-0123456789ab"
OTHER = "7a2d3b01-2c3d-4e5f-9a01-123456789abc"
QUESTION = "ship v15 or hold it?"


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now

    def advance(self, minutes):
        self.now += minutes * 60


def lead_lane(mesh, name="lead", agent_id="agent-lead", workspace="ws1"):
    """An addressee lane of the same runner as the desk sees it: its identity, its key's view of Mesh, its
    pause and host holds, its transport gate and its event log."""
    ar = SimpleNamespace(agent=SimpleNamespace(name=name), me={"id": agent_id, "workspace_id": workspace},
                         mesh=AgentView(mesh, agent_id, name, workspace), launch_mode="interactive",
                         memgate=SimpleNamespace(holding=False), loadgate=SimpleNamespace(holding=False),
                         is_paused=False, gate_hold="", events=[])
    ar.paused = lambda: ar.is_paused
    ar.emit = lambda kind, a=None, **detail: ar.events.append((kind, detail))
    ar._gate = lambda phase, task_id, mode: (ar.gate_hold, {})
    return ar


class Desk(TypedTurn):
    """An asking lane (a real AgentRunner on the fake Mesh) and an addressee lane ``lead`` on one desk."""

    def setUp(self):
        super().setUp()
        self.cfg.mesh_effects = True
        self.cfg.cardless_question_lanes = ["lead"]
        self.clock = Clock()
        self.turns = []                 # (lane, request, task, prompt) of each desk turn
        self.answer = True              # the fake session answers through answer_decision

    def assertNoInabilityThroughTheCommand(self, text):
        """A non-answer must not close the request: work the session cannot do goes to the request card."""
        low = text.casefold()
        for phrase in ("say so through `answer_decision`", "or why you cannot answer",
                       "if you cannot answer, say so"):
            self.assertNotIn(phrase, low)

    def desk(self, mesh, r, lead, spawn=None):
        def run_turn(ar, rid, task_id, text):
            self.turns.append((ar.agent.name, rid, task_id, text))
            if self.answer:
                mesh.answer_decision(rid, "ship it: the canary is green", author=ar.me["id"])
            return "end_turn"
        desk = AnswerDesk(self.cfg, [r, lead], Store(self.root / "state" / "desk.sqlite"), clock=self.clock,
                          spawn=spawn or (lambda fn: fn()), run_turn=run_turn, tmux=SimpleNamespace(
                              all_windows=lambda: [], kill_window=lambda s, w: None))
        return desk.attach()

    def ask(self, mesh, t, rid=REQUEST, question=QUESTION):
        """What Mesh's ask_agent leaves: the typed decision wait, its comment, the open request."""
        self.typed(mesh, t, "wait", {"kind": "decision", "decision_request_id": rid, "note": "asked lead"},
                   note="asked lead")
        mesh.comment_log.setdefault(t["id"], []).append({
            "id": "c-" + rid[:8], "author_id": AGENT_ID, "author_name": "builder", "author_type": "agent",
            "body": f"❔ **Question to lead** · request `{rid}`\n\n{question}",
            "metadata": {"wakes": False, "command": "ask_agent", "decision_request_id": rid},
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat()})
        mesh.open_decision(rid, t["id"], question)

    def parked(self, mesh, t, desk=None, lead=None):
        """Run the asking turn to its end: the attempt parks on the question."""
        r = self.run_turn(mesh, t)
        lead = lead or lead_lane(mesh)
        desk = desk if desk is not None else self.desk(mesh, r, lead)
        if desk:
            desk.step()                                   # the probe: the desk is live on this Mesh
        self.ask(mesh, t)
        a = self.finish_turn(r)
        self.assertEqual((a.state, a.wait["kind"]), ("waiting", "decision"))
        self.clock.advance(1)                             # past the probe's poll interval
        return r, lead, desk, a


class Off(Desk):
    def test_off_by_default_nothing_is_built_and_the_card_is_filed_as_before(self):
        self.cfg.cardless_question_lanes = []
        self.assertEqual(type(self.cfg).__dataclass_fields__["cardless_question_lanes"].default_factory(), [])
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.run_turn(mesh, t)
        self.assertIsNone(answer_desk.build(self.cfg, [r, lead_lane(mesh)]))
        self.assertIsNone(r.effects.desk)
        self.ask(mesh, t)
        a = self.finish_turn(r)
        self.assertEqual(len(mesh.created), 1)
        self.assertEqual(a.wait["request"]["id"], mesh.created[0]["id"])
        self.assertNotIn("cardless", a.wait)
        self.assertFalse(hasattr(mesh, "decision_reads"))           # no new network call
        self.assertEqual(r.store.marked("lead", answer_desk.MARK), {})

    def test_a_listed_lane_name_that_is_not_in_this_runner_builds_nothing(self):
        self.cfg.cardless_question_lanes = ["nobody"]
        mesh = FakeMesh([task(1)])
        with self.assertLogs("mesh_runner", "WARNING"):
            self.assertIsNone(answer_desk.build(self.cfg, [lead_lane(mesh)], store=Store(self.root / "s.sqlite")))


class Answered(Desk):
    def test_a_listed_addressee_answers_on_its_desk_and_no_card_is_filed(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r, lead, desk, a = self.parked(mesh, t)
        self.assertEqual(mesh.created, [])
        self.assertEqual(a.wait["cardless"]["lane"], "lead")
        self.assertNotIn("ask_filed", a.wait)
        self.assertEqual(desk.state("lead", REQUEST), "queued")
        self.assertIn(("decision_cardless", f"lead {REQUEST}"), [(k, d) for _, k, d in r.store.events(a.id)])
        desk.step()
        self.assertEqual(len(self.turns), 1)
        lane, rid, task_id, text = self.turns[0]
        self.assertEqual((lane, rid, task_id), ("lead", REQUEST, t["id"]))
        for part in (QUESTION, REQUEST, f"#{t['id'][:8]}", "task 1", "builder", "answer_decision",
                     "Do not take, move or edit", "no worktree of its own", "change no files",
                     "do NOT call `answer_decision`", "ordinary request card"):
            self.assertIn(part, text)
        self.assertNoInabilityThroughTheCommand(text)
        self.assertEqual(desk.state("lead", REQUEST), "answered")
        self.assertTrue(any("ship it" in c["body"] for c in mesh.comment_log[t["id"]]))   # on the asking card
        # answered: no second turn, and the asker's later passes file nothing
        desk.step()
        self.clock.advance(5)
        desk.step()
        r.tick()
        self.assertEqual(len(self.turns), 1)
        self.assertEqual(mesh.created, [])

    def test_events_and_the_status_block(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r, lead, desk, a = self.parked(mesh, t)
        desk.step()
        kinds = [k for k, _ in lead.events]
        self.assertEqual(kinds, ["answer_started", "answer_done"])
        detail = lead.events[1][1]
        self.assertEqual((detail["request"], detail["task"], detail["from"], detail["to"], detail["turns"]),
                         (REQUEST, t["id"], "builder", "lead", 1))
        rows = [{"agent": "builder"}, {"agent": "lead"}]
        desk.annotate(rows)
        self.assertNotIn("answer_desk", rows[0])                     # not listed: no block
        self.assertEqual(rows[1]["answer_desk"],
                         {"open": 0, "running": "", "answered_today": 1, "failed_today": 0})

    def test_the_session_is_keyed_by_the_asking_card(self):
        """Every question about one card goes to one session: the window key is the asking task's, and a
        second question resumes the session the first one started."""
        from mesh_runner.interactive import InteractiveAdapter
        from test_chat_host import Stub
        state = Path(tempfile.mkdtemp())
        ad = Stub(state)
        ad.__class__ = type("A", (Stub, InteractiveAdapter), {})
        ar = SimpleNamespace(_routed_harness="claude", agent=SimpleNamespace(profile_dir=state, harness="claude", name="lead"),
                             cfg=SimpleNamespace(state_dir=state), _read_env=lambda: {"HOME": "/h", "MESH_AGENT_KEY": "k"},
                             _route_env=lambda raw: (dict(raw), "claude", None), adapter_for=lambda h: ad)
        card = task(1)["id"]
        argv, cwd, env, text, harness = desk_plan(ar, card, "first question")({"turn_id": "t1",
                                                                                "thread_id": thread_of(card)})
        key = window_key(thread_of(card))
        self.assertEqual(argv[argv.index("--key") + 1], key)
        self.assertTrue(key.startswith("chat-ask") and card.replace("-", "")[:9] in key)
        self.assertIn("--session-id", argv)
        self.assertIn("--disallowedTools", argv)                     # write tools denied
        self.assertEqual((cwd, text, env.get("MESH_AGENT_KEY")), (state, "first question", "k"))
        sid = argv[argv.index("--session-id") + 1]
        write_meta(ad.dir, key, {"session_id": sid})                 # what the driver records
        argv2, *_ = desk_plan(ar, card, "second question")({"turn_id": "t2", "thread_id": thread_of(card)})
        self.assertEqual(argv2[argv2.index("--key") + 1], key)
        self.assertEqual(argv2[argv2.index("--resume") + 1], sid)
        other, *_ = desk_plan(ar, task(2)["id"], "q")({"turn_id": "t3", "thread_id": thread_of(task(2)["id"])})
        self.assertNotEqual(other[other.index("--key") + 1], key)
        # the lane's ordinary tools when the addressee is not confined: nothing is denied by the desk
        full, *_ = desk_plan(ar, task(3)["id"], "q", read_only=False)({"turn_id": "t4",
                                                                         "thread_id": thread_of(task(3)["id"])})
        self.assertNotIn("--disallowedTools", full)

    def test_only_the_confined_addressees_answer_without_commands(self):
        """A request card confines the addressees of ``decision_request_metadata_only_recipients`` and gives
        everybody else the lane's tools; the desk keeps that split, in the session and in the prompt."""
        from mesh_runner.answer_desk import prompt
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r, lead, desk, a = self.parked(mesh, t)
        self.assertFalse(desk.read_only(lead))
        self.cfg.decision_request_metadata_only_recipients = [lead.me["id"]]
        self.assertTrue(desk.read_only(lead))
        item = {"id": REQUEST, "task_id": task(1)["id"], "question": QUESTION, "from_agent": {"name": "builder"},
                "task": {"title": "t"}}
        confined, free = prompt("lead", item, True), prompt("lead", item, False)
        self.assertIn("cannot run commands", confined)
        self.assertNotIn("cannot run commands", free)
        self.assertIn("change no files", free)
        for text in (confined, free):
            self.assertIn("do NOT call `answer_decision`", text)
            self.assertIn("ordinary request card", text)
            self.assertNoInabilityThroughTheCommand(text)


class Drive(unittest.TestCase):
    def test_a_turn_is_one_driver_delivery_keyed_by_the_asking_card(self):
        """The production turn: a DriverDelivery (the chat turn runner) over the desk plan, in the window of
        the asking card; it ends at the delivery's final or error event."""
        from unittest import mock
        seen = {}

        class FakeDelivery:
            def __init__(self, harness, plan, log_dir):
                seen.update(harness=harness, plan=plan, log_dir=log_dir)

            def run(self, turn, cancelled):
                seen.update(turn=turn, cancelled=cancelled())
                yield {"type": "text_delta", "text": "answered"}
                yield {"type": "final", "stop_reason": "end_turn"}
        state = Path(tempfile.mkdtemp())
        cfg = SimpleNamespace(state_dir=state, cardless_question_lanes=["lead"], tmux_binary="tmux")
        ar = SimpleNamespace(agent=SimpleNamespace(name="lead", harness="claude"), _routed_harness="codex")
        desk = AnswerDesk(cfg, [ar], Store(state / "s.sqlite"))
        card = task(1)["id"]
        with mock.patch("mesh_runner.chat_delivery.DriverDelivery", FakeDelivery):
            ended = desk._drive(ar, REQUEST, card, "the question")
        self.assertEqual(ended, "end_turn")
        self.assertEqual(seen["harness"], "codex")
        self.assertEqual(seen["turn"]["thread_id"], thread_of(card))
        self.assertFalse(seen["cancelled"])
        self.assertEqual(seen["log_dir"], state / "answer_desk" / "logs")


class Unanswered(Desk):
    def test_one_reminder_then_failed_then_the_asker_files_the_card(self):
        self.answer = False
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r, lead, desk, a = self.parked(mesh, t)
        with self.assertLogs("mesh_runner", "WARNING"):
            desk.step()
        self.assertEqual(len(self.turns), 2)
        self.assertIn("still open", self.turns[1][3])
        self.assertIn(REQUEST, self.turns[1][3])
        self.assertIn("reply with one line saying so and stop", self.turns[1][3])
        self.assertIn("reach you as a request card", self.turns[1][3])
        for text in (self.turns[0][3], self.turns[1][3]):
            self.assertNoInabilityThroughTheCommand(text)
        self.assertEqual(desk.state("lead", REQUEST), "failed")
        self.assertEqual([k for k, _ in lead.events], ["answer_started", "answer_failed"])
        self.assertEqual(mesh.created, [])
        r.tick()                                                      # the asker's next pass over its wait
        a = r.store.get(a.id)
        self.assertEqual(len(mesh.created), 1)
        self.assertIn(QUESTION, mesh.created[0]["description"])
        self.assertEqual(mesh.created[0]["assignee_id"], "agent-lead")
        self.assertEqual(a.wait["request"]["id"], mesh.created[0]["id"])
        self.assertNotIn("cardless", a.wait)
        self.assertIn("could not get an answer", a.wait["desk_fallback"])
        desk.step()
        r.tick()
        self.assertEqual((len(self.turns), len(mesh.created)), (2, 1))   # nothing repeats

    def test_after_twenty_minutes_the_card_is_filed(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r, lead, desk, a = self.parked(mesh, t)
        lead.memgate.holding = True                                   # the desk cannot run it meanwhile
        desk.step()
        r.tick()
        self.clock.advance(18)
        desk.step()
        r.tick()
        self.assertEqual((self.turns, mesh.created), ([], []))
        self.clock.advance(2)
        r.tick()
        self.assertEqual(len(mesh.created), 1)
        a = r.store.get(a.id)
        self.assertIn("20 minutes", a.wait["desk_fallback"])
        self.assertEqual(desk.state("lead", REQUEST), "carded")
        lead.memgate.holding = False
        desk.step()
        self.assertEqual(self.turns, [])                              # the card has it now

    def test_a_paused_addressee_gets_the_card(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r, lead, desk, a = self.parked(mesh, t)
        lead.is_paused = True
        desk.step()
        r.tick()
        self.assertEqual(self.turns, [])
        self.assertEqual(len(mesh.created), 1)
        self.assertEqual(r.store.get(a.id).wait["desk_fallback"], "paused")

    def test_paused_at_ask_time_files_the_card_at_once(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        lead = lead_lane(mesh)
        lead.is_paused = True
        r, lead, desk, a = self.parked(mesh, t, lead=lead)
        self.assertEqual(len(mesh.created), 1)
        self.assertNotIn("cardless", a.wait)

    def test_an_addressee_no_longer_listed_gets_the_card(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r, lead, desk, a = self.parked(mesh, t)
        r.effects.desk = None                                         # restarted with the option emptied
        r.tick()
        self.assertEqual(len(mesh.created), 1)
        self.assertEqual(r.store.get(a.id).wait["desk_fallback"], "the answer desk is off")


class NoRoute(Desk):
    def test_a_mesh_without_the_list_keeps_the_desk_idle_and_the_card_is_filed_at_once(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        mesh.decision_error = MeshError(404, "not found")
        with self.assertLogs("mesh_runner", "WARNING"):
            r, lead, desk, a = self.parked(mesh, t)
        self.assertEqual(len(mesh.created), 1)                        # at once, not after a deferral
        self.assertNotIn("cardless", a.wait)
        reads = mesh.decision_reads
        for _ in range(3):
            desk.step()
        self.assertEqual((mesh.decision_reads, self.turns), (reads, []))   # idle for ten minutes
        self.assertIn("desk disabled", desk.status("lead")["hold"])
        self.clock.advance(11)
        mesh.decision_error = None
        desk.step()
        self.assertEqual(desk.hold("lead"), "")                       # asked again after ten minutes

    def test_another_read_error_is_skipped_and_retried(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r, lead, desk, a = self.parked(mesh, t)
        mesh.decision_error = MeshError(503, "unavailable")
        self.clock.advance(1)
        with self.assertLogs("mesh_runner", "WARNING"):
            desk.step()
        self.assertEqual(self.turns, [])
        self.assertEqual(desk.state("lead", REQUEST), "queued")
        mesh.decision_error = None
        desk.step()                                                   # within 30 s of the failed read: no read
        self.assertEqual(self.turns, [])
        self.clock.advance(1)
        desk.step()
        self.assertEqual(len(self.turns), 1)


class CrossWorkspace(Desk):
    def test_a_card_outside_the_addressees_workspace_files_the_card_at_once(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        lead = lead_lane(mesh, workspace="ws2")
        r, lead, desk, a = self.parked(mesh, t, lead=lead)
        self.assertEqual(len(mesh.created), 1)
        self.assertNotIn("cardless", a.wait)


class Restart(Desk):
    def queued(self, mesh, desk, t, rid, state, minutes_ago, **extra):
        mesh.open_decision(rid, t["id"], QUESTION)
        at = dt.datetime.fromtimestamp(self.clock() - minutes_ago * 60, dt.timezone.utc).isoformat()
        desk._set("lead", rid, {"state": state, "at": at, "since": at, "task": t["id"], "from": "builder", **extra})

    def test_a_running_request_left_by_a_crash_is_retried_once_after_its_lease(self):
        t = task(1)
        mesh = FakeMesh([t])
        lead = lead_lane(mesh)
        desk = AnswerDesk(self.cfg, [lead], Store(self.root / "d.sqlite"), clock=self.clock,
                          spawn=lambda fn: fn(), run_turn=lambda ar, rid, tid, text: self.turns.append(text) or "")
        self.queued(mesh, desk, t, REQUEST, "running", 5)
        desk.step()
        self.assertEqual(self.turns, [])                               # its turn may still be live
        self.clock.advance(11)
        with self.assertLogs("mesh_runner", "WARNING"):
            desk.step()
        self.assertEqual(len(self.turns), 2)                           # the question, then the reminder
        self.assertEqual(desk.state("lead", REQUEST), "failed")
        self.assertTrue(desk._get("lead", REQUEST)["recovered"])

    def test_a_crashed_retry_is_not_retried_again(self):
        t = task(1)
        mesh = FakeMesh([t])
        lead = lead_lane(mesh)
        desk = AnswerDesk(self.cfg, [lead], Store(self.root / "d.sqlite"), clock=self.clock,
                          spawn=lambda fn: fn(), run_turn=lambda ar, rid, tid, text: self.turns.append(text) or "")
        self.queued(mesh, desk, t, REQUEST, "running", 16, recovered=True, turns=1)
        with self.assertLogs("mesh_runner", "WARNING"):
            desk.step()
        self.assertEqual(self.turns, [])
        self.assertEqual(desk.state("lead", REQUEST), "failed")       # the asker files the card

    def test_a_crash_after_the_first_turn_retries_with_the_reminder_only(self):
        t = task(1)
        mesh = FakeMesh([t])
        lead = lead_lane(mesh)

        def run_turn(ar, rid, tid, text):
            self.turns.append(text)
            mesh.answer_decision(rid, "yes", author="agent-lead")
            return ""
        desk = AnswerDesk(self.cfg, [lead], Store(self.root / "d.sqlite"), clock=self.clock,
                          spawn=lambda fn: fn(), run_turn=run_turn)
        self.queued(mesh, desk, t, REQUEST, "running", 16, turns=1)
        desk.step()
        self.assertEqual(len(self.turns), 1)
        self.assertIn("still open", self.turns[0])
        self.assertEqual(desk.state("lead", REQUEST), "answered")

    def test_answered_and_failed_requests_are_not_repeated_after_a_restart(self):
        t = task(1)
        mesh = FakeMesh([t])
        lead = lead_lane(mesh)
        desk = AnswerDesk(self.cfg, [lead], Store(self.root / "d.sqlite"), clock=self.clock,
                          spawn=lambda fn: fn(), run_turn=lambda ar, rid, tid, text: self.turns.append(text) or "")
        self.queued(mesh, desk, t, REQUEST, "answered", 1)
        self.queued(mesh, desk, t, OTHER, "failed", 1)
        fresh = AnswerDesk(self.cfg, [lead], Store(self.root / "d.sqlite"), clock=self.clock,
                           spawn=lambda fn: fn(), run_turn=lambda ar, rid, tid, text: self.turns.append(text) or "")
        fresh.step()
        self.assertEqual(self.turns, [])
        self.assertEqual((fresh.state("lead", REQUEST), fresh.state("lead", OTHER)), ("answered", "failed"))


class Order(Restart):
    def test_two_requests_of_one_lane_run_one_after_another(self):
        t1, t2 = task(1), task(2)
        mesh = FakeMesh([t1, t2])
        lead = lead_lane(mesh)
        pending = []
        ran = []

        def run_turn(ar, rid, tid, text):
            ran.append(rid)
            mesh.answer_decision(rid, "yes", author="agent-lead")
            return ""
        desk = AnswerDesk(self.cfg, [lead], Store(self.root / "d.sqlite"), clock=self.clock,
                          spawn=pending.append, run_turn=run_turn)
        self.queued(mesh, desk, t1, REQUEST, "queued", 2)
        self.queued(mesh, desk, t2, OTHER, "queued", 1)
        desk.step()
        desk.step()
        self.assertEqual(len(pending), 1)                              # one turn per lane at a time
        self.assertEqual(desk.status("lead")["running"], REQUEST)      # the oldest first
        pending.pop()()
        self.assertEqual((ran, desk.state("lead", REQUEST)), ([REQUEST], "answered"))
        desk.step()
        self.assertEqual(len(pending), 1)
        pending.pop()()
        self.assertEqual((ran, desk.state("lead", OTHER)), ([REQUEST, OTHER], "answered"))

    def test_a_request_the_desk_was_not_given_is_ignored(self):
        """Open requests of the lane that no asker of this runner put on the desk (another runner's, or one
        that has its card), and requests for other lanes, start nothing."""
        t = task(1)
        mesh = FakeMesh([t])
        lead, reviewer = lead_lane(mesh), lead_lane(mesh, "reviewer", "agent-reviewer")
        desk = AnswerDesk(self.cfg, [lead, reviewer], Store(self.root / "d.sqlite"), clock=self.clock,
                          spawn=lambda fn: fn(), run_turn=lambda ar, rid, tid, text: self.turns.append(rid) or "")
        mesh.open_decision(REQUEST, t["id"], QUESTION)                 # lead's, never deferred to its desk
        self.queued(mesh, desk, t, OTHER, "queued", 1)
        mesh.decision_rows[-1]["to_agent_id"] = "agent-reviewer"        # a queued request listed for another lane
        for _ in range(3):
            desk.step()
            self.clock.advance(1)
        self.assertEqual(self.turns, [])
        self.assertEqual(desk.hold("reviewer"), "not listed")


if __name__ == "__main__":
    unittest.main()
