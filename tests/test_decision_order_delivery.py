"""A question that names a live card of its addressee is an order to act there. It is delivered as an
``@addressee`` comment on that card (which wakes a ``manual`` wait); the answer desk never takes it and no request
card is filed. Questions that name no card of the addressee keep the desk / request-card path."""
import datetime as dt
from unittest import mock

from fakes import AGENT_ID, task
from mesh_runner.effects import Effects
from mesh_runner.mesh import MeshError
from test_answer_desk import REQUEST, Desk, lead_lane

LEAD = "agent-lead"


class OrderDelivery(Desk):
    def park(self, mesh, t, question):
        """The asking lane ends its turn with ``ask_agent`` to ``lead``; the desk of ``lead`` is live."""
        r = self.run_turn(mesh, t)
        lead = lead_lane(mesh)
        desk = self.desk(mesh, r, lead)
        desk.step()
        self.ask(mesh, t, question=question)
        return r, desk

    def test_an_order_naming_the_addressees_manual_card_is_delivered_there(self):
        t = task(1)
        c = task(2, cat="in_progress", assignee_id=LEAD, labels=["wake:manual"])
        mesh = FakeMeshWith([t, c])
        r, desk = self.park(mesh, t, f"GO на v30 (исполнение на #{c['id'][:8]})")
        mesh.waits[c["id"]] = {"id": "w-c", "kind": "manual", "note": "waiting for the lead's GO"}
        a = self.finish_turn(r)
        self.assertEqual((a.state, a.wait["kind"]), ("waiting", "decision"))
        on_c = mesh.comment_log.get(c["id"], [])
        self.assertEqual(len(on_c), 1)
        body = on_c[0]["body"]
        self.assertTrue(body.startswith("@lead,"))
        for part in (REQUEST, "answer_decision", f"#{t['id'][:8]}", "GO на v30"):
            self.assertIn(part, body)
        self.assertNotEqual(on_c[0]["author_id"], LEAD)              # someone else's comment: it wakes manual
        self.assertEqual(mesh.created, [])                           # no request card
        self.assertNotIn("cardless", a.wait)                         # not on the desk
        self.assertEqual(desk.state("lead", REQUEST), "")
        self.assertEqual(a.wait["delivered"]["card"], c["id"])
        self.assertIn(("decision_delivered", f"#{c['id'][:8]} {REQUEST}"),
                      [(k, d) for _, k, d in r.store.events(a.id)])
        # later passes neither deliver again, nor file a card, nor reach the desk
        desk.step()
        self.clock.advance(30)
        r.tick()
        desk.step()
        self.assertEqual(len(mesh.comment_log.get(c["id"], [])), 1)
        self.assertEqual(mesh.created, [])
        self.assertEqual(self.turns, [])
        self.assertNotIn(mesh.tasks[c["id"]]["status_id"], ("s-done", "s-cancelled"))

    def test_an_in_progress_card_of_the_addressee_without_a_wait_gets_it_too(self):
        t = task(1)
        c = task(2, cat="in_progress", assignee_id=LEAD)
        mesh = FakeMeshWith([t, c])
        r, desk = self.park(mesh, t, f"run the migration on #{c['id'][:8]} now")
        a = self.finish_turn(r)
        self.assertEqual(len(mesh.comment_log.get(c["id"], [])), 1)
        self.assertEqual((mesh.created, a.wait.get("cardless")), ([], None))

    def test_a_question_naming_no_card_still_goes_to_the_desk(self):
        t = task(1)
        mesh = FakeMeshWith([t])
        r, desk = self.park(mesh, t, "ship v15 or hold it?")
        a = self.finish_turn(r)
        self.assertEqual(a.wait["cardless"]["lane"], "lead")
        self.assertEqual(desk.state("lead", REQUEST), "queued")
        self.assertNotIn("delivered", a.wait)

    def test_a_question_naming_someone_elses_card_still_goes_to_the_desk(self):
        t = task(1)
        other = task(3, cat="in_progress", assignee_id="agent-x")
        mesh = FakeMeshWith([t, other])
        r, desk = self.park(mesh, t, f"is #{other['id'][:8]} safe to ship?")
        a = self.finish_turn(r)
        self.assertEqual(a.wait["cardless"]["lane"], "lead")
        self.assertEqual(mesh.comment_log.get(other["id"], []), [])

    def _named_after_others(self, position):
        """The addressee's live card is named at ``position`` (1-based), after cards of someone else."""
        t = task(1)
        others = [task(10 + i, cat="in_progress", assignee_id="agent-x") for i in range(position - 1)]
        c = task(2, cat="in_progress", assignee_id=LEAD, labels=["wake:manual"])
        mesh = FakeMeshWith([t, c, *others])
        mesh.waits[c["id"]] = {"id": "w-c", "kind": "manual", "note": "waiting for the lead's GO"}
        refs = " ".join(f"#{o['id'][:8]}" for o in others)
        r, desk = self.park(mesh, t, f"after {refs} are green: GO on #{c['id'][:8]}")
        a = self.finish_turn(r)
        return mesh, desk, a, c, others

    def test_the_addressees_live_card_named_fifth_gets_it(self):
        mesh, desk, a, c, others = self._named_after_others(5)
        self.assertEqual(len(mesh.comment_log.get(c["id"], [])), 1)
        self.assertEqual((desk.state("lead", REQUEST), mesh.created, a.wait.get("cardless")), ("", [], None))
        for o in others:
            self.assertEqual(mesh.comment_log.get(o["id"], []), [])

    def test_the_addressees_live_card_named_after_five_other_cards_gets_it_too(self):
        """No cap on the named cards: the live card of the addressee in sixth place is not skipped for the desk."""
        mesh, desk, a, c, others = self._named_after_others(6)
        self.assertEqual(len(mesh.comment_log.get(c["id"], [])), 1)
        self.assertEqual((desk.state("lead", REQUEST), mesh.created, a.wait.get("cardless")), ("", [], None))
        self.assertEqual(a.wait["delivered"]["card"], c["id"])
        for o in others:
            self.assertEqual(mesh.comment_log.get(o["id"], []), [])

    def _failing_lookups(self, mesh, short, failures):
        """``task_by_short(short)`` raises MeshError(503) ``failures`` times, then reads; returns the call count."""
        calls, plain = [0], mesh.task_by_short

        def task_by_short(ref):
            if ref == short:
                calls[0] += 1
                if calls[0] <= failures:
                    raise MeshError(503, "upstream unavailable")
            return plain(ref)
        mesh.task_by_short = task_by_short
        return calls

    def test_a_transient_lookup_failure_chooses_no_route_and_the_next_pass_delivers(self):
        t = task(1)
        c = task(2, cat="in_progress", assignee_id=LEAD, labels=["wake:manual"])
        mesh = FakeMeshWith([t, c])
        calls = self._failing_lookups(mesh, c["id"][:8], failures=1)
        r, desk = self.park(mesh, t, f"GO on #{c['id'][:8]}")
        with self.assertLogs("mesh_runner", "INFO") as logs:
            a = self.finish_turn(r)
        self.assertEqual(mesh.comment_log.get(c["id"], []), [])
        self.assertEqual((mesh.created, a.wait.get("cardless"), desk.state("lead", REQUEST)), ([], None, ""))
        self.assertTrue(any(line.startswith("WARNING") and "no route chosen" in line for line in logs.output))
        self.assertNotIn("order_checked", a.wait)                 # the route is not chosen yet
        self.assertEqual(a.wait["order_unread"], 1)
        r.tick()                                                   # the next pass reads the card again
        a = r.store.get(a.id)
        self.assertEqual(calls[0], 2)
        self.assertEqual(len(mesh.comment_log.get(c["id"], [])), 1)
        self.assertEqual(a.wait["delivered"]["card"], c["id"])
        self.assertNotIn("order_unread", a.wait)
        r.tick()
        desk.step()
        self.assertEqual((len(mesh.comment_log.get(c["id"], [])), mesh.created, desk.state("lead", REQUEST)),
                         (1, [], ""))

    def test_a_card_that_stays_unread_ends_on_a_request_card_never_on_the_desk(self):
        t = task(1)
        c = task(2, cat="in_progress", assignee_id=LEAD)
        mesh = FakeMeshWith([t, c])
        calls = self._failing_lookups(mesh, c["id"][:8], failures=10 ** 6)
        with mock.patch.object(Effects, "ORDER_LOOKUP_PASSES", 3, create=True):
            r, desk = self.park(mesh, t, f"GO on #{c['id'][:8]}")
            with self.assertLogs("mesh_runner", "INFO"):
                a = self.finish_turn(r)
            r.tick()
            self.assertEqual((mesh.created, desk.state("lead", REQUEST)), ([], ""))   # two passes: no route yet
            with self.assertLogs("mesh_runner", "WARNING") as logs:
                r.tick()                                           # the third: request card
        a = r.store.get(a.id)
        self.assertEqual(calls[0], 3)
        self.assertTrue(any("request card, not the answer desk" in line for line in logs.output))
        self.assertEqual(len(mesh.created), 1)
        self.assertNotIn("cardless", a.wait)
        self.assertEqual(desk.state("lead", REQUEST), "")
        self.assertEqual(mesh.comment_log.get(c["id"], []), [])

    def test_an_unread_wait_of_the_addressees_card_is_read_again(self):
        t = task(1)
        c = task(2, cat="todo", assignee_id=LEAD, labels=["wake:manual"])
        mesh = FakeMeshWith([t, c])
        r, desk = self.park(mesh, t, f"GO on #{c['id'][:8]}")
        mesh.waits[c["id"]] = {"id": "w-c", "kind": "manual", "note": "waiting for the lead's GO"}
        plain = mesh.active_wait
        failed = []

        def active_wait(tid, agent_id):
            if tid == c["id"] and not failed:
                failed.append(tid)
                raise MeshError(502, "bad gateway")
            return plain(tid, agent_id)
        mesh.active_wait = active_wait
        with self.assertLogs("mesh_runner", "INFO"):
            a = self.finish_turn(r)
        self.assertEqual((mesh.comment_log.get(c["id"], []), mesh.created), ([], []))
        r.tick()
        a = r.store.get(a.id)
        self.assertEqual(len(mesh.comment_log.get(c["id"], [])), 1)
        self.assertEqual((mesh.created, a.wait.get("cardless")), ([], None))

    def test_a_named_card_that_does_not_exist_is_a_definite_answer(self):
        """404 is definite: no pass is spent reading again and a question naming no card of the addressee goes to
        the desk at once."""
        t = task(1)
        mesh = FakeMeshWith([t])
        r, desk = self.park(mesh, t, "is #deadbeef shipped?")
        a = self.finish_turn(r)
        self.assertEqual(a.wait["cardless"]["lane"], "lead")
        self.assertNotIn("order_unread", a.wait)
        self.assertTrue(a.wait["order_checked"])

    def test_a_named_card_of_the_addressee_that_is_not_live_keeps_it_off_the_desk(self):
        t = task(1)
        c = task(2, cat="todo", assignee_id=LEAD)
        mesh = FakeMeshWith([t, c])
        r, desk = self.park(mesh, t, f"take #{c['id'][:8]} next")
        a = self.finish_turn(r)
        self.assertNotIn("cardless", a.wait)
        self.assertEqual(desk.state("lead", REQUEST), "")
        self.assertEqual(len(mesh.created), 1)                       # the ordinary request card
        self.assertEqual(mesh.comment_log.get(c["id"], []), [])


    def test_a_lost_answer_to_the_delivery_post_writes_it_again_with_the_same_key_and_files_no_card(self):
        """The comment is written, the reply is lost (MeshError 0): no request card, the same write is repeated
        with the same Idempotency-Key on the next pass and Mesh keeps one comment."""
        t = task(1)
        c = task(2, cat="in_progress", assignee_id=LEAD)
        mesh = FakeMeshWith([t, c])
        written, keys, lost = {}, [], [True]
        plain = mesh.add_comment

        def add_comment(tid, body, author=AGENT_ID, at=None, key=""):
            if tid != c["id"]:
                return plain(tid, body, author=author, at=at, key=key)
            keys.append(key)
            if key in written:                                  # Mesh's idempotent replay
                return written[key]
            written[key] = plain(tid, body, author=author, at=at, key=key)
            if lost and lost.pop():
                raise MeshError(0, "connection reset after the write")
            return written[key]
        mesh.add_comment = add_comment
        r, desk = self.park(mesh, t, f"GO on #{c['id'][:8]}")
        with self.assertLogs("mesh_runner", "WARNING"):
            a = self.finish_turn(r)
        self.assertEqual(len(mesh.comment_log.get(c["id"], [])), 1)
        self.assertEqual(mesh.created, [])
        self.assertNotIn("cardless", a.wait)
        self.assertIn("delivering", r.store.get(a.id).wait)
        r.tick()                                                # the next pass: the same write again
        a = r.store.get(a.id)
        self.assertEqual(len(mesh.comment_log.get(c["id"], [])), 1)
        self.assertEqual(len(keys), 2)
        self.assertEqual(len(set(keys)), 1)                      # the same Idempotency-Key
        self.assertEqual(mesh.created, [])
        self.assertEqual(a.wait["delivered"]["card"], c["id"])
        self.assertNotIn("delivering", a.wait)
        r.tick()
        self.assertEqual((len(keys), mesh.created), (2, []))    # delivered: nothing more is written

    def test_a_refused_delivery_falls_back_to_the_request_card(self):
        t = task(1)
        c = task(2, cat="in_progress", assignee_id=LEAD)
        mesh = FakeMeshWith([t, c])
        plain = mesh.add_comment

        def add_comment(tid, body, author=AGENT_ID, at=None, key=""):
            if tid == c["id"]:
                raise MeshError(403, "agent is not a member of this project")
            return plain(tid, body, author=author, at=at, key=key)
        mesh.add_comment = add_comment
        r, desk = self.park(mesh, t, f"GO on #{c['id'][:8]}")
        with self.assertLogs("mesh_runner", "WARNING"):
            a = self.finish_turn(r)
        self.assertNotIn("delivered", a.wait)
        self.assertNotIn("cardless", a.wait)                     # an order still never reaches the desk
        self.assertEqual(len(mesh.created), 1)

    def test_the_addressees_answer_resumes_the_asker(self):
        t = task(1)
        c = task(2, cat="in_progress", assignee_id=LEAD)
        mesh = FakeMeshWith([t, c])
        r, desk = self.park(mesh, t, f"GO on #{c['id'][:8]}")
        a = self.finish_turn(r)
        self.assertEqual(a.state, "waiting")
        mesh.answer_decision(REQUEST, "done on the card: v30 applied", author=LEAD)
        answer = mesh.comment_log[t["id"]][-1]
        answer["created_at"] = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30)).isoformat()  # after since
        mesh.waits.pop(t["id"], None)                            # Mesh resolves the typed wait on the answer
        r.tick()
        resumed = r.store.get(a.id)
        self.assertNotEqual(resumed.state, "waiting")
        self.assertIn(("mesh_wait_resolved", "w-1:met"),                 # the addressee's answer: met
                      [(k, d) for _, k, d in r.store.events(a.id)])
        self.assertNotIn(mesh.tasks[c["id"]]["status_id"], ("s-done",))   # the execution card is not closed
        self.assertEqual(mesh.created, [])


def FakeMeshWith(tasks):
    from fakes import FakeMesh
    mesh = FakeMesh(tasks, max_concurrent=1)
    mesh.waits = {}
    return mesh
