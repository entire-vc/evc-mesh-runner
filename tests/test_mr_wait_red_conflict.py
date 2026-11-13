"""A wait for the merge of an MR that will not merge as is (a red head already told, or a merge
conflict with its target) is sent back at declaration instead of parking for good."""
import datetime as dt
import socket
import unittest
from unittest import mock

from mesh_runner.store import iso
from mesh_runner.outcome import parse_line
from mesh_runner.wake import blocked_mr_next_step, ci_delivery_key, mr_merge_blocker, typed_event
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
            'need_rebase': (mr_row('success', detailed_merge_status='need_rebase'), 'is behind its target branch'),
            'running_head': (mr_row('running', has_conflicts=True), 'has a merge conflict'),
            'no_pipeline': ({'sha': SHA, 'state': 'opened', 'labels': [], 'has_conflicts': True,
                             'head_pipeline': None}, 'has a merge conflict'),
        }
        for name, (mr, needle) in cases.items():
            with self.subTest(name=name):
                self.tearDown(), self.setUp()
                r, t, gl = self.flow(mr, linked=False)
                a = self.turn(r, t, merged_line())
                self.assert_sent_back(a, needle, 'merge the target branch into the MR branch',
                                      'cover the merge delta')
                self.assertNotIn('rebase the branch onto', a.wake_text)
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


    # -- behind only, merge gate (merge-and-release rule 2) ------------------------------------

    def test_behind_mr_behind_a_merge_gate_is_accepted(self):
        # the gate syncs a branch that is only behind right before merging: the wait is not sent back
        for linked in (True, False):
            with self.subTest(linked=linked):
                self.tearDown(), self.setUp()
                self.cfg.merge_command = ('merge-gate', '{iid}', '{project}', '{sha}')
                r, t, gl = self.flow(mr_row('success', detailed_merge_status='need_rebase'), linked=linked)
                a = self.turn(r, t, merged_line())
                self.assertEqual(a.state, 'waiting')
                self.assertEqual(a.wait['target'], MR)
                r.store.db.close()

    def test_conflict_behind_a_merge_gate_is_still_sent_back(self):
        self.cfg.merge_command = ('merge-gate', '{iid}', '{project}', '{sha}')
        r, t, gl = self.flow(mr_row('success', has_conflicts=True, detailed_merge_status='need_rebase'))
        a = self.turn(r, t, merged_line())
        self.assert_sent_back(a, 'has a merge conflict', 'merge the target branch into the MR branch')

    def test_behind_mr_next_step_behind_a_merge_gate_leaves_the_branch_alone(self):
        text = blocked_mr_next_step(MR, 'need_rebase', True, SHA)
        self.assertNotIn('merge the target branch', text)
        self.assertNotIn('Do not wait for this MR to merge again', text)
        self.assertIn('leave the branch as it is', text)
        self.assertIn(f'`⏳ WAIT merge:{MR} after={SHA}`', text)     # the current head, not a new one
        self.assertNotIn('new-head', text)
        self.assertIn('<full-head-sha>', blocked_mr_next_step(MR, 'need_rebase', True))
        # without a gate, or for a conflict, the author still makes the new head
        for cause, gate in (('need_rebase', False), ('conflict', True), ('conflict', False)):
            with self.subTest(cause=cause, gate=gate):
                text = blocked_mr_next_step(MR, cause, gate, SHA)
                self.assertIn('merge the target branch into the MR branch', text)
                self.assertIn('Do not wait for this MR to merge again', text)
        # a provider-qualified target has no merge: wait, so no gate either
        self.assertIn('merge the target branch',
                      blocked_mr_next_step('github:o/r!5', 'need_rebase', True, SHA))


def success_line(sha=SHA, status='failed'):
    return f'⏳ WAIT mr:{MR} until=success after={sha}:14845:{status}'


class SuccessWaitOnRedMR(MergeWaitOnBlockedMR):
    """``until=success`` on an MR whose red head was already told: no new head pipeline comes by
    itself, so it is sent back like ``until=merged``; told once first by the owner notice."""

    def test_redeclared_success_wait_after_the_red_notice_is_sent_back(self):
        for status, linked in (('failed', True), ('canceled', True), ('failed', False)):
            with self.subTest(status=status, linked=linked):
                self.tearDown(), self.setUp()
                r, t, gl = self.flow(mr_row(status), linked=linked)
                a = self.turn(r, t, success_line(status=status))
                self.assertEqual(a.state, 'waiting')        # not told yet: accepted once
                r._wake_one(a)
                a = r.store.get(a.id)
                self.assertEqual(a.state, 'ready')
                self.assertIn('the success predicate is not met', a.wake_text)
                a = self.turn(r, t, success_line(status=status), first=False)
                self.assertEqual(a.state, 'ready')
                for needle in (f'pipeline 14845 finished: {status}', 'you were already told',
                               'was sent back', 'open the failed job'):
                    self.assertIn(needle, a.wake_text)
                r.store.db.close()

    def test_success_wait_on_a_running_or_new_red_head_is_accepted(self):
        for mr, line in ((mr_row('running'), success_line(status='running')),
                         (mr_row('failed', sha=SHA2, pid=14900), f'⏳ WAIT mr:{MR} until=success after={SHA2}:14900:failed')):
            with self.subTest(line=line):
                self.tearDown(), self.setUp()
                r, t, gl = self.flow(mr_row('failed'))
                r.store.mark(self.agent.name, t['id'], ci_delivery_key(MR, 14845), f'{SHA}:14845:failed')
                gl.mr_rows[MR] = mr
                a = self.turn(r, t, line)
                self.assertEqual(a.state, 'waiting')
                r.store.db.close()


class StandingWaitOnRedMR(MergeWaitOnBlockedMR):
    """A wait declared before the refusal rules (the red head already told, then the same MR
    re-armed) is judged by those rules once, after its evaluator found nothing new."""

    def told_then_standing(self, line, mr=None):
        r, t, gl = self.flow(mr or mr_row('failed'))
        a = self.turn(r, t, merged_line())
        r._wake_one(a)                                   # the one owner notice for this red head
        a = r.store.get(a.id)
        self.assertIn('action required', a.wake_text)
        told = a.wait['ci_action_delivery']              # acknowledged by the turn that carried it
        r.store.mark(self.agent.name, t['id'], told['key'], told['value'])
        o = parse_line(line, dt.datetime.now(dt.timezone.utc))
        r.store.update(a, state='waiting', wake_text='',
                       wait={'kind': o.kind, 'target': o.target, **o.extra, 'since': iso(), 'started': iso()})
        return r, t, gl, r.store.get(a.id)

    def test_standing_success_or_merge_wait_on_a_told_red_head_wakes_once(self):
        for line in (success_line(), merged_line()):
            with self.subTest(line=line):
                self.tearDown(), self.setUp()
                r, t, gl, a = self.told_then_standing(line)
                with self.assertLogs('mesh_runner', 'INFO') as logs:
                    r._wake_budgeted(a)
                got = r.store.get(a.id)
                self.assertEqual(got.state, 'ready')
                self.assertIn('you were already told', got.wake_text)
                self.assertIn('was sent back', got.wake_text)
                self.assertTrue(any('woke: MR g/app!56' in m for m in logs.output))
                r.store.db.close()

    def test_standing_wait_with_timing_out_reads_wakes_within_the_bound(self):
        r, t, gl, a = self.told_then_standing(success_line())
        real = gl.get_mr
        gl.get_mr = mock.Mock(side_effect=socket.timeout('The read operation timed out'))
        clock = {'t': dt.datetime(2026, 11, 7, tzinfo=dt.timezone.utc)}
        with mock.patch('mesh_runner.runner.now', lambda: clock['t']):
            for _ in range(2):
                r._wake_budgeted(r.store.get(a.id))
                clock['t'] += dt.timedelta(hours=3)
            self.assertEqual(r.store.get(a.id).state, 'waiting')   # 2 failures over 3h: still inside the bound
            r._wake_budgeted(r.store.get(a.id))                     # the 3rd, 6h after the first
        got = r.store.get(a.id)
        self.assertEqual(got.state, 'ready')
        self.assertIn('failed 3 times in a row over 6.0h', got.wake_text)
        self.assertIn('kind=timeout', got.wake_text)
        gl.get_mr = real

    def test_a_read_that_recovers_inside_the_bound_sends_the_wait_back(self):
        r, t, gl, a = self.told_then_standing(success_line())
        real = gl.get_mr
        gl.get_mr = mock.Mock(side_effect=socket.timeout('The read operation timed out'))
        r._wake_budgeted(r.store.get(a.id))
        self.assertEqual(r.store.get(a.id).state, 'waiting')
        gl.get_mr = real
        r._wake_budgeted(r.store.get(a.id))
        got = r.store.get(a.id)
        self.assertEqual(got.state, 'ready')
        self.assertIn('was sent back', got.wake_text)

    def test_standing_waits_that_may_still_complete_stay(self):
        cases = (
            ('green', mr_row('success', labels=[]), merged_line()),          # a reviewer may still label it
            ('running', mr_row('running'), success_line(status='running')),
            ('change', mr_row('failed'), f'⏳ WAIT mr:{MR} until=change after={SHA}:14845:failed'),
        )
        for name, mr, line in cases:
            with self.subTest(name=name):
                self.tearDown(), self.setUp()
                r, t, gl, a = self.told_then_standing(line)
                gl.mr_rows[MR] = mr
                for _ in range(2):
                    r._wake_budgeted(r.store.get(a.id))
                self.assertEqual(r.store.get(a.id).state, 'waiting')
                r.store.db.close()

    def test_a_manual_head_is_left_to_its_reviewer(self):
        r, t, gl, a = self.told_then_standing(success_line())
        gl.mr_rows[MR] = mr_row('manual')
        for line in (success_line(status='manual'), merged_line()):
            with self.subTest(line=line):
                o = parse_line(line, dt.datetime.now(dt.timezone.utc))
                self.assertEqual(r._dead_wait_text(a, 'mr', MR, o.extra.get('ci_change'), o.extra.get('mr_state'),
                                                   parked=True), '')


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
        self.assertIn('is behind its target branch', event)
        self.assertNotEqual(first['key'], second['key'])

    def test_behind_mr_gives_no_notice_behind_a_merge_gate(self):
        gl = FakeGitLab()
        gl.mr_rows[MR] = mr_row('success', detailed_merge_status='need_rebase')
        self.assertEqual(typed_event(gl, 'mr', MR, self.wait(), '', {}, allow_action_required=True,
                                     merge_gate=True), '')
        event = typed_event(gl, 'mr', MR, self.wait(), '', {}, allow_action_required=True, merge_gate=False)
        self.assertIn('is behind its target branch', event)
        self.assertIn('merge the target branch into the MR branch', event)

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
