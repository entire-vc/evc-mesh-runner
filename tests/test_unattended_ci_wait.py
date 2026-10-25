"""A typed CI/MR wait that nobody will satisfy is sent back at declaration, not parked."""
import unittest

from mesh_runner.wake import ci_delivery_key
from fakes import FakeGitLab, FakeMesh, task, write_replies
from test_runner import Bench

SHA = 'a' * 40
OTHER = 'b' * 40
MR = 'g/app!56'
PIPELINE = 'g/app#14845'
MR_URL = 'https://git.example/g/app/-/merge_requests/56'


def mr_row(status='success', labels=(), sha=SHA, pid=14845, state='opened'):
    out = {'sha': sha, 'state': state, 'head_pipeline': {'sha': sha, 'id': pid, 'status': status}}
    if labels is not None:
        out['labels'] = list(labels)
    return out


def pipeline_line(status, baseline='running'):
    return f'⏳ WAIT pipeline:{PIPELINE} until=success after={SHA}:14845:{baseline}'


def merged_line():
    return f'⏳ WAIT mr:{MR} until=merged after={SHA}:opened'


def change_line(status, pid=14845):
    return f'⏳ WAIT mr:{MR} until=change after={SHA}:{pid}:{status}'


class UnattendedWait(Bench):
    def flow(self, pipeline_status='failed', mr=None, linked=True):
        """Real settle + SQLite store: one turn declares the wait under test."""
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        if linked:
            mesh.links = {t['id']: [{'url': MR_URL, 'link_type': 'pr', 'status': 'open'}]}
        gl = FakeGitLab()
        gl.pipeline_rows[PIPELINE] = {'sha': SHA, 'id': 14845, 'status': pipeline_status}
        gl.mr_rows[MR] = mr if mr is not None else mr_row()
        r = self.runner(mesh, gl)
        self.addCleanup(r.store.db.close)
        r.refresh_me()
        return r, t, gl

    def turn(self, r, t, line, first=True):
        write_replies(self.replies, {t['id'][:8]: line, t['id'][:8] + ':resume': line})
        self.assertEqual(r.start_new(1) if first else r.resume_ready(1), 0)
        return self.finish_turn(r)

    def assert_bounced(self, a, r, t, *needles):
        self.assertEqual(a.state, 'ready')
        for needle in needles:
            self.assertIn(needle, a.wake_text)
        self.assertEqual(r.store.outcomes_today(self.agent.name, t['id']), {'wait': 1})
        self.assertEqual(r.store.idle_waits_today(self.agent.name, t['id']), 1)

    # -- the three unattended cases ------------------------------------------------------

    def test_success_wait_on_manual_pipeline_is_sent_back(self):
        r, t, gl = self.flow('manual')
        a = self.turn(r, t, pipeline_line('manual', baseline='manual'))
        self.assert_bounced(a, r, t, f'pipeline {PIPELINE} is blocked on manual jobs',
                            'will not reach success by itself', 'cannot complete on its own',
                            'arrange the required review or authorized manual action', 'until=merged')
        self.assertNotIn('until=success', a.wake_text)

    def test_success_wait_on_failed_pipeline_is_sent_back(self):
        for status in ('failed',):
            with self.subTest(status=status):
                self.tearDown(), self.setUp()
                r, t, gl = self.flow(status)
                a = self.turn(r, t, pipeline_line(status))
                self.assert_bounced(a, r, t, f'already finished: {status}',
                                    'until=success will not arrive on its own',
                                    'retry the failed job once')
                r.store.db.close()

    def test_wait_on_own_mr_without_review_label_is_sent_back(self):
        cases = {
            'merged_success': (mr_row('success'), merged_line(), 'until=merged'),
            'merged_manual': (mr_row('manual'), merged_line(), 'until=merged'),
            'change_success': (mr_row('success'), change_line('success'), 'until=change'),
            'change_manual': (mr_row('manual'), change_line('manual'), 'until=change'),
            'other_labels': (mr_row('success', labels=['bug', 'review']), merged_line(), 'until=merged'),
        }
        for name, (mr, line, until) in cases.items():
            with self.subTest(name=name):
                self.tearDown(), self.setUp()
                r, t, gl = self.flow(mr=mr)
                a = self.turn(r, t, line)
                status = mr['head_pipeline']['status']
                self.assert_bounced(a, r, t, f'MR {MR} has no review label (verified-by:...)',
                                    f'head pipeline 14845 is {status}', 'nobody will merge it',
                                    f'{until} will not arrive on its own', 'Run the review',
                                    "project's merge tool")
                r.store.db.close()

    # -- parked as before ----------------------------------------------------------------

    def test_success_wait_on_running_or_pending_pipeline_is_parked(self):
        for status in ('running', 'pending'):
            with self.subTest(status=status):
                self.tearDown(), self.setUp()
                r, t, gl = self.flow(status)
                a = self.turn(r, t, pipeline_line(status, baseline=status))
                self.assertEqual(a.state, 'waiting')
                self.assertEqual(a.wait['target'], PIPELINE)
                r.store.db.close()

    def test_mr_with_review_label_is_parked(self):
        for labels in (['verified-by:reviewer'], [{'name': 'verified-by:qa'}]):
            with self.subTest(labels=labels):
                self.tearDown(), self.setUp()
                r, t, gl = self.flow(mr=mr_row('success', labels=labels))
                a = self.turn(r, t, merged_line())
                self.assertEqual(a.state, 'waiting')
                r.store.db.close()

    def test_mr_with_an_unreadable_label_keeps_the_wait(self):
        for labels in ([{'title': 'verified-by:qa'}], [{'name': None}], [42], [None]):
            with self.subTest(labels=labels):
                self.tearDown(), self.setUp()
                r, t, gl = self.flow(mr=mr_row('success', labels=labels))
                a = self.turn(r, t, merged_line())
                self.assertEqual(a.state, 'waiting')
                r.store.db.close()

    def test_mr_with_running_or_failed_head_is_not_this_bounce(self):
        for status in ('running', 'failed'):
            with self.subTest(status=status):
                self.tearDown(), self.setUp()
                r, t, gl = self.flow(mr=mr_row(status))
                a = self.turn(r, t, merged_line())
                self.assertNotIn('no review label', a.wake_text or '')
                self.assertEqual(a.state, 'waiting')
                r.store.db.close()

    def test_mr_not_linked_to_the_task_is_parked(self):
        r, t, gl = self.flow(mr=mr_row('success'), linked=False)
        a = self.turn(r, t, merged_line())
        self.assertEqual(a.state, 'waiting')

    def test_change_wait_with_a_result_new_to_the_agent_is_left_to_wake(self):
        # baseline running, head now success and never delivered: the evaluator wakes on it
        r, t, gl = self.flow(mr=mr_row('success'))
        a = self.turn(r, t, change_line('running'))
        self.assertEqual(a.state, 'waiting')

    def test_change_wait_on_an_already_delivered_result_is_sent_back(self):
        r, t, gl = self.flow(mr=mr_row('manual', pid=14900))
        r.store.mark(self.agent.name, t['id'], ci_delivery_key(MR, 14900), f'{SHA}:14900:manual')
        a = self.turn(r, t, change_line('running'))
        self.assertEqual(a.state, 'ready')
        # a manual result the agent was told about is a stale baseline; the stale check answers first
        self.assertIn('stale', a.wake_text)
        self.assertIn('blocked on manual jobs', a.wake_text)

    def test_review_label_check_can_be_turned_off(self):
        self.cfg.review_label_prefix = ''
        r, t, gl = self.flow(mr=mr_row('success'))
        a = self.turn(r, t, merged_line())
        self.assertEqual(a.state, 'waiting')

    def test_unreadable_status_keeps_the_wait(self):
        cases = {
            'pipeline_missing': (lambda gl: gl.pipeline_rows.update({PIPELINE: {}}), pipeline_line('failed')),
            'pipeline_bad_sha': (lambda gl: gl.pipeline_rows.update({PIPELINE: {
                'sha': 'nothex', 'id': 14845, 'status': 'failed'}}), pipeline_line('failed')),
            'mr_missing': (lambda gl: gl.mr_rows.update({MR: {}}), merged_line()),
            'mr_no_labels': (lambda gl: gl.mr_rows.update({MR: mr_row('success', labels=None)}), merged_line()),
            'mr_off_head': (lambda gl: gl.mr_rows.update({MR: {'sha': SHA, 'state': 'opened', 'labels': [],
                            'head_pipeline': {'sha': OTHER, 'id': 14845, 'status': 'success'}}}), merged_line()),
            'mr_raises': (lambda gl: setattr(gl, 'get_mr', _raise), merged_line()),
        }
        for name, (mutate, line) in cases.items():
            with self.subTest(name=name):
                self.tearDown(), self.setUp()
                r, t, gl = self.flow('failed')
                mutate(gl)
                with self.assertLogs('mesh_runner', level='WARNING') as logs:
                    a = self.turn(r, t, line)
                self.assertIn('wait check failed, wait kept', '\n'.join(logs.output))
                self.assertNotEqual(a.state, 'ready')
                self.assertNotIn('will not arrive on its own', a.wake_text or '')
                r.store.db.close()

    def test_unreadable_vcs_links_keep_the_wait(self):
        r, t, gl = self.flow(mr=mr_row('success'))
        r.mesh.vcs_links = _raise
        with self.assertLogs('mesh_runner', level='WARNING'):
            a = self.turn(r, t, merged_line())
        self.assertEqual(a.state, 'waiting')

    # -- terminal canceled state ----------------------------------------------------------

    NEWER_SHA = 'c' * 40

    def newer_row(self, status='running'):
        return {'id': 14900, 'sha': self.NEWER_SHA, 'status': status, 'ref': 'refs/merge-requests/56/head'}

    def test_canceled_target_is_sent_back_once_with_the_newer_pipeline(self):
        for until, status in (('success', 'canceled'), ('change', 'canceled'), ('change', 'cancelled'),
                              ('change', 'skipped')):
            with self.subTest(until=until, status=status):
                self.tearDown(), self.setUp()
                r, t, gl = self.flow(status)
                gl.newer[PIPELINE] = self.newer_row()
                line = f'⏳ WAIT pipeline:{PIPELINE} until={until} after={SHA}:14845:{status}'
                a = self.turn(r, t, line)
                self.assert_bounced(a, r, t, f'pipeline 14845 is {status}; nothing will change it',
                                    'Retry it, push, or wait on the newer pipeline 14900',
                                    f'⏳ WAIT pipeline:g/app#14900 until=change after={self.NEWER_SHA}:14900:running')
                self.assertIsNotNone(r.store.mark(self.agent.name, t['id'], f'canceled_bounce:{PIPELINE}:{status}'))
                r.store.db.close()

    def test_canceled_target_without_a_newer_pipeline_says_so(self):
        r, t, gl = self.flow('canceled')
        a = self.turn(r, t, pipeline_line('canceled', baseline='canceled'))
        self.assert_bounced(a, r, t, 'pipeline 14845 is canceled; nothing will change it', 'Retry it, push',
                            'none exists yet')

    def test_canceled_target_is_sent_back_even_when_the_stale_baseline_was_delivered(self):
        r, t, gl = self.flow('canceled')
        r.store.mark(self.agent.name, t['id'], ci_delivery_key(PIPELINE, 14845), f'{SHA}:14845:canceled')
        a = self.turn(r, t, pipeline_line('canceled', baseline='running'))
        self.assert_bounced(a, r, t, 'nothing will change it')
        self.assertNotIn('stale', a.wake_text)

    def test_canceled_dead_wait_declared_again_is_kept_and_marked(self):
        r, t, gl = self.flow('canceled')
        line = f'⏳ WAIT pipeline:{PIPELINE} until=change after={SHA}:14845:canceled'
        a = self.turn(r, t, line)
        self.assertEqual(a.state, 'ready')
        a = self.turn(r, t, line, first=False)
        self.assertEqual(a.state, 'waiting')
        self.assertEqual(a.wait['target'], PIPELINE)
        self.assertEqual(r.store.mark(self.agent.name, t['id'], 'dead_pipeline_rewait')[0], f'{PIPELINE}:canceled')
        # even a stale-baseline re-declaration is not bounced again
        self.assertEqual(r.store.outcomes_today(self.agent.name, t['id']).get('wait', 0)
                         + r.store.outcomes_today(self.agent.name, t['id']).get('recheck', 0), 2)

    def test_bounce_history_is_not_truncated(self):
        self.cfg.daily_wait_budget = 1000
        r, t, gl = self.flow('canceled')
        first = f'⏳ WAIT pipeline:{PIPELINE} until=change after={SHA}:14845:canceled'
        self.assertEqual(self.turn(r, t, first).state, 'ready')
        for i in range(25):
            other = f'g/app#{20000 + i}'
            gl.pipeline_rows[other] = {'sha': SHA, 'id': 20000 + i, 'status': 'canceled'}
            line = f'⏳ WAIT pipeline:{other} until=change after={SHA}:{20000 + i}:canceled'
            self.assertEqual(self.turn(r, t, line, first=False).state, 'ready')
        a = self.turn(r, t, first, first=False)
        self.assertEqual(a.state, 'waiting')
        self.assertEqual(r.store.mark(self.agent.name, t['id'], 'dead_pipeline_rewait')[0], f'{PIPELINE}:canceled')

    def test_canceled_target_with_a_running_job_is_not_this_bounce(self):
        r, t, gl = self.flow('canceled')
        gl.active_jobs.add(PIPELINE)
        a = self.turn(r, t, pipeline_line('canceled', baseline='running'))
        self.assertNotIn('nothing will change it', a.wake_text or '')
        self.assertIsNone(r.store.mark(self.agent.name, t['id'], f'canceled_bounce:{PIPELINE}:canceled'))

    def test_manual_failed_and_running_targets_get_no_canceled_bounce(self):
        for status in ('manual', 'failed', 'running'):
            with self.subTest(status=status):
                self.tearDown(), self.setUp()
                r, t, gl = self.flow(status)
                line = f'⏳ WAIT pipeline:{PIPELINE} until=change after={SHA}:14845:{status}'
                a = self.turn(r, t, line)
                self.assertNotIn('nothing will change it', a.wake_text or '')
                self.assertIsNone(r.store.mark(self.agent.name, t['id'], f'canceled_bounce:{PIPELINE}:canceled'))
                if status == 'failed':
                    self.assertIn('already finished: failed', a.wake_text)
                if status == 'running':
                    self.assertEqual(a.state, 'waiting')
                r.store.db.close()

    def test_unreadable_jobs_keep_the_wait(self):
        r, t, gl = self.flow('canceled')
        gl.pipeline_has_active_jobs = _raise
        with self.assertLogs('mesh_runner', level='WARNING'):
            a = self.turn(r, t, pipeline_line('canceled', baseline='canceled'))
        self.assertNotIn('nothing will change it', a.wake_text or '')

    # -- budget ---------------------------------------------------------------------------

    def test_repeated_unattended_wait_cannot_loop_past_the_wait_budget(self):
        for name, kwargs, line in (('pipeline', {'pipeline_status': 'manual'}, pipeline_line('manual', 'manual')),
                                   ('mr', {'mr': mr_row('success')}, merged_line())):
            with self.subTest(name=name):
                self.tearDown(), self.setUp()
                self.cfg.daily_wait_budget = 1
                r, t, gl = self.flow(**kwargs)
                a = self.turn(r, t, line)
                self.assertEqual(a.state, 'ready')
                for _ in range(4):
                    if a.state != 'ready':
                        break
                    a = self.turn(r, t, line, first=False)
                self.assertNotEqual(a.state, 'ready')
                used = r.store.outcomes_today(self.agent.name, t['id'])
                self.assertLessEqual(used.get('wait', 0), 3)
                self.assertNotIn('recheck', used)
                r.store.db.close()


class ReviewLabelConfig(unittest.TestCase):
    def test_review_label_prefix_defaults_and_loads(self):
        import tempfile
        from pathlib import Path
        from mesh_runner import config
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'runner.toml'
            path.write_text('[runner]\nmesh_url = "http://mesh"\n')
            self.assertEqual(config.load(path).review_label_prefix, 'verified-by:')
            path.write_text('[runner]\nmesh_url = "http://mesh"\nreview_label_prefix = ""\n')
            self.assertEqual(config.load(path).review_label_prefix, '')


def _raise(*_a, **_k):
    raise OSError('unreachable')


if __name__ == '__main__':
    unittest.main()
