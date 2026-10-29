"""Resume guidance reaches a real child; deterministic adoption is not live agent compliance."""
import datetime as dt
from unittest.mock import patch

from fakes import FAKE_AGENT, FakeMesh, calls, task, write_replies, FUTURE_AT
from mesh_runner.store import iso, now
from test_runner import Bench


class DecisionTimerAdoption(Bench):
    def setUp(self):
        super().setUp()
        # A deterministic producer: follow the supplied decision guidance, else
        # reproduce a forbidden timer loop. This is no language-model simulation.
        script = FAKE_AGENT.replace('if reply.startswith("GATE:"):', f'''
if reply == "PENDING_ANSWER":
    guided = "WAIT decision:@" in prompt and "same question" in prompt
    reply = ("⏳ WAIT decision:@lead: approve the plan?" if guided
             else "⏳ WAIT 2h: awaiting response")
if reply == "SCHEDULED_WORK":
    from pathlib import Path
    Path(__file__).with_name("scheduled-result.txt").write_text("scheduled result\\n")
    reply = "⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 next scheduled check"
if reply.startswith("GATE:"):''')
        (self.root / 'agent.py').write_text(script)

    def turn(self, runner):
        runner.tick()
        return self.finish_turn(runner)

    def question_then_timer(self):
        source = task(1)
        mesh = FakeMesh([source], max_concurrent=1)
        runner = self.runner(mesh)
        write_replies(self.replies, {source['id'][:8]: '⏳ WAIT decision:@lead: approve the plan?'})
        attempt = self.turn(runner)
        request = attempt.wait['request']['id']
        # An observation is not an answer. A separate sourced external deadline
        # must retain the canonical question for adoption after the event.
        mesh.add_comment(source['id'], 'review still pending', author='bystander')
        write_replies(self.replies, {source['id'][:8]: f'⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 awaiting response'})
        attempt = self.turn(runner)
        self.assertEqual(attempt.wait['kind'], 'time')
        self.assertEqual(mesh.category(mesh.tasks[request]), 'todo')
        runner.store.update(attempt, wait=dict(attempt.wait, until=iso(now()-dt.timedelta(seconds=1))))
        return runner, mesh, source, runner.store.get(attempt.id), request

    def adopt(self):
        runner, mesh, source, attempt, request = self.question_then_timer()
        write_replies(self.replies, {source['id'][:8]: 'PENDING_ANSWER'})
        attempt = self.turn(runner)
        received = calls(self.calls)[-1]['prompt']
        self.assertIn('elapsed', received)
        self.assertEqual(attempt.wait['kind'], 'decision')
        self.assertEqual(attempt.wait['request']['id'], request)
        self.assertEqual(len(mesh.created), 1)
        return runner, mesh, source, attempt, request

    def test_expired_timer_resume_adopts_existing_question_and_sleeps_without_answer(self):
        runner, mesh, source, attempt, request = self.adopt()
        before = len(calls(self.calls))
        self.assertEqual(before, 3)
        with patch('mesh_runner.runner.now', return_value=now()+dt.timedelta(hours=12)):
            runner._wake_one(attempt)
        self.assertEqual(runner.store.get(attempt.id).state, 'waiting')
        self.assertEqual(runner.procs, {})
        self.assertEqual(len(calls(self.calls)), before)
        self.assertEqual(mesh.category(mesh.tasks[request]), 'todo')

    def test_addressed_answer_resumes_real_child_and_closes_retained_question(self):
        runner, mesh, source, attempt, request = self.adopt()
        mesh.add_comment(source['id'], 'approved; proceed', author='agent-lead')
        write_replies(self.replies, {source['id'][:8]: 'SCHEDULED_WORK'})
        attempt = self.turn(runner)
        self.assertEqual(len(calls(self.calls)), 4)
        self.assertIn('approved; proceed', calls(self.calls)[-1]['prompt'])
        self.assertEqual((self.root/'scheduled-result.txt').read_text(), 'scheduled result\n')
        self.assertEqual(mesh.category(mesh.tasks[request]), 'done')
        self.assertFalse(runner.effects._decision(source['id']))

    def test_bystander_evidence_wakes_but_reuses_question_without_answering_it(self):
        runner, mesh, source, attempt, request = self.adopt()
        mesh.add_comment(source['id'], 'new review evidence', author='bystander')
        attempt = self.turn(runner)
        self.assertEqual(len(calls(self.calls)), 4)
        self.assertIn('new review evidence', calls(self.calls)[-1]['prompt'])
        self.assertEqual(attempt.wait['request']['id'], request)
        self.assertEqual(len(mesh.created), 1)
        self.assertEqual(mesh.category(mesh.tasks[request]), 'todo')
        mark = runner.store.mark(self.agent.name, source['id'], 'met_wake')
        self.assertFalse(mark and mark[0])

    def test_actionable_deadline_runs_even_with_an_unanswered_question(self):
        runner, mesh, source, attempt, request = self.question_then_timer()
        write_replies(self.replies, {source['id'][:8]: 'SCHEDULED_WORK'})
        attempt = self.turn(runner)
        self.assertEqual(len(calls(self.calls)), 3)
        self.assertIn('elapsed', calls(self.calls)[-1]['prompt'])
        self.assertEqual((self.root/'scheduled-result.txt').read_text(), 'scheduled result\n')
        self.assertEqual(attempt.wait['kind'], 'time')
        self.assertEqual(mesh.category(mesh.tasks[request]), 'todo')
        self.assertEqual(len(mesh.created), 1)
