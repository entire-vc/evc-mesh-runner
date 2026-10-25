"""A question asked again of the same agent from the same source reuses its request card.

Measured on one runner over a day: 57% of the filed decision requests repeated an earlier
(source, addressee) pair, many with the same question minutes apart. Each new card started
a fresh session for the addressee and added a bookkeeping card to the board.
"""
import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fakes import FakeMesh, task, write_replies
from mesh_runner.config import ConfigError, load
from mesh_runner.mesh import MeshError
from mesh_runner.store import iso, now
from test_runner import Bench


class DecisionRequestReuse(Bench):
    def setup_question(self, note='approve the plan?', handle='lead'):
        source = task(1)
        mesh = FakeMesh([source])
        r = self.runner(mesh)
        self.addCleanup(r.store.db.close)
        r.refresh_me()
        attempt = r.store.create(self.agent.name, source['id'], 'claude')
        wait = self.question(note, handle)
        r.effects.on_wait(attempt, source, wait, r.me)
        r.store.update(attempt, state='waiting', wait=wait)
        return r, mesh, source, r.store.get(attempt.id), wait

    @staticmethod
    def question(note='approve the plan?', handle='lead', ago=1):
        return {'kind': 'decision', 'handle': handle, 'note': note,
                'line': f'⏳ WAIT decision:@{handle}: {note}',
                'since': iso(now() - dt.timedelta(minutes=ago)), 'started': iso()}

    def close(self, r, attempt, wait):
        r.effects.on_end(attempt, wait, 'answered', close_request=True)

    def request_comments(self, mesh, request):
        return [c['body'] for c in mesh.comments(request) if not c['body'].startswith('[runner] closed')]

    # ------------------------------------------------------------ reuse
    def test_redeclared_same_pair_reuses_closed_request_reopened_with_comment(self):
        r, mesh, source, attempt, wait = self.setup_question()
        request = wait['request']['id']
        self.close(r, attempt, wait)
        self.assertEqual(mesh.category(mesh.tasks[request]), 'done')
        again = self.question('approve the revised plan?')
        r.effects.on_wait(attempt, source, again, r.me)
        self.assertEqual(len(mesh.created), 1)                     # no new card
        self.assertEqual(again['request']['id'], request)
        self.assertEqual(mesh.category(mesh.tasks[request]), 'todo')   # reopened
        self.assertEqual(mesh.tasks[request]['assignee_id'], 'agent-lead')   # assignee kept
        asked = self.request_comments(mesh, request)
        self.assertEqual(len(asked), 1)
        self.assertIn('approve the revised plan?', asked[0])
        self.assertIn('@lead', asked[0])
        self.assertIn(f"ON TASK #{source['id'][:8]}", asked[0])
        record = r.effects._decision(source['id'])
        self.assertEqual(record['state'], 'open')
        self.assertEqual(record['request']['id'], request)
        self.assertEqual(record['key'], r.effects._decision_key(source['id'], 'agent-lead', again))

    def test_changed_question_on_open_request_is_a_comment_not_a_supersede(self):
        r, mesh, source, attempt, wait = self.setup_question()
        request = wait['request']['id']
        changed = self.question('approve another plan?')
        r.effects.on_wait(attempt, source, changed, r.me)
        self.assertEqual(len(mesh.created), 1)
        self.assertEqual(changed['request']['id'], request)
        self.assertEqual(mesh.category(mesh.tasks[request]), 'todo')
        self.assertEqual(len(self.request_comments(mesh, request)), 1)

    def test_different_addressee_files_a_new_request(self):
        r, mesh, source, attempt, wait = self.setup_question()
        first = wait['request']['id']
        self.close(r, attempt, wait)
        other = self.question('approve the plan?', 'reviewer')
        r.effects.on_wait(attempt, source, other, r.me)
        self.assertEqual(len(mesh.created), 2)
        self.assertNotEqual(other['request']['id'], first)
        self.assertEqual(mesh.tasks[other['request']['id']]['assignee_id'], 'agent-reviewer')
        self.assertEqual(mesh.category(mesh.tasks[first]), 'done')       # not reopened
        # back to the first addressee: its own request is reused, the reviewer's superseded
        back = self.question('approve the plan, second round?')
        r.effects.on_wait(attempt, source, back, r.me)
        self.assertEqual(len(mesh.created), 2)
        self.assertEqual(back['request']['id'], first)
        self.assertEqual(mesh.category(mesh.tasks[other['request']['id']]), 'done')

    def test_different_source_files_a_new_request(self):
        r, mesh, source, attempt, wait = self.setup_question()
        first = wait['request']['id']
        self.close(r, attempt, wait)
        second = task(2)
        mesh.tasks[second['id']] = second
        other = r.store.create(self.agent.name, second['id'], 'claude')
        again = self.question()
        r.effects.on_wait(other, second, again, r.me)
        self.assertEqual(len(mesh.created), 2)
        self.assertNotEqual(again['request']['id'], first)

    def test_window_zero_keeps_filing_new_requests(self):
        self.cfg.decision_reuse_hours = 0
        r, mesh, source, attempt, wait = self.setup_question()
        first = wait['request']['id']
        self.close(r, attempt, wait)
        again = self.question()
        r.effects.on_wait(attempt, source, again, r.me)
        self.assertEqual(len(mesh.created), 2)
        self.assertNotEqual(again['request']['id'], first)
        self.assertEqual(mesh.category(mesh.tasks[first]), 'done')
        mesh.add_comment(source['id'], 'not yet', author='agent-lead')
        self.assertEqual(r.effects.answered_repeat(attempt, 'lead', 'approve the plan?',
                                                   mesh.comments(source['id']), r.me), '')

    def test_request_older_than_the_window_is_not_reused(self):
        r, mesh, source, attempt, wait = self.setup_question()
        self.close(r, attempt, wait)
        later = now() + dt.timedelta(hours=25)
        with patch('mesh_runner.effects.now', return_value=later):
            again = self.question('approve the plan, a day later?')
            r.effects.on_wait(attempt, source, again, r.me)
        self.assertEqual(len(mesh.created), 2)

    def test_expired_entry_of_the_current_recipient_is_pruned(self):
        r, mesh, source, attempt, wait = self.setup_question()
        first = wait['request']['id']
        self.assertIn('agent-lead', r.effects._recent(source['id']))
        later = now() + dt.timedelta(hours=25)
        with patch('mesh_runner.effects.now', return_value=later):
            self.close(r, attempt, wait)           # saved again for the same recipient, expired
            self.assertEqual(r.effects._recent(source['id']), {})
            self.assertIsNone(r.effects._reusable(source['id'], 'agent-lead', {}))
            again = self.question('approve the plan, a day later?')
            r.effects.on_wait(attempt, source, again, r.me)
        self.assertEqual(len(mesh.created), 2)
        self.assertNotEqual(again['request']['id'], first)
        self.assertEqual(mesh.category(mesh.tasks[first]), 'done')

    def test_reassigned_request_is_not_reused(self):
        r, mesh, source, attempt, wait = self.setup_question()
        request = wait['request']['id']
        self.close(r, attempt, wait)
        mesh.tasks[request]['assignee_id'] = 'agent-reviewer'
        again = self.question('approve the revised plan?')
        r.effects.on_wait(attempt, source, again, r.me)
        self.assertEqual(len(mesh.created), 2)
        self.assertEqual(mesh.category(mesh.tasks[request]), 'done')

    def test_failed_comment_is_retried_without_a_new_card(self):
        r, mesh, source, attempt, wait = self.setup_question()
        request = wait['request']['id']
        self.close(r, attempt, wait)
        again = self.question('approve the revised plan?')
        with patch.object(mesh, 'add_comment', side_effect=MeshError(503, 'unavailable')):
            r.effects.on_wait(attempt, source, again, r.me)
        self.assertFalse(again['fx'])                               # adopt() retries it
        self.assertNotIn('request', again)
        self.assertEqual(len(mesh.created), 1)
        self.assertTrue(r.effects.adopt(attempt, source, again, r.me))
        self.assertEqual(again['request']['id'], request)
        self.assertEqual(len(mesh.created), 1)
        self.assertEqual(len(self.request_comments(mesh, request)), 1)

    def test_person_handle_is_never_reused_or_sent_back(self):
        r, mesh, source, attempt, wait = self.setup_question(handle='owner')
        self.assertNotIn('request', wait)
        self.assertEqual(mesh.created, [])
        mesh.add_comment(source['id'], 'not yet', author='owner')
        self.assertEqual(r.effects.answered_repeat(attempt, 'owner', 'approve the plan?',
                                                   mesh.comments(source['id']), r.me), '')

    # ------------------------------------------------------------ verbatim repeat
    def test_verbatim_repeat_after_an_answer_is_sent_back_quoting_it(self):
        r, mesh, source, attempt, wait = self.setup_question()
        answer = 'Not yet: the release is still being cut. ' + 'x' * 400
        mesh.add_comment(source['id'], answer, author='agent-lead')
        self.close(r, attempt, wait)
        told = r.effects.answered_repeat(attempt, 'lead', '  Approve   THE plan? ',
                                         mesh.comments(source['id']), r.me)
        self.assertIn('@lead already answered this exact question', told)
        self.assertIn('Not yet: the release is still being cut.', told)
        quoted = told.split('«', 1)[1].split('»', 1)[0]
        self.assertLessEqual(len(quoted), 300)
        self.assertIn('same answer', told)
        self.assertIn('⏳ WAIT card:#', told)
        self.assertIn('different question', told)
        # a changed question is asked, not sent back
        self.assertEqual(r.effects.answered_repeat(attempt, 'lead', 'approve plan B?',
                                                   mesh.comments(source['id']), r.me), '')

    def test_verbatim_repeat_without_an_answer_is_reused_not_sent_back(self):
        # the reopen path; with decision_identical_no_reopen (default) the same question is attached
        # to the closed request instead: test_decision_waste
        self.cfg.decision_identical_no_reopen = False
        r, mesh, source, attempt, wait = self.setup_question()
        request = wait['request']['id']
        mesh.add_comment(source['id'], 'Decision: someone else decided', author='bystander')
        self.close(r, attempt, wait)
        self.assertEqual(r.effects.answered_repeat(attempt, 'lead', 'approve the plan?',
                                                   mesh.comments(source['id']), r.me), '')
        again = self.question()
        r.effects.on_wait(attempt, source, again, r.me)
        self.assertEqual(len(mesh.created), 1)
        self.assertEqual(again['request']['id'], request)
        self.assertEqual(mesh.category(mesh.tasks[request]), 'todo')

    def test_answer_older_than_the_question_does_not_send_back(self):
        r, mesh, source, attempt, wait = self.setup_question()
        mesh.add_comment(source['id'], 'an earlier remark', author='agent-lead',
                         at=now() - dt.timedelta(minutes=30))
        self.close(r, attempt, wait)
        self.assertEqual(r.effects.answered_repeat(attempt, 'lead', 'approve the plan?',
                                                   mesh.comments(source['id']), r.me), '')

    def test_addressee_comment_before_the_reuse_comment_does_not_send_back(self):
        # the wait was declared 5 min ago, the addressee wrote 2 min ago, the question reaches it
        # only now (the reuse comment): that earlier note is no answer to it
        r, mesh, source, attempt, wait = self.setup_question()
        first = wait['request']['id']
        self.close(r, attempt, wait)
        again = self.question('approve the revised plan?', ago=5)
        mesh.add_comment(source['id'], 'still looking at the first plan', author='agent-lead',
                         at=now() - dt.timedelta(minutes=2))
        r.effects.on_wait(attempt, source, again, r.me)
        self.assertEqual(again['request']['id'], first)            # reused
        self.assertEqual(len(mesh.created), 1)
        self.assertEqual(r.effects.answered_repeat(attempt, 'lead', 'approve the revised plan?',
                                                   mesh.comments(source['id']), r.me), '')
        # a comment after the reuse comment is an answer
        mesh.add_comment(source['id'], 'Not yet.', author='agent-lead', at=now() + dt.timedelta(seconds=1))
        self.assertIn('Not yet.', r.effects.answered_repeat(attempt, 'lead', 'approve the revised plan?',
                                                            mesh.comments(source['id']), r.me))

    def test_addressee_comment_before_a_new_request_exists_does_not_send_back(self):
        source = task(1)
        mesh = FakeMesh([source])
        r = self.runner(mesh)
        self.addCleanup(r.store.db.close)
        r.refresh_me()
        attempt = r.store.create(self.agent.name, source['id'], 'claude')
        wait = self.question(ago=5)
        mesh.add_comment(source['id'], 'an unrelated note', author='agent-lead',
                         at=now() - dt.timedelta(minutes=2))
        r.effects.on_wait(attempt, source, wait, r.me)
        self.assertEqual(len(mesh.created), 1)
        self.assertEqual(r.effects.answered_repeat(attempt, 'lead', 'approve the plan?',
                                                   mesh.comments(source['id']), r.me), '')

    def test_runner_sends_back_a_verbatim_repeat_at_declaration(self):
        source = task(1)
        mesh = FakeMesh([source], max_concurrent=1)
        r = self.runner(mesh)
        self.addCleanup(r.store.db.close)
        line = '⏳ WAIT decision:@lead: approve the plan?'
        write_replies(self.replies, {source['id'][:8]: line})
        r.tick()
        attempt = self.finish_turn(r)
        self.assertEqual(attempt.state, 'waiting')
        request = attempt.wait['request']['id']
        mesh.add_comment(source['id'], 'Not yet: waiting for the release.', author='agent-lead')
        r._wake_one(attempt)
        self.assertEqual(mesh.category(mesh.tasks[request]), 'done')
        r.tick()
        attempt = self.finish_turn(r)
        self.assertEqual(attempt.state, 'ready')
        self.assertIn('already answered this exact question', attempt.wake_text)
        self.assertIn('Not yet: waiting for the release.', attempt.wake_text)
        self.assertEqual(len(mesh.created), 1)
        self.assertEqual(mesh.category(mesh.tasks[request]), 'done')   # not reopened

    # ------------------------------------------------------------ reconcile
    def test_reconcile_finds_the_reused_request_after_a_restart(self):
        r, mesh, source, attempt, wait = self.setup_question()
        request = wait['request']['id']
        self.close(r, attempt, wait)
        restarted = self.runner(mesh)               # the remembered request survives a restart
        self.addCleanup(restarted.store.db.close)
        restarted.refresh_me()
        again = self.question('approve the revised plan?')
        restarted.effects.on_wait(attempt, source, again, restarted.me)
        self.assertEqual(again['request']['id'], request)
        self.assertEqual(len(mesh.created), 1)
        restarted.store.update(attempt, wait=again)
        # another restart: the same question binds to the reused request again
        third = self.runner(mesh)
        self.addCleanup(third.store.db.close)
        third.refresh_me()
        same = dict(again)
        same.pop('request')
        third.effects.on_wait(attempt, source, same, third.me)
        self.assertEqual(same['request']['id'], request)
        self.assertEqual(len(mesh.created), 1)
        # and the source closing closes it through reconcile
        mesh.set_category(source['id'], 'done')
        third.effects.reconcile_decisions()
        self.assertEqual(mesh.category(mesh.tasks[request]), 'done')
        self.assertFalse(third.effects._decision(source['id']))


class ReuseConfig(unittest.TestCase):
    def read(self, runner_extra=''):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'runner.toml'
            (Path(tmp) / 'p').mkdir()
            p.write_text('[runner]\nmesh_url = "x"\nstate_dir = "%s"\n%s\n[[agents]]\nname = "a"\n'
                         'key_env = "K"\nprofile_dir = "%s/p"\n' % (tmp, runner_extra, tmp))
            os.environ.setdefault('K', 'k')
            return load(p)

    def test_default_value_and_off(self):
        self.assertEqual(self.read().decision_reuse_hours, 24.0)
        self.assertEqual(self.read('decision_reuse_hours = 0').decision_reuse_hours, 0.0)
        self.assertEqual(self.read('decision_reuse_hours = 6.5').decision_reuse_hours, 6.5)

    def test_bad_values_are_refused(self):
        for bad in ('-1', '"24"', 'true', 'nan'):
            with self.subTest(bad), self.assertRaises(ConfigError):
                self.read(f'decision_reuse_hours = {bad}')


if __name__ == '__main__':
    unittest.main()
