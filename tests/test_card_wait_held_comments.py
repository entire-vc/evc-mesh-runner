"""A card WAIT whose target observation is held by an open typed edge still hears its own card.

Observed: lane A declared ``wait_for on=card <B> artifact_published``; B's owner then asked A a question
(``ask_agent``) that the runner delivered as ``@A, ... asks`` on A's card and went to wait for the answer.
The stale-observation guard (``blocks_stale_observation``) returned before the comment and cycle checks,
so A never woke: both sides waited on each other until a person released the edge by hand.

The guard keeps holding the TARGET's event (closed, shipped): an unreleased typed edge is no
"target closed". A foreign comment that may wake this card wait and a wait cycle are checked first.
"""
import copy
import datetime as dt
import unittest

from fakes import FakeMesh, task
from mesh_runner.card_wait_reconcile import blocks_stale_observation
from mesh_runner.store import iso
from test_runner import Bench


def typed_edge(tid, target_id, condition="artifact_published", satisfied=False, n=1):
    return {"id": f"edge-{n}", "task_id": tid, "depends_on_task_id": target_id,
            "dependency_type": "blocks", "condition": condition, "condition_satisfied": satisfied}


class HeldTypedCardWait(Bench):
    def prepare(self, condition="artifact_published", comment_wake="addressed", n=1):
        self.cfg.comment_wake = comment_wake
        t, awaited = task(n), task(n + 1)
        # Mesh is the external HTTP boundary; Store, the guard, the cycle check and the wake CAS are real.
        mesh = FakeMesh([t, awaited]); r = self.runner(mesh); r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        r.store.update(a, state="waiting", wait={"kind": "card", "target": awaited["id"][:8],
            "since": iso(), "started": iso(), "until": "", "handle": "", "card_condition": condition})
        mesh.deps[t["id"]] = [typed_edge(t["id"], awaited["id"], condition)]
        # precondition: this is the held path, not the ordinary observer
        self.assertTrue(blocks_stale_observation(mesh.dependencies(t["id"]), t["id"], awaited["id"][:8]))
        return r, r.store.get(a.id), awaited

    def comment(self, r, a, body, author="owner-lane", author_type="agent"):
        c = r.mesh.add_comment(a.task_id, body, author=author,
                               at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1))
        if author_type:
            c["author_type"] = author_type
        return c

    def delivered_question(self, a, awaited):
        # the body effects._deliver_to_addressee_card writes for an ask_agent about this card
        return (f"@{self.agent.name}, owner-lane asks on #{awaited['id'][:8]} ({awaited['id']}) for this card "
                f"#{a.task_id[:8]}.\n\nQuestion (verbatim):\n> Подтверди путь установки?\n\n"
                f"Act on it here, on #{a.task_id[:8]}: it names this card, so it is delivered here instead of an "
                "answer desk or a request card.")

    # --- criterion 1: an @-mention of the lane wakes the held wait
    def test_mention_wakes_typed_wait_with_open_edge(self):
        for n, condition in enumerate(("artifact_published", "shipped", "deployed", "accepted",
                                       "access_ready", "contract_ready")):
            with self.subTest(condition=condition):
                r, a, _ = self.prepare(condition, n=1 + 2 * n)
                self.comment(r, a, f"@{self.agent.name} подтверди, что экспорт снова включён")
                r._wake_one(a)
                b = r.store.get(a.id)
                self.assertEqual((b.id, b.state), (a.id, "ready"))
                self.assertIn("экспорт снова включён", b.wake_text)
                self.assertNotIn("is done", b.wake_text)
                self.assertFalse(r.procs)

    # --- criterion 2: the ask_agent question delivered on the card wakes it; an unaddressed note does not
    def test_delivered_ask_agent_question_wakes(self):
        r, a, awaited = self.prepare()
        self.comment(r, a, self.delivered_question(a, awaited))
        r._wake_one(a)
        b = r.store.get(a.id)
        self.assertEqual(b.state, "ready")
        self.assertIn("owner-lane asks on", b.wake_text)
        # never ``met``: the awaited card did not deliver the named result
        self.assertIsNone(r.store.mark(self.agent.name, a.task_id, "met_wake"))

    def test_unaddressed_agent_note_still_does_not_wake(self):
        r, a, _ = self.prepare()
        self.comment(r, a, "FYI progress only: export is being reconfigured")
        before = copy.deepcopy(r.mesh.deps)
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, "waiting")
        self.assertEqual(r.mesh.deps, before)                       # nothing released or removed
        self.assertFalse(r.procs)

    def test_person_comment_wakes(self):
        r, a, _ = self.prepare()
        self.comment(r, a, "что с экспортом?", author="a-person", author_type=None)
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, "ready")

    def test_own_comment_does_not_wake(self):
        r, a, _ = self.prepare()
        self.comment(r, a, f"@{self.agent.name} note to self", author=r.agent_id)
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, "waiting")

    # --- criterion 3: a mutual wait (A waits on B's card, B's owner waits for A's answer) wakes A
    def mutual(self):
        r, a, awaited = self.prepare()
        theirs = r.store.create("owner-lane", awaited["id"], "claude", "fixture")
        r.store.update(theirs, state="waiting", wait={
            "kind": "decision", "handle": self.agent.name, "since": iso(), "started": iso(),
            "line": f"⏳ WAIT decision:@{self.agent.name}: подтверди путь установки"})
        return r, a, awaited

    def test_mutual_wait_cycle_wakes_the_card_waiter(self):
        r, a, awaited = self.mutual()
        r._wake_one(a)
        b = r.store.get(a.id)
        self.assertEqual((b.id, b.state), (a.id, "ready"))
        self.assertIn("wait cycle", b.wake_text)
        self.assertIn(f"#{awaited['id'][:8]} (owner-lane) is itself waiting on you", b.wake_text)

    def test_mutual_wait_cycle_is_told_once(self):
        r, a, _ = self.mutual()
        declared = copy.deepcopy(a.wait)
        r._wake_one(a)
        told = r.store.get(a.id)
        self.assertEqual(told.state, "ready")
        # the owner declares the same wait again (no cycle_told on a fresh declaration)
        r.store.update(told, state="waiting", wait=declared)
        again = r.store.get(a.id)
        r._wake_one(again)
        self.assertEqual(r.store.get(a.id).state, "waiting")
        kinds = [k for _, k, _ in r.store.events(a.id)]
        self.assertIn("cycle_wake_suppressed", kinds)
        # the told signature is kept on the wait: the next tick does not re-read and re-log it
        self.assertTrue(r.store.get(a.id).wait.get("cycle_told"))
        n = kinds.count("cycle_wake_suppressed")
        r._wake_one(r.store.get(a.id))
        self.assertEqual([k for _, k, _ in r.store.events(a.id)].count("cycle_wake_suppressed"), n)

    # --- criterion 4: the target's own event stays held while the typed edge is open
    def test_closed_target_with_open_typed_edge_is_not_a_wake(self):
        r, a, awaited = self.prepare()
        awaited["status_id"] = "s-done"
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, "waiting")
        self.assertFalse(r.procs)

    def test_comment_wake_beside_closed_target_reports_the_comment_not_the_target(self):
        r, a, awaited = self.prepare()
        awaited["status_id"] = "s-done"
        self.comment(r, a, f"@{self.agent.name} вопрос по экспорту")
        r._wake_one(a)
        b = r.store.get(a.id)
        self.assertEqual(b.state, "ready")
        self.assertIn("вопрос по экспорту", b.wake_text)
        self.assertNotIn(f"task #{awaited['id'][:8]} is done", b.wake_text)
        self.assertIsNone(r.store.mark(self.agent.name, a.task_id, "met_wake"))

    def test_stale_waiting_snapshot_cannot_wake_twice(self):
        r, a, _ = self.prepare()
        self.comment(r, a, f"@{self.agent.name} вопрос")
        r._wake_one(a)
        b = r.store.get(a.id)
        r._wake_one(a)                                              # replay of the stale snapshot
        c = r.store.get(a.id)
        self.assertEqual((c.state, c.wake_text, c.wait), (b.state, b.wake_text, b.wait))
        self.assertEqual(len(r.store.by_state(a.agent, ("ready", "waiting"))), 1)


if __name__ == "__main__":
    unittest.main()
