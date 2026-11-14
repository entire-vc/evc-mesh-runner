"""Decision waits that would start a session for the addressee with nothing to decide.

Measured over a day on one runner: a large share of the `[decision]` request cards needed no
decision. The same question again from the same asker and source is attached to the request that
already carries it, which is neither commented on again nor reopened.

Two runner-side shortcuts that read the question before any request existed (a question about the
addressee's own live task became a card wait; a question with the asker's proposed default
proceeded without a request) are gone: a typed ``ask_agent`` arrives with the request Mesh already
opened, so there is nothing left for the runner to avoid filing. ``DeclaredBench`` stays for the
tests that drive a typed question through a runner.
"""
import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path

from unittest import mock

from fakes import FakeMesh, ask_agent, ask_human, task, write_replies
from mesh_runner.config import load
from mesh_runner.mesh import MeshError
from mesh_runner.store import iso, now
from mesh_runner.wake import check as wake_check
from test_runner import Bench


def later(seconds=5):
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)


class DeclaredBench(Bench):
    """One source task owned by the asker; the asker's turn ends with the Mesh command ``turn``."""

    def declare(self, turn, *others):
        self.source = task(1)
        self.mesh = FakeMesh([self.source, *others], max_concurrent=1)
        self.r = self.runner(self.mesh)
        self.addCleanup(self.r.store.db.close)
        write_replies(self.replies, {self.source["id"][:8]: turn})
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
        # declared again after that re-check handed it back: the request answered it (closed as
        # done), so it is still neither moved nor commented on, and the asker gets the answer reference
        insisted = dict(self.question("approve the plan"), rechecks=1)
        with mock.patch.object(self.mesh, "move", wraps=self.mesh.move) as moved, \
                mock.patch.object(self.mesh, "add_comment", wraps=self.mesh.add_comment) as commented:
            self.r.effects.on_wait(self.a, self.source, insisted, self.r.me)
        self.assertEqual([c for c in moved.call_args_list if c.args[0] == first], [])
        self.assertEqual([c for c in commented.call_args_list if c.args[0] == first], [])
        self.assertEqual(insisted["request"]["id"], first)
        self.assertEqual(self.mesh.category(self.mesh.tasks[first]), "done")   # still not reopened
        self.assertEqual(self.asked(first), [])
        self.assertEqual(insisted.get("attached_closed"), first[:8])
        self.assertEqual(len(self.mesh.created), 1)
        event = wake_check(self.mesh, self.r.cats, None, self.source, dict(insisted, started=iso()),
                           self.r.agent_id, [], now() + dt.timedelta(hours=7))
        self.assertIn(f"Answer reference: request #{first[:8]}", event)
        self.assertIn("already answered", event)

    def test_an_earlier_answered_question_is_not_reopened_after_a_later_one(self):
        # A answered, B asked on the same request (reopened) and answered: A again matches the
        # request's history, not only its last question, and reopens nothing
        wait = self.setup_question("approve the plan?")
        first = wait["request"]["id"]
        self.r.effects.on_end(self.a, wait, "answered", close_request=True)
        second = self.question("approve the revised plan?")
        self.r.effects.on_wait(self.a, self.source, second, self.r.me)
        self.assertEqual(self.mesh.category(self.mesh.tasks[first]), "todo")   # B reopened it
        self.r.effects.on_end(self.a, second, "answered", close_request=True)
        self.assertEqual(self.mesh.category(self.mesh.tasks[first]), "done")
        asked = len(self.asked(first))
        for insisted in (self.question("Approve the plan"), dict(self.question("approve the plan?"), rechecks=1)):
            with self.subTest(insisted["note"]):
                self.r.effects.on_wait(self.a, self.source, insisted, self.r.me)
                self.assertEqual(insisted["request"]["id"], first)
                self.assertEqual(self.mesh.category(self.mesh.tasks[first]), "done")
                self.assertEqual(len(self.asked(first)), asked)
                self.assertEqual(insisted.get("attached_closed"), first[:8])
        self.assertEqual(len(self.mesh.created), 1)

    def test_no_answered_question_is_forgotten_however_many_followed(self):
        # the history is not truncated: the first of 25 answered questions still matches, and a
        # repeat of it after the re-check neither moves nor comments on the request
        wait = self.setup_question("question 0?")
        first = wait["request"]["id"]
        for n in range(1, 25):
            self.r.effects.on_end(self.a, wait, "answered", close_request=True)
            wait = self.question(f"question {n}?")
            self.r.effects.on_wait(self.a, self.source, wait, self.r.me)
            self.assertEqual(wait["request"]["id"], first)
        self.r.effects.on_end(self.a, wait, "answered", close_request=True)
        remembered = self.r.effects._recent(self.source["id"])["agent-lead"]
        self.assertEqual(len(remembered["answered"]), 24)                     # 0..23; 24 is the current one
        asked = len(self.asked(first))
        for insisted in (self.question("Question 0"), dict(self.question("question 0?"), rechecks=1)):
            self.r.effects.on_wait(self.a, self.source, insisted, self.r.me)
            self.assertEqual(insisted.get("attached_closed"), first[:8])
        self.assertEqual(self.mesh.category(self.mesh.tasks[first]), "done")
        self.assertEqual(len(self.asked(first)), asked)
        self.assertEqual(len(self.mesh.created), 1)

    def test_the_same_question_on_a_cancelled_request_is_asked_again_after_the_recheck(self):
        # positive control: a request closed WITHOUT an answer (cancelled) is reopened when the
        # asker repeats the question after the re-check
        wait = self.setup_question()
        first = wait["request"]["id"]
        self.r.effects.on_end(self.a, wait, "answered", close_request=True)
        category = self.r.effects.cats.category
        same, insisted = self.question("approve the plan"), dict(self.question("approve the plan"), rechecks=1)
        with mock.patch.object(self.r.effects.cats, "category",
                               side_effect=lambda t: "cancelled" if t.get("id") == first else category(t)):
            self.r.effects.on_wait(self.a, self.source, same, self.r.me)
            self.assertEqual(same.get("attached_closed"), first[:8])         # first repeat: attached
            self.assertNotIn("attached_answered", same)
            self.r.effects.on_wait(self.a, self.source, insisted, self.r.me)
        self.assertEqual(insisted["request"]["id"], first)
        self.assertEqual(self.mesh.category(self.mesh.tasks[first]), "todo")   # reopened
        self.assertEqual(len(self.asked(first)), 1)

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


class Switches(unittest.TestCase):
    def read(self, runner_extra=""):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "runner.toml"
            (Path(tmp) / "p").mkdir()
            p.write_text('[runner]\nmesh_url = "x"\nstate_dir = "%s"\n%s\n[[agents]]\nname = "a"\n'
                         'key_env = "K"\nprofile_dir = "%s/p"\n' % (tmp, runner_extra, tmp))
            os.environ.setdefault("K", "k")
            return load(p)

    def test_on_by_default_and_a_file_cannot_switch_them_off(self):
        # no longer [runner] options (always on); the off paths are exercised by setting the field above
        names = ("decision_live_task_card_wait", "decision_identical_no_reopen", "decision_default_proceeds")
        cfg = self.read()
        self.assertEqual([getattr(cfg, n) for n in names], [True, True, True])
        for name in names:
            with self.subTest(name), self.assertLogs("mesh_runner", "WARNING") as logs:
                self.assertTrue(getattr(self.read(f"{name} = false"), name))
            self.assertIn(f"{name} is no longer an option", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
