"""At max_turns an attempt that still makes progress continues in a fresh session on the same agent."""
import json
import tempfile
import unittest
from pathlib import Path

from mesh_runner import config as config_mod
from fakes import AGENT_ID, FakeMesh, calls, next_turn, task, turn_rows, write_replies, wait_time
from test_runner import Bench


class TurnCapBench(Bench):
    def setUp(self):
        super().setUp()
        self.cfg.max_turns = 12
        self.cfg.daily_start_cap = 4
        self.agent.lead = "lead"

    def capped(self, r, t, outcomes, *, turns=None):
        """An attempt at its turn cap, the given outcomes already declared, about to resume
        (the point where the cap of a work attempt is applied)."""
        r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        for kind in outcomes:
            r.store.outcome(a, kind)
        r.store.update(a, state="ready", turns=turns or self.cfg.max_turns, session_id="s-cap",
                       wake_text="continue")
        r._resume_one(r.store.get(a.id))
        return r.store.get(a.id)

    def declare(self, mesh, t, turn):
        """Mesh holds the turn command ``turn`` as the capped turn's typed outcome (and its wait)."""
        row, wait = turn_rows(turn)
        mesh.outcomes = {t["id"]: [row]}
        mesh.waits = {t["id"]: wait} if wait else {}

    def last_turn(self, r, t, outcomes, turn):
        """The capped turn itself settles through the production path, declaring ``turn``."""
        self.declare(r.mesh, t, turn)
        text = "done for now"
        r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        for kind in outcomes:
            r.store.outcome(a, kind)
        path = self.root / f"{a.id}.jsonl"
        path.write_text(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                    "result": text, "session_id": "s-cap"}) + "\n")
        r.store.update(a, state="running", turns=self.cfg.max_turns, log_path=str(path))
        r.settle(a)
        return r.store.get(a.id)

    def bodies(self, mesh, t):
        return [c["body"] for c in mesh.comment_log.get(t["id"], [])]


class Continue(TurnCapBench):
    def test_progressing_attempt_continues_on_the_same_agent_in_a_new_attempt(self):
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: wait_time(why="soak")})
        r = self.runner(mesh)
        a = self.capped(r, t, ["step", "step", "next", "wait", "step", "wait", "next", "step", "wait"])
        self.assertEqual(a.state, "completed")
        self.assertTrue(a.note.startswith("turn cap: continued in a fresh session"), a.note)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], AGENT_ID)         # no lead
        self.assertEqual(self.bodies(mesh, t), [])                              # no hand-up comment
        self.assertIn(("turn_cap", "continued: " + a.note), [(k, d) for _, k, d in r.store.events(a.id)])
        self.settle_all(r)                                                      # the next tick starts it afresh
        attempts = [x for x in r.store.by_state(self.agent.name, ("running", "waiting", "completed", "ready"))
                    if x.task_id == t["id"]]
        self.assertEqual(len(attempts), 2)
        fresh = next(x for x in attempts if x.id != a.id)
        self.assertEqual(fresh.turns, 1)
        self.assertEqual(r.store.starts_today(self.agent.name, t["id"]), 2)     # counted as a start

    def test_capped_turn_declaring_a_wait_waits_then_continues_on_wake(self):
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: wait_time(why="soak")})
        r = self.runner(mesh)
        a = self.last_turn(r, t, ["step", "next", "step"], wait_time(why="soak"))
        self.assertEqual(a.state, "waiting")                       # the wait is recorded, not lost to the cap
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], AGENT_ID)
        self.assertEqual(self.bodies(mesh, t), [])
        self.assertEqual(r.store.recent_outcomes(a.id, 1), ["wait"])
        r.store.update(a, state="ready", wake_text="the wait is over")        # the wake
        r._resume_one(r.store.get(a.id))
        a = r.store.get(a.id)
        self.assertEqual(a.state, "completed")
        self.assertTrue(a.note.startswith("turn cap: continued in a fresh session"), a.note)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], AGENT_ID)
        self.assertEqual(self.bodies(mesh, t), [])

    def test_capped_turn_declaring_a_wait_hands_up_on_wake_when_stagnant(self):
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        a = self.last_turn(r, t, ["wait"] * 5, wait_time(why="soak"))
        self.assertEqual(a.state, "waiting")
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], AGENT_ID)
        r.store.update(a, state="ready", wake_text="the wait is over")
        r._resume_one(r.store.get(a.id))
        a = r.store.get(a.id)
        self.assertIn("routed to lead", a.note)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-lead")
        self.assertIn("no recent progress", self.bodies(mesh, t)[0])

    def test_capped_turn_declaring_next_counts_and_continues(self):
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        a = self.last_turn(r, t, ["step", "step"], next_turn("more to do"))
        self.assertEqual(a.state, "ready")                         # the NEXT is recorded and counted
        self.assertEqual(r.store.recent_outcomes(a.id, 1), ["next"])
        r._resume_one(a)
        a = r.store.get(a.id)
        self.assertTrue(a.note.startswith("turn cap: continued in a fresh session"), a.note)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], AGENT_ID)

    def test_a_review_attempt_is_capped_at_settle_before_the_outcome_is_read(self):
        """Only a work attempt has its cap moved to the resume; a review is stopped when the turn settles."""
        t = task(1, cat="review")
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        self.declare(mesh, t, wait_time(why="soak"))
        a = r.store.create(self.agent.name, t["id"], "claude", mode="review")
        path = self.root / f"{a.id}.jsonl"
        path.write_text(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                    "result": "idle",
                                    "session_id": "s-cap"}) + "\n")
        r.store.update(a, state="running", turns=self.cfg.max_turns, log_path=str(path))
        r.settle(a)
        a = r.store.get(a.id)
        self.assertNotEqual(a.state, "waiting")
        self.assertIn(f"stopped after {self.cfg.max_turns} turns", a.note)
        self.assertEqual(r.store.recent_outcomes(a.id, 1), [])                 # the wait was never recorded
        self.assertNotIn("turn_cap", [k for _, k, _ in r.store.events(a.id)])

    def test_stagnant_attempt_goes_to_the_lead_as_before(self):
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        a = self.capped(r, t, ["wait"] * 8 + ["step", "next"])
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-lead")
        self.assertIn("routed to lead", a.note)
        self.assertTrue(any("RUNNER GAVE UP" in b for b in self.bodies(mesh, t)))
        self.assertIn("no recent progress", self.bodies(mesh, t)[0])

    def test_only_the_last_ten_outcomes_count(self):
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        a = self.capped(r, t, ["step"] * 5 + ["wait"] * 10)       # old progress, then ten idle waits
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-lead")
        self.assertIn("routed to lead", a.note)

    def test_progress_min_is_configurable(self):
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.turn_cap_progress_min = 2
        r = self.runner(mesh)
        a = self.capped(r, t, ["wait", "step", "wait", "next"])
        self.assertTrue(a.note.startswith("turn cap: continued"), a.note)

    def test_spent_start_cap_stops_the_continuation(self):
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        for _ in range(self.cfg.daily_start_cap - 1):              # three earlier starts today
            earlier = r.store.create(self.agent.name, t["id"], "claude")
            r.store.update(earlier, state="completed", turns=1, note="turn cap: continued in a fresh session")
        a = self.capped(r, t, ["step"] * 9)                        # this is the fourth
        self.assertEqual(r.store.starts_today(self.agent.name, t["id"]), self.cfg.daily_start_cap)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-lead")
        self.assertIn("routed to lead", a.note)
        self.assertIn("start cap reached", self.bodies(mesh, t)[0])

    def test_continuation_chain_ends_at_the_start_cap(self):
        """Every capped attempt keeps progressing; the restarts stop at daily_start_cap."""
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        for n in range(1, self.cfg.daily_start_cap + 1):
            a = self.capped(r, t, ["step"] * 9)
            self.assertEqual(mesh.tasks[t["id"]]["assignee_id"],
                             "agent-lead" if n == self.cfg.daily_start_cap else AGENT_ID, n)
        self.assertIn("routed to lead", a.note)

    def test_without_a_start_cap_the_loop_would_be_unbounded_so_it_hands_up(self):
        t = task(1, cat="in_progress")
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.daily_start_cap = 0
        r = self.runner(mesh)
        a = self.capped(r, t, ["step"] * 9)
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], "agent-lead")
        self.assertIn("daily_start_cap is off", self.bodies(mesh, t)[0])
        self.assertIn("routed to lead", a.note)


class ConfigKeys(unittest.TestCase):
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

    def test_default_progress_min(self):
        self.assertEqual(self.load().turn_cap_progress_min, 3)

    def test_progress_min_is_fixed(self):
        # no longer a [runner] option: a file that sets it keeps 3
        with self.assertLogs("mesh_runner", "WARNING") as logs:
            self.assertEqual(self.load("turn_cap_progress_min = 5\n").turn_cap_progress_min, 3)
        self.assertIn("turn_cap_progress_min is no longer an option", "\n".join(logs.output))
