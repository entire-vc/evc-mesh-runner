"""`decision_wake = "answer"`: the addressee's progress notes do not end a decision wait."""
import datetime as dt
import tempfile
import unittest
from pathlib import Path

from fakes import FakeMesh, task
from mesh_runner.config import ConfigError, RunnerConfig, load
from mesh_runner.store import iso, now
from mesh_runner.wake import check
from test_runner import Bench


class Fixture(Bench):
    def parked(self, mode="answer", request=True, agent_mode="", n=1):
        """A lane waiting on `lead` with a request card (or, without one, as a legacy wait)."""
        self.cfg.decision_wake = mode
        self.agent.decision_wake = agent_mode
        source = task(n)
        mesh = FakeMesh([source])
        r = self.runner(mesh)
        r.refresh_me()
        a = r.store.create(self.agent.name, source["id"], "claude")
        wait = {"kind": "decision", "handle": "lead", "note": "approve the plan?",
                "line": "⏳ WAIT decision:@lead: approve the plan?",
                "since": iso(now() - dt.timedelta(minutes=1)), "started": iso()}
        if request:
            r.effects.on_wait(a, source, wait, r.me)
            self.assertTrue(wait.get("request"))
        else:
            wait["recipient_id"] = "agent-lead"
        r.store.update(a, state="waiting", wait=wait)
        return r, mesh, source, r.store.get(a.id)

    def say(self, mesh, task_id, body, author="agent-lead", kind="agent", name=None):
        c = mesh.add_comment(task_id, body, author=author, at=now() + dt.timedelta(seconds=5))
        c["author_type"] = kind
        if name:
            c["author_name"] = name
        return c

    def state(self, r, a):
        r._wake_one(a)
        return r.store.get(a.id).state


class AnswerMode(Fixture):
    NOTE = "progress: CI is green on the branch, continuing with the next step"

    def test_a_progress_note_by_the_addressee_on_the_waiting_card_does_not_wake(self):
        r, mesh, source, a = self.parked()
        self.say(mesh, source["id"], self.NOTE)
        self.assertEqual(self.state(r, a), "waiting")
        self.assertFalse(r.store.get(a.id).wait.get("met"))
        self.assertEqual(mesh.category(mesh.tasks[a.wait["request"]["id"]]), "todo")   # still open

    def test_the_default_mode_wakes_when_the_request_is_closed_without_a_comment(self):
        for n, cat in enumerate(("done", "cancelled"), 1):
            with self.subTest(cat):
                r, mesh, source, a = self.parked(mode="any", n=n)
                mesh.tasks[a.wait["request"]["id"]]["status_category"] = cat
                self.assertEqual(self.state(r, a), "ready")
                self.assertIn(f"is {cat}", r.store.get(a.id).wake_text)

    def test_the_default_mode_ignores_a_comment_on_the_request_card(self):
        r, mesh, source, a = self.parked(mode="any")
        self.say(mesh, a.wait["request"]["id"], "fyi, unrelated", author="agent-reviewer")
        self.assertEqual(self.state(r, a), "waiting")

    def test_the_default_mode_wakes_on_the_same_note(self):
        r, mesh, source, a = self.parked(mode="any")
        self.say(mesh, source["id"], self.NOTE)
        self.assertEqual(self.state(r, a), "ready")
        self.assertEqual(RunnerConfig(mesh_url="x", state_dir=Path("/tmp"), agents=[]).decision_wake, "any")

    def test_an_answer_on_the_request_card_wakes_and_closes_the_request(self):
        r, mesh, source, a = self.parked()
        request = a.wait["request"]["id"]
        self.say(mesh, request, "approved, go ahead")
        self.assertEqual(self.state(r, a), "ready")
        self.assertIn("approved, go ahead", r.store.get(a.id).wake_text)
        self.assertEqual(mesh.category(mesh.tasks[request]), "done")        # met: closed by the wake

    def test_a_person_commenting_on_the_request_card_wakes(self):
        r, mesh, source, a = self.parked()
        self.say(mesh, a.wait["request"]["id"], "do it", author="owner", kind="user")
        self.assertEqual(self.state(r, a), "ready")

    def test_a_bystander_agent_on_the_request_card_does_not_wake(self):
        r, mesh, source, a = self.parked()
        self.say(mesh, a.wait["request"]["id"], "fyi, unrelated", author="agent-reviewer")
        self.assertEqual(self.state(r, a), "waiting")

    def test_a_closed_request_card_wakes(self):
        for n, cat in enumerate(("done", "cancelled"), 1):
            with self.subTest(cat):
                r, mesh, source, a = self.parked(n=n)
                request = a.wait["request"]["id"]
                mesh.tasks[request]["status_category"] = cat
                self.assertEqual(self.state(r, a), "ready")
                self.assertIn(f"is {cat}", r.store.get(a.id).wake_text)

    def test_an_unreadable_request_card_is_not_an_answer(self):
        r, mesh, source, a = self.parked()
        mesh.broken_tasks.add(a.wait["request"]["id"])
        self.assertEqual(self.state(r, a), "waiting")

    def test_an_answer_marker_a_mention_or_a_quoted_request_id_on_the_waiting_card_wakes(self):
        probes = {"marker": "Answer: yes", "decision marker": "**Decision:** go", "lead word": "Ответ на запрос: да",
                  "tagged": "[decision-answer] ok, proceed", "mention": "thanks @builder, see my note",
                  "request id": "re #{short}: approved"}
        for n, (label, body) in enumerate(probes.items(), 1):
            with self.subTest(label):
                r, mesh, source, a = self.parked(n=n)
                self.say(mesh, source["id"], body.replace("{short}", a.wait["request"]["id"][:8]))
                self.assertEqual(self.state(r, a), "ready")

    def test_a_marker_word_inside_a_sentence_is_not_a_marker(self):
        r, mesh, source, a = self.parked()
        self.say(mesh, source["id"], "I will ask for a decision tomorrow; the answer is not ready")
        self.assertEqual(self.state(r, a), "waiting")

    def test_a_person_wakes_on_the_waiting_card(self):
        for n, kind in enumerate(("user", None), 1):
            with self.subTest(kind):
                r, mesh, source, a = self.parked(n=n)
                c = self.say(mesh, source["id"], "stop, wrong plan", author="owner", kind=kind)
                if kind is None:
                    del c["author_type"]
                self.assertEqual(self.state(r, a), "ready")

    def test_an_author_on_the_always_list_wakes_even_with_a_plain_note(self):
        r, mesh, source, a = self.parked()
        self.cfg.comment_wake_always_from = ["Coordinator"]
        self.say(mesh, source["id"], self.NOTE, author="agent-coord", name="coordinator")
        self.assertEqual(self.state(r, a), "ready")

    def test_a_held_note_is_still_in_the_thread_when_the_answer_arrives(self):
        r, mesh, source, a = self.parked()
        self.say(mesh, source["id"], self.NOTE)
        self.assertEqual(self.state(r, a), "waiting")
        self.say(mesh, source["id"], "Answer: approved")
        self.assertEqual(self.state(r, a), "ready")
        self.assertEqual([c["body"] for c in mesh.comments(source["id"])], [self.NOTE, "Answer: approved"])

    def test_a_wait_without_a_request_card_keeps_the_old_behaviour(self):
        r, mesh, source, a = self.parked(request=False)
        self.assertNotIn("request", a.wait)
        self.say(mesh, source["id"], self.NOTE)
        self.assertEqual(self.state(r, a), "ready")

    def test_only_decision_waits_are_filtered(self):
        r, mesh, source, a = self.parked()
        r.store.update(a, wait=dict(a.wait, kind="card", target="deadbeef", handle=""))
        self.say(mesh, source["id"], self.NOTE)
        self.assertEqual(self.state(r, r.store.get(a.id)), "ready")
        self.assertIn("commented", r.store.get(a.id).wake_text)

    def test_an_agent_override_applies_to_that_lane_only(self):
        r, mesh, source, a = self.parked(mode="any", agent_mode="answer")
        self.say(mesh, source["id"], self.NOTE)
        self.assertEqual(self.state(r, a), "waiting")
        r, mesh, source, a = self.parked(mode="answer", agent_mode="any", n=2)
        self.say(mesh, source["id"], self.NOTE)
        self.assertEqual(self.state(r, a), "ready")

    def test_the_request_tells_the_addressee_how_to_answer_only_in_answer_mode(self):
        for n, (mode, hint) in enumerate((("answer", True), ("any", False)), 1):
            with self.subTest(mode):
                r, mesh, source, a = self.parked(mode=mode, n=n)
                body = mesh.tasks[a.wait["request"]["id"]]["description"]
                self.assertEqual("Start it with `Answer:`" in body, hint)


class HeldNoteRecheck(unittest.TestCase):
    """A plain-comment answer must not be lost: a held-back note ends the wait at the re-check interval."""
    SINCE = "2026-10-23T00:00:00+00:00"

    def run_check(self, minutes, mode="answer", **kw):
        w = {"kind": "decision", "handle": "lead", "recipient_id": "agent-lead", "request": {"id": "r" * 8, "project_id": "p1"},
             "since": self.SINCE, "started": self.SINCE}
        c = {"id": "c1", "author_id": "agent-lead", "author_name": "lead", "author_type": "agent",
             "body": "approved, go", "created_at": "2026-10-23T00:01:00+00:00"}
        at = dt.datetime(2026, 10, 23, 0, 1, tzinfo=dt.timezone.utc) + dt.timedelta(minutes=minutes)
        return w, check(None, None, None, {}, w, "me", [c], at, agent_name="builder", decision_wake=mode,
                        first_recheck=dt.timedelta(hours=2), **kw)

    def test_a_held_note_wakes_once_it_is_older_than_the_recheck_interval(self):
        w, out = self.run_check(119)
        self.assertEqual((out, w.get("recheck_due")), ("", None))
        w, out = self.run_check(121)
        self.assertIn("re-check: lead commented", out)
        self.assertTrue(w.get("recheck_due"))
        self.assertFalse(w.get("met"))

    def test_without_the_mode_the_note_wakes_at_once(self):
        w, out = self.run_check(1, mode="any")
        self.assertIn("lead commented: approved, go", out)
        self.assertTrue(w.get("met"))


class Config(unittest.TestCase):
    def read(self, runner_extra="", agent_extra=""):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "runner.toml"
            (Path(tmp) / "p").mkdir()
            (Path(tmp) / "base").mkdir()
            p.write_text('[runner]\nmesh_url = "x"\nstate_dir = "%s"\n%s\n[[agents]]\nname = "a"\n'
                         'key_env = "K"\nprofile_dir = "%s/p"\n%s\n' % (tmp, runner_extra, tmp, agent_extra))
            import os
            os.environ.setdefault("K", "k")
            return load(p)

    def test_defaults_and_values(self):
        c = self.read()
        self.assertEqual((c.decision_wake, c.agents[0].decision_wake), ("any", ""))
        c = self.read('decision_wake = "answer"', 'decision_wake = "any"')
        self.assertEqual((c.decision_wake, c.agents[0].decision_wake), ("answer", "any"))

    def test_bad_values_are_refused(self):
        for runner_extra, agent_extra in (('decision_wake = "mentions"', ""), ('decision_wake = true', ""),
                                          ('decision_wake = ""', ""), ("", 'decision_wake = "all"'),
                                          ("", "decision_wake = false"), ("", "decision_wake = 0"),
                                          ("", "decision_wake = []")):
            with self.subTest((runner_extra, agent_extra)), self.assertRaises(ConfigError):
                self.read(runner_extra, agent_extra)


if __name__ == "__main__":
    unittest.main()
