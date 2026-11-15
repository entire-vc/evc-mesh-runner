"""A ``required_jobs`` wait whose named job sits in ``created`` behind a manual job.

GitLab leaves a job ``created`` while the job it ``needs`` is ``manual``; nothing moves until a person
plays the manual job. Read as ``running`` such a wait never woke (``db-gate`` manual, ``coverage-gate``
created behind it: the owner slept for days on a pipeline only a person could move).
"""
import unittest

from mesh_runner.store import iso
from mesh_runner.wake import ci_change_event
from fakes import FakeGitLab, FakeMesh, task, typed, wait_pipeline
from test_runner import Bench

SHA = 'c' * 40
TARGET = 'example/project#30292'


def gitlab(jobs: dict, pipeline_status='manual', extra=()):
    """A pipeline 30292 with the named job statuses (ids in order) and unnamed ``extra`` jobs."""
    gl = FakeGitLab()
    gl.pipeline_rows[TARGET] = {'id': 30292, 'sha': SHA, 'status': pipeline_status,
                                'ref': 'refs/merge-requests/7/head'}
    rows = [{'id': 636700 + i, 'name': n, 'status': st} for i, (n, st) in enumerate(jobs.items())]
    rows += [{'id': 636800 + i, 'name': n, 'status': st} for i, (n, st) in enumerate(extra)]
    gl.jobs[TARGET] = rows
    return gl


def contract(jobs, predicate='success', status='manual'):
    return {'predicate': predicate, 'sha': SHA, 'pipeline': 30292, 'status': status, 'jobs': list(jobs)}


def event(gl, jobs, predicate='success', delivered='', observation=None):
    """The evaluator of an attended wait; a ``jobs`` contract is read by ``_jobs_event``."""
    return ci_change_event(gl, 'pipeline', TARGET, contract(jobs, predicate), delivered, observation,
                           allow_action_required=True)


class BlockedBehindManual(unittest.TestCase):
    def test_manual_and_created_behind_it_is_action_required(self):
        gl = gitlab({'db-gate': 'manual', 'coverage-gate': 'created'})
        text = event(gl, ['db-gate', 'coverage-gate'])
        self.assertTrue(text.startswith('action required: pipeline example/project#30292'), text)
        self.assertIn('job(s) db-gate manual', text)
        self.assertIn('job(s) coverage-gate blocked behind a manual job', text)
        self.assertIn('Do not run jobs or skip a project gate automatically', text)

    def test_created_job_behind_an_unnamed_manual_job_is_action_required(self):
        gl = gitlab({'coverage-gate': 'created'}, extra=[('db-gate', 'manual')])
        text = event(gl, ['coverage-gate'])
        self.assertIn('action required', text)
        self.assertIn('job(s) coverage-gate blocked behind a manual job', text)

    def test_positive_control_running_and_created_stays_silent(self):
        # a job that moves will finish by itself: the wait sleeps, whatever the aggregate says
        for aggregate in ('running', 'manual'):
            with self.subTest(aggregate=aggregate):
                gl = gitlab({'a': 'running', 'b': 'created'}, pipeline_status=aggregate)
                self.assertEqual(event(gl, ['a', 'b']), '')

    def test_any_moving_job_of_the_pipeline_keeps_the_wait(self):
        for moving in ('pending', 'running', 'preparing', 'waiting_for_resource', 'scheduled',
                       'canceling', 'waiting_for_callback'):
            with self.subTest(moving=moving):
                gl = gitlab({'db-gate': 'manual', 'coverage-gate': 'created'}, extra=[('build', moving)])
                self.assertEqual(event(gl, ['db-gate', 'coverage-gate']), '')

    def test_aggregate_not_manual_keeps_the_wait(self):
        # a running trigger job is not in the jobs list; the aggregate status still shows it
        for aggregate in ('running', 'pending', 'created'):
            with self.subTest(aggregate=aggregate):
                gl = gitlab({'db-gate': 'manual', 'coverage-gate': 'created'}, pipeline_status=aggregate)
                self.assertEqual(event(gl, ['db-gate', 'coverage-gate']), '')

    def test_notice_is_delivered_once(self):
        gl = gitlab({'db-gate': 'manual', 'coverage-gate': 'created'})
        seen = {}
        self.assertIn('action required', event(gl, ['db-gate', 'coverage-gate'], observation=seen))
        stored = {seen['key']: seen['value']}
        self.assertEqual(event(gl, ['db-gate', 'coverage-gate'], delivered=stored.get), '')

    def test_played_gate_sleeps_then_wakes_on_success(self):
        gl = gitlab({'db-gate': 'running', 'coverage-gate': 'created'}, pipeline_status='running')
        self.assertEqual(event(gl, ['db-gate', 'coverage-gate']), '')
        gl = gitlab({'db-gate': 'success', 'coverage-gate': 'success'}, pipeline_status='success')
        self.assertIn('job(s) db-gate,coverage-gate finished: success', event(gl, ['db-gate', 'coverage-gate']))

    def test_until_change_wakes_on_the_blocked_state(self):
        gl = gitlab({'db-gate': 'manual', 'coverage-gate': 'created'})
        text = event(gl, ['db-gate', 'coverage-gate'], predicate='change')
        self.assertIn('job(s) db-gate manual, job(s) coverage-gate blocked behind a manual job', text)
        self.assertIn('they start only by hand', text)


class RunnerWakesTheLane(Bench):
    def test_waiting_lane_is_woken_with_the_notice(self):
        t = task(1)
        gl = gitlab({'db-gate': 'manual', 'coverage-gate': 'created'})
        r = self.runner(FakeMesh([t]), gl)
        r.refresh_me()
        a = r.store.create(self.agent.name, t['id'], 'claude')
        o = typed(wait_pipeline('example/project', 30292, until='green', jobs=['db-gate', 'coverage-gate'],
                                after_sha=SHA, after_pipeline_id=30292, after_status='manual'))
        self.assertEqual(r._jobs_wait_problem(a, o), '')       # new to the agent: accepted, the evaluator wakes
        r.store.update(a, state='waiting', wait={'kind': o.kind, 'target': o.target, **o.extra,
                                                 'since': iso(), 'started': iso()})
        r._wake_one(a)
        got = r.store.get(a.id)
        self.assertEqual(got.state, 'ready')
        self.assertIn('coverage-gate blocked behind a manual job', got.wake_text)
        # the turn carrying the notice was delivered (the runner stores its mark on the prompt ack) ...
        delivery = got.wait['ci_action_delivery']
        r.store.mark(self.agent.name, t['id'], delivery['key'], delivery['value'])
        # ... so the same wait declared again is sent back instead of sleeping for good
        self.assertIn('job(s) db-gate,coverage-gate are held by a manual job', r._jobs_wait_problem(a, o))


if __name__ == '__main__':
    unittest.main()
