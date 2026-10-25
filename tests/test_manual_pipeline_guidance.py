"""A pipeline blocked on manual jobs never reaches success by itself.

The owner notice and the declaration send-back point at the next real step (the review verdict,
then the merge gate when one is configured) and never at another wait on the blocked pipeline:
measured, agents copied such a hint literally and re-armed the same dead wait.
"""
import unittest

from fakes import FakeGitLab, FakeMesh, task, write_replies
from mesh_runner.wake import (ci_change_event, ci_delivery_key, manual_next_step, pipeline_merge_request,
                              typed_event)
from test_runner import Bench

SHA = 'a' * 40
MERGE_SHA = 'c' * 40
PIPELINE = 'g/app#14845'
MR = 'g/app!56'


def contract(baseline='running', predicate='success', pid=14845):
    return {'predicate': predicate, 'sha': SHA, 'pipeline': pid, 'status': baseline}


def gitlab(status='manual', ref='refs/merge-requests/56/head', sha=SHA):
    gl = FakeGitLab()
    row = {'sha': sha, 'id': 14845, 'status': status}
    if ref is not None:
        row['ref'] = ref
    gl.pipeline_rows[PIPELINE] = row
    gl.pipelines[PIPELINE] = status
    gl.mr_rows[MR] = {'sha': SHA, 'state': 'opened', 'labels': [],
                      'head_pipeline': {'sha': SHA, 'id': 14845, 'status': status}}
    return gl


def notice(gl, merge_gate, kind='pipeline'):
    return ci_change_event(gl, kind, PIPELINE if kind == 'pipeline' else MR, contract(),
                           allow_action_required=True, merge_gate=merge_gate)


class WakeText(unittest.TestCase):
    def test_notice_with_a_merge_gate_points_at_the_merge_wait(self):
        text = notice(gitlab(), merge_gate=True)
        self.assertTrue(text.startswith(f'action required: pipeline {PIPELINE} on {SHA[:8]} '
                                        'is blocked on manual jobs'))
        self.assertIn('will not reach success by itself', text)
        self.assertIn('Do not run jobs or skip a project gate automatically', text)
        self.assertIn(f'obtain the review verdict on head {SHA}', text)
        self.assertIn(f'`⏳ WAIT merge:g/app!56 after={SHA}`', text)
        self.assertNotIn('until=success on this pipeline', text)
        self.assertNotIn('until=success', text)
        self.assertNotIn('<iid>', text)

    def test_notice_without_a_merge_gate_is_neutral(self):
        text = notice(gitlab(), merge_gate=False)
        self.assertIn('blocked on manual jobs', text)
        self.assertIn('arrange the required review or authorized manual action', text)
        self.assertIn('Do not run jobs or skip a project gate automatically', text)
        self.assertNotIn('WAIT merge:', text)
        self.assertNotIn('until=success', text)
        self.assertIn(f'`⏳ WAIT mr:g/app!56 until=merged after={SHA}:opened`', text)

    def test_finished_non_success_notice_uses_the_reworded_hint(self):
        text = notice(gitlab(status='failed'), merge_gate=False)
        self.assertIn('finished: failed', text)
        self.assertIn('Do not retry pipelines, run jobs or skip a project gate automatically', text)
        self.assertNotIn('bypass', text)

    def test_notice_through_the_shared_evaluator_carries_the_flag(self):
        wait = {'ci_change': contract()}
        text = typed_event(gitlab(), 'pipeline', PIPELINE, wait, allow_action_required=True, merge_gate=True)
        self.assertIn('WAIT merge:g/app!56', text)
        self.assertNotIn('until=success', text)

    def test_unknown_merge_request_uses_placeholders_the_agent_fills(self):
        cases = {
            'branch': ('main', 'g/app!<iid>', f'after={SHA}'),
            'no_ref': (None, 'g/app!<iid>', 'after=<full-head-sha>'),
            'merged_results': ('refs/merge-requests/56/merge', 'g/app!56', 'after=<full-head-sha>'),
        }
        for name, (ref, target, after) in cases.items():
            with self.subTest(name=name):
                sha = MERGE_SHA if name == 'merged_results' else SHA
                gl = gitlab(ref=ref, sha=sha)
                text = ci_change_event(gl, 'pipeline', PIPELINE,
                                       {**contract(), 'sha': sha}, allow_action_required=True, merge_gate=True)
                self.assertIn(f'WAIT merge:{target} {after}', text)
                self.assertNotIn(MERGE_SHA, text.split('Next step:', 1)[1])
                self.assertNotIn('until=success', text)

    def test_mr_notice_with_a_merge_gate_names_the_mr_and_its_head(self):
        text = notice(gitlab(), merge_gate=True, kind='mr')
        self.assertIn(f'MR {MR} head {SHA[:8]} pipeline 14845 is blocked on manual jobs', text)
        self.assertIn(f'`⏳ WAIT merge:{MR} after={SHA}`', text)
        self.assertNotIn('until=success', text)

    def test_merge_request_of_a_pipeline_row(self):
        self.assertEqual(pipeline_merge_request({'ref': 'refs/merge-requests/7/head', 'sha': SHA}), ('7', SHA))
        self.assertEqual(pipeline_merge_request({'ref': 'refs/merge-requests/7/train', 'sha': SHA}), ('7', ''))
        self.assertEqual(pipeline_merge_request({'ref': 'feature/x', 'sha': SHA}), ('', SHA))
        self.assertEqual(pipeline_merge_request({'ref': 'refs/tags/v1', 'sha': SHA}), ('', ''))
        self.assertEqual(pipeline_merge_request({'ref': 'main', 'sha': 'nothex'}), ('', ''))
        self.assertEqual(pipeline_merge_request(None), ('', ''))
        self.assertNotIn('until=success', manual_next_step('pipeline', PIPELINE, SHA, None, False))


class SendBack(Bench):
    """Real settle + SQLite store: the wait under test is declared at the end of a turn."""

    def flow(self, status='running', merge_gate=True, ref='refs/merge-requests/56/head'):
        if merge_gate:
            self.cfg.merge_command = ('merge-gate', '{iid}', '{project}', '{sha}')
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        gl = gitlab(status, ref=ref)
        r = self.runner(mesh, gl)
        self.addCleanup(r.store.db.close)
        r.refresh_me()
        return r, t, gl

    def set_status(self, gl, status):
        gl.pipeline_rows[PIPELINE]['status'] = status
        gl.pipelines[PIPELINE] = status

    def turn(self, r, t, line, first=True):
        write_replies(self.replies, {t['id'][:8]: line, t['id'][:8] + ':resume': line})
        self.assertEqual(r.start_new(1) if first else r.resume_ready(1), 0)
        return self.finish_turn(r)

    def wake(self, r, a):
        r._wake_one(a)
        a = r.store.get(a.id)
        self.assertEqual(a.state, 'ready')
        return a

    def assert_sent_back(self, a, merge_gate=True):
        self.assertEqual(a.state, 'ready')
        self.assertIn(f'pipeline {PIPELINE} is blocked on manual jobs (pipeline 14845, {SHA[:8]})', a.wake_text)
        self.assertIn('cannot complete on its own and was sent back', a.wake_text)
        self.assertIn('Do not run jobs or skip a project gate automatically', a.wake_text)
        self.assertNotIn('until=success', a.wake_text)
        if merge_gate:
            self.assertIn(f'`⏳ WAIT merge:g/app!56 after={SHA}`', a.wake_text)
        else:
            self.assertNotIn('WAIT merge:', a.wake_text)
            self.assertIn('arrange the required review or authorized manual action', a.wake_text)

    success_line = f'⏳ WAIT pipeline:{PIPELINE} until=success after={SHA}:14845:running'
    plain_line = f'⏳ WAIT pipeline:{PIPELINE}'

    def notice_delivered(self, r, t, merge_gate=True):
        """until=success on a running pipeline, the pipeline stops at its manual stage, the owner
        notice wakes the agent (delivered by the resume that follows)."""
        a = self.turn(r, t, self.success_line)
        self.assertEqual(a.state, 'waiting')
        self.set_status(r.gitlab, 'manual')
        a = self.wake(r, a)
        self.assertIn('action required', a.wake_text)
        self.assertEqual('WAIT merge:g/app!56' in a.wake_text, merge_gate)
        self.assertNotIn('until=success', a.wake_text)
        return a

    def test_success_wait_rearmed_after_the_delivered_notice_is_sent_back(self):
        for merge_gate in (True, False):
            with self.subTest(merge_gate=merge_gate):
                self.tearDown(), self.setUp()
                r, t, gl = self.flow(merge_gate=merge_gate)
                self.notice_delivered(r, t, merge_gate)
                for baseline in ('running', 'manual'):
                    a = self.turn(r, t, f'⏳ WAIT pipeline:{PIPELINE} until=success after={SHA}:14845:{baseline}',
                                  first=False)
                    self.assert_sent_back(a, merge_gate)

    def test_plain_wait_after_the_delivered_notice_is_sent_back(self):
        r, t, gl = self.flow()
        self.notice_delivered(r, t)
        a = self.turn(r, t, self.plain_line, first=False)
        self.assert_sent_back(a)

    def test_plain_wait_rearmed_after_waking_on_manual_is_sent_back(self):
        r, t, gl = self.flow()
        a = self.turn(r, t, self.plain_line)
        self.assertEqual(a.state, 'waiting')
        self.set_status(gl, 'manual')
        a = self.wake(r, a)
        self.assertIn('finished: manual', a.wake_text)
        a = self.turn(r, t, self.plain_line, first=False)
        self.assert_sent_back(a)

    def test_plain_wait_after_a_delivered_change_observation_is_sent_back(self):
        r, t, gl = self.flow('manual')
        r.store.mark(self.agent.name, t['id'], ci_delivery_key(PIPELINE, 14845), f'{SHA}:14845:manual')
        a = self.turn(r, t, self.plain_line)
        self.assert_sent_back(a)

    def test_plain_wait_on_an_undelivered_manual_state_is_accepted(self):
        # the wait's own seed tells the agent once; nothing is refused before that
        r, t, gl = self.flow('manual')
        a = self.turn(r, t, self.plain_line)
        self.assertEqual(a.state, 'waiting')
        self.assertEqual(a.wait['target'], PIPELINE)

    def test_running_or_pending_pipeline_is_not_sent_back(self):
        for status in ('running', 'pending'):
            for line in (f'⏳ WAIT pipeline:{PIPELINE} until=success after={SHA}:14845:{status}', self.plain_line):
                with self.subTest(status=status, line=line):
                    self.tearDown(), self.setUp()
                    r, t, gl = self.flow(status)
                    a = self.turn(r, t, line)
                    self.assertEqual(a.state, 'waiting')

    def test_started_manual_stage_is_not_sent_back(self):
        # the manual state was delivered, then someone started the post-review stage
        for status in ('running', 'pending'):
            for line in (f'⏳ WAIT pipeline:{PIPELINE} until=success after={SHA}:14845:{status}', self.plain_line):
                with self.subTest(status=status, line=line):
                    self.tearDown(), self.setUp()
                    r, t, gl = self.flow()
                    self.notice_delivered(r, t)
                    self.set_status(gl, status)
                    a = self.turn(r, t, line, first=False)
                    self.assertEqual(a.state, 'waiting')
                    self.assertEqual(a.wait['target'], PIPELINE)

    def test_unreadable_pipeline_keeps_a_plain_wait(self):
        r, t, gl = self.flow('manual')
        r.store.mark(self.agent.name, t['id'], ci_delivery_key(PIPELINE, 14845), f'{SHA}:14845:manual')
        gl.pipeline_rows[PIPELINE] = {'sha': 'nothex', 'id': 14845, 'status': 'manual'}
        a = self.turn(r, t, self.plain_line)
        self.assertEqual(a.state, 'waiting')


if __name__ == '__main__':
    unittest.main()
