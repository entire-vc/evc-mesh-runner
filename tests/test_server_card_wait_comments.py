"""A server-observed card WAIT (target in another workspace) still hears its own card.

Observed (live, Runner 0.2.54): a card wait whose target sat in another workspace was marked
``server_observed`` by Mesh; ``_wake_server_card`` read only the source receipt, and Mesh resolves that
receipt only on the target's events (status, ship, VCS, edge, membership). A person's ``@<lane>``
question on the waiting card itself never woke it, while the same question on a same-workspace wait
woke it in the same tick. A question ``ask_agent`` delivers on the card and a wait cycle went unheard
the same way: the deafness already fixed for runner-observed waits held by an open typed edge.

The receipt stays authoritative for the TARGET: no target, edge or foreign-workspace read, no
"target closed" and no ``met`` without a resolved receipt. Only the source card's own comments
and the runner's own store (wait cycle) are read while the receipt is pending.
"""
import datetime as dt
import unittest

from fakes import AGENT_ID, task
from mesh_runner.store import iso
from test_card_wait_receipt import SourceReceiptMesh
from test_typed_outcome import TypedTurn

TARGET = "0f1e2d3c-0000-4000-8000-000000000001"


class ServerObservedCardWaitHearsItsCard(TypedTurn):
    def flow(self, comment_wake="addressed"):
        self.cfg.comment_wake = comment_wake
        source = task(1)
        mesh = SourceReceiptMesh([source], max_concurrent=1)
        mesh.source_id = source["id"]
        r = self.run_turn(mesh, source)
        receipt = dict(id="w-1", task_id=source["id"], agent_id=AGENT_ID, kind="card", server_observed=True,
                       target_task_id=TARGET, target_condition="artifact_published")
        self.typed(mesh, source, "wait", receipt)
        mesh.outcomes[source["id"]][0]["wait_server_observed"] = True
        mesh.receipt = receipt
        mesh.strict = True                       # any target / edge / foreign read raises
        a = self.finish_turn(r)
        self.assertEqual((a.state, a.wait["mesh_wait_id"], a.wait["server_observed"]), ("waiting", "w-1", True))
        self.assertEqual(a.wait["target"], TARGET[:8])
        return r, a, mesh

    def comment(self, mesh, a, body, author="owner-lane", author_type="agent"):
        c = mesh.add_comment(a.task_id, body, author=author,
                             at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=10))
        if author_type:
            c["author_type"] = author_type
        return c

    def assert_woken_not_met(self, r, a, mesh, text):
        b = r.store.get(a.id)
        self.assertEqual((b.id, b.state, b.session_id), (a.id, "ready", a.session_id))
        self.assertIn(text, b.wake_text)
        self.assertNotIn("condition as met", b.wake_text)        # the target delivered nothing
        self.assertIsNone(r.store.mark(self.agent.name, a.task_id, "met_wake"))
        self.assertEqual(mesh.edges, {})                          # nothing written to the graph
        return b

    # (a) an @-mention of the lane by another agent wakes the pending server-observed wait
    def test_mention_wakes_pending_server_observed_wait(self):
        r, a, mesh = self.flow()
        self.comment(mesh, a, f"@{self.agent.name} подтверди, что экспорт снова включён")
        r._wake_budgeted(a)
        self.assert_woken_not_met(r, a, mesh, "экспорт снова включён")
        # the runner ends the Mesh wait it stopped observing as cancelled, never met
        self.assertEqual([x[:3] for x in mesh.resolved], [(a.task_id, "w-1", "cancelled")])

    # (a) a person's question (author kind not an agent) wakes it, as in the live probe
    def test_person_question_wakes(self):
        r, a, mesh = self.flow()
        self.comment(mesh, a, f"@{self.agent.name}, вопрос от участника QA", author="QA Member", author_type="user")
        r._wake_one(a)
        self.assert_woken_not_met(r, a, mesh, "вопрос от участника QA")

    # (b) the ask_agent question the runner delivers on this card wakes it
    def test_delivered_ask_agent_question_wakes(self):
        r, a, mesh = self.flow()
        self.comment(mesh, a, f"@{self.agent.name}, owner-lane asks on #{TARGET[:8]} ({TARGET}) for this card "
                              f"#{a.task_id[:8]}.\n\nQuestion (verbatim):\n> Подтверди путь установки?")
        r._wake_one(a)
        self.assert_woken_not_met(r, a, mesh, "owner-lane asks on")

    def test_unaddressed_agent_note_does_not_wake(self):
        r, a, mesh = self.flow()
        self.comment(mesh, a, "FYI progress only: export is being reconfigured")
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, "waiting")

    def test_own_comment_does_not_wake(self):
        r, a, mesh = self.flow()
        self.comment(mesh, a, f"@{self.agent.name} note to self", author=AGENT_ID)
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, "waiting")

    def test_comment_before_the_wait_does_not_wake(self):
        r, a, mesh = self.flow()
        c = self.comment(mesh, a, f"@{self.agent.name} old question")
        c["created_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, "waiting")

    # (c) a mutual wait: the target's owner waits for this lane's answer
    def mutual(self):
        r, a, mesh = self.flow()
        theirs = r.store.create("owner-lane", TARGET, "claude", "fixture")
        r.store.update(theirs, state="waiting", wait={
            "kind": "decision", "handle": self.agent.name, "since": iso(), "started": iso(),
            "line": f"⏳ WAIT decision:@{self.agent.name}: подтверди путь установки"})
        return r, a, mesh

    def test_mutual_wait_cycle_wakes_once(self):
        r, a, mesh = self.mutual()
        declared = dict(a.wait)
        r._wake_one(a)
        b = self.assert_woken_not_met(r, a, mesh, "wait cycle")
        self.assertIn(f"#{TARGET[:8]} (owner-lane) is itself waiting on you", b.wake_text)
        r.store.update(b, state="waiting", wait=declared)        # the same wait declared again
        r._wake_one(r.store.get(a.id))
        self.assertEqual(r.store.get(a.id).state, "waiting")
        self.assertTrue(self.events(r, a, "cycle_wake_suppressed"))

    # the receipt stays authoritative for the target's event
    def test_resolved_receipt_still_wins_over_a_comment(self):
        r, a, mesh = self.flow()
        self.comment(mesh, a, f"@{self.agent.name} вопрос")
        mesh.receipt.update(resolved_at="now", resolution="met")
        r._wake_one(a)
        b = r.store.get(a.id)
        self.assertEqual(b.state, "ready")
        self.assertIn("condition as met", b.wake_text)
        self.assertEqual(r.store.mark(self.agent.name, a.task_id, "met_wake")[0], a.id)

    def test_pending_receipt_without_news_keeps_waiting_and_reads_no_target(self):
        r, a, mesh = self.flow()
        r._wake_budgeted(a)
        self.assertEqual(r.store.get(a.id).state, "waiting")
        self.assertFalse(getattr(mesh, "resolved", []))

    def test_stale_waiting_snapshot_cannot_wake_twice(self):
        r, a, mesh = self.flow()
        self.comment(mesh, a, f"@{self.agent.name} вопрос")
        r._wake_one(a)
        b = r.store.get(a.id)
        r._wake_one(a)                                            # replay of the stale snapshot
        c = r.store.get(a.id)
        self.assertEqual((c.state, c.wake_text, c.wait), (b.state, b.wake_text, b.wait))


if __name__ == "__main__":
    unittest.main()
