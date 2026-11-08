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


def typed_row(task_id, target_id, condition, satisfied, edge_id='e1'):
    row = edge_row(task_id, target_id, edge_id, satisfied)
    row['condition'] = condition
    return row


class CardEdgeNarrowed(Bench):
    flow = CardEdgeRemoved.flow
    """An owner narrowed the WAIT edge task_done -> deployed (AC2/AC5 audit 07.11)."""

    def test_satisfied_typed_condition_wakes_same_attempt_once_target_open(self):
        r, a, t, g = self.flow(lambda t, g: [typed_row(t['id'], g['id'], 'deployed', True)])
        r._wake_one(a)
        s = r.store.get(a.id)
        self.assertEqual((s.state, s.id, s.session_id), ('ready', a.id, 'native-session'))
        self.assertIn('deployed', s.wake_text)
        self.assertIn('not released', s.wake_text)
        r.store.update(s, state='waiting')            # replay of the same verdict
        r._wake_one(r.store.get(a.id))
        self.assertEqual(r.store.get(a.id).state, 'waiting')

    def test_satisfied_condition_on_a_closed_target_is_still_one_wake(self):
        r, a, t, g = self.flow(lambda t, g: [typed_row(t['id'], g['id'], 'deployed', True)])
        g['status'] = {'slug': 'done'}
        g['status_category'] = 'done'
        r._wake_one(a)
        s = r.store.get(a.id)
        self.assertEqual((s.state, s.id), ('ready', a.id))
        self.assertIn('typed condition deployed', s.wake_text)   # the typed verdict wins; one wake, one attempt

    def test_malformed_rows_create_nothing_and_keep_the_record(self):
        for bad in ({'x': 1}, {'id': 'e9'}, 'junk',
                    typed_row('other', 'y', 'deployed', True)):
            with self.subTest(bad=bad):
                r, a, t, g = self.flow(lambda t, g: [])
                real = r.mesh.dependencies
                r.mesh.dependencies = lambda tid, real=real, bad=bad: dict(real(tid), outgoing=[bad])
                wait2 = {'kind': 'card', 'target': g['id'][:8]}
                r.effects._add_edge(a, wait2)              # unreadable set is not "no edge"
                self.assertNotIn('edge', wait2)
                self.assertEqual(r.mesh.deps.get(t['id'], []), [])
                wait = {'kind': 'card', 'target': g['id'][:8], 'edge': 'e1', 'fx': True}
                r.effects.on_end(a, wait, 'woken')         # cleanup cannot tell: record kept for retry
                self.assertEqual(wait.get('edge'), 'e1')
                r.store.db.execute('DELETE FROM attempts WHERE id=?', (a.id,))

    def test_redeclared_wait_keeps_observing_the_narrowed_edge(self):
        # own e1 narrowed to deployed/false -> cleanup keeps it -> same target re-declared
        r, a, t, g = self.flow(lambda t, g: [typed_row(t['id'], g['id'], 'deployed', False)])
        old = dict(r.store.get(a.id).wait)
        r.effects.on_end(a, old, 'woken')
        self.assertNotIn('edge', old)
        wait = {'kind': 'card', 'target': g['id'][:8], 'since': '2026-10-01T00:00:00Z',
                'started': '2026-10-01T00:00:00Z', 'fx': True}
        r.effects._add_edge(a, wait)
        self.assertNotIn('edge', wait)                 # owns nothing
        self.assertEqual(wait.get('observe_edge'), 'e1')
        r.store.update(r.store.get(a.id), state='waiting', wait=wait)
        r._wake_one(r.store.get(a.id))
        self.assertEqual(r.store.get(a.id).state, 'waiting')       # unsatisfied: holds
        r.mesh.deps[t['id']][0]['condition_satisfied'] = True      # server verdict, target still open
        r._wake_one(r.store.get(a.id))
        s = r.store.get(a.id)
        self.assertEqual((s.state, s.id), ('ready', a.id))
        self.assertEqual([e['condition'] for e in r.mesh.deps[t['id']]], ['deployed'])   # never touched
        end = dict(s.wait)
        r.effects.on_end(s, end, 'woken')
        self.assertNotIn('observe_edge', end)
        self.assertEqual(len(r.mesh.deps[t['id']]), 1)             # observed edge is not deleted

    def test_ambiguous_existing_edges_are_not_observed(self):
        r, a, t, g = self.flow(lambda t, g: [typed_row(t['id'], g['id'], 'deployed', True, 'e1'),
                                             typed_row(t['id'], g['id'], 'accepted', True, 'e2')])
        wait = {'kind': 'card', 'target': g['id'][:8]}
        r.effects._add_edge(a, wait)
        self.assertNotIn('observe_edge', wait)

    def _held(self, row):
        r, a, t, g = self.flow(lambda t, g: [row(t, g)])
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, 'waiting')

    def test_unsatisfied_does_not_wake(self):
        self._held(lambda t, g: typed_row(t['id'], g['id'], 'deployed', False))

    def test_non_boolean_verdict_does_not_wake(self):
        self._held(lambda t, g: typed_row(t['id'], g['id'], 'deployed', 'true'))
       
    def test_unknown_verdict_does_not_wake(self):
        self._held(lambda t, g: typed_row(t['id'], g['id'], 'deployed', None))

    def test_edge_to_other_target_does_not_wake(self):
        self._held(lambda t, g: typed_row(t['id'], task(4)['id'], 'deployed', True))

    def test_foreign_task_edge_does_not_wake(self):
        self._held(lambda t, g: typed_row(task(5)['id'], g['id'], 'deployed', True))

    def test_revoked_generation_does_not_wake(self):
        r, a, t, g = self.flow(lambda t, g: [typed_row(t['id'], g['id'], 'accepted', True)])
        real, calls = r.mesh.dependencies, []

        def revoked(tid):
            calls.append(tid)
            rows = real(tid)
            if len(calls) > 2:                       # verdict withdrawn before the commit
                for e in rows['outgoing']:
                    e['condition_satisfied'] = False
            return rows
        r.mesh.dependencies = revoked
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, 'waiting')

    def test_cleanup_leaves_narrowed_edge_and_redeclare_does_not_recreate(self):
        r, a, t, g = self.flow(lambda t, g: [typed_row(t['id'], g['id'], 'deployed', False)])
        wait = dict(r.store.get(a.id).wait)
        r.effects.on_end(a, wait, 'done')
        self.assertNotIn('edge', wait)
        self.assertEqual([e['condition'] for e in r.mesh.deps[t['id']]], ['deployed'])
        wait2 = {'kind': 'card', 'target': g['id'][:8]}
        before = list(r.mesh.deps[t['id']])
        r.effects._add_edge(a, wait2)                 # re-declared WAIT: no second broad edge
        self.assertNotIn('edge', wait2)
        self.assertEqual(r.mesh.deps[t['id']], before)

    def test_user_removed_edge_is_not_restored_by_redeclared_wait(self):
        r, a, t, g = self.flow(lambda t, g: [])
        r._wake_one(a)                                # removal observed, mark written
        self.assertEqual(r.store.get(a.id).state, 'ready')
        wait2 = {'kind': 'card', 'target': g['id'][:8]}
        r.effects._add_edge(a, wait2)
        self.assertNotIn('edge', wait2)
        self.assertEqual(r.mesh.deps.get(t['id'], []), [])
