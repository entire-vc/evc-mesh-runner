"""Scenario tests of a turn that ends with marker text instead of a Mesh turn command.

The runner reads a turn's outcome only from the Mesh records the turn commands leave (turn outcomes and the
active wait). A ``⏳ WAIT`` / ``↻ NEXT`` / ``❓ Blocking`` line the agent writes as prose, in its final message
or in a comment, is ignored: the turn ends with no outcome, the runner files no request, posts no ask and
raises no gate. The rest of the file keeps the scenarios that used to be driven by those lines (the decision
request, its adoption and reconcile, the turn cap, the bounded timer correction) on the typed commands."""
import datetime as dt
import unittest
from unittest import mock

from fakes import (AGENT_ID, FUTURE_AT, FakeMesh, ask_agent, calls, mesh_turn, task, wait_card, wait_time,
                   write_replies)
from test_runner import Bench
from test_requeue_on_cap import CapBench

NOW = dt.datetime(2026, 10, 28, 12, tzinfo=dt.timezone.utc)
MARKERS = ("⏳ WAIT card:#abcdef12: the review",
           "⏳ WAIT decision:@lead: approve the plan?",
           "⏳ WAIT decision:@owner: which plan?",
           f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window",
           "↻ NEXT: did a piece",
           "❓ Blocking @owner: which plan?")


class MarkerTextIsIgnored(Bench):
    """A marker written as text, with no Mesh command recorded, is not an outcome."""

    def test_a_marker_only_final_message_ends_with_no_outcome_and_acts_on_nothing(self):
        for marker in MARKERS:
            with self.subTest(marker):
                self.tearDown()
                self.setUp()
                t = task(1)
                mesh = FakeMesh([t], max_concurrent=1)
                write_replies(self.replies, {t["id"][:8]: marker})
                r = self.runner(mesh)
                self.addCleanup(r.store.db.close)
                r.tick()
                a = self.finish_turn(r)
                self.assertEqual((a.state, a.no_outcome, a.wait), ("ready", 1, {}))
                self.assertEqual(r.store.events_since_readable(t["id"], "no_outcome"), 1)
                self.assertEqual(r.store.outcomes_today(self.agent.name, t["id"]), {})   # nothing was counted
                self.assertEqual(mesh.created, [])                                      # no decision request
                self.assertFalse(mesh.tasks[t["id"]].get("human_gate"))                 # no gate
                self.assertFalse(mesh.tasks[t["id"]].get("start_after"))                # no timer
                self.assertFalse(mesh.edges.get(t["id"]))                               # no card-wait edge
                self.assertFalse(any("Blocking" in c["body"] for c in mesh.comment_log.get(t["id"], [])))

    def test_a_marker_in_the_agents_own_comment_is_not_an_outcome(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "I posted my status on the card."})
        r = self.runner(mesh)
        self.addCleanup(r.store.db.close)
        r.tick()
        # the agent comments a marker line while the turn runs; the turn itself records no command
        mesh.add_comment(t["id"], "done for now\n↻ NEXT: continue with the second half", author=r.agent_id)
        a = self.finish_turn(r)
        self.assertEqual((a.state, a.no_outcome), ("ready", 1))
        self.assertEqual(r.store.outcomes_today(self.agent.name, t["id"]), {})

    def test_the_second_marker_only_turn_waits_for_new_input_not_for_the_marker(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT card:#abcdef12: the review"})
        r = self.runner(mesh)
        self.addCleanup(r.store.db.close)
        r.tick()
        self.settle_all(r)
        r.tick()
        self.settle_all(r)
        self.assertFalse(r.store.by_state("builder", ("waiting",)))                  # never parked on the marker
        self.assertEqual(mesh.category(mesh.tasks[t["id"]]), "todo")
        self.assertEqual((r._cap_wait(t["id"]) or {}).get("kind"), "no outcome")
        self.assertTrue(any("NO OUTCOME" in c["body"] for c in mesh.comment_log[t["id"]]))
        told = calls(self.calls)[1]
        self.assertTrue(told["resume"])
        self.assertIn("Text is never read as an outcome", told["prompt"])            # the correction names the commands
        self.assertIn("wait_for", told["prompt"])
        self.assertNotIn("⏳ WAIT", told["prompt"])


class DecisionRequest(Bench):
    """``ask_agent``: Mesh records the question; the runner files the request card that starts the addressee."""

    def test_ask_agent_files_and_closes_a_request(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: ask_agent("lead", "approve the plan?"),
                                     t["id"][:8] + ":resume": wait_time(FUTURE_AT, why="soak window",
                                                                         event_ref="fixture-window",
                                                                         source="card:#abcdef12")})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        self.assertEqual(len(mesh.created), 1)
        req = mesh.created[0]
        self.assertEqual(req["assignee_id"], "agent-lead")
        self.assertIn("decision-request", req["labels"])
        self.assertTrue(req["title"].startswith("[decision] approve the plan?"))
        mesh.add_comment(t["id"], "approved", author="agent-lead")
        r.tick()
        self.assertEqual(mesh.category(mesh.tasks[req["id"]]), "done")
        self.settle_all(r)

    def test_wait_begun_without_effects_is_adopted(self):
        # a wait that started under an older runner (or with effects off) still
        # gets its decision request once effects are on
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: ask_agent("lead", "approve the plan?")})
        self.cfg.mesh_effects = False
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        self.assertEqual(mesh.created, [])
        self.cfg.mesh_effects = True
        r = self.runner(mesh)
        r.tick()
        r.tick()
        self.assertEqual(len(mesh.created), 1)
        self.assertEqual(mesh.created[0]["assignee_id"], "agent-lead")
        self.assertFalse(mesh.tasks[t["id"]].get("start_after"))     # an event wait writes no hold

    def test_ambiguous_decision_create_requires_reconciliation_instead_of_duplicate(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        mesh.fail_create = 1
        write_replies(self.replies, {t["id"][:8]: ask_agent("lead", "approve the plan?")})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        r.tick()
        r.tick()
        self.assertEqual(mesh.fail_create, 0)        # the first try did fail
        self.assertEqual(len(mesh.created), 0)       # 503 might have created it: do not repeat POST
        r.tick()
        self.assertEqual(len(mesh.created), 0)
        self.assertEqual(r.effects._decision(t["id"])["state"], "creating")


class ManualWaitOnAnotherCard(Bench):
    """Mesh refuses ``wait_for on=manual`` with another card as target; a row that still names one is refused."""

    def test_a_manual_wait_naming_another_card_is_refused(self):
        t = task(1, labels=["wake:manual"])
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: mesh_turn("wait", on="manual", note="new attachment artifact",
                                                              target_task_id="abcdef12-0000-4000-8000-000000000000")})
        r = self.runner(mesh)
        r.tick()
        a = self.finish_turn(r)
        self.assertEqual((a.state, a.no_outcome, a.wait), ("ready", 1, {}))
        self.assertIn("wake:manual", a.wake_text)


class TimeGuard(Bench):
    """An invalid time wait is corrected once, then ends in a bounded correction hold, never a schedule."""

    def test_invalid_repeated_timer_is_bounded_even_when_already_warned_today(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        bad = wait_time("2026-12-01T06:00Z", why="poll: inbox", source="card:#abcdef12")
        write_replies(self.replies, {t['id'][:8]: bad, t['id'][:8] + ':resume': bad})
        r = self.runner(mesh)
        r.store.mark('*', t['id'], 'time_wait_told', NOW.date().isoformat())
        r.tick()
        self.settle_all(r)
        self.assertFalse(r.store.by_state(self.agent.name, ('waiting', 'running', 'ready', 'starting')))
        self.assertLessEqual(len(calls(self.calls)), self.cfg.max_no_outcome + 1)
        self.assertFalse(mesh.tasks[t['id']].get('start_after'))
        self.assertTrue(any('wait_for on=time' in c['body'] for c in mesh.comment_log[t['id']]))
        self.assertTrue(r.store.by_state(self.agent.name, ('failed',))[0].note.startswith('wait correction required:'))
        count = len(calls(self.calls))
        self.cfg.failed_cooldown_hours = 0
        with mock.patch('mesh_runner.runner.now', return_value=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)):
            r.tick()
            self.settle_all(r)
        self.assertEqual(len(calls(self.calls)), count)
        self.assertFalse(r._task_backoff)
        # The persisted failed attempt, not a process-local timer, owns the hold.
        self.assertIn('wait correction required', r._hold_reason(mesh.tasks[t['id']], 'work'))
        r.agent.name = 'replacement-lane'
        self.assertNotIn('wait correction required', r._hold_reason(mesh.tasks[t['id']], 'work'))
        r.agent.name = 'builder'
        mesh.add_comment(t['id'], 'Corrected: wait for card event instead', author='lead')
        other = task(0xabcdef12, assignee_id='other')
        mesh.tasks[other['id']] = other
        good = wait_card(other['id'])
        write_replies(self.replies, {t['id'][:8]: good, t['id'][:8] + ':resume': good})
        r.tick()
        self.settle_all(r)
        self.assertGreater(len(calls(self.calls)), count)


class DecisionCaps(CapBench):
    """A turn cap on an ``ask_agent`` wait: the runner files the request itself before requeueing."""

    def test_turn_cap_on_a_decision_wait_still_files_the_request(self):
        # The cap used to run before the ask was routed: the card went back to todo waiting for
        # "new input", but the addressee was never asked, so nothing could ever arrive.
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.daily_next_budget = 2
        self.cfg.daily_wait_budget = 0
        self.peer("lead", repos=[])                    # an addressee this runner serves
        write_replies(self.replies, {t["id"][:8]: ask_agent("lead", "approve the plan?")})
        r = self.runner(mesh)
        r.tick()
        self.finish_turn(r)
        self.assertEqual(len(mesh.created), 1)
        req = mesh.created[0]
        self.assertEqual(req["assignee_id"], "agent-lead")
        self.assertIn("decision-request", req["labels"])
        self.assertIn("cap: requeued (turn budget)", self.notes(r, t))
        self.assertEqual(mesh.category(mesh.tasks[t["id"]]), "todo")
        mesh.add_comment(t["id"], "approved", author="agent-lead")
        self.assertNotIn("waiting for new input", r.hold_reason(dict(mesh.tasks[t["id"]]), "work") or "")

    def test_turn_cap_files_the_request_when_the_agent_shares_its_slug_with_a_service_user(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.daily_next_budget = 2
        self.cfg.daily_wait_budget = 0
        self.peer("lead", repos=[])
        lead_id = next(a["id"] for a in mesh.agents if a["name"] == "lead")
        mesh.recipients = [
            {"id": lead_id, "kind": "agent", "slug": "lead", "display_name": "lead"},
            {"id": "u-lead", "kind": "user", "slug": "lead", "display_name": "lead (QA)",
             "is_service_user": True, "agent_id": lead_id}]
        write_replies(self.replies, {t["id"][:8]: ask_agent("lead", "approve the plan?")})
        r = self.runner(mesh)
        r.tick()
        self.finish_turn(r)
        self.assertEqual(len(mesh.created), 1)


if __name__ == "__main__":
    unittest.main()
