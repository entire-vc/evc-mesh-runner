import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mesh_runner.store import Store
from mesh_runner.workspace import WorkspaceError, WorkspacePool, RepoSpec, git
from test_workspace import make_repo

CAS = "error: cannot lock ref 'refs/remotes/origin/main': is at " + "a" * 40 + " but expected " + "b" * 40
STATUS = " ! abc123..def456 main -> origin/main  (unable to update local ref)"


def result(code=1, stderr=CAS, stdout=""):
    return subprocess.CompletedProcess(["git"], code, stdout, stderr)


class FetchCAS(unittest.TestCase):
    def test_retry_keeps_exact_arguments_environment_and_original_timeout_budget(self):
        with patch("mesh_runner.workspace.subprocess.run", side_effect=[result(), result(0, "", "ok\n")]) as run, patch(
                "mesh_runner.workspace.time.monotonic", side_effect=[100, 100, 103]):
            self.assertEqual(git(Path('/repo'), 'fetch', 'origin', '--prune', '--quiet',
                                 '--recurse-submodules=on-demand', env={'CUSTOM': 'value'}, timeout=10), 'ok')
        self.assertEqual(run.call_count, 2)
        first, second = run.call_args_list
        self.assertEqual(first.args, second.args)
        self.assertEqual(first.kwargs['env'], second.kwargs['env'])
        self.assertEqual(second.kwargs['env']['GIT_TERMINAL_PROMPT'], '0')
        self.assertEqual([first.kwargs['timeout'], second.kwargs['timeout']], [10, 7])

    def test_classifies_full_stderr_and_allows_normal_fetch_summary(self):
        stderr = 'From ' + 'x' * 350 + '\n' + CAS + '\n' + STATUS + '\n'
        with patch('mesh_runner.workspace.subprocess.run', side_effect=[result(stderr=stderr), result(0, '')]) as run:
            git(Path('/repo'), 'fetch', 'origin', '--quiet')
        self.assertEqual(run.call_count, 2)

    def test_persistent_cas_fails_after_one_retry(self):
        with patch('mesh_runner.workspace.subprocess.run', return_value=result()) as run:
            with self.assertRaises(WorkspaceError):
                git(Path('/repo'), 'fetch', 'origin')
        self.assertEqual(run.call_count, 2)

    def test_other_failures_and_mixed_errors_are_not_retried(self):
        errors = [
            "fatal: couldn't find remote ref gone",
            "fatal: remote error: upload-pack: not our ref " + 'a' * 40,
            "fatal: Authentication failed",
            "error: cannot lock ref 'refs/remotes/origin/main': Unable to create 'main.lock': File exists.",
            "Fetching submodule child\nfatal: not our ref " + 'a' * 40,
            CAS + '\nfatal: Authentication failed',
            CAS + '\nFetching submodule child\nErrors during submodule fetch:\n child',
            CAS + '\nerror: unable to read askpass response',
            CAS.replace('b' * 40, 'a' * 40),
            CAS.replace('b' * 40, 'b' * 64),
            CAS + '\nunknown failure diagnostic',
        ]
        for stderr in errors:
            with self.subTest(stderr=stderr), patch('mesh_runner.workspace.subprocess.run', return_value=result(stderr=stderr)) as run:
                with self.assertRaises(WorkspaceError):
                    git(Path('/repo'), 'fetch', 'origin')
                self.assertEqual(run.call_count, 1)

    def test_retry_failure_is_not_hidden(self):
        with patch('mesh_runner.workspace.subprocess.run', side_effect=[result(), result(stderr='fatal: no ref')]) as run:
            with self.assertRaisesRegex(WorkspaceError, 'fatal: no ref'):
                git(Path('/repo'), 'fetch', 'origin')
        self.assertEqual(run.call_count, 2)

    def test_unchecked_and_nonfetch_commands_never_retry(self):
        with patch('mesh_runner.workspace.subprocess.run', return_value=result()) as run:
            self.assertEqual(git(Path('/repo'), 'fetch', 'origin', check=False), '')
            with self.assertRaises(WorkspaceError):
                git(Path('/repo'), 'update-ref', 'refs/heads/main', 'HEAD')
        self.assertEqual(run.call_count, 2)

    def test_timeout_does_not_retry_or_expand_budget(self):
        for side_effect in ([subprocess.TimeoutExpired('git', 10)], [result(), subprocess.TimeoutExpired('git', 10)]):
            with self.subTest(calls=len(side_effect)), patch('mesh_runner.workspace.subprocess.run', side_effect=side_effect) as run:
                with self.assertRaisesRegex(WorkspaceError, 'timed out'):
                    git(Path('/repo'), 'fetch', 'origin', timeout=10)
                self.assertEqual(run.call_count, len(side_effect))
        with patch('mesh_runner.workspace.subprocess.run', return_value=result()) as run, patch(
                'mesh_runner.workspace.time.monotonic', side_effect=[100, 100, 111]):
            with self.assertRaisesRegex(WorkspaceError, 'timed out'):
                git(Path('/repo'), 'fetch', 'origin', timeout=10)
        self.assertEqual(run.call_count, 1)

    def test_real_pool_lease_recovers_and_does_not_allocate_before_fetch_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = make_repo(root)
            store = Store(root / 's.sqlite')
            pool = WorkspacePool(store, root / 'pool', [RepoSpec('app', base)])
            original_run = subprocess.run
            failures = 0
            def fetch_race(argv, **kwargs):
                nonlocal failures
                if len(argv) > 3 and argv[3] == 'fetch' and failures == 0:
                    failures += 1
                    self.assertEqual(pool.rows(), [])
                    return result()
                return original_run(argv, **kwargs)
            with patch('mesh_runner.workspace.subprocess.run', side_effect=fetch_race):
                path = pool.lease('attempt', 'app')
            self.assertTrue((path / 'README').exists())
            self.assertEqual(len(pool.rows()), 1)
            self.assertEqual(pool.rows()[0]['attempt'], 'attempt')
            store.db.close()


if __name__ == '__main__':
    unittest.main()
