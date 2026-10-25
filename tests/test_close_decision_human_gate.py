"""A decision request whose close Mesh refuses behind a human gate is not retried every pass."""
import datetime as dt
import unittest
from unittest.mock import patch

import test_decision_lifecycle as lifecycle
from test_runner import Bench
from fakes import FakeMesh
from mesh_runner import effects as fx
from mesh_runner.effects import CLOSE_BLOCKED_MARK, CLOSE_BLOCKED_RETRY, DECISION_OWNER
from mesh_runner.mesh import MeshError
from mesh_runner.store import now

GATE = ("Task is awaiting human sign-off (human_gate=true). "
        "Only a human can approve or reject it.")


class GatedMove:
    """Mesh.move that refuses the request card while its human gate is up."""

    def __init__(self, mesh: FakeMesh, request_id: str):
        self.mesh, self.request_id, self.gated, self.calls = mesh, request_id, True, 0
        self._move = mesh.move

    def __call__(self, tid, status_id):
        if tid == self.request_id:
            self.calls += 1
            if self.gated:
                raise MeshError(422, GATE, body={"error": GATE})
        return self._move(tid, status_id)


class CloseDecisionHumanGate(Bench):
    setup_question = lifecycle.DecisionLifecycle.setup_question
    question = lifecycle.DecisionLifecycle.question

    def gated(self):
        r, mesh, source, attempt, wait = self.setup_question()
        request = wait['request']['id']
        mesh.tasks[request]['updated_at'] = '2026-10-20T10:00:00Z'
        move = GatedMove(mesh, request)
        mesh.move = move
        return r, mesh, source, attempt, wait, request, move

    def tick(self, r):
        r.effects._decision_checked.clear()       # every pass is due, as across ticks and restarts
        r.effects.reconcile_decisions()

    def test_gate_refusal_is_not_retried_on_the_next_pass(self):
        r, mesh, source, attempt, wait, request, move = self.gated()
        with self.assertLogs('mesh_runner', level='INFO') as logs:
            r.effects.on_end(attempt, wait, 'answered', close_request=True)
            self.assertEqual(move.calls, 1)
            for _ in range(3):
                self.tick(r)
        self.assertEqual(move.calls, 1)
        blocked = [line for line in logs.output if 'awaiting human sign-off' in line]
        self.assertEqual(len(blocked), 1, logs.output)
        self.assertEqual(r.effects._decision(source['id'])['state'], 'closing')
        self.assertEqual(mesh.category(mesh.tasks[request]), 'todo')
        self.assertTrue(r.store.mark(DECISION_OWNER, request, CLOSE_BLOCKED_MARK)[0])

    def test_changed_card_is_retried_and_closed(self):
        r, mesh, source, attempt, wait, request, move = self.gated()
        r.effects.on_end(attempt, wait, 'answered', close_request=True)
        self.tick(r)
        self.assertEqual(move.calls, 1)
        move.gated = False
        mesh.tasks[request]['updated_at'] = '2026-10-21T08:00:00Z'   # the gate was answered
        with self.assertLogs('mesh_runner', level='INFO') as logs:
            self.tick(r)
        self.assertEqual(move.calls, 2)
        self.assertEqual(mesh.category(mesh.tasks[request]), 'done')
        self.assertFalse(r.effects._decision(source['id']))
        self.assertEqual(len([line for line in logs.output if 'no longer blocks' in line]), 1, logs.output)
        self.assertFalse(r.store.mark(DECISION_OWNER, request, CLOSE_BLOCKED_MARK)[0])

    def test_still_gated_after_change_holds_again_without_a_new_log(self):
        r, mesh, source, attempt, wait, request, move = self.gated()
        r.effects.on_end(attempt, wait, 'answered', close_request=True)
        mesh.tasks[request]['updated_at'] = '2026-10-21T08:00:00Z'
        with self.assertLogs('mesh_runner', level='INFO') as logs:
            self.tick(r)
            self.tick(r)
            fx.log.info('marker')
        self.assertEqual(move.calls, 2)
        self.assertFalse([line for line in logs.output if 'awaiting human sign-off' in line], logs.output)

    def test_unchanged_card_is_retried_after_the_retry_interval(self):
        r, mesh, source, attempt, wait, request, move = self.gated()
        r.effects.on_end(attempt, wait, 'answered', close_request=True)
        later = now() + CLOSE_BLOCKED_RETRY + dt.timedelta(minutes=1)
        with patch.object(fx, 'now', return_value=later):
            self.tick(r)
        self.assertEqual(move.calls, 2)

    def test_other_errors_keep_retrying_every_pass(self):
        for err in (MeshError(422, 'invalid status transition'), MeshError(409, 'conflict'),
                    MeshError(503, 'unavailable'), MeshError(500, 'internal error')):
            with self.subTest(status=err.status, message=str(err)):
                r, mesh, source, attempt, wait = self.setup_question()
                request = wait['request']['id']
                with patch.object(mesh, 'move', side_effect=err) as move:
                    r.effects.on_end(attempt, wait, 'answered', close_request=True)
                    self.tick(r)
                    self.tick(r)
                    self.assertEqual(move.call_count, 3)
                self.assertFalse(r.store.mark(DECISION_OWNER, request, CLOSE_BLOCKED_MARK))
                self.tick(r)
                self.assertEqual(mesh.category(mesh.tasks[request]), 'done')
                self.tearDown()
                self.setUp()

    def test_restart_keeps_the_mark(self):
        r, mesh, source, attempt, wait, request, move = self.gated()
        r.effects.on_end(attempt, wait, 'answered', close_request=True)
        restarted = self.runner(mesh)
        restarted.effects.reconcile_decisions()
        restarted.effects.reconcile_decisions()
        self.assertEqual(move.calls, 1)
        self.assertEqual(restarted.effects._decision(source['id'])['state'], 'closing')
        move.gated = False
        mesh.tasks[request]['updated_at'] = '2026-10-21T08:00:00Z'
        self.tick(restarted)
        self.assertEqual(mesh.category(mesh.tasks[request]), 'done')

    def test_card_closed_elsewhere_clears_the_mark(self):
        r, mesh, source, attempt, wait, request, move = self.gated()
        r.effects.on_end(attempt, wait, 'answered', close_request=True)
        mesh.set_category(request, 'done')
        self.tick(r)
        self.assertEqual(move.calls, 1)
        self.assertFalse(r.effects._decision(source['id']))
        self.assertFalse(r.store.mark(DECISION_OWNER, request, CLOSE_BLOCKED_MARK)[0])

    def test_legacy_wait_without_store_holds_in_memory(self):
        r, mesh, source, attempt, wait, request, move = self.gated()
        r.effects.store = None
        pending = {'request': dict(wait['request'])}
        r.effects._close_decision(pending, 'answered')
        r.effects._close_decision(pending, 'answered')
        self.assertEqual(move.calls, 1)
        self.assertIn('request', pending)

    def test_refusal_detection(self):
        self.assertTrue(fx._human_gate_refusal(MeshError(422, GATE)))
        self.assertTrue(fx._human_gate_refusal(MeshError(422, 'unprocessable', body={'error': GATE})))
        self.assertFalse(fx._human_gate_refusal(MeshError(403, GATE)))
        self.assertFalse(fx._human_gate_refusal(MeshError(422, 'invalid status')))


if __name__ == '__main__':
    unittest.main()
