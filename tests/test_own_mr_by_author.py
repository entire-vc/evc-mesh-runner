"""An MR authored by the agent's own GitLab user is the task's own MR in the unattended-wait
check, even when it is not linked to the task and does not name the task id."""
import tempfile
import unittest
from pathlib import Path

from mesh_runner import config as config_mod
from mesh_runner.config import AgentConfig, ConfigError
from test_runner import Bench
from test_unattended_ci_wait import MR, UnattendedWait, change_line, merged_line, mr_row


def authored(username, status='manual', **kw):
    row = mr_row(status, **kw)
    row.update({'title': 'fix: refresh the index', 'description': 'no task id here',
                'source_branch': 'fix/checkpoint-refresh-index'})
    if username is not None:
        row['author'] = {'id': 7, 'username': username}
    return row


class OwnMrByAuthor(Bench):
    flow, turn, assert_bounced = UnattendedWait.flow, UnattendedWait.turn, UnattendedWait.assert_bounced

    def bounced(self, a, r, t, until='until=merged', status='manual'):
        self.assert_bounced(a, r, t, f'MR {MR} has no review label (verified-by:...)',
                            f'head pipeline 14845 is {status}', 'nobody will merge it',
                            f'{until} will not arrive on its own')

    def test_mr_authored_by_the_agent_is_sent_back(self):
        cases = {
            'merged_manual': (authored('builder'), merged_line(), 'until=merged', 'manual'),
            'merged_success': (authored('builder', 'success'), merged_line(), 'until=merged', 'success'),
            'change_success': (authored('builder', 'success'), change_line('success'), 'until=change',
                               'success'),
            'case_insensitive': (authored('Builder'), merged_line(), 'until=merged', 'manual'),
        }
        for name, (mr, line, until, status) in cases.items():
            with self.subTest(name=name):
                self.tearDown(), self.setUp()
                r, t, gl = self.flow(mr=mr, linked=False)
                a = self.turn(r, t, line)
                self.bounced(a, r, t, until, status)
                r.store.db.close()

    def test_mr_authored_by_someone_else_is_accepted(self):
        for mr in (authored('someone-else'), authored(None), authored(''),
                   {**authored(None), 'author': 'builder'}, {**authored(None), 'author': {'username': None}}):
            with self.subTest(author=mr.get('author')):
                self.tearDown(), self.setUp()
                r, t, gl = self.flow(mr=mr, linked=False)
                a = self.turn(r, t, merged_line())
                self.assertEqual(a.state, 'waiting')
                self.assertNotIn('no review label', a.wake_text or '')
                r.store.db.close()

    def test_explicit_gitlab_username_overrides_the_name(self):
        self.agent.gitlab_username = 'ci-bot-7'
        r, t, gl = self.flow(mr=authored('ci-bot-7'), linked=False)
        a = self.turn(r, t, merged_line())
        self.bounced(a, r, t)

    def test_with_an_override_the_name_no_longer_matches(self):
        self.agent.gitlab_username = 'ci-bot-7'
        r, t, gl = self.flow(mr=authored('builder'), linked=False)
        a = self.turn(r, t, merged_line())
        self.assertEqual(a.state, 'waiting')

    def test_empty_gitlab_username_turns_the_author_rule_off(self):
        self.agent.gitlab_username = ''
        r, t, gl = self.flow(mr=authored('builder'), linked=False)
        a = self.turn(r, t, merged_line())
        self.assertEqual(a.state, 'waiting')

    def test_link_and_task_id_rules_still_apply(self):
        r, t, gl = self.flow(mr=authored('someone-else'), linked=True)
        a = self.turn(r, t, merged_line())
        self.bounced(a, r, t)
        r.store.db.close()
        self.tearDown(), self.setUp()
        r, t, gl = self.flow(mr=authored('someone-else'), linked=False)
        gl.mr_rows[MR]['source_branch'] = f"fix/{t['id'][:8]}-refresh"
        a = self.turn(r, t, merged_line())
        self.bounced(a, r, t)

    def test_reviewed_mr_by_the_agent_is_parked(self):
        r, t, gl = self.flow(mr=authored('builder', 'success', labels=['verified-by:qa']), linked=False)
        a = self.turn(r, t, merged_line())
        self.assertEqual(a.state, 'waiting')


class GitlabUsernameConfig(unittest.TestCase):
    def load(self, agent_extra=''):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        key = Path(d.name) / 'k'
        key.write_text('secret')
        key.chmod(0o600)
        p = Path(d.name) / 'runner.toml'
        p.write_text(f'[runner]\nmesh_url = "http://mesh"\nstate_dir = "{d.name}/s"\n'
                     f'[[agents]]\nname = "Builder"\nkey_file = "{key}"\nprofile_dir = "{d.name}"\n'
                     f'{agent_extra}\n')
        return config_mod.load(p).agents[0]

    def test_default_is_the_name_lower_cased(self):
        self.assertEqual(self.load().gitlab_username, 'builder')

    def test_identity_does_not_change_the_default(self):
        agent = self.load('identity = ["Builder Bot", "bot@example.com"]')
        self.assertEqual(agent.gitlab_username, 'builder')

    def test_explicit_key_is_read(self):
        self.assertEqual(self.load('gitlab_username = "ci-bot-7"').gitlab_username, 'ci-bot-7')

    def test_empty_key_turns_the_rule_off(self):
        self.assertEqual(self.load('gitlab_username = ""').gitlab_username, '')

    def test_non_string_is_rejected(self):
        with self.assertRaises(ConfigError):
            self.load('gitlab_username = 42')

    def test_dataclass_default(self):
        a = AgentConfig(name='Lane-X', key='k', harness='claude', profile_dir=Path('.'), repos=[])
        self.assertEqual(a.gitlab_username, 'lane-x')
        b = AgentConfig(name='Lane-X', key='k', harness='claude', profile_dir=Path('.'), repos=[],
                        gitlab_username='other')
        self.assertEqual(b.gitlab_username, 'other')


del Bench, UnattendedWait   # imported fixtures only: their tests run in their own modules

if __name__ == '__main__':
    unittest.main()
