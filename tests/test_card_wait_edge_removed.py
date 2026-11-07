"""Deleting the blocks edge of a card WAIT wakes the same attempt exactly once.

FakeMesh is the external Mesh boundary; Runner, Store and the observer are real.
"""
import copy

from fakes import FakeMesh, task
from mesh_runner.runner import AgentRunner
from mesh_runner.store import Store
from test_runner import Bench


def edge_row(task_id, target_id, edge_id='e1', satisfied=False):
    return {'id': edge_id, 'task_id': task_id, 'depends_on_task_id': target_id,
            'dependency_type': 'blocks', 'condition': 'task_done', 'condition_satisfied': satisfied}


class CardEdgeRemoved(Bench):
    def flow(self, deps):
        t, target = task(1), task(3)          # target stays open
        mesh = FakeMesh([t, target])
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        r = AgentRunner(self.cfg, self.agent, Store(self.root / 'edge.sqlite'), mesh=mesh, adapter=self.adapter)
        self.addCleanup(r.store.db.close)
        r.refresh_me()
        a = r.store.create(self.agent.name, t['id'], 'claude')
        wait = {'kind': 'card', 'target': target['id'][:8], 'since': '2026-10-01T00:00:00Z',
                'started': '2026-10-01T00:00:00Z', 'edge': 'e1', 'fx': True}
        r.store.update(a, state='waiting', session_id='native-session', wait=copy.deepcopy(wait))
        t['checkout_session_id'] = a.id
        mesh.deps[t['id']] = deps(t, target)
        return r, a, t, target

    def test_deleted_edge_wakes_same_attempt_once(self):
        r, a, t, target = self.flow(lambda t, g: [])
        r._wake_one(a)
        s = r.store.get(a.id)
        self.assertEqual((s.state, s.id, s.session_id), ('ready', a.id, 'native-session'))
        self.assertNotIn('edge', s.wait)
        self.assertIn('removed', s.wake_text)
        self.assertIn('re-read', s.wake_text)           # a link restored after the commit is re-checked by the owner
        text = s.wake_text
        r.store.update(s, state='waiting')            # a replayed tick on the same attempt
        r._wake_one(r.store.get(a.id))
        self.assertEqual(r.store.get(a.id).state, 'waiting')
        self.assertEqual(r.store.get(a.id).wake_text, text)

    def test_edge_still_present_holds(self):
        r, a, t, g = self.flow(lambda t, g: [edge_row(t['id'], g['id'])])
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, 'waiting')

    def test_unrelated_edge_deleted_is_not_ours(self):
        r, a, t, g = self.flow(lambda t, g: [edge_row(t['id'], g['id'])])
        r.mesh.deps[t['id']] = [edge_row(t['id'], g['id'])]
        r.mesh.deps[t['id']][0]['id'] = 'e1'
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, 'waiting')

    def test_remaining_real_blocker_holds(self):
        r, a, t, g = self.flow(lambda t, g: [edge_row(t['id'], task(4)['id'], 'e2')])
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, 'waiting')
        self.assertEqual(r.store.get(a.id).wait['edge'], 'e1')

    def test_paginated_or_malformed_edges_fail_closed(self):
        from mesh_runner import card_wait_reconcile as c
        w, tid = {'edge': 'e1'}, task(1)['id']
        good = edge_row(tid, task(4)['id'], 'e2', True)
        self.assertEqual(c.removed_wait_edge({'outgoing': [good]}, w, tid), 'e1')
        self.assertIsNone(c.removed_wait_edge({'outgoing': [], 'has_more': True}, w, tid))
        self.assertIsNone(c.removed_wait_edge({'outgoing': [{'x': 1}]}, w, tid))
        self.assertIsNone(c.removed_wait_edge({}, w, tid))
        self.assertIsNone(c.removed_wait_edge({'outgoing': []}, {}, tid))
        self.assertEqual(c.removed_wait_edge({'outgoing': []}, w, tid), 'e1')
        for bad in ({'id': 'x', 'dependency_type': 'unknown'},
                    dict(good, dependency_type='unknown'), dict(good, condition_satisfied='yes'),
                    dict(good, task_id=task(9)['id']), dict(good, depends_on_task_id='nope'),
                    dict(good, condition='bogus'), dict(good, id='')):
            with self.subTest(bad=bad):
                self.assertIsNone(c.removed_wait_edge({'outgoing': [bad]}, w, tid))
        self.assertIsNone(c.removed_wait_edge({'outgoing': [good, dict(good)]}, w, tid))

    def test_satisfied_remaining_edge_does_not_hold(self):
        r, a, t, g = self.flow(lambda t, g: [edge_row(t['id'], task(4)['id'], 'e2', True)])
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, 'ready')

    def test_edge_restored_between_read_and_apply_holds(self):
        from mesh_runner import card_wait_reconcile as c
        r, a, t, g = self.flow(lambda t, g: [])
        real_deps, real_fn, state = r.mesh.dependencies, c.removed_wait_edge, {'restored': False}

        def deps(tid):
            if state['restored']:
                return {'outgoing': [edge_row(tid, g['id'])], 'incoming': []}
            return real_deps(tid)

        def removed(rows, wait, task_id):
            out = real_fn(rows, wait, task_id)
            if out and not state['restored']:
                state['restored'] = True             # Mesh restores the link after this read
            return out
        r.mesh.dependencies = deps
        c.removed_wait_edge = removed
        self.addCleanup(setattr, c, 'removed_wait_edge', real_fn)
        r._wake_one(a)
        self.assertTrue(state['restored'])
        s = r.store.get(a.id)
        self.assertEqual((s.state, s.wait.get('edge')), ('waiting', 'e1'))
        # no dedup mark left behind: a later real deletion still wakes exactly once
        state['restored'] = False
        c.removed_wait_edge = real_fn
        r.mesh.dependencies = real_deps
        r._wake_one(r.store.get(a.id))
        self.assertEqual(r.store.get(a.id).state, 'ready')

    def test_dependency_read_failure_at_apply_holds(self):
        from mesh_runner.mesh import MeshError
        r, a, t, g = self.flow(lambda t, g: [])
        real, calls = r.mesh.dependencies, []

        def flaky(tid):
            calls.append(tid)
            if len(calls) > 1:
                raise MeshError(502, 'bad gateway')
            return real(tid)
        r.mesh.dependencies = flaky
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, 'waiting')
