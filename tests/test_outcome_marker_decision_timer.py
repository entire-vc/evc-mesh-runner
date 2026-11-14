"""An expired timer resumes the real child even while an ask_agent question stays open.

The turns end with typed ``ask_agent`` / ``wait_for on=time`` commands. Deleted with the marker fallback:
the runner-side filing, adoption and closing of a decision request (Mesh opens ask_agent requests itself)."""
import datetime as dt
from unittest.mock import patch

from fakes import FAKE_AGENT, FakeMesh, ask_agent, calls, task, wait_time, write_replies, FUTURE_AT
from mesh_runner.store import iso, now
from test_runner import Bench


class DecisionTimerAdoption(Bench):
    def setUp(self):
        super().setUp()
        # A deterministic producer: follow the supplied decision guidance, else
        # reproduce a forbidden timer loop. This is no language-model simulation.
        script = FAKE_AGENT.replace('if reply.startswith("GATE:"):', f'''
if reply == "SCHEDULED_WORK":
    from pathlib import Path
    Path(__file__).with_name("scheduled-result.txt").write_text("scheduled result\\n")
    reply = ""
if reply.startswith("GATE:"):''')
        (self.root / 'agent.py').write_text(script)

    def turn(self, runner):
        runner.tick()
        return self.finish_turn(runner)

    def question_then_timer(self):
        source = task(1)
        mesh = FakeMesh([source], max_concurrent=1)
        runner = self.runner(mesh)
        write_replies(self.replies, {source['id'][:8]: ask_agent('lead', 'approve the plan?')})
        attempt = self.turn(runner)
        self.assertEqual(attempt.wait['kind'], 'decision')
        request = attempt.wait['decision_request_id']
        # An observation is not an answer. A separate sourced external deadline replaces the question wait.
        mesh.add_comment(source['id'], 'review still pending', author='bystander')
        write_replies(self.replies, {source['id'][:8]: wait_time(FUTURE_AT, why='awaiting response', source='card:#abcdef12')})
        attempt = self.turn(runner)
        self.assertEqual(attempt.wait['kind'], 'time')
        runner.store.update(attempt, wait=dict(attempt.wait, until=iso(now()-dt.timedelta(seconds=1))))
        return runner, mesh, source, runner.store.get(attempt.id), request

    def test_actionable_deadline_runs_even_with_an_unanswered_question(self):
        runner, mesh, source, attempt, request = self.question_then_timer()
        write_replies(self.replies, {source['id'][:8]: wait_time(FUTURE_AT, why='next scheduled check',
                                                                 source='card:#abcdef12', text='SCHEDULED_WORK')})
        attempt = self.turn(runner)
        self.assertEqual(len(calls(self.calls)), 3)
        self.assertIn('elapsed', calls(self.calls)[-1]['prompt'])
        self.assertEqual((self.root/'scheduled-result.txt').read_text(), 'scheduled result\n')
        self.assertEqual(attempt.wait['kind'], 'time')
        # the one request card filed for the question; the timer turns file nothing more
        self.assertEqual([c["assignee_id"] for c in mesh.created], ["agent-lead"])
