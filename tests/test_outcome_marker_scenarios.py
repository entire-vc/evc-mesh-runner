"""Scenario tests of the marker-line fallback (``⏳ WAIT`` / ``↻ NEXT`` / ``❓ Blocking`` written as text).

The parser's own tests: a lane that ends a turn with a marker line instead of a Mesh turn command is
read by ``parse_outcome`` and routed by the runner itself (it files the decision request, posts the
``❓ Blocking`` ask, quotes an unreadable line back, bounds an invalid timer). Every other scenario
test drives the fake agent through typed turn commands (``fakes.mesh_turn``), the way live lanes end
turns. These go away together with the marker parser."""
import datetime as dt
import unittest
from unittest import mock

from mesh_runner.mesh import MeshError
from mesh_runner.outcome import Outcome

from fakes import AGENT_ID, FUTURE_AT, FakeMesh, calls, task, write_replies
from test_runner import Bench
from test_requeue_on_cap import CapBench

NOW = dt.datetime(2026, 10, 28, 12, tzinfo=dt.timezone.utc)


class MarkerAsks(Bench):
    """``❓ Blocking`` and ``⏳ WAIT decision:`` lines: the runner raises the ask itself."""

    def test_a_question_mesh_did_not_raise_is_told_once_then_waits(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "❓ Blocking @owner: which plan?"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        r.tick()
        self.settle_all(r)
        a = r.store.by_state("builder", ("failed",))[0]
        self.assertTrue(a.note.startswith("wait correction required:"))
        self.assertFalse(r.store.by_state("builder", ("waiting",)))
        self.assertFalse(mesh.tasks[t["id"]].get("human_gate"))       # the runner never sets it
        told = calls(self.calls)[1]
        self.assertTrue(told["resume"])
        self.assertIn("Mesh raised no question to them", told["prompt"])
        self.assertIn("❓ Blocking @owner:", told["prompt"])
        for _ in range(3):
            r.tick()
        self.assertEqual(len(calls(self.calls)), 2)        # told once, no ping-pong without an answer

    def test_wait_decision_on_a_person_is_posted_as_a_blocking_ask(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        read = mesh.task
        # Mesh raises the gate from the posted `❓ Blocking` line
        mesh.task = lambda tid: ({"task": dict(read(tid), human_gate=True)}
                                 if any("❓ Blocking @owner:" in c["body"] for c in mesh.comments(tid))
                                 else read(tid))
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT decision:@owner: which plan?"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        a = r.store.by_state("builder", ("waiting",))[0]
        self.assertEqual(a.wait["kind"], "blocking")
        self.assertTrue(any("❓ Blocking @owner: which plan?" in c["body"] for c in mesh.comments(t["id"])))
        self.assertEqual(len(calls(self.calls)), 1)        # the agent is not woken to repeat itself

    def test_person_ask_dedup_is_per_question_and_addressee_and_survives_a_failed_post(self):
        from unittest.mock import patch
        from mesh_runner.outcome import Outcome
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT decision:@owner: which plan?"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        a = r.store.by_state("builder", ("failed",))[0]
        self.assertTrue(a.note.startswith("wait correction required:"))

        def asks():
            return [c for c in mesh.comments(t["id"]) if "❓ Blocking @" in c["body"]]

        def route(handle, note):
            r._route_ask(a, t, Outcome(kind="decision", handle=handle, note=note), {}, "2026-10-21T00:00:00Z")
        base = len(asks())
        with patch.object(mesh, "add_comment", side_effect=MeshError(503, "down")):
            route("owner2", "pick one?")
        self.assertEqual(len(asks()), base)               # not delivered
        route("owner2", "pick one?")
        self.assertEqual(len(asks()), base + 1)           # retried after the failure
        route("owner2", "pick one?")
        self.assertEqual(len(asks()), base + 1)           # once delivered, not repeated
        route("owner3", "pick one?")
        self.assertEqual(len(asks()), base + 2)           # same text, new addressee
        route("owner2", "other?")
        route("owner2", "pick one?")
        self.assertEqual(len(asks()), base + 3)           # A-B-A: A is not posted again

    def test_a_refused_repeat_question_hands_the_agent_the_earlier_answer(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "❓ Blocking @owner: keep both machines?"})
        comments = mesh.comments

        def with_refusal(tid, limit=20):
            out = comments(tid, limit)
            if calls(self.calls):                    # every ask is refused, each time a new notice
                out.append({"id": f"refusal-{len(calls(self.calls))}",
                            "author_id": "00000000-0000-0000-0000-000000000000",
                            "system_params": {"answered_at": "2026-10-10 11:24"},
                            "author_type": "system", "system_code": "repeat_ask_refused",
                            "body": "🔁 Auto: human_gate not armed — the previous question already has an "
                                    "answer (2026-10-10 11:24): «разрешаю».",
                            "created_at": dt.datetime.now(dt.timezone.utc).isoformat()})
            return out
        mesh.comments = with_refusal
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        r.tick()
        self.settle_all(r)
        told = calls(self.calls)[1]
        self.assertIn("refused it as already answered", told["prompt"])
        self.assertIn("«разрешаю»", told["prompt"])
        self.assertIn("supersedes:", told["prompt"])
        self.assertFalse(mesh.tasks[t["id"]].get("human_gate"))
        for _ in range(3):
            r.tick()
        self.assertEqual(len(calls(self.calls)), 2)        # the same refusal again does not resume it

    def test_blocking_addressed_to_an_agent_files_a_decision_request_not_a_gate(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "❓ Blocking @lead: list it from the personal account?"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        a = r.store.by_state("builder", ("waiting",))[0]
        self.assertEqual(a.wait["kind"], "decision")
        self.assertFalse(mesh.tasks[t["id"]].get("human_gate"))
        self.assertEqual(mesh.created[0]["assignee_id"], "agent-lead")
        self.assertEqual(len(calls(self.calls)), 1)

    def test_decision_wait_on_an_agent_files_and_closes_a_request(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT decision:@lead: approve the plan?",
                                     t["id"][:8] + ":resume": f"⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 soak window"})
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
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT decision:@lead: approve the plan?"})
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
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT decision:@lead: approve the plan?"})
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

    def test_decision_wait_on_a_human_files_nothing_and_sets_no_gate(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT decision:@person"})
        r = self.runner(mesh)
        r.tick()
        self.assertEqual(self.finish_turn(r).state, "ready")
        self.assertEqual(mesh.created, [])
        self.assertFalse(mesh.tasks[t["id"]].get("human_gate"))   # only Mesh raises a person's gate
        r.tick()
        self.assertEqual(self.finish_turn(r).state, "failed")
        self.assertIn("is for agents", calls(self.calls)[1]["prompt"])
        self.assertEqual(mesh.created, [])
        self.assertFalse(mesh.tasks[t["id"]].get("human_gate"))

    def test_unreadable_declaration_is_quoted_back_then_the_task_waits_for_new_input(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.agent.lead = "lead"
        # a malformed event wait; a non-event WAIT is a time wait and ends in a correction hold instead
        write_replies(self.replies, {t["id"][:8]: "done\n⏳ WAIT card:soon-ish"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        prompts_seen = [c["prompt"] for c in calls(self.calls)]
        self.assertEqual(len(prompts_seen), 2)
        self.assertIn("⏳ WAIT card:soon-ish", prompts_seen[1])     # the agent sees what was wrong
        self.assertIn("⏳ WAIT card:#<8hex>", prompts_seen[1])      # and the forms that work
        self.assertEqual(mesh.tasks[t["id"]]["assignee_id"], AGENT_ID)      # the second miss is not a give-up
        self.assertEqual(mesh.category(mesh.tasks[t["id"]]), "todo")
        self.assertEqual((r._cap_wait(t["id"]) or {}).get("kind"), "no outcome")
        self.assertTrue(any("NO OUTCOME" in c["body"] for c in mesh.comment_log[t["id"]]))
        self.assertFalse(any("RUNNER GAVE UP" in c["body"] for c in mesh.comment_log[t["id"]]))

    def test_refused_person_gate_finishes_without_resurrecting_as_waiting(self):
        from mesh_runner.outcome import Outcome
        t=task(1);mesh=FakeMesh([t],max_concurrent=1)
        write_replies(self.replies,{t['id'][:8]:'❓ Blocking @owner: choose?'})
        r=self.runner(mesh)
        r.store.mark(self.agent.name,t['id'],'ask_not_raised','no-marker:blocking')
        r.tick();self.settle_all(r)
        self.assertFalse(r.store.by_state(self.agent.name,('waiting','ready','running')))
        self.assertTrue(r.store.by_state(self.agent.name,('failed',)))
        self.assertFalse(mesh.tasks[t['id']].get('start_after'))
        self.assertTrue(any('Repair owner:' in c['body'] for c in mesh.comment_log[t['id']]))

    def test_repeated_invalid_decision_wait_stops_after_one_correction(self):
        """An addressee outside the directory: Mesh refuses ask_agent to it (not_an_agent), so only a
        marker line reaches the runner's recipient check."""
        t = task(1)
        mesh = FakeMesh([t])
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT decision:@code-reviewer: pick one?"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        for _ in range(4):
            r.tick()
        self.assertEqual(len(calls(self.calls)), 2)
        self.assertEqual(r.store.by_state("builder", ("failed",))[0].state, "failed")
        self.assertFalse(any("❓ Blocking" in c["body"] for c in mesh.comments(t["id"])))


class MarkerManualWait(Bench):
    """``⏳ WAIT manual:#<card>`` naming another card: Mesh refuses wait_for on=manual with another target."""

    def test_a_manual_marker_naming_another_card_is_refused(self):
        """Mesh refuses `wait_for on=manual` with another card as target; only a marker line can name one."""
        t = task(1, labels=["wake:manual"])
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT manual:#abcdef12 — вход: новый attachment-артефакт"})
        r = self.runner(mesh)
        r.tick()
        a = self.finish_turn(r)
        self.assertEqual((a.state, a.no_outcome, a.wait), ("ready", 1, {}))
        self.assertIn("wake:manual", a.wake_text)


class MarkerTimeGuard(Bench):
    """An invalid ``⏳ WAIT`` timer line ends in a bounded correction hold, never a schedule."""

    def test_invalid_repeated_timer_is_bounded_even_when_already_warned_today(self):
        t = task(1); mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t['id'][:8]:'⏳ WAIT 6h: poll: inbox', t['id'][:8]+':resume':'⏳ WAIT 6h: poll: inbox'})
        r = self.runner(mesh)
        r.store.mark('*', t['id'], 'time_wait_told', NOW.date().isoformat())
        r.tick(); self.settle_all(r)
        self.assertFalse(r.store.by_state(self.agent.name, ('waiting','running','ready','starting')))
        self.assertLessEqual(len(calls(self.calls)), self.cfg.max_no_outcome + 1)
        self.assertFalse(mesh.tasks[t['id']].get('start_after'))
        self.assertTrue(any('source=' in c['body'] for c in mesh.comment_log[t['id']]))
        self.assertTrue(r.store.by_state(self.agent.name, ('failed',))[0].note.startswith('wait correction required:'))
        count = len(calls(self.calls))
        self.cfg.failed_cooldown_hours = 0
        with mock.patch('mesh_runner.runner.now', return_value=dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=2)):
            r.tick(); self.settle_all(r)
        self.assertEqual(len(calls(self.calls)), count)
        self.assertFalse(r._task_backoff)
        # The persisted failed attempt, not a process-local timer, owns the hold.
        self.assertIn('wait correction required', r._hold_reason(mesh.tasks[t['id']], 'work'))
        r.agent.name='replacement-lane'
        self.assertNotIn('wait correction required', r._hold_reason(mesh.tasks[t['id']], 'work'))
        r.agent.name='builder'
        mesh.add_comment(t['id'], 'Corrected: wait for card event instead', author='lead')
        mesh.tasks[task(0xabcdef12, assignee_id='other')['id']] = task(0xabcdef12, assignee_id='other')
        write_replies(self.replies, {t['id'][:8]:'⏳ WAIT card:#abcdef12', t['id'][:8]+':resume':'⏳ WAIT card:#abcdef12'})
        r.tick(); self.settle_all(r)
        self.assertGreater(len(calls(self.calls)), count)


class MarkerCaps(CapBench):
    """A turn cap on a ``⏳ WAIT decision:`` line: the runner files the request itself before requeueing."""

    def test_turn_cap_on_a_decision_wait_still_files_the_request(self):
        # The cap used to run before the ask was routed: the card went back to todo waiting for
        # "new input", but the addressee was never asked, so nothing could ever arrive.
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.daily_next_budget = 2
        self.cfg.daily_wait_budget = 0
        self.peer("lead", repos=[])                    # an addressee this runner serves
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT decision:@lead: approve the plan?"})
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
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT decision:@lead: approve the plan?"})
        r = self.runner(mesh)
        r.tick()
        self.finish_turn(r)
        self.assertEqual(len(mesh.created), 1)


if __name__ == "__main__":
    unittest.main()
