"""Decision waits that would start a session for the addressee with nothing to decide.

Measured over a day on one runner: a large share of the `[decision]` request cards needed no
decision. Three kinds are handled before any request is filed:

1. a question about a task the addressee itself is working on ("pass the handback from #x when
   ready"): it becomes a card wait on that task;
2. the same question again from the same asker and source: it is attached to the request that
   already carries it, which is neither commented on again nor reopened;
3. a question that carries the asker's own proposed default: the asker proceeds, the addressee
   may object on the asking task.
"""
import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path

from unittest import mock

from fakes import FakeMesh, task, write_replies
from mesh_runner.config import load
from mesh_runner.mesh import MeshError
from mesh_runner.store import iso, now
from mesh_runner.wake import check as wake_check
from test_runner import Bench


def later(seconds=5):
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)


class DeclaredBench(Bench):
    """One source task owned by the asker; the asker's turn ends with ``line``."""

    def declare(self, line, *others):
        self.source = task(1)
        self.mesh = FakeMesh([self.source, *others], max_concurrent=1)
        self.r = self.runner(self.mesh)
        self.addCleanup(self.r.store.db.close)
        write_replies(self.replies, {self.source["id"][:8]: line})
        self.r.tick()
        return self.finish_turn(self.r)

    def source_comments(self):
        return [c["body"] for c in self.mesh.comment_log.get(self.source["id"], [])]

    def events(self, attempt):
        return [kind for _, kind, _ in self.r.store.events(attempt.id)]

    def wake(self, attempt, at=None):
        wait = dict(attempt.wait)
        return wake_check(self.mesh, self.r.cats, None, self.source, wait, self.r.agent_id,
                          self.mesh.comments(self.source["id"]), at or later(10)), wait


# ---------------------------------------------------------------- 1. the addressee's live task
class LiveTaskOfTheAddressee(DeclaredBench):
    def live(self, cat="in_progress", assignee="agent-lead"):
        return task(2, cat=cat, assignee_id=assignee)

    def test_question_about_the_addressees_live_task_becomes_a_card_wait(self):
        for spelled in ("short", "uuid"):
            with self.subTest(spelled):
                self.setUp()
                live = self.live()
                ref = f"#{live['id'][:8]}" if spelled == "short" else live["id"]
                a = self.declare(f"⏳ WAIT decision:@lead: pass the handback from {ref} when ready", live)
                self.assertEqual(a.state, "waiting")
                self.assertEqual((a.wait["kind"], a.wait["target"]), ("card", live["id"][:8]))
                self.assertEqual(a.wait["watch_comments_by"], {"id": "agent-lead", "name": "lead"})
                self.assertEqual(a.wait["converted_from"], "decision:@lead")
                self.assertNotIn("request", a.wait)
                self.assertEqual(self.mesh.created, [])                       # no request card
                self.assertEqual(self.mesh.comment_log.get(live["id"], []), [])   # nothing posted to lead
                self.assertEqual([e["depends_on_task_id"] for e in self.mesh.edges[self.source["id"]]],
                                 [live["id"]])                                 # the card wait's edge
                self.assertIn("decision_to_card_wait", self.events(a))
                self.tearDown()

    def test_the_card_wait_wakes_on_the_addressees_comment_on_that_task(self):
        live = self.live()
        a = self.declare(f"⏳ WAIT decision:@lead: tell me when #{live['id'][:8]}'s verdict is in", live)
        event, _ = self.wake(a)
        self.assertEqual(event, "")                                         # nothing yet
        self.mesh.add_comment(live["id"], "progress note", author="agent-reviewer", at=later())
        event, _ = self.wake(a)
        self.assertEqual(event, "")                                         # someone else's comment
        self.mesh.add_comment(live["id"], "Verdict: SHIP", author="agent-lead", at=later())
        event, wait = self.wake(a)
        self.assertIn("commented on #" + live["id"][:8], event)
        self.assertIn("Verdict: SHIP", event)
        self.assertTrue(wait.get("met"))

    def test_the_card_wait_wakes_when_the_task_is_done(self):
        live = self.live()
        a = self.declare(f"⏳ WAIT decision:@lead: pass the handback from #{live['id'][:8]} when ready", live)
        self.mesh.set_category(live["id"], "done")
        event, wait = self.wake(a)
        self.assertIn(f"task #{live['id'][:8]} is done", event)
        self.assertTrue(wait.get("met"))

    def test_someone_elses_task_or_a_closed_task_still_files_a_request(self):
        for label, live in (("another agent's task", self.live(assignee="agent-reviewer")),
                            ("a closed task", self.live(cat="done")),
                            ("a backlog task", self.live(cat="backlog"))):
            with self.subTest(label):
                self.setUp()
                a = self.declare(f"⏳ WAIT decision:@lead: pass the handback from #{live['id'][:8]} when ready", live)
                self.assertEqual(a.state, "waiting")
                self.assertEqual(a.wait["kind"], "decision")
                self.assertEqual(len(self.mesh.created), 1)
                self.assertEqual(self.mesh.created[0]["assignee_id"], "agent-lead")
                self.tearDown()

    def test_several_task_references_still_file_a_request(self):
        live, other = self.live(), task(3, cat="in_progress", assignee_id="agent-lead")
        a = self.declare(f"⏳ WAIT decision:@lead: pass the handbacks from #{live['id'][:8]} and "
                         f"#{other['id'][:8]} when ready", live, other)
        self.assertEqual(a.wait["kind"], "decision")
        self.assertEqual(len(self.mesh.created), 1)
        self.assertNotIn("decision_to_card_wait", self.events(a))

    def test_a_person_addressee_is_asked_as_before(self):
        live = self.live(assignee="user-owner")
        a = self.declare(f"⏳ WAIT decision:@owner: pass the handback from #{live['id'][:8]} when ready", live)
        self.assertNotEqual(a.wait.get("kind"), "card")
        self.assertNotIn("decision_to_card_wait", self.events(a))
        self.assertTrue([b for b in self.source_comments() if "❓ Blocking @owner" in b])

    def test_a_human_gate_on_the_asking_task_is_left_alone(self):
        live = self.live()
        self.source = task(1)
        self.mesh = FakeMesh([self.source, live], max_concurrent=1)
        self.r = self.runner(self.mesh)
        self.addCleanup(self.r.store.db.close)
        write_replies(self.replies, {self.source["id"][:8]:
                      f"⏳ WAIT decision:@lead: pass the handback from #{live['id'][:8]} when ready"})
        self.r.tick()
        self.mesh.tasks[self.source["id"]]["human_gate"] = True             # gate raised during the turn
        a = self.finish_turn(self.r)
        self.assertNotEqual(a.wait.get("kind"), "card")
        self.assertNotIn("decision_to_card_wait", self.events(a))

    def test_off_switch_files_a_request(self):
        self.cfg.decision_live_task_card_wait = False
        live = self.live()
        a = self.declare(f"⏳ WAIT decision:@lead: pass the handback from #{live['id'][:8]} when ready", live)
        self.assertEqual(a.wait["kind"], "decision")
        self.assertEqual(len(self.mesh.created), 1)


# ---------------------------------------------------------------- 2. the same question again
class SameQuestionAgain(Bench):
    def setup_question(self, note="approve the plan?"):
        self.source = task(1)
        self.mesh = FakeMesh([self.source])
        self.r = self.runner(self.mesh)
        self.addCleanup(self.r.store.db.close)
        self.r.refresh_me()
        self.a = self.r.store.create(self.agent.name, self.source["id"], "claude")
        wait = self.question(note)
        self.r.effects.on_wait(self.a, self.source, wait, self.r.me)
        self.r.store.update(self.a, state="waiting", wait=wait)
        self.a = self.r.store.get(self.a.id)
        return wait

    @staticmethod
    def question(note):
        return {"kind": "decision", "handle": "lead", "note": note, "line": f"⏳ WAIT decision:@lead: {note}",
                "since": iso(now() - dt.timedelta(minutes=1)), "started": iso()}

    def asked(self, request):
        return [c["body"] for c in self.mesh.comments(request) if not c["body"].startswith("[runner] closed")]

    def test_the_same_question_on_an_open_request_is_attached_without_a_comment(self):
        first = self.setup_question()["request"]["id"]
        for repeat in ("Approve   the plan.", f"approve the plan? (#{self.source['id'][:8]})", "APPROVE THE PLAN"):
            with self.subTest(repeat):
                again = self.question(repeat)
                self.r.effects.on_wait(self.a, self.source, again, self.r.me)
                self.assertEqual(again["request"]["id"], first)
                self.assertEqual(len(self.mesh.created), 1)                   # one card
                self.assertEqual(self.asked(first), [])                       # nothing asked again
                self.assertEqual(self.mesh.category(self.mesh.tasks[first]), "todo")
        self.assertIn("decision_attached", [k for _, k, _ in self.r.store.events(self.a.id)])

    def test_the_same_question_after_the_request_closed_does_not_reopen_it(self):
        wait = self.setup_question()
        first = wait["request"]["id"]
        self.r.effects.on_end(self.a, wait, "answered", close_request=True)
        self.assertEqual(self.mesh.category(self.mesh.tasks[first]), "done")
        again = self.question("approve the plan")
        self.r.effects.on_wait(self.a, self.source, again, self.r.me)
        self.assertEqual(len(self.mesh.created), 1)
        self.assertEqual(again["request"]["id"], first)
        self.assertEqual(self.mesh.category(self.mesh.tasks[first]), "done")   # not reopened
        self.assertEqual(self.asked(first), [])
        # nobody is asked again, so the asker gets the question back after the re-check interval
        # instead of waiting on a closed request for ever
        self.assertEqual(wake_check(self.mesh, self.r.cats, None, self.source, dict(again), self.r.agent_id,
                                    [], now()), "")
        event = wake_check(self.mesh, self.r.cats, None, self.source, dict(again), self.r.agent_id, [],
                           now() + dt.timedelta(hours=7))
        self.assertIn("re-check", event)
        self.assertIn("not reopened", event)
        # declared the same way again after a plain wake: still attached, still not reopened
        same = self.question("approve the plan")
        self.r.effects.on_wait(self.a, self.source, same, self.r.me)
        self.assertEqual(same.get("attached_closed"), first[:8])
        self.assertEqual(self.mesh.category(self.mesh.tasks[first]), "done")
        # declared again after that re-check handed it back: the asker still needs it, ask once more
        insisted = dict(self.question("approve the plan"), rechecks=1)
        self.r.effects.on_wait(self.a, self.source, insisted, self.r.me)
        self.assertEqual(insisted["request"]["id"], first)
        self.assertEqual(self.mesh.category(self.mesh.tasks[first]), "todo")   # reopened now
        self.assertEqual(len(self.asked(first)), 1)
        self.assertNotIn("attached_closed", insisted)
        self.assertEqual(len(self.mesh.created), 1)

    def test_a_materially_different_question_reopens_as_before(self):
        wait = self.setup_question()
        first = wait["request"]["id"]
        self.r.effects.on_end(self.a, wait, "answered", close_request=True)
        again = self.question("approve the revised plan?")
        self.r.effects.on_wait(self.a, self.source, again, self.r.me)
        self.assertEqual(len(self.mesh.created), 1)
        self.assertEqual(again["request"]["id"], first)
        self.assertEqual(self.mesh.category(self.mesh.tasks[first]), "todo")   # reopened
        self.assertEqual(len(self.asked(first)), 1)
        self.assertNotIn("attached_closed", again)

    def test_another_task_named_at_the_end_is_a_different_question(self):
        wait = self.setup_question("status of #aaaa1111?")
        first = wait["request"]["id"]
        again = self.question("status of #bbbb2222?")
        self.r.effects.on_wait(self.a, self.source, again, self.r.me)
        self.assertEqual(again["request"]["id"], first)
        self.assertEqual(len(self.asked(first)), 1)                           # asked, not attached

    def test_off_switch_reopens_the_same_question(self):
        self.cfg.decision_identical_no_reopen = False
        wait = self.setup_question()
        first = wait["request"]["id"]
        self.r.effects.on_end(self.a, wait, "answered", close_request=True)
        again = self.question("approve the plan.")
        self.r.effects.on_wait(self.a, self.source, again, self.r.me)
        self.assertEqual(self.mesh.category(self.mesh.tasks[first]), "todo")


# ---------------------------------------------------------------- 3. the asker's own default
class ProposedDefault(DeclaredBench):
    def test_a_question_with_a_proposed_default_proceeds_and_files_nothing(self):
        for q in ("keep the current schema? Default: keep it as is",
                  "which directory for the fixtures? proposed: tests/data",
                  "rename the module to core, if no objection",
                  "назвать модуль core? По умолчанию: core"):
            with self.subTest(q):
                self.setUp()
                a = self.declare(f"⏳ WAIT decision:@lead: {q}")
                self.assertEqual(a.state, "ready")
                self.assertIn("Proceed with your stated default now", a.wake_text)
                self.assertIn("real next outcome", a.wake_text)
                self.assertEqual(self.mesh.created, [])                      # no request card
                posted = [b for b in self.source_comments() if b.startswith("[runner] Proceeding")]
                self.assertEqual(len(posted), 1)
                self.assertIn("@lead", posted[0])
                self.assertIn(f"> {q}", posted[0])                            # the question verbatim
                self.assertIn("decision_default_proceeds", self.events(a))
                self.tearDown()

    def test_a_hard_question_with_a_default_still_files_a_request(self):
        a = self.declare("⏳ WAIT decision:@lead: rotate the deploy token now? Default: rotate tonight")
        self.assertEqual(a.state, "waiting")
        self.assertEqual(a.wait["kind"], "decision")
        self.assertEqual(len(self.mesh.created), 1)
        self.assertFalse([b for b in self.source_comments() if b.startswith("[runner] Proceeding")])

    def test_a_question_without_a_default_files_a_request(self):
        a = self.declare("⏳ WAIT decision:@lead: keep the current schema?")
        self.assertEqual(a.state, "waiting")
        self.assertEqual(len(self.mesh.created), 1)
        self.assertFalse([b for b in self.source_comments() if b.startswith("[runner] Proceeding")])

    def test_a_question_to_a_person_with_a_default_is_asked_as_before(self):
        a = self.declare("⏳ WAIT decision:@owner: keep the current schema? Default: keep it")
        self.assertEqual(self.mesh.created, [])
        self.assertFalse([b for b in self.source_comments() if b.startswith("[runner] Proceeding")])
        self.assertTrue([b for b in self.source_comments() if "❓ Blocking @owner" in b])
        self.assertNotIn("Proceed with your stated default", a.wake_text or "")

    def test_a_note_posted_before_a_lost_mark_is_not_posted_again(self):
        q = "keep the current schema? Default: keep it as is"
        body = f"[runner] Proceeding with the proposed default; @lead, reply on this task to object:\n> {q}"
        real_init = FakeMesh.__init__

        def with_note(mesh, tasks, *args, **kwargs):
            real_init(mesh, tasks, *args, **kwargs)
            mesh.add_comment(tasks[0]["id"], body, at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1))

        with mock.patch.object(FakeMesh, "__init__", with_note):
            a = self.declare(f"⏳ WAIT decision:@lead: {q}")
        self.assertEqual(a.state, "ready")
        self.assertEqual(len([b for b in self.source_comments() if b.startswith("[runner] Proceeding")]), 1)
        self.assertIn("decision_default_proceeds", self.events(a))

    def test_a_failed_runner_comment_asks_as_usual(self):
        real = FakeMesh.add_comment

        def refuse_runner_note(mesh, tid, body, *args, **kwargs):
            if body.startswith("[runner] Proceeding"):
                raise MeshError(503, "comment refused")
            return real(mesh, tid, body, *args, **kwargs)

        with mock.patch.object(FakeMesh, "add_comment", refuse_runner_note):
            a = self.declare("⏳ WAIT decision:@lead: keep the current schema? Default: keep it as is")
        self.assertEqual(a.state, "waiting")
        self.assertEqual(a.wait["kind"], "decision")
        self.assertEqual(len(self.mesh.created), 1)                          # asked as before
        self.assertFalse([b for b in self.source_comments() if b.startswith("[runner] Proceeding")])
        self.assertNotIn("decision_default_proceeds", self.events(a))
        self.assertNotIn("Proceed with your stated default", a.wake_text or "")

    def test_off_switch_files_a_request(self):
        self.cfg.decision_default_proceeds = False
        a = self.declare("⏳ WAIT decision:@lead: keep the current schema? Default: keep it as is")
        self.assertEqual(a.state, "waiting")
        self.assertEqual(len(self.mesh.created), 1)


class Switches(unittest.TestCase):
    def read(self, runner_extra=""):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "runner.toml"
            (Path(tmp) / "p").mkdir()
            p.write_text('[runner]\nmesh_url = "x"\nstate_dir = "%s"\n%s\n[[agents]]\nname = "a"\n'
                         'key_env = "K"\nprofile_dir = "%s/p"\n' % (tmp, runner_extra, tmp))
            os.environ.setdefault("K", "k")
            return load(p)

    def test_on_by_default_and_each_can_be_switched_off(self):
        names = ("decision_live_task_card_wait", "decision_identical_no_reopen", "decision_default_proceeds")
        cfg = self.read()
        self.assertEqual([getattr(cfg, n) for n in names], [True, True, True])
        for name in names:
            with self.subTest(name):
                self.assertFalse(getattr(self.read(f"{name} = false"), name))


if __name__ == "__main__":
    unittest.main()
