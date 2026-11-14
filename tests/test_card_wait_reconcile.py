"""A card WAIT whose formal dependency disagrees with the target it observes is held, never woken on the old target.

Comment text is not owner intent: a newer wait replaces this one only through Mesh (a typed wait_for read
when the turn settles), so no comment, marker or prose, retargets or releases it.
FakeMesh is the external Mesh service boundary; Runner and SQLite are real.
"""
import copy
import datetime as dt

from fakes import FakeMesh, task
from mesh_runner.mesh import MeshError
from mesh_runner.runner import AgentRunner
from mesh_runner.store import Store
from test_runner import Bench


class CardWaitReconcile(Bench):
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
        c = mesh.add_comment(t['id'], 'Corroborated handoff.',
                             at=dt.datetime(2026, 10, 2, tzinfo=dt.timezone.utc))
        return r, a, t, target, edge, c

    def unchanged(self, r, a):
        r._wake_one(a)
        saved = r.store.get(a.id)
        self.assertEqual(saved.wait, self.oldwait)
        self.assertEqual(saved.state, 'waiting')
        self.assertFalse(r.store.mark(a.agent, a.task_id, 'card_wait_reconcile:' + a.id))

    def test_unknown_or_ambiguous_dependency_never_wakes_old_done_target(self):
        for change in ({'condition_satisfied': None}, {'condition_satisfied': 0}, {'condition': 'accepted'},
                       {'task_id': 'foreign'}, {'depends_on_task_id': task(4)['id']}, {'id': ''}):
            with self.subTest(change=change):
                r, a, _, _, edge, _ = self.flow()
                edge.update(change)
                self.unchanged(r, a)

    def test_known_released_or_informational_edges_let_the_ordinary_observer_wake_the_old_done_target(self):
        # the same flow, but Mesh's verdict is explicit: the satisfied block / the relates_to link
        # does not constrain observation, so the done target wakes the attempt
        for change in ({'condition_satisfied': True}, {'dependency_type': 'relates_to'}):
            with self.subTest(change=change):
                r, a, _, _, edge, _ = self.flow()
                edge.update(change)
                r._wake_one(a)
                self.assertEqual(r.store.get(a.id).state, 'ready')

    def test_a_block_on_the_waited_target_corroborates_the_wait(self):
        # positive control for the hold: the unsatisfied block names the target the wait observes
        r, a, t, _, edge, _ = self.flow()
        edge.update(depends_on_task_id=task(2)['id'])
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, 'ready')

    def test_comment_text_never_retargets_or_releases_the_wait(self):
        # a marker line, a fenced or doubled one, or prose in the owner's own comments is not intent:
        # the wait stays on its old target, held by the disagreeing dependency, and nothing is written
        for body in ('\u23f3 WAIT card:#00000003', '\u21bb NEXT', '\u23f3 WAIT card:#00000003\n\u23f3 WAIT card:#00000004',
                     '```\n\u23f3 WAIT card:#00000003\n```', 'Owner status update without a declaration.'):
            with self.subTest(body=body):
                r, a, t, _, edge, _ = self.flow()
                before = copy.deepcopy(edge)
                r.mesh.add_comment(t['id'], body, at=dt.datetime(2026, 10, 3, tzinfo=dt.timezone.utc))
                self.unchanged(r, a)
                self.assertEqual(r.mesh.deps[t['id']], [before])
                self.assertFalse(self.calls.exists())

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

    def test_prose_with_unknown_edges_holds_but_matching_edge_uses_normal_observer(self):
        r, a, t, _, edge, _ = self.flow()
        r.mesh.add_comment(t['id'], '[runner] completed in 2m',
                           at=dt.datetime(2026, 10, 3, tzinfo=dt.timezone.utc))
        edge.pop('condition_satisfied')
        self.unchanged(r, a)
        edge.update(condition_satisfied=True, depends_on_task_id=task(2)['id'])
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, 'ready')
