"""Held ready attempts return only their own verified Git leases."""
import datetime as dt
import json
import os
from pathlib import Path
from contextlib import nullcontext
from unittest.mock import patch

from mesh_runner.store import iso, now
from mesh_runner.workspace import WorkspaceError
from mesh_runner.workspace_scope import RECEIPT_KEY
import test_workspace_requests as requests_fixture
from test_runner import Bench
from fakes import write_replies


class HeldReady(Bench):
    parked = requests_fixture.WorkspaceRequests.parked

    def ready(self, human=False):
        r, (a,) = self.parked()
        self.agent.workspace_wait_release_minutes = 1
        r.store.update(a, state='ready')
        if human:
            r.mesh.tasks[a.task_id]['human_gate'] = True
            r.store.mark(a.agent, a.task_id, RECEIPT_KEY,
                         json.dumps({'attempt': a.id, 'scope': 'workspace'}))
        return r, a

    def denied(self, r, a):
        with patch.object(r, '_gate', return_value=('held by the start gate', {})):
            r._resume_one(r.store.get(a.id))

    def age(self, r, a):
        r.store.mark(a.agent, a.task_id, 'workspace_ready_hold',
                     json.dumps({'attempt': a.id, 'since': iso(now()-dt.timedelta(minutes=2))}))

    def test_start_gate_denial_releases_and_restores(self):
        self.roundtrip(False)

    def test_human_gate_denial_releases_and_restores(self):
        self.roundtrip(True)

    def roundtrip(self, human):
        r, a = self.ready(human)
        source = Path(a.workspaces[0])
        (source/'README').write_text('valuable progress')
        old_wait = dict(a.wait)
        self.denied(r, a)
        self.assertTrue(r.pool.leased_to(a.id))
        self.age(r, a)
        restarted = self.runner(r.mesh)
        restarted.refresh_me()
        self.denied(restarted, a)
        self.assertEqual(restarted.pool.leased_to(a.id), [])
        held = restarted.store.get(a.id)
        self.assertEqual((held.state, held.wait), ('ready', old_wait))
        self.assertFalse(restarted.procs)
        self.assertEqual(bool(r.mesh.tasks[a.task_id].get('human_gate')), human)
        r.mesh.tasks[a.task_id]['human_gate'] = False  # authorized owner fixture action
        write_replies(self.replies, {a.task8: '⏳ WAIT 2h: external work'})
        restarted._resume_one(held)
        resumed = restarted.store.get(a.id)
        self.assertEqual(resumed.state, 'running')
        self.assertEqual(restarted.store.mark(a.agent, a.task_id, 'workspace_ready_hold')[0], '')
        self.assertEqual(Path(resumed.workspaces[0], 'README').read_text(), 'valuable progress')
        self.settle_all(restarted)


    def test_standing_review_hold_releases_existing_leases(self):
        r, a = self.ready()
        r.store.update(a, mode='review')
        r.mesh.set_category(a.task_id, 'review')
        self.age(r, a)
        old_wait = dict(a.wait)
        with patch.object(r, '_unstartable_hold', return_value='qa_artifact_unresolved'):
            r._resume_one(r.store.get(a.id))
        self.assertEqual(r.pool.leased_to(a.id), [])
        held = r.store.get(a.id)
        self.assertEqual((held.state, held.wait), ('ready', old_wait))
        self.assertFalse(r.procs)

    def test_fresh_human_refusal_preserves_elapsed_hold_clock(self):
        r, a = self.ready()
        r.store.mark(a.agent, a.task_id, RECEIPT_KEY,
                     json.dumps({'attempt': a.id, 'scope': 'workspace'}))
        self.age(r, a)
        old_clock = r.store.mark(a.agent, a.task_id, 'workspace_ready_hold')[0]
        reads = 0

        def fresh_task(task_id):
            nonlocal reads
            reads += 1
            # The initial read passes; the post-checkout fresh read finds a gate.
            r.mesh.tasks[task_id]['human_gate'] = reads > 1
            return dict(r.mesh.tasks[task_id])

        with patch.object(r, '_task', side_effect=fresh_task):
            r._resume_one(r.store.get(a.id))
        self.assertGreaterEqual(reads, 3)  # includes the release ownership fence
        self.assertEqual(r.pool.leased_to(a.id), [])
        self.assertEqual(r.store.mark(a.agent, a.task_id, 'workspace_ready_hold')[0], old_clock)
        self.assertEqual(r.store.get(a.id).state, 'ready')
        self.assertTrue(r.mesh.tasks[a.task_id]['human_gate'])
        self.assertFalse(r.procs)

    def test_held_release_fails_closed_for_writer_budget_inventory_and_owner(self):
        for refusal in ('writer', 'disk', 'inventory', 'owner', 'disabled', 'other-attempt'):
            with self.subTest(refusal=refusal):
                r, a = self.ready()
                self.age(r, a)
                if refusal == 'other-attempt':
                    r.store.mark(a.agent, a.task_id, 'workspace_ready_hold',
                                 json.dumps({'attempt': 'different', 'since': '2000-01-01T00:00:00Z'}))
                source = Path(a.workspaces[0])
                (source/'README').write_text('preserved')
                if refusal == 'owner':
                    r.mesh.tasks[a.task_id]['assignee_id'] = 'other'
                    # Direct helper must fresh-check ownership, independently of resume's read.
                if refusal == 'disabled':
                    self.agent.workspace_wait_release_minutes = 0
                if refusal == 'inventory':
                    os.mkfifo(source/'unsupported.fifo')
                target = ('mesh_runner.runner.group_alive' if refusal == 'writer' else
                          'mesh_runner.checkpoint.shutil.disk_usage')
                value = True if refusal == 'writer' else type('D', (), {'free': 0})()
                with patch(target, return_value=value) if refusal in ('writer', 'disk') else nullcontext():
                    try:
                        r._suspend_held_ready(r.store.get(a.id))
                    except WorkspaceError:
                        pass
                self.assertTrue(r.pool.leased_to(a.id))
                self.assertEqual((source/'README').read_text(), 'preserved')
                self.assertEqual(r.store.get(a.id).state, 'ready')
                self.tearDown(); self.setUp()

    def test_release_never_touches_another_attempt_and_repeated_denial_is_idempotent(self):
        r, (a, other) = self.parked(2)
        self.agent.workspace_wait_release_minutes = 1
        r.store.update(a, state='ready')
        self.age(r, a)
        other_paths = r.pool.leased_to(other.id)
        self.denied(r, a)
        journal = r.pool._checkpoint_rows(a.id)
        self.assertTrue(journal)
        self.denied(r, a)
        self.assertEqual(r.pool._checkpoint_rows(a.id), journal)
        self.assertEqual(r.pool.leased_to(other.id), other_paths)
        self.assertEqual(r.store.get(other.id).state, 'waiting')
