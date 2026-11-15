"""A question delivered on its addressee's card must reach a session that wakes for it.

Observed (Runner 0.2.61): lane ``asker`` asked lane ``ops-vc`` (``ask_agent``); the question named one of its
cards as context. The runner delivered it there as ``@ops, asker asks ...``. That card's session (lane ``ops-vc``,
which runs Mesh agent ``ops`` of another workspace) slept in a ``card`` wait. Under ``comment_wake = "addressed"``
the wake filter compared the mention with the LANE name only, so the comment never woke it, and nothing showed
the question was lost until a person answered by hand.

1. An ``@``-mention of the lane's Mesh name (or slug) wakes it as the lane name does.
2. A named card of the addressee that itself waits on another card is context, not the card to act on: the
   question takes the desk / request-card path. A card whose wait a comment does not end (``registered``,
   ``human``) is not delivered to either: a request card.
3. A delivered question still unanswered 30 min later, while the addressee's session there still holds the wait
   it held at delivery, is logged and kept on the wait (``delivered.unheard``) for wait-cycle-watch.
"""
import datetime as dt

from fakes import FakeMesh, task
from mesh_runner.store import iso
from test_answer_desk import Desk, lead_lane
from test_decision_order_delivery import LEAD, FakeMeshWith
from test_runner import Bench


def incident_body(owner_lane, own_card8, asking8):
    """The comment ``effects._deliver_to_addressee_card`` writes on the addressee's card (question shortened)."""
    return (f"@Ops, {owner_lane} asks on #{asking8} for this card #{own_card8}.\n\n"
            f"Question (verbatim):\n> проверь фактический статус !137/#bbbbbbbb/#{own_card8}\n\n"
            f"Act on it here, on #{own_card8}: it names this card, so it is delivered here instead of an answer "
            "desk or a request card.")


class MeshNameMentionWakes(Bench):
    """Lane ``builder`` runs Mesh agent ``Ops``: an ``@Ops`` comment is addressed to it."""

    def prepare(self, server_observed=False, mesh_name="Ops"):
        self.cfg.comment_wake = "addressed"
        t, awaited = task(1), task(2)
        mesh = FakeMesh([t, awaited])
        plain_me = mesh.me
        mesh.me = lambda: {**plain_me(), "name": mesh_name, "slug": mesh_name.casefold()}
        r = self.runner(mesh); r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        wait = {"kind": "card", "target": awaited["id"][:8], "since": iso(), "started": iso(), "until": "",
                "handle": ""}
        if server_observed:
            wait.update(server_observed=True, mesh_wait_id="w-srv")
            mesh.wait_receipt = lambda task_id, wait_id: {
                "id": wait_id, "task_id": task_id, "agent_id": r.agent_id, "kind": "card", "server_observed": True,
                "target_task_id": awaited["id"], "resolved_at": None}
        r.store.update(a, state="waiting", wait=wait)
        return r, r.store.get(a.id), awaited

    def comment(self, r, a, body, author="asker"):
        c = r.mesh.add_comment(a.task_id, body, author=author,
                               at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1))
        c["author_type"] = "agent"
        c["author_name"] = author
        return c

    def test_own_handles_are_the_lane_and_mesh_names(self):
        r, _, _ = self.prepare()
        self.assertEqual(r.own_handles, ("builder", "Ops"))          # the slug repeats the name

    def test_question_delivered_as_mesh_name_wakes_a_card_wait(self):
        r, a, awaited = self.prepare()
        self.comment(r, a, incident_body("asker", a.task8, "0000aaaa"))
        r._wake_one(a)
        b = r.store.get(a.id)
        self.assertEqual((b.id, b.state), (a.id, "ready"))
        self.assertIn("asker asks on", b.wake_text)
        self.assertIsNone(r.store.mark(self.agent.name, a.task_id, "met_wake"))     # the target did not deliver

    def test_question_delivered_as_mesh_name_wakes_a_server_observed_card_wait(self):
        r, a, _ = self.prepare(server_observed=True)
        self.comment(r, a, incident_body("asker", a.task8, "0000aaaa"))
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, "ready")

    def test_mention_of_the_slug_wakes_too(self):
        r, a, _ = self.prepare()
        self.comment(r, a, "@ops глянь, пожалуйста, что с канарейкой")
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, "ready")

    def test_mention_of_someone_else_still_does_not_wake(self):
        r, a, _ = self.prepare()
        self.comment(r, a, "@opsx, @lead: FYI канарейка ещё не стоит")
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, "waiting")
        self.assertFalse(r.procs)


class Asking(Desk):
    def park(self, mesh, t, question, waits=None):
        """The asking lane ends its turn with ``ask_agent`` to ``lead``; the desk of ``lead`` is live. ``waits``:
        the addressee's declared waits on its cards (set after ``ask``, which writes the asker's own wait)."""
        r = self.run_turn(mesh, t)
        desk = self.desk(mesh, r, lead_lane(mesh))
        desk.step()
        self.ask(mesh, t, question=question)
        mesh.waits.update(waits or {})
        return r, desk


class ContextCardIsNotTheExecutionCard(Asking):
    """The asker side: which named card of the addressee may take the question."""

    def setup_named(self, wait_kind):
        t = task(1)
        c = task(2, cat="in_progress", assignee_id=LEAD)
        mesh = FakeMeshWith([t, c])
        q = f"GO требует отсутствие канарейки: проверь фактический статус !137/#{c['id'][:8]}"
        r, desk = self.park(mesh, t, q, {c["id"]: {"id": "w-c", "kind": wait_kind,
                                                   "target_task_id": "bbbbbbbb" + "0" * 28}})
        return mesh, r, desk, t, c

    def test_a_card_waiting_on_another_card_is_context_the_question_goes_to_the_desk(self):
        mesh, r, desk, t, c = self.setup_named("card")
        with self.assertLogs("mesh_runner", "INFO") as logs:
            a = self.finish_turn(r)
        self.assertEqual(mesh.comment_log.get(c["id"], []), [])            # not silently left on #C
        self.assertEqual(a.wait["cardless"]["lane"], "lead")               # the ordinary path: the desk
        self.assertNotIn("names_addressee_card", a.wait)
        self.assertNotIn("delivered", a.wait)
        self.assertTrue(any(f"#{c['id'][:8]} waits card #bbbbbbbb" in line and "answer desk" in line
                            for line in logs.output), logs.output)
        self.assertIn("decision_not_delivered", [k for _, k, _ in r.store.events(a.id)])

    def test_a_context_card_is_passed_over_for_a_live_card_named_after_it(self):
        d = task(3, cat="in_progress", assignee_id=LEAD, labels=["wake:manual"])
        t = task(1)
        c = task(2, cat="in_progress", assignee_id=LEAD)
        mesh = FakeMeshWith([t, c, d])
        r, desk = self.park(mesh, t, f"#{c['id'][:8]} ждёт канарейку; GO на #{d['id'][:8]}", {
            c["id"]: {"id": "w-c", "kind": "card", "target_task_id": "bbbbbbbb" + "0" * 28},
            d["id"]: {"id": "w-d", "kind": "manual", "note": "waiting for the GO"}})
        a = self.finish_turn(r)
        self.assertEqual(mesh.comment_log.get(c["id"], []), [])
        self.assertEqual(len(mesh.comment_log.get(d["id"], [])), 1)
        self.assertEqual(a.wait["delivered"]["card"], d["id"])
        self.assertEqual(a.wait["delivered"]["addressee_wait"], "w-d")

    def deaf(self, kind):
        mesh, r, desk, t, c = self.setup_named(kind)
        with self.assertLogs("mesh_runner", "INFO") as logs:
            a = self.finish_turn(r)
        self.assertEqual(mesh.comment_log.get(c["id"], []), [])
        self.assertNotIn("cardless", a.wait)                         # an order never goes to the desk
        self.assertEqual(len(mesh.created), 1)                       # the request card reaches them
        self.assertTrue(any(f"waits {kind}, which a comment does not end" in line and "request card" in line
                            for line in logs.output), logs.output)

    def test_a_card_in_a_registered_wait_cannot_take_it_a_request_card(self):
        self.deaf("registered")

    def test_a_card_waiting_for_a_person_cannot_take_it_a_request_card(self):
        self.deaf("human")

    def test_a_card_in_an_mr_wait_still_gets_it(self):
        mesh, r, desk, t, c = self.setup_named("mr")
        a = self.finish_turn(r)
        self.assertEqual(len(mesh.comment_log.get(c["id"], [])), 1)
        self.assertEqual((mesh.created, a.wait.get("cardless")), ([], None))
        self.assertEqual(a.wait["delivered"]["addressee_wait"], "w-c")


class UnheardDeliveryIsSeen(Asking):
    def delivered(self):
        t = task(1)
        c = task(2, cat="in_progress", assignee_id=LEAD, labels=["wake:manual"])
        mesh = FakeMeshWith([t, c])
        r, desk = self.park(mesh, t, f"GO на v30 (исполнение на #{c['id'][:8]})",
                            {c["id"]: {"id": "w-c", "kind": "manual", "note": "waiting for the lead's GO"}})
        a = self.finish_turn(r)
        self.assertEqual(a.wait["delivered"]["card"], c["id"])
        return mesh, r, a, c

    def age(self, r, a, minutes):
        wait = dict(r.store.get(a.id).wait)
        wait["delivered"] = dict(wait["delivered"], at=(dt.datetime.now(dt.timezone.utc)
                                                         - dt.timedelta(minutes=minutes)).isoformat())
        r.store.update(r.store.get(a.id), wait=wait)

    def test_fresh_delivery_is_not_checked(self):
        mesh, r, a, c = self.delivered()
        reads = mesh.typed_reads
        self.age(r, a, 5)
        r.tick()
        self.assertNotIn("unheard", r.store.get(a.id).wait["delivered"])
        self.assertNotIn("decision_delivered_unheard", [k for _, k, _ in r.store.events(a.id)])

    def test_unanswered_while_the_addressee_still_sleeps_is_logged_once(self):
        mesh, r, a, c = self.delivered()
        self.age(r, a, 31)
        with self.assertLogs("mesh_runner", "WARNING") as logs:
            r.tick()
        b = r.store.get(a.id)
        self.assertEqual(b.state, "waiting")                                # nothing re-routed
        self.assertTrue(b.wait["delivered"].get("unheard"))
        self.assertTrue(any(f"delivered on #{c['id'][:8]}" in line and "still sleeps in the manual wait" in line
                            for line in logs.output), logs.output)
        events = [k for _, k, _ in r.store.events(a.id)]
        self.assertEqual(events.count("decision_delivered_unheard"), 1)
        r.tick()
        self.assertEqual([k for _, k, _ in r.store.events(a.id)].count("decision_delivered_unheard"), 1)

    def test_a_woken_addressee_is_not_flagged(self):
        mesh, r, a, c = self.delivered()
        mesh.waits[c["id"]] = {"id": "w-c2", "kind": "mr", "note": "re-declared after reading the question"}
        self.age(r, a, 31)
        r.tick()
        d = r.store.get(a.id).wait["delivered"]
        self.assertTrue(d.get("heard"))
        self.assertNotIn("unheard", d)


if __name__ == "__main__":
    import unittest
    unittest.main()
