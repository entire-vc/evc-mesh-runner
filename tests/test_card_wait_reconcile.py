"""Owner ticks repair stale card intent without waking or modifying Mesh.

FakeMesh is the external Mesh service boundary; Runner, parser and SQLite are real.
"""
import copy
import datetime as dt
import json
import subprocess
import sys

from fakes import AGENT_ID, FakeMesh, task
from mesh_runner.effects import runtime_state
from mesh_runner.mesh import MeshError
from mesh_runner.runner import AgentRunner
from mesh_runner.store import Store
from test_runner import Bench


class CardWaitReconcile(Bench):
    def test_tied_newest_prose_and_wait_hold_before_ordinary_observer(self):
        for prose_first in (True, False):
            for empty_edges in (True, False):
                with self.subTest(prose_first=prose_first, empty_edges=empty_edges):
                    r, a, t, _, edge, c = self.flow()
                    prose = dict(c, id='tied-prose', body='Owner progress update.')
                    r.mesh.comment_log[t['id']] = [prose, c] if prose_first else [c, prose]
                    edge.update(depends_on_task_id=task(2)['id'], condition_satisfied=True)
                    r.mesh.deps[t['id']] = [] if empty_edges else [edge]
                    self.unchanged(r, a)

    def test_tied_prose_only_keeps_ordinary_valid_wait_observation(self):
        r, a, t, _, edge, c = self.flow()
        c['body'] = 'Owner progress update.'
        r.mesh.comment_log[t['id']] = [c, dict(c, id='other-prose', body='Another update.')]
        edge.update(depends_on_task_id=task(2)['id'], condition_satisfied=True)
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, 'ready')

    def flow(self):
        t, old, target = task(1), task(2, cat='done'), task(3)
        mesh = FakeMesh([t, old, target])
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.flow_number = getattr(self, "flow_number", 0) + 1
        self.db_path = self.root / f"flow{self.flow_number}.sqlite"
        r = AgentRunner(self.cfg, self.agent, Store(self.db_path), mesh=mesh, adapter=self.adapter)
        self.addCleanup(r.store.db.close)
        r.refresh_me()
        a = r.store.create(self.agent.name, t['id'], 'claude')
        self.oldwait = {'kind': 'card', 'target': old['id'][:8],
                        'since': '2026-10-01T00:00:00Z', 'started': '2026-10-01T00:00:00Z',
                        'edge': 'removed-old-edge', 'fx': True, 'errors': 8}
        r.store.update(a, state='waiting', session_id='native-session', workspaces=['/leased/wt'],
                       wait=copy.deepcopy(self.oldwait))
        t['checkout_session_id'] = a.id
        edge = {'id': 'new-edge', 'task_id': t['id'], 'depends_on_task_id': target['id'],
                'dependency_type': 'blocks', 'condition': 'task_done', 'condition_satisfied': False}
        mesh.deps[t['id']] = [edge]
        c = mesh.add_comment(t['id'], 'Corroborated handoff.\n\n⏳ WAIT card:#' + target['id'][:8],
                             at=dt.datetime(2026, 10, 2, tzinfo=dt.timezone.utc))
        return r, a, t, target, edge, c

    def unchanged(self, r, a):
        r._wake_one(a)
        saved = r.store.get(a.id)
        self.assertEqual(saved.wait, self.oldwait)
        self.assertEqual(saved.state, 'waiting')
        self.assertFalse(r.store.mark(a.agent, a.task_id, 'card_wait_reconcile:' + a.id))

    def test_owner_tick_retargets_without_waking_or_claiming_edge(self):
        r, a, t, target, edge, c = self.flow()
        before = copy.deepcopy(edge)
        r.check_wakes()
        saved = r.store.get(a.id)
        self.assertEqual((saved.state, saved.wait['target']), ('waiting', target['id'][:8]))
        self.assertEqual((saved.session_id, saved.workspaces, saved.turns, saved.wake_text),
                         ('native-session', ['/leased/wt'], 0, ''))
        self.assertNotIn('edge', saved.wait)
        self.assertNotIn('errors', saved.wait)
        self.assertEqual(saved.wait['since'], c['created_at'])
        receipt = json.loads(r.store.mark(a.agent, a.task_id, 'card_wait_reconcile:' + a.id)[0])
        self.assertEqual(receipt, saved.wait['card_reconcile'])
        self.assertEqual((receipt['old_target'], receipt['comment_id'], receipt['dependency_id']),
                         ('00000002', c['id'], edge['id']))
        self.assertEqual(r.mesh.deps[t['id']], [before])
        self.assertEqual(len(r.mesh.comment_log[t['id']]), 1)
        self.assertFalse(self.calls.exists())
        desired = runtime_state(saved, 'test')
        self.assertEqual((desired['kind'], desired['ref']), ('waiting', 'card:#' + target['id'][:8]))

    def test_recorded_990_stale_installed_wait_shape(self):
        # Whitelisted fields from the read-only 2026-10-31 17:51Z receipt.
        # The installed registry retained 66accdaf while the own marker and
        # sole task_done dependency both already named 1d7adc21.
        r, a, t, _, edge, c = self.flow()
        source = '990dc14d-8986-4971-b3f7-3bfb5e09cd77'
        destination = '1d7adc21-5ff5-4b54-a79c-b60b522a98e7'
        t['id'] = source
        r.mesh.tasks[source] = t
        c.update(id='3a2f64da-3d57-4e15-bf07-7ce566fcfc97',
                 created_at='2026-10-31T17:16:43.993731Z', body='Owner correction.\n⏳ WAIT card:#1d7adc21')
        edge.update(id='b9bd496f-da7e-4b8c-b1fa-b0eca34ba4c9', task_id=source,
                    depends_on_task_id=destination)
        r.mesh.deps[source] = [edge]
        r.mesh.comment_log[source] = [c]
        wait = dict(self.oldwait, target='66accdaf', since='2026-10-31T06:27:36.625721Z')
        r.store.update(a, task_id=source, wait=wait)
        r._wake_one(a)
        saved = r.store.get(a.id)
        self.assertEqual((saved.state, saved.wait['target']), ('waiting', '1d7adc21'))
        receipt = saved.wait['card_reconcile']
        self.assertEqual((receipt['old_target'], receipt['comment_id'], receipt['dependency_id']),
                         ('66accdaf', c['id'], edge['id']))
        self.assertEqual(runtime_state(saved, 'riker')['ref'], 'card:#1d7adc21')
        self.assertEqual(r.mesh.deps[source], [edge])
        self.assertFalse(self.calls.exists())

    def test_settled_pid_is_retained_but_live_process_group_blocks_repair(self):
        r, a, _, target, _, _ = self.flow()
        r.store.update(a, pid=999999999)
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).wait['target'], target['id'][:8])
        r, a, _, _, _, _ = self.flow()
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
        try:
            r.store.update(a, pid=proc.pid)
            self.unchanged(r, a)
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_unknown_or_ambiguous_dependency_never_wakes_old_done_target(self):
        for change in ({'condition_satisfied': None}, {'condition_satisfied': True},
                       {'condition_satisfied': 0}, {'condition': 'accepted'},
                       {'dependency_type': 'relates_to'}, {'task_id': 'foreign'},
                       {'depends_on_task_id': task(4)['id']}, {'id': ''}):
            with self.subTest(change=change):
                r, a, _, _, edge, _ = self.flow()
                edge.update(change)
                self.unchanged(r, a)

    def test_multiple_edges_are_ambiguous_even_if_one_satisfied(self):
        r, a, t, _, edge, _ = self.flow()
        r.mesh.deps[t['id']].append(dict(edge, id='another', condition_satisfied=True))
        self.unchanged(r, a)

    def test_unknown_endpoint_fails_closed(self):
        class UnavailableMesh(FakeMesh):
            def dependencies(self, tid):
                raise MeshError(503, 'unavailable')
        r, a, _, _, _, _ = self.flow()
        r.mesh.__class__ = UnavailableMesh  # true external service failure
        with self.assertRaises(MeshError):
            r._wake_one(a)
        self.assertEqual(r.store.get(a.id).wait, self.oldwait)

    def test_wrong_checkout_or_foreign_writer_cannot_retarget(self):
        for changes in ({'checkout_session_id': None}, {'checkout_session_id': 'other'},
                        {'checked_out_by': 'other-agent'}):
            with self.subTest(changes=changes):
                r, a, t, _, _, _ = self.flow()
                t.update(changes)
                self.unchanged(r, a)

    def test_ambiguous_marker_is_not_authority(self):
        for body in ('⏳ WAIT card:#00000003\n⏳ WAIT card:#00000004',
                     '```\n⏳ WAIT card:#00000003\n```',
                     '⏳ WAIT card:#00000003\nExplanatory footer',
                     '⏳ WAIT card:#00000003-0000-4000-8000-000000000003'):
            with self.subTest(body=body):
                r, a, _, _, _, c = self.flow()
                c['body'] = body
                self.unchanged(r, a)

    def test_old_and_foreign_comments_do_not_retarget(self):
        for foreign in (False, True):
            r, a, _, _, _, c = self.flow()
            if foreign:
                c['author_id'] = 'other-agent'
            else:
                c['created_at'] = self.oldwait['since']
            # Old/foreign text cannot authorize retargeting. The mismatched
            # pending dependency still forbids observing the old DONE target.
            self.unchanged(r, a)
            self.assertEqual(r.store.get(a.id).wait, self.oldwait)

    def test_same_instant_markers_and_newest_own_nonmarker_are_not_reused(self):
        r, a, t, _, _, c = self.flow()
        r.mesh.add_comment(t['id'], '⏳ WAIT card:#00000004',
                           at=dt.datetime(2026, 10, 2, tzinfo=dt.timezone.utc))
        self.unchanged(r, a)
        r, a, t, _, _, c = self.flow()
        r.mesh.add_comment(t['id'], 'A newer owner update without a declaration.',
                           at=dt.datetime(2026, 10, 3, tzinfo=dt.timezone.utc))
        self.unchanged(r, a)

    def test_missing_old_wait_timestamp_and_scheduled_effect_are_not_repaired(self):
        for change in ({'since': ''}, {'scheduled': '2026-10-04T00:00:00Z'}):
            r, a, _, _, _, _ = self.flow()
            self.oldwait.update(change)
            r.store.update(a, wait=copy.deepcopy(self.oldwait))
            self.unchanged(r, a)

    def test_prose_after_wait_never_retargets_from_history_or_wakes_old_done(self):
        for prose in ('[runner] completed in 2m', 'Owner status update without a declaration.'):
            with self.subTest(prose=prose):
                r, a, t, _, _, _ = self.flow()
                r.mesh.add_comment(t['id'], prose,
                                   at=dt.datetime(2026, 10, 3, tzinfo=dt.timezone.utc))
                self.unchanged(r, a)  # full _wake_one; old target is DONE
                self.assertEqual(r.store.get(a.id).wait['target'], '00000002')
                self.assertFalse(self.calls.exists())

    def test_real_top_level_session_has_no_nested_fallback(self):
        r, a, t, target, _, _ = self.flow()
        self.assertNotIn('metadata', t)
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).wait['target'], target['id'][:8])
        r, a, t, _, _, _ = self.flow()
        t.pop('checkout_session_id')  # released/absent session provenance
        t['metadata'] = {'checkout_session_id': a.id}  # unsupported field cannot authorize
        self.unchanged(r, a)

    def test_prose_with_unknown_edges_holds_but_matching_edge_uses_normal_observer(self):
        r, a, t, _, edge, _ = self.flow()
        r.mesh.add_comment(t['id'], '[runner] completed in 2m',
                           at=dt.datetime(2026, 10, 3, tzinfo=dt.timezone.utc))
        edge.pop('condition_satisfied')
        self.unchanged(r, a)
        edge.update(condition_satisfied=True, depends_on_task_id=task(2)['id'])
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, 'ready')

    def test_future_or_missing_timestamp_fails_closed(self):
        for created in ('', 'not-a-date', '2999-01-01T00:00:00Z'):
            r, a, _, _, _, c = self.flow()
            c['created_at'] = created
            self.unchanged(r, a)

    def test_state_and_wait_cas_preserve_concurrent_owner_change(self):
        for state, wait in (('ready', {'kind': 'card', 'target': 'concurrent'}),
                            ('waiting', {'kind': 'card', 'target': 'deadbeef', 'since': '2026-10-03T00:00:00Z'})):
            with self.subTest(state=state):
                r, stale, _, _, _, _ = self.flow()
                # A second genuine SQLite connection commits between snapshot and CAS.
                other = Store(self.db_path)
                live = other.get(stale.id)
                other.update(live, state=state, wait=wait)
                other.db.close()
                r._wake_one(stale)
                saved = r.store.get(stale.id)
                self.assertEqual((saved.state, saved.wait), (state, wait))
                self.assertFalse(r.store.mark(stale.agent, stale.task_id, 'card_wait_reconcile:' + stale.id))

    def test_identity_cas_rejects_changed_agent_even_with_same_wait(self):
        r, stale, _, _, _, _ = self.flow()
        other = Store(self.db_path)
        other.db.execute('UPDATE attempts SET agent=? WHERE id=?', ('other', stale.id))
        other.db.close()
        r._wake_one(stale)
        saved = r.store.get(stale.id)
        self.assertEqual((saved.agent, saved.state, saved.wait), ('other', 'waiting', self.oldwait))
        self.assertFalse(r.store.mark(stale.agent, stale.task_id, 'card_wait_reconcile:' + stale.id))

    def test_removed_old_edge_never_becomes_owned_new_edge_on_later_tick(self):
        r, a, t, target, edge, _ = self.flow()
        r._wake_one(a)
        # Once target closes the ordinary observer wakes, but on_end must leave
        # the independently supplied dependency alone.
        target['status_id'] = 's-done'
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, 'ready')
        self.assertEqual(r.mesh.deps[t['id']], [edge])
