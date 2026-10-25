"""A decision request that mirrors one the addressee already put to this lane is not filed."""
import unittest

from fakes import FakeMesh, task
from mesh_runner.store import iso, now
import datetime as dt

from test_runner import Bench


class Mirror(Bench):
    @staticmethod
    def question(note, handle):
        return {"kind": "decision", "handle": handle, "note": note, "line": f"⏳ WAIT decision:@{handle}: {note}",
                "since": iso(now() - dt.timedelta(minutes=1)), "started": iso()}

    def counter(self, r, mesh, other, question, lane="lead", state="open"):
        """`lane` asked this lane about `question` from its own task `other`; its request is open."""
        request = mesh.create_task(other["project_id"], {"title": "[decision] mirror", "assignee_id": r.me["id"]})
        r.effects._save_decision(other["id"], {
            "key": "k", "recipient": r.me["id"], "lane": lane, "state": state, "question": question,
            "request": {"id": request["id"], "project_id": other["project_id"]}, "filed_at": iso(), "asked_at": iso()})
        return request

    def ask(self, r, mesh, source, note, handle="lead"):
        attempt = r.store.create(self.agent.name, source["id"], "claude")
        wait = self.question(note, handle)
        before = len(mesh.tasks)
        r.effects.on_wait(attempt, source, wait, r.me)
        return attempt, wait, len(mesh.tasks) - before

    def runner_with(self, *tasks):
        mesh = FakeMesh(list(tasks))
        r = self.runner(mesh)
        self.addCleanup(r.store.db.close)
        r.refresh_me()
        return r, mesh

    def test_the_same_question_files_no_card_and_waits_on_the_counter_request(self):
        mine, theirs = task(1), task(2)
        r, mesh = self.runner_with(mine, theirs)
        request = self.counter(r, mesh, theirs, "Approve the plan?")
        other = r.store.create("lead", theirs["id"], "claude")
        r.store.update(other, state="waiting", wait={"kind": "decision", "handle": self.agent.name,
                                                      "note": "Approve the plan?"})
        _, wait, filed = self.ask(r, mesh, mine, "approve the plan?")
        self.assertEqual(filed, 0)
        self.assertEqual((wait["kind"], wait["target"]), ("card", request["id"][:8]))
        self.assertNotIn("request", wait)
        moved = r.store.get(other.id).wait
        self.assertEqual((moved["kind"], moved["target"]), ("card", request["id"][:8]))

    def test_the_same_target_card_moves_both_waits_to_that_card(self):
        mine, theirs = task(1), task(2)
        r, mesh = self.runner_with(mine, theirs)
        self.counter(r, mesh, theirs, "is #abcdef12 safe to merge?")
        other = r.store.create("lead", theirs["id"], "claude")
        r.store.update(other, state="waiting", wait={"kind": "decision", "handle": self.agent.name,
                                                      "note": "is #abcdef12 safe to merge?"})
        _, wait, filed = self.ask(r, mesh, mine, "should we merge #abcdef12 now?")
        self.assertEqual(filed, 0)
        self.assertEqual((wait["kind"], wait["target"]), ("card", "abcdef12"))
        moved = r.store.get(other.id).wait
        self.assertEqual((moved["kind"], moved["target"]), ("card", "abcdef12"))

    def test_a_different_question_is_filed_as_before(self):
        mine, theirs = task(1), task(2)
        r, mesh = self.runner_with(mine, theirs)
        self.counter(r, mesh, theirs, "approve the plan?")
        _, wait, filed = self.ask(r, mesh, mine, "which database do we use?")
        self.assertEqual(filed, 1)
        self.assertEqual(wait["kind"], "decision")

    def test_a_closed_counter_request_or_another_asker_is_no_mirror(self):
        for n, (lane, state) in enumerate((("lead", "closed"), ("someone-else", "open")), 1):
            with self.subTest(lane=lane, state=state):
                mine, theirs = task(n * 10 + 1), task(n * 10 + 2)
                r, mesh = self.runner_with(mine, theirs)
                self.counter(r, mesh, theirs, "approve the plan?", lane=lane, state=state)
                _, wait, filed = self.ask(r, mesh, mine, "approve the plan?")
                self.assertEqual((filed, wait["kind"]), (1, "decision"))


if __name__ == "__main__":
    unittest.main()
