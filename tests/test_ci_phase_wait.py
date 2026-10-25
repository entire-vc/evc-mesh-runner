"""An until=change wait treats the unfinished pipeline states as one phase."""
import unittest

from mesh_runner.store import iso
from mesh_runner.wake import ci_change_event, ci_delivery_key
from fakes import FakeGitLab
import test_ci_change_wait as base
from test_ci_change_wait import marker, row, SHA, OTHER

IN_PROGRESS = ('created', 'waiting_for_resource', 'preparing', 'pending', 'running', 'scheduled')


def contract(status='pending', predicate='change', pid=14845, sha=SHA):
    return {'predicate': predicate, 'sha': sha, 'pipeline': pid, 'status': status}


def gitlab(status, pid=14845, sha=SHA):
    gl = FakeGitLab()
    gl.pipeline_rows['g/app#14845'] = {'sha': sha, 'id': pid, 'status': status}
    gl.mr_rows['g/app!56'] = row(pid=pid, status=status, sha=sha)
    return gl


def event(status, base='pending', predicate='change', pid=14845, sha=SHA, kind='pipeline', observation=None):
    target = 'g/app#14845' if kind == 'pipeline' else 'g/app!56'
    return ci_change_event(gitlab(status, pid, sha), kind, target,
                           contract(base, predicate), '', observation)


class InProgressPhase(unittest.TestCase):
    def test_hops_inside_the_in_progress_phase_do_not_wake(self):
        for kind in ('pipeline', 'mr'):
            for base in IN_PROGRESS:
                for now in IN_PROGRESS:
                    with self.subTest(kind=kind, base=base, now=now):
                        seen = {}
                        self.assertEqual(event(now, base, kind=kind, observation=seen), '')
                        self.assertEqual(seen, {})        # nothing recorded as delivered

    def test_pending_to_running_and_created_to_pending(self):
        self.assertEqual(event('running', 'pending'), '')
        self.assertEqual(event('pending', 'created'), '')

    def test_leaving_the_phase_wakes(self):
        for kind in ('pipeline', 'mr'):
            for base in ('running', 'pending'):
                for final in ('success', 'failed', 'canceled', 'cancelled', 'skipped', 'manual'):
                    with self.subTest(kind=kind, base=base, final=final):
                        self.assertIn(f'pipeline 14845 {final}', event(final, base, kind=kind))

    def test_entering_the_phase_from_a_final_state_wakes(self):
        for base in ('failed', 'manual', 'success', 'canceled'):
            with self.subTest(base=base):
                self.assertIn('running', event('running', base))

    def test_new_pipeline_id_on_same_sha_wakes_while_running(self):
        self.assertIn('pipeline 14846 running', event('running', 'running', pid=14846, kind='mr'))

    def test_new_sha_wakes_while_running(self):
        self.assertIn(OTHER[:8], event('running', 'running', sha=OTHER))

    def test_until_success_is_unchanged(self):
        for now in IN_PROGRESS + ('failed', 'canceled', 'manual'):
            with self.subTest(now=now):
                self.assertEqual(event(now, 'pending', predicate='success'), '')
        self.assertIn('success', event('success', 'pending', predicate='success'))


class InProgressHopThenFinal(base.TypedCI):
    def test_hop_is_silent_and_the_final_state_wakes_once(self):
        r, a, t, gl = self.setup_wait(marker('pipeline:g/app#14845', status='pending'))
        key = ci_delivery_key(a.wait['target'], 14845)
        gl.pipeline_rows['g/app#14845'] = {'sha': SHA, 'id': 14845, 'status': 'running'}
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, 'waiting')
        self.assertIsNone(r.store.mark(self.agent.name, t['id'], key))
        gl.pipeline_rows['g/app#14845'] = {'sha': SHA, 'id': 14845, 'status': 'success'}
        r._wake_one(a)
        woken = r.store.get(a.id)
        self.assertEqual(woken.state, 'ready')
        self.assertIn('success', woken.wake_text)
        self.assertEqual(r.store.mark(self.agent.name, t['id'], key)[0], f'{SHA}:14845:success')

    def test_mr_hop_between_in_progress_states_keeps_the_wait(self):
        r, a, t, gl = self.setup_wait(marker('mr:g/app!56', status='created'))
        for status in ('pending', 'running'):
            gl.mr_rows['g/app!56'] = row(status=status)
            r._wake_one(a)
            self.assertEqual(r.store.get(a.id).state, 'waiting')


if __name__ == '__main__':
    unittest.main()
