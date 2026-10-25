"""A typed CI/MR wait that can no longer fire is refused at declaration, and a wait whose
observed state changes (including a retried pipeline that fails again) wakes on the next tick."""
import datetime as dt

from fakes import FakeGitLab, FakeMesh, task, write_replies
from mesh_runner.outcome import parse_line
from mesh_runner.store import iso
from mesh_runner.wake import ci_delivery_key, ci_observation, typed_event
from test_runner import Bench

SHA = 'a' * 40
NEW = 'c' * 40
MR = 'g/app!224'
PIPELINE = 'g/app#16355'
MR_URL = 'https://git.example/g/app/-/merge_requests/224'
LONG_ID = '1' * 8


def mr_row(status='failed', sha=SHA, pid=16447, labels=(), **extra):
    return {'sha': sha, 'state': 'opened', 'labels': list(labels),
            'head_pipeline': {'sha': sha, 'id': pid, 'status': status}, **extra}


def change(kind, status, pid=16447, sha=SHA):
    target = MR if kind == 'mr' else PIPELINE
    return f'⏳ WAIT {kind}:{target} until=change after={sha}:{pid}:{status}'


class DeadWaits(Bench):
    def flow(self, mr=None, pipeline=None, linked=True):
        t = task(0x819d7ee2)
        mesh = FakeMesh([t], max_concurrent=1)
        mesh.links = {t['id']: [{'url': MR_URL, 'link_type': 'pr', 'status': 'open'}]} if linked else {}
        gl = FakeGitLab()
        if mr is not None:
            gl.mr_rows[MR] = mr
        if pipeline is not None:
            gl.pipeline_rows[PIPELINE] = {'sha': SHA, 'id': 16355, 'status': pipeline}
        r = self.runner(mesh, gl)
        self.addCleanup(r.store.db.close)
        r.refresh_me()
        return r, t, gl

    def turn(self, r, t, line):
        write_replies(self.replies, {t['id'][:8]: line, t['id'][:8] + ':resume': line})
        self.assertEqual(r.start_new(1), 0)
        return self.finish_turn(r)

    def assert_bounced(self, a, *needles):
        self.assertEqual(a.state, 'ready')
        for needle in needles:
            self.assertIn(needle, a.wake_text)

    # -- a: a baseline that is already out of date ---------------------------------------------

    def test_baseline_running_but_head_already_failed_and_delivered_is_sent_back(self):
        r, t, gl = self.flow(mr=mr_row('failed'))
        r.store.mark(self.agent.name, t['id'], ci_delivery_key(MR, 16447), f'{SHA}:16447:failed')
        a = self.turn(r, t, change('mr', 'running'))
        self.assert_bounced(a, 'stale', f'after={SHA}:16447:failed')
        self.assertEqual(r.store.idle_waits_today(self.agent.name, t['id']), 1)

    def test_baseline_running_but_pipeline_already_manual_and_delivered_is_sent_back(self):
        r, t, gl = self.flow(pipeline='manual')
        r.store.mark(self.agent.name, t['id'], ci_delivery_key(PIPELINE, 16355), f'{SHA}:16355:manual')
        a = self.turn(r, t, change('pipeline', 'running', pid=16355))
        self.assert_bounced(a, 'blocked on manual jobs', 'stale', f'after={SHA}:16355:manual')

    def test_changed_result_not_yet_delivered_is_left_to_wake(self):
        r, t, gl = self.flow(mr=mr_row('failed'))
        a = self.turn(r, t, change('mr', 'running'))
        self.assertEqual(a.state, 'waiting')
        r._wake_one(a)
        self.assertEqual(a.state, 'ready')
        self.assertIn('pipeline 16447 failed', a.wake_text)

    def test_failed_head_at_the_declared_baseline_of_a_foreign_mr_is_sent_back(self):
        # the shape of the wait that sat for hours: an MR already [opened/failed]
        r, t, gl = self.flow(mr=mr_row('failed'), linked=False)
        a = self.turn(r, t, change('mr', 'failed'))
        self.assert_bounced(a, f'MR {MR}', 'pipeline 16447', 'already finished: failed',
                            'nothing changes without a push or a retry', 'WAIT card:')
        self.assertEqual(r.store.idle_waits_today(self.agent.name, t['id']), 1)

    def test_canceled_or_skipped_head_is_sent_back_like_failed(self):
        for status in ('canceled', 'cancelled', 'skipped'):
            with self.subTest(status=status):
                self.tearDown(), self.setUp()
                r, t, gl = self.flow(mr=mr_row(status))
                a = self.turn(r, t, change('mr', status))
                self.assert_bounced(a, f'already finished: {status}')
                r.store.db.close()

    def test_finished_pipeline_at_the_baseline_is_sent_back(self):
        for status in ('failed', 'success'):
            with self.subTest(status=status):
                self.tearDown(), self.setUp()
                r, t, gl = self.flow(pipeline=status)
                a = self.turn(r, t, change('pipeline', status, pid=16355))
                self.assert_bounced(a, f'already finished: {status}', 'nothing changes without')
                r.store.db.close()

    def test_canceled_pipeline_at_the_baseline_is_sent_back_with_its_own_text(self):
        r, t, gl = self.flow(pipeline='canceled')
        a = self.turn(r, t, change('pipeline', 'canceled', pid=16355))
        self.assert_bounced(a, 'pipeline 16355 is canceled; nothing will change it')

    # -- b: manual baseline -------------------------------------------------------------------

    def test_change_wait_on_a_manual_pipeline_is_sent_back_with_the_way_out(self):
        r, t, gl = self.flow(pipeline='manual')
        a = self.turn(r, t, change('pipeline', 'manual', pid=16355))
        self.assert_bounced(a, 'blocked on manual jobs', 'nothing changes without your action',
                            'merge wrapper', 'start the job')

    def test_change_wait_on_own_mr_with_manual_head_is_sent_back_even_with_a_review_label(self):
        for labels in ((), ('verified-by:reviewer',)):
            with self.subTest(labels=labels):
                self.tearDown(), self.setUp()
                r, t, gl = self.flow(mr=mr_row('manual', labels=labels))
                a = self.turn(r, t, change('mr', 'manual'))
                self.assertEqual(a.state, 'ready')
                self.assertIn('pipeline 16447', a.wake_text)
                r.store.db.close()

    def test_manual_head_of_a_foreign_mr_is_parked(self):
        r, t, gl = self.flow(mr=mr_row('manual'), linked=False)
        a = self.turn(r, t, change('mr', 'manual'))
        self.assertEqual(a.state, 'waiting')

    def test_live_pipelines_are_parked(self):
        for kind, status in (('pipeline', 'running'), ('pipeline', 'pending'), ('mr', 'running'),
                             ('mr', 'created')):
            with self.subTest(kind=kind, status=status):
                self.tearDown(), self.setUp()
                r, t, gl = self.flow(mr=mr_row(status), pipeline=status)
                pid = 16355 if kind == 'pipeline' else 16447
                a = self.turn(r, t, change(kind, status, pid=pid))
                self.assertEqual(a.state, 'waiting')
                r.store.db.close()

    def test_unreadable_head_keeps_the_change_wait(self):
        r, t, gl = self.flow(mr={'sha': SHA, 'state': 'opened', 'labels': []}, linked=False)
        with self.assertLogs('mesh_runner', level='WARNING'):
            a = self.turn(r, t, change('mr', 'failed'))
        self.assertEqual(a.state, 'waiting')

    # -- c: until=merged on the task's own MR that was never linked --------------------------

    def merged(self):
        return f'⏳ WAIT mr:{MR} until=merged after={SHA}:opened'

    def test_unlinked_mr_naming_the_task_in_its_description_is_the_tasks_own(self):
        r, t, gl = self.flow(mr=mr_row('success', pid=16467,
                                       description='Fixes 819d7ee2: a decision wait was rejected'),
                             linked=False)
        a = self.turn(r, t, self.merged())
        self.assert_bounced(a, 'has no review label', 'nobody will merge it', 'until=merged')

    def test_unlinked_mr_naming_the_task_in_its_title_or_branch_is_the_tasks_own(self):
        for extra in ({'title': 'route #819D7EE2 fix'}, {'source_branch': 'fix/819d7ee2-ask'}):
            with self.subTest(extra=extra):
                self.tearDown(), self.setUp()
                r, t, gl = self.flow(mr=mr_row('success', **extra), linked=False)
                a = self.turn(r, t, self.merged())
                self.assertEqual(a.state, 'ready')
                self.assertIn('has no review label', a.wake_text)
                r.store.db.close()

    def test_unlinked_mr_that_does_not_name_the_task_is_parked(self):
        for extra in ({'description': 'unrelated'}, {'title': 'fix 819d7ee20 and 1819d7ee2'}, {}):
            with self.subTest(extra=extra):
                self.tearDown(), self.setUp()
                r, t, gl = self.flow(mr=mr_row('success', **extra), linked=False)
                a = self.turn(r, t, self.merged())
                self.assertEqual(a.state, 'waiting')
                r.store.db.close()

    def test_unlinked_own_mr_with_a_review_label_is_parked(self):
        r, t, gl = self.flow(mr=mr_row('success', labels=['verified-by:qa'],
                                       description='Fixes 819d7ee2'), linked=False)
        a = self.turn(r, t, self.merged())
        self.assertEqual(a.state, 'waiting')

    # -- d: an accepted wait wakes when the observation changes -------------------------------

    def test_ci_observation_follows_the_head_pipeline_through_a_retry(self):
        gl = FakeGitLab()
        for status in ('running', 'failed', 'running', 'failed', 'manual', 'success'):
            gl.mr_rows[MR] = mr_row(status)
            self.assertEqual(ci_observation(gl, 'mr', MR), (SHA, 16447, status))
        gl.pipeline_rows[PIPELINE] = {'sha': SHA, 'id': 16355, 'status': 'running'}
        self.assertEqual(ci_observation(gl, 'pipeline', PIPELINE), (SHA, 16355, 'running'))

    def test_typed_event_fires_on_each_transition_away_from_the_baseline(self):
        gl = FakeGitLab()
        for kind, target, pid in (('mr', MR, 16447), ('pipeline', PIPELINE, 16355)):
            wait = parse_line(change(kind, 'running', pid=pid), dt.datetime.now(dt.timezone.utc)).extra
            for status in ('failed', 'success', 'manual'):
                gl.mr_rows[MR] = mr_row(status)
                gl.pipeline_rows[PIPELINE] = {'sha': SHA, 'id': 16355, 'status': status}
                self.assertIn(f'pipeline {pid} {status}', typed_event(gl, kind, target, wait))
            gl.mr_rows[MR] = mr_row('running')
            gl.pipeline_rows[PIPELINE] = {'sha': SHA, 'id': 16355, 'status': 'running'}
            self.assertEqual(typed_event(gl, kind, target, wait), '')

    def test_accepted_wait_wakes_when_running_turns_failed(self):
        r, t, gl = self.flow(mr=mr_row('running'))
        a = self.turn(r, t, change('mr', 'running'))
        self.assertEqual(a.state, 'waiting')
        r._wake_one(a)
        self.assertEqual(a.state, 'waiting')
        gl.mr_rows[MR] = mr_row('failed')
        r._wake_one(a)
        self.assertEqual(a.state, 'ready')
        self.assertIn('pipeline 16447 failed', a.wake_text)

    def test_retried_pipeline_that_fails_again_is_not_swallowed_by_the_earlier_delivery(self):
        # failed -> the agent was told -> retry (same id, running) -> wait -> failed again
        r, t, gl = self.flow(mr=mr_row('running'))
        key = ci_delivery_key(MR, 16447)
        r.store.mark(self.agent.name, t['id'], key, f'{SHA}:16447:failed')
        a = self.turn(r, t, change('mr', 'running'))
        self.assertEqual(a.state, 'waiting')
        r._wake_one(a)
        self.assertEqual(a.state, 'waiting')
        gl.mr_rows[MR] = mr_row('failed')
        r._wake_one(a)
        self.assertEqual(a.state, 'ready')
        self.assertIn('pipeline 16447 failed', a.wake_text)

    def test_delivery_of_the_current_result_is_kept(self):
        # a wait declared on the very result the agent was told about is not re-woken
        r, t, gl = self.flow(mr=mr_row('running'))
        key = ci_delivery_key(MR, 16447)
        r.store.mark(self.agent.name, t['id'], key, f'{SHA}:16447:running')
        self.turn(r, t, change('mr', 'running'))
        self.assertEqual(r.store.mark(self.agent.name, t['id'], key)[0], f'{SHA}:16447:running')

    # -- the review-label check can be turned off; the dead-change checks cannot --------------

    def test_dead_change_waits_are_sent_back_with_the_review_label_check_off(self):
        self.cfg.review_label_prefix = ''
        cases = {
            'foreign_failed': (mr_row('failed'), False, 'failed'),
            'foreign_canceled': (mr_row('canceled'), False, 'canceled'),
            'own_manual': (mr_row('manual'), True, 'manual'),
            'own_manual_with_labels': (mr_row('manual', labels=['whatever']), True, 'manual'),
        }
        for name, (mr, linked, status) in cases.items():
            with self.subTest(name=name):
                self.tearDown(), self.setUp()
                self.cfg.review_label_prefix = ''
                r, t, gl = self.flow(mr=mr, linked=linked)
                a = self.turn(r, t, change('mr', status))
                self.assertEqual(a.state, 'ready')
                self.assertIn('pipeline 16447', a.wake_text)
                self.assertIn('nothing changes without', a.wake_text)
                r.store.db.close()

    def test_label_rule_stays_off_and_live_or_foreign_waits_stay_parked_with_the_check_off(self):
        self.cfg.review_label_prefix = ''
        r, t, gl = self.flow(mr=mr_row('success'))
        self.assertEqual(self.turn(r, t, self.merged()).state, 'waiting')
        for mr, linked, status in ((mr_row('running'), True, 'running'), (mr_row('manual'), False, 'manual')):
            with self.subTest(status=status, linked=linked):
                self.tearDown(), self.setUp()
                self.cfg.review_label_prefix = ''
                r, t, gl = self.flow(mr=mr, linked=linked)
                self.assertEqual(self.turn(r, t, change('mr', status)).state, 'waiting')
                r.store.db.close()

    # -- a rejected declaration leaves the delivery receipt alone -----------------------------

    def test_card_wait_rejected_as_idle_leaves_the_delivery_mark_unchanged(self):
        t, x = task(1), task(2)
        r = self.runner(FakeMesh([t, x], max_concurrent=1), FakeGitLab())
        self.addCleanup(r.store.db.close)
        r.refresh_me()
        ax = r.store.create(self.agent.name, x['id'], 'claude')
        r.store.update(ax, state='waiting', wait={'kind': 'time', 'target': '', 'until': iso(),
                                                  'since': iso(), 'started': iso()})
        key = ci_delivery_key(MR, 16447)
        r.store.mark(self.agent.name, t['id'], key, f'{SHA}:16447:failed')
        a = self.turn(r, t, f"⏳ WAIT card:#{x['id'][:8]}")
        self.assertEqual(a.state, 'ready')
        self.assertIn('does not hold development', a.wake_text)
        self.assertEqual(r.store.mark(self.agent.name, t['id'], key)[0], f'{SHA}:16447:failed')
