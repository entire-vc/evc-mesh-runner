"""A decision request whose close Mesh refuses behind a human gate or the delivery gate
is not retried every pass."""
import datetime as dt
import json
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
# Mesh's done-evidence refusal, as it answers a move to done while a linked merge request is open.
UNMERGED = ("PR «fix: example change» is not merged (verified live against GitLab just now) — merge it "
            "first, or if this read is wrong, re-link it with an explicit status (add_vcs_link ... status=merged)")
UNMERGED_BODY = {"code": "done_evidence_required", "error_code": "done_evidence_required", "message": UNMERGED}
NO_EVIDENCE = ("done requires evidence: add a PR/VCS link, artifact upload, or comment with proof "
               "before closing")


class GatedMove:
    """Mesh.move that refuses the request card while its human gate is up."""

    def __init__(self, mesh: FakeMesh, request_id: str):
        self.mesh, self.request_id, self.gated, self.calls = mesh, request_id, True, 0
        self.error = MeshError(422, GATE, body={"error": GATE})
        self._move = mesh.move

    def __call__(self, tid, status_id):
        if tid == self.request_id:
            self.calls += 1
            if self.gated:
                raise self.error
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

    def test_request_is_not_closed_under_the_addressees_running_turn(self):
        # The asker's lane closed the request while the addressee's turn held its checkout;
        # the addressee's heartbeat got 403 and its session was killed.
        r, mesh, source, attempt, wait = self.setup_question()
        request = wait['request']['id']
        mesh.tasks[request]['checked_out_by'] = 'agent-addressee'
        r.effects.on_end(attempt, wait, 'answered', close_request=True)
        for _ in range(3):
            self.tick(r)
        self.assertEqual(mesh.category(mesh.tasks[request]), 'todo')
        self.assertFalse(any('[runner] closed' in c['body'] for c in mesh.comment_log.get(request, [])))
        self.assertEqual(r.effects._decision(source['id'])['state'], 'closing')
        mesh.tasks[request]['checked_out_by'] = None           # the addressee's turn ended
        self.tick(r)
        self.assertEqual(mesh.category(mesh.tasks[request]), 'done')

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

    def delivery_gated(self, body=None):
        r, mesh, source, attempt, wait = self.setup_question()
        request = wait['request']['id']
        mesh.tasks[request]['updated_at'] = '2026-10-20T10:00:00Z'
        move = GatedMove(mesh, request)
        move.error = MeshError(422, UNMERGED, body=UNMERGED_BODY if body is None else body)
        mesh.move = move
        return r, mesh, source, attempt, wait, request, move

    def test_delivery_refusal_is_not_retried_until_the_card_changes(self):
        # Mesh refuses done while a linked merge request is open; the card stays as it is,
        # so the same write on every pass only repeats the refusal and a warning line.
        for body in (UNMERGED_BODY, None):
            with self.subTest(body=bool(body)):
                r, mesh, source, attempt, wait, request, move = self.delivery_gated(body or {})
                with self.assertLogs('mesh_runner', level='INFO') as logs:
                    r.effects.on_end(attempt, wait, 'answered', close_request=True)
                    for _ in range(3):
                        self.tick(r)
                    fx.log.info('marker')
                self.assertEqual(move.calls, 1)
                warnings = [line for line in logs.output if line.startswith('WARNING')]
                self.assertEqual(len(warnings), 1, logs.output)
                self.assertIn('linked merge request not merged', warnings[0])
                self.assertEqual(r.effects._decision(source['id'])['state'], 'closing')
                mark = json.loads(r.store.mark(DECISION_OWNER, request, CLOSE_BLOCKED_MARK)[0])
                self.assertEqual(mark['gate'], 'delivery')
                self.tearDown()
                self.setUp()

    def test_delivery_refusal_is_retried_when_the_card_changes(self):
        r, mesh, source, attempt, wait, request, move = self.delivery_gated()
        r.effects.on_end(attempt, wait, 'answered', close_request=True)
        self.tick(r)
        mesh.tasks[request]['updated_at'] = '2026-10-21T08:00:00Z'   # a comment, the MR still open
        with self.assertLogs('mesh_runner', level='INFO') as logs:
            self.tick(r)
            self.tick(r)
            fx.log.info('marker')
        self.assertEqual(move.calls, 2)
        self.assertFalse([line for line in logs.output if line.startswith('WARNING')], logs.output)
        move.gated = False
        mesh.tasks[request]['updated_at'] = '2026-10-22T08:00:00Z'   # the merge request was merged
        with self.assertLogs('mesh_runner', level='INFO') as logs:
            self.tick(r)
        self.assertEqual(move.calls, 3)
        self.assertEqual(mesh.category(mesh.tasks[request]), 'done')
        self.assertFalse(r.effects._decision(source['id']))
        self.assertFalse(r.store.mark(DECISION_OWNER, request, CLOSE_BLOCKED_MARK)[0])
        self.assertEqual(len([line for line in logs.output if 'delivery gate no longer blocks' in line]), 1,
                         logs.output)

    def test_delivery_refusal_is_retried_after_the_retry_interval(self):
        r, mesh, source, attempt, wait, request, move = self.delivery_gated()
        r.effects.on_end(attempt, wait, 'answered', close_request=True)
        later = now() + CLOSE_BLOCKED_RETRY + dt.timedelta(minutes=1)
        with patch.object(fx, 'now', return_value=later):
            self.tick(r)
        self.assertEqual(move.calls, 2)

    def test_gate_change_logs_again(self):
        r, mesh, source, attempt, wait, request, move = self.gated()
        r.effects.on_end(attempt, wait, 'answered', close_request=True)
        move.error = MeshError(422, UNMERGED, body=UNMERGED_BODY)
        mesh.tasks[request]['updated_at'] = '2026-10-21T08:00:00Z'   # sign-off given, the MR still open
        with self.assertLogs('mesh_runner', level='INFO') as logs:
            self.tick(r)
            self.tick(r)
        self.assertEqual(move.calls, 2)
        self.assertEqual(len([line for line in logs.output if 'linked merge request not merged' in line]), 1,
                         logs.output)

    def test_legacy_mark_without_gate_reads_as_human_gate(self):
        r, mesh, source, attempt, wait, request, move = self.gated()
        r.effects.on_end(attempt, wait, 'answered', close_request=True)
        mark = json.loads(r.store.mark(DECISION_OWNER, request, CLOSE_BLOCKED_MARK)[0])
        mark.pop('gate')
        r.store.mark(DECISION_OWNER, request, CLOSE_BLOCKED_MARK, json.dumps(mark))
        move.gated = False
        mesh.tasks[request]['updated_at'] = '2026-10-21T08:00:00Z'
        with self.assertLogs('mesh_runner', level='INFO') as logs:
            self.tick(r)
        self.assertEqual(len([line for line in logs.output if 'human gate no longer blocks' in line]), 1,
                         logs.output)

    def test_refusal_detection(self):
        self.assertTrue(fx._human_gate_refusal(MeshError(422, GATE)))
        self.assertTrue(fx._human_gate_refusal(MeshError(422, 'unprocessable', body={'error': GATE})))
        self.assertFalse(fx._human_gate_refusal(MeshError(403, GATE)))
        self.assertFalse(fx._human_gate_refusal(MeshError(422, 'invalid status')))
        self.assertEqual(fx._close_refusal_gate(MeshError(422, GATE)), 'human_gate')
        self.assertEqual(fx._close_refusal_gate(MeshError(422, UNMERGED, body=UNMERGED_BODY)), 'delivery')
        self.assertEqual(fx._close_refusal_gate(MeshError(422, UNMERGED)), 'delivery')
        self.assertEqual(fx._close_refusal_gate(MeshError(422, 'x', body={'error_code': 'done_evidence_required'})),
                         'delivery')
        self.assertEqual(fx._close_refusal_gate(MeshError(422, NO_EVIDENCE)), 'delivery')
        self.assertEqual(fx._close_refusal_gate(MeshError(409, UNMERGED, body=UNMERGED_BODY)), '')
        self.assertEqual(fx._close_refusal_gate(MeshError(422, 'invalid status transition')), '')
        self.assertEqual(fx._close_refusal_gate(MeshError(422, 'x', body={'code': 'dod_gate_blocked'})), '')


if __name__ == '__main__':
    unittest.main()
