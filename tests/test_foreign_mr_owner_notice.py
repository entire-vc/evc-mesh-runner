"""A card that waits for the merge of ANOTHER card's MR is never told to push the fix itself.

Live: one lane held two cards at once. Card A owned an MR (linked to A, its branch named A); card B
only waited for that MR to merge. When the MR head went red the runner woke both with the owner
notice ("The owner must push a fix ... fix the cause and push"). Both sessions wrote the same fix;
B pushed first into A's branch, and A's push was refused as non-fast-forward. Two live sessions on
one MR branch is the defect.

The owner notice (red head, merge conflict, failed named jobs) and the declaration send-backs now go
by the MR's owner: the task's own MR gets them as before; a waiter on someone else's MR is told whose
MR it is and to wait for that MR to merge (never on its card), once per head; a re-armed merge or success wait on it stands, and
an until=change wait on its red head is sent back without a push instruction."""
import unittest

from fakes import FakeGitLab, FakeMesh, task, wait_mr, write_replies
from test_runner import Bench

SHA = 'a' * 40
SHA2 = 'b' * 40
MR = 'g/app!56'
MR_URL = 'https://git.example/g/app/-/merge_requests/56'
LINK = {'url': MR_URL, 'link_type': 'pr', 'status': 'open'}
PUSH_INSTRUCTIONS = ('fix the cause and push', 'Fix the cause and push', 'open the failed job',
                     'The owner must push a fix', 'merge the target branch into the MR branch',
                     'fix and push', 'Act on the result (fix')


def lane_mr(owner8, status='failed', sha=SHA, pid=14845, **extra):
    """An MR the lane's GitLab user opened for card ``owner8``, as the lane names its branches."""
    row = {'sha': sha, 'state': 'opened', 'labels': [], 'title': 'ci: explicit anchor occurrences',
           'description': 'see the card', 'source_branch': f'builder/{owner8}-anchor-occurrence',
           'author': {'id': 7, 'username': 'builder'},
           'head_pipeline': {'sha': sha, 'id': pid, 'status': status}}
    row.update(extra)
    return row


def merged_line(sha=SHA):
    return wait_mr('g/app', 56, until='merged', after_sha=sha, after_status='opened')


def change_line(status='failed', jobs=()):
    return wait_mr('g/app', 56, until='changed', jobs=jobs, after_sha=SHA, after_pipeline_id=14845,
                   after_status=status)


def jobs_line(until='green', status='running'):
    return wait_mr('g/app', 56, until=until, jobs=['test'], after_sha=SHA, after_pipeline_id=14845,
                   after_status=status)


class TwoCardsOfOneLaneOnOneMR(Bench):
    def flow(self, mr_status='failed', linked=True, owner_elsewhere=False, **extra):
        # owner_elsewhere: another lane holds the owner card now (found by the id in the branch)
        owner = task(1, cat='review', assignee_id='reviewer-agent-id') if owner_elsewhere else task(1)
        dependent = task(2)
        mesh = FakeMesh([owner, dependent], max_concurrent=2)
        if linked:
            mesh.links = {owner['id']: [dict(LINK)]}
        gl = FakeGitLab()
        gl.mr_rows[MR] = lane_mr(owner['id'][:8], mr_status, **extra)
        r = self.runner(mesh, gl)
        self.addCleanup(r.store.db.close)
        r.refresh_me()
        return r, owner, dependent, gl

    def declare(self, r, t, line, first=True):
        write_replies(self.replies, {t['id'][:8]: line, t['id'][:8] + ':resume': line})
        self.assertEqual(r.start_new(1) if first else r.resume_ready(1), 0)
        return self.finish_turn(r)

    def both_waiting(self, r, owner, dependent):
        """Both cards hold a live session that waits for the merge of the owner's MR."""
        a = self.declare(r, owner, merged_line())
        b = self.declare(r, dependent, merged_line())
        self.assertEqual((a.task_id, a.state), (owner['id'], 'waiting'))
        self.assertEqual((b.task_id, b.state), (dependent['id'], 'waiting'))
        return a, b

    def woken(self, r, a):
        r._wake_one(a)
        return r.store.get(a.id)

    def assert_merge_wait(self, text):
        # its code is the MR: a merge wait or a stack on its branch, never a card wait
        project, _, iid = MR.rpartition('!')
        self.assertIn(f"wait_for on=mr project_path={project} mr_iid={iid} until=merged", text)
        self.assertNotIn('wait_for on=card', text)

    def assert_no_push(self, text):
        for needle in PUSH_INSTRUCTIONS:
            self.assertNotIn(needle, text)

    # -- the live case: a red head ----------------------------------------------------------

    def test_red_head_only_the_owner_is_told_to_push(self):
        r, owner, dependent, gl = self.flow('failed')
        a, b = self.both_waiting(r, owner, dependent)
        a, b = self.woken(r, a), self.woken(r, b)
        # the owner keeps its notice (positive control: the same MR, the same red head)
        self.assertEqual(a.state, 'ready')
        self.assertIn('action required', a.wake_text)
        self.assertIn('open the failed job, fix the cause and push', a.wake_text)
        # the dependent learns whose MR it is and where to wait, never to push into it
        self.assertEqual(b.state, 'ready')
        self.assertIn(f"not this task's: it is the MR of card #{owner['id'][:8]}", b.wake_text)
        self.assert_merge_wait(b.wake_text)
        self.assertIn('do not push, merge or rebase its branch', b.wake_text)
        self.assert_no_push(b.wake_text)

    def test_the_dependent_hears_it_once_per_head_and_a_rearmed_wait_stands(self):
        r, owner, dependent, gl = self.flow('failed')
        a, b = self.both_waiting(r, owner, dependent)
        b = self.woken(r, b)
        self.assertEqual(b.state, 'ready')
        b = self.declare(r, dependent, merged_line(), first=False)
        self.assertEqual(b.state, 'waiting')            # not sent back to "push a fix"
        b = self.woken(r, b)
        self.assertEqual(b.state, 'waiting')            # the same red head is not told twice
        gl.mr_rows[MR] = lane_mr(owner['id'][:8], 'success', sha=SHA2, pid=14900)
        gl.mr_rows[MR]['state'], gl.mr_rows[MR]['merged_at'] = 'merged', '2026-11-16T01:22:20Z'
        b = self.woken(r, b)
        self.assertEqual(b.state, 'ready')              # the merge still wakes it
        self.assertIn(f'mr {MR}: merged', b.wake_text)

    def test_owner_found_among_the_lane_open_cards_without_a_name(self):
        # the MR names no card: the owner is the lane's other open card that links it
        r, owner, dependent, gl = self.flow('failed', source_branch='ci/anchor-occurrence')
        a, b = self.both_waiting(r, owner, dependent)
        b = self.woken(r, b)
        self.assert_merge_wait(b.wake_text)
        self.assert_no_push(b.wake_text)

    def test_unlinked_foreign_mr_names_no_card_and_still_no_push(self):
        r, owner, dependent, gl = self.flow('failed', linked=False,
                                            author={'id': 9, 'username': 'someone-else'})
        a, b = self.both_waiting(r, owner, dependent)
        b = self.woken(r, b)
        self.assertEqual(b.state, 'ready')
        self.assertIn('not linked to this card and does not name it', b.wake_text)
        self.assertIn('`add_vcs_link`', b.wake_text)
        self.assert_no_push(b.wake_text)

    # -- a merge conflict -----------------------------------------------------------------

    def test_conflict_the_owner_is_still_sent_back_to_merge_the_target(self):
        r, owner, dependent, gl = self.flow('success', has_conflicts=True)
        a = self.declare(r, owner, merged_line())
        self.assertEqual((a.task_id, a.state), (owner['id'], 'ready'))     # sent back at declaration
        self.assertIn('merge the target branch into the MR branch', a.wake_text)

    def test_conflict_the_dependent_is_told_whose_mr_it_is(self):
        r, owner, dependent, gl = self.flow('success', owner_elsewhere=True, has_conflicts=True)
        b = self.declare(r, dependent, merged_line())
        self.assertEqual((b.task_id, b.state), (dependent['id'], 'waiting'))   # its wait stands
        b = self.woken(r, b)
        self.assertEqual(b.state, 'ready')
        self.assertIn('has a merge conflict with its target branch', b.wake_text)
        self.assert_merge_wait(b.wake_text)
        self.assert_no_push(b.wake_text)
        b = self.declare(r, dependent, merged_line(), first=False)
        self.assertEqual(b.state, 'waiting')
        self.assertEqual(self.woken(r, b).state, 'waiting')     # once per head and cause

    # -- an until=change wait on a red head -----------------------------------------------

    def test_change_wait_on_a_red_head_only_the_owner_is_asked_for_the_fix(self):
        r, owner, dependent, gl = self.flow('failed')
        a = self.declare(r, owner, change_line())
        self.assertEqual((a.task_id, a.state), (owner['id'], 'ready'))       # positive control
        self.assertIn('Fix the cause and push', a.wake_text)
        b = self.declare(r, dependent, change_line())
        self.assertEqual((b.task_id, b.state), (dependent['id'], 'ready'))   # sent back as well
        self.assertIn("nothing changes without the MR owner's push or retry", b.wake_text)
        self.assert_merge_wait(b.wake_text)
        self.assert_no_push(b.wake_text)

    def test_change_wait_woken_by_a_red_result_names_the_owner_of_someone_elses_mr(self):
        r, owner, dependent, gl = self.flow('running')
        a = self.declare(r, owner, change_line('running'))
        b = self.declare(r, dependent, change_line('running'))
        self.assertEqual((a.state, b.state), ('waiting', 'waiting'))
        gl.mr_rows[MR] = lane_mr(owner['id'][:8], 'failed')
        a, b = self.woken(r, a), self.woken(r, b)
        self.assertEqual((a.state, b.state), ('ready', 'ready'))
        self.assertIn('changed CI aaaaaaaa pipeline 14845 failed', a.wake_text)
        self.assertNotIn("This MR is not this task's", a.wake_text)             # the owner: as before
        self.assertIn('changed CI aaaaaaaa pipeline 14845 failed', b.wake_text)
        self.assert_merge_wait(b.wake_text)
        self.assert_no_push(b.wake_text)

    # -- a wait on named jobs of the MR ---------------------------------------------------

    def jobs_flow(self):
        r, owner, dependent, gl = self.flow('running')
        gl.jobs['g/app#14845'] = [{'id': 1, 'name': 'test', 'status': 'running'}]
        a = self.declare(r, owner, jobs_line())
        b = self.declare(r, dependent, jobs_line())
        self.assertEqual((a.state, b.state), ('waiting', 'waiting'))
        gl.jobs['g/app#14845'] = [{'id': 1, 'name': 'test', 'status': 'failed'}]
        gl.mr_rows[MR] = lane_mr(owner['id'][:8], 'failed')
        return r, owner, dependent, gl, a, b

    def test_failed_named_job_only_the_owner_is_told_to_fix(self):
        r, owner, dependent, gl, a, b = self.jobs_flow()
        a, b = self.woken(r, a), self.woken(r, b)
        self.assertEqual(a.state, 'ready')                                   # positive control
        self.assertIn('fix and push or retry it', a.wake_text)
        self.assertEqual(b.state, 'ready')
        self.assertIn('job(s) test finished: failed', b.wake_text)
        self.assert_merge_wait(b.wake_text)
        self.assert_no_push(b.wake_text)

    def test_named_job_success_wait_declared_again_on_someone_elses_failed_job_stands(self):
        # one ready attempt at a time: resume_ready would pick the other card's
        r, owner, dependent, gl, a, b = self.jobs_flow()
        b = self.woken(r, b)
        self.assertEqual(b.state, 'ready')
        b = self.declare(r, dependent, jobs_line(), first=False)
        self.assertEqual((b.task_id, b.state), (dependent['id'], 'waiting'))
        self.assertEqual(self.woken(r, b).state, 'waiting')                  # told once per head
        a = self.woken(r, a)
        a = self.declare(r, owner, jobs_line(), first=False)
        self.assertEqual((a.task_id, a.state), (owner['id'], 'ready'))      # the owner acts on it
        self.assertIn('Act on the result (fix', a.wake_text)

    def test_named_job_change_wait_on_someone_elses_failed_job_is_sent_back_without_a_push(self):
        r, owner, dependent, gl = self.flow('running')
        gl.jobs['g/app#14845'] = [{'id': 1, 'name': 'test', 'status': 'running'}]
        a = self.declare(r, owner, merged_line())
        b = self.declare(r, dependent, jobs_line('changed'))
        self.assertEqual((a.state, b.state), ('waiting', 'waiting'))
        gl.jobs['g/app#14845'] = [{'id': 1, 'name': 'test', 'status': 'failed'}]
        gl.mr_rows[MR] = lane_mr(owner['id'][:8], 'failed')
        b = self.woken(r, b)
        self.assertEqual(b.state, 'ready')                                   # the change itself
        self.assertIn('job(s) test finished: failed', b.wake_text)
        self.assert_merge_wait(b.wake_text)
        self.assert_no_push(b.wake_text)
        b = self.declare(r, dependent, change_line('failed', jobs=['test']), first=False)
        self.assertEqual((b.task_id, b.state), (dependent['id'], 'ready'))  # told: sent back
        self.assertIn("nothing changes without the MR owner's push or retry", b.wake_text)
        self.assert_merge_wait(b.wake_text)
        self.assert_no_push(b.wake_text)

if __name__ == '__main__':
    unittest.main()
