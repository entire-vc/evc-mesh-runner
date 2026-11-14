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

from fakes import FakeMesh, ask_agent, task, write_replies
from mesh_runner.config import load
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

    def test_person_handle_is_never_reused(self):
        r, mesh, source, attempt, wait = self.setup_question(handle='owner')
        self.assertNotIn('request', wait)
        self.assertEqual(mesh.created, [])

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

    def test_default_value_and_a_retired_key_is_ignored(self):
        # decision_reuse_hours is no longer a [runner] option: it stays 24 whatever a file says
        self.assertEqual(self.read().decision_reuse_hours, 24.0)
        for value in ('0', '6.5', '-1', '"24"', 'true', 'nan'):
            with self.subTest(value), self.assertLogs('mesh_runner', 'WARNING') as logs:
                self.assertEqual(self.read(f'decision_reuse_hours = {value}').decision_reuse_hours, 24.0)
            self.assertIn('decision_reuse_hours is no longer an option', '\n'.join(logs.output))


if __name__ == '__main__':
    unittest.main()
