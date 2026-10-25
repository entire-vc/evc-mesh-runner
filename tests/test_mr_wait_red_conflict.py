"""A wait for the merge of an MR that will not merge as is (a red head already told, or a merge
conflict with its target) is sent back at declaration instead of parking for good."""
import unittest

from mesh_runner.wake import ci_delivery_key, mr_merge_blocker, typed_event
from fakes import FakeGitLab, FakeMesh, task, write_replies
from test_runner import Bench

SHA = 'a' * 40
SHA2 = 'b' * 40
MR = 'g/app!56'
MR_URL = 'https://git.example/g/app/-/merge_requests/56'
REVIEWED = ['verified-by:reviewer']


def mr_row(status='success', labels=REVIEWED, sha=SHA, pid=14845, **extra):
    out = {'sha': sha, 'state': 'opened', 'labels': list(labels),
           'head_pipeline': {'sha': sha, 'id': pid, 'status': status}}
    out.update(extra)
    return out


def merged_line(sha=SHA):
    return f'⏳ WAIT mr:{MR} until=merged after={sha}:opened'


class MergeWaitOnBlockedMR(Bench):
    def flow(self, mr, linked=True):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        if linked:
            mesh.links = {t['id']: [{'url': MR_URL, 'link_type': 'pr', 'status': 'open'}]}
        gl = FakeGitLab()
        gl.mr_rows[MR] = mr
        r = self.runner(mesh, gl)
        self.addCleanup(r.store.db.close)
        r.refresh_me()
        return r, t, gl

    def turn(self, r, t, line, first=True):
        write_replies(self.replies, {t['id'][:8]: line, t['id'][:8] + ':resume': line})
        self.assertEqual(r.start_new(1) if first else r.resume_ready(1), 0)
        return self.finish_turn(r)

    def assert_sent_back(self, a, *needles):
        self.assertEqual(a.state, 'ready')
        for needle in needles:
            self.assertIn(needle, a.wake_text)
        self.assertIn('will not merge as is', a.wake_text)
        self.assertIn('was sent back', a.wake_text)
        self.assertNotIn('until=merged', a.wake_text)

    # -- red head -------------------------------------------------------------------------

    def test_rearmed_merge_wait_after_the_delivered_red_notice_is_sent_back(self):
        for status, linked in (('failed', True), ('canceled', True), ('failed', False)):
            with self.subTest(status=status, linked=linked):
                self.tearDown(), self.setUp()
                r, t, gl = self.flow(mr_row(status), linked=linked)
                a = self.turn(r, t, merged_line())
                self.assertEqual(a.state, 'waiting')       # not told yet: accepted once
                r._wake_one(a)
                a = r.store.get(a.id)
                self.assertEqual(a.state, 'ready')
                self.assertIn('action required', a.wake_text)
                self.assertIn('will not merge as is', a.wake_text)
                a = self.turn(r, t, merged_line(), first=False)
                self.assert_sent_back(a, f'pipeline 14845 finished: {status}', 'you were already told',
                                      'open the failed job', 'proven flake')
                r.store.db.close()

    def test_red_head_not_yet_delivered_is_accepted(self):
        for status in ('failed', 'canceled', 'cancelled', 'skipped'):
            with self.subTest(status=status):
                self.tearDown(), self.setUp()
                r, t, gl = self.flow(mr_row(status))
                a = self.turn(r, t, merged_line())
                self.assertEqual(a.state, 'waiting')
                self.assertEqual(a.wait['target'], MR)
                r.store.db.close()

    def test_red_head_told_by_a_change_observation_is_sent_back(self):
        for status, gate in (('failed', False), ('skipped', True)):
            with self.subTest(status=status):
                self.tearDown(), self.setUp()
                if gate:                       # a skipped head is red only behind a merge gate
                    self.cfg.merge_command = ('merge-gate', '{iid}', '{project}', '{sha}')
                r, t, gl = self.flow(mr_row(status), linked=False)
                r.store.mark(self.agent.name, t['id'], ci_delivery_key(MR, 14845), f'{SHA}:14845:{status}')
                a = self.turn(r, t, merged_line())
                self.assert_sent_back(a, f'finished: {status}')
                r.store.db.close()

    def test_a_told_skipped_head_without_a_merge_gate_is_accepted(self):
        r, t, gl = self.flow(mr_row('skipped'), linked=False)
        r.store.mark(self.agent.name, t['id'], ci_delivery_key(MR, 14845), f'{SHA}:14845:skipped')
        a = self.turn(r, t, merged_line())
        self.assertEqual(a.state, 'waiting')

    def test_a_new_red_head_is_accepted_once_more(self):
        r, t, gl = self.flow(mr_row('failed'))
        r.store.mark(self.agent.name, t['id'], ci_delivery_key(MR, 14845), f'{SHA}:14845:failed')
        gl.mr_rows[MR] = mr_row('failed', sha=SHA2, pid=14900)
        a = self.turn(r, t, merged_line(SHA2))
        self.assertEqual(a.state, 'waiting')

    # -- merge conflict -------------------------------------------------------------------

    def test_conflicting_mr_is_sent_back_regardless_of_delivery_owner_or_head(self):
        cases = {
            'has_conflicts': (mr_row('success', has_conflicts=True), 'has a merge conflict'),
            'detailed_conflict': (mr_row('success', detailed_merge_status='conflict'), 'has a merge conflict'),
            'need_rebase': (mr_row('success', detailed_merge_status='need_rebase'), 'must be rebased'),
            'running_head': (mr_row('running', has_conflicts=True), 'has a merge conflict'),
            'no_pipeline': ({'sha': SHA, 'state': 'opened', 'labels': [], 'has_conflicts': True,
                             'head_pipeline': None}, 'has a merge conflict'),
        }
        for name, (mr, needle) in cases.items():
            with self.subTest(name=name):
                self.tearDown(), self.setUp()
                r, t, gl = self.flow(mr, linked=False)
                a = self.turn(r, t, merged_line())
                self.assert_sent_back(a, needle, 'rebase the branch onto its target',
                                      'cover the rebased delta')
                r.store.db.close()

    def test_green_mr_without_conflict_is_accepted(self):
        for extra in ({}, {'has_conflicts': False, 'detailed_merge_status': 'mergeable'},
                      {'detailed_merge_status': 'checking'}, {'detailed_merge_status': 'unchecked'},
                      {'has_conflicts': 'yes'}):
            with self.subTest(extra=extra):
                self.tearDown(), self.setUp()
                r, t, gl = self.flow(mr_row('success', **extra))
                a = self.turn(r, t, merged_line())
                self.assertEqual(a.state, 'waiting')
                self.assertEqual(a.wait['target'], MR)
                r.store.db.close()

    def test_change_wait_on_a_conflicting_mr_keeps_its_own_rules(self):
        r, t, gl = self.flow(mr_row('running', has_conflicts=True))
        a = self.turn(r, t, f'⏳ WAIT mr:{MR} until=change after={SHA}:14845:running')
        self.assertEqual(a.state, 'waiting')

    # -- next step ------------------------------------------------------------------------

    def test_send_back_names_the_merge_gate_when_one_is_configured(self):
        for name, mr, delivered in (('red', mr_row('failed'), True),
                                    ('conflict', mr_row('success', has_conflicts=True), False)):
            with self.subTest(name=name):
                self.tearDown(), self.setUp()
                self.cfg.merge_command = ('merge-gate', '{iid}', '{project}', '{sha}')
                r, t, gl = self.flow(mr)
                if delivered:
                    r.store.mark(self.agent.name, t['id'], ci_delivery_key(MR, 14845), f'{SHA}:14845:failed')
                a = self.turn(r, t, merged_line())
                self.assert_sent_back(a, f'`⏳ WAIT merge:{MR} after=<full-new-head-sha>`', 'once it is SHIP')
                r.store.db.close()

    def test_send_back_without_a_merge_gate_names_no_merge_wait(self):
        r, t, gl = self.flow(mr_row('success', has_conflicts=True))
        a = self.turn(r, t, merged_line())
        self.assert_sent_back(a, "project's merge tool")
        self.assertNotIn('WAIT merge:', a.wake_text)


class ConflictOwnerNotice(unittest.TestCase):
    def wait(self):
        return {'kind': 'mr', 'target': MR, 'mr_state': {'predicate': 'merged', 'sha': SHA, 'state': 'opened'}}

    def test_conflict_notifies_once_per_head(self):
        gl = FakeGitLab()
        gl.mr_rows[MR] = mr_row('success', has_conflicts=True)
        first = {}
        event = typed_event(gl, 'mr', MR, self.wait(), '', first, allow_action_required=True, merge_gate=True)
        self.assertIn('action required', event)
        self.assertIn('has a merge conflict with its target branch', event)
        self.assertIn(f'WAIT merge:{MR} after=', event)
        self.assertNotIn('until=merged', event)
        self.assertTrue(first['action_required'])
        self.assertEqual(typed_event(gl, 'mr', MR, self.wait(), lambda key: first['value'], {},
                                     allow_action_required=True), '')
        # observers that do not opt into owner action keep the old contract
        self.assertEqual(typed_event(gl, 'mr', MR, self.wait(), '', {}), '')
        gl.mr_rows[MR] = mr_row('success', sha=SHA2, detailed_merge_status='need_rebase')
        second = {}
        event = typed_event(gl, 'mr', MR, self.wait(), lambda key: first['value'], second,
                            allow_action_required=True)
        self.assertIn('must be rebased', event)
        self.assertNotEqual(first['key'], second['key'])

    def test_a_skipped_head_is_red_only_behind_a_merge_gate(self):
        # the merge gate merges on success alone; without it a project may let a skipped head merge
        gl = FakeGitLab()
        gl.mr_rows[MR] = mr_row('skipped')
        event = typed_event(gl, 'mr', MR, self.wait(), '', {}, allow_action_required=True, merge_gate=True)
        self.assertIn('finished: skipped; it will not merge as is', event)
        self.assertEqual(typed_event(gl, 'mr', MR, self.wait(), '', {}, allow_action_required=True,
                                     merge_gate=False), '')

    def test_red_notice_comes_first_then_the_conflict(self):
        gl = FakeGitLab()
        gl.mr_rows[MR] = mr_row('failed', has_conflicts=True)
        seen = {}
        red = {}
        event = typed_event(gl, 'mr', MR, self.wait(), '', red, allow_action_required=True)
        self.assertIn('finished: failed', event)
        seen[red['key']] = red['value']
        conflict = {}
        event = typed_event(gl, 'mr', MR, self.wait(), lambda key: seen.get(key, ''), conflict,
                            allow_action_required=True)
        self.assertIn('merge conflict', event)

    def test_green_mr_without_conflict_gives_no_notice(self):
        gl = FakeGitLab()
        for extra in ({}, {'has_conflicts': False}, {'detailed_merge_status': 'checking'}):
            gl.mr_rows[MR] = mr_row('success', **extra)
            self.assertEqual(typed_event(gl, 'mr', MR, self.wait(), '', {}, allow_action_required=True), '')

    def test_blocker_reads_only_explicit_provider_signals(self):
        self.assertEqual(mr_merge_blocker({'has_conflicts': True}), 'conflict')
        self.assertEqual(mr_merge_blocker({'detailed_merge_status': 'conflict'}), 'conflict')
        self.assertEqual(mr_merge_blocker({'detailed_merge_status': 'need_rebase'}), 'need_rebase')
        for row in (None, [], {}, {'has_conflicts': 1}, {'detailed_merge_status': 'broken_status'},
                    {'detailed_merge_status': 'ci_must_pass'}):
            self.assertEqual(mr_merge_blocker(row), '', row)


if __name__ == '__main__':
    unittest.main()
