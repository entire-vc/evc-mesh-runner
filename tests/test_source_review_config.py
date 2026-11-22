"""Source-review discovery is explicitly opt-in and has a finite process deadline."""
from pathlib import Path
import tempfile
import unittest

from mesh_runner.config import load


class SourceReviewConfig(unittest.TestCase):
    def config(self, agent_options=''):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = root / 'key'
            key.write_text('fixture')
            key.chmod(0o600)
            path = root / 'runner.toml'
            path.write_text(f'[runner]\nmesh_url="http://mesh.test"\nstate_dir="{tmp}"\n'
                            f'[[agents]]\nname="reviewer"\nkey_file="{key}"\nprofile_dir="{tmp}"\n'
                            + agent_options)
            return load(path).agents[0]

    def test_off_by_default(self):
        self.assertEqual(self.config().source_review_command, [])

    def test_explicit_helper_and_timeout(self):
        agent = self.config('source_review_command=["python3", "helper.py"]\n'
                            'source_review_timeout_seconds=12.5\n')
        self.assertEqual(agent.source_review_command, ['python3', 'helper.py'])
        self.assertEqual(agent.source_review_timeout_seconds, 12.5)

    def test_malformed_argv_and_unbounded_deadlines_are_refused(self):
        for value in ('"helper.py"', '[""]', '[1]', 'true'):
            with self.subTest(argv=value), self.assertRaisesRegex(ValueError, 'source_review_command'):
                self.config('source_review_command=' + value + '\n')
        for value in ('0', '-1', 'true', 'inf', 'nan', '"60"'):
            with self.subTest(timeout=value), self.assertRaisesRegex(ValueError, 'source_review_timeout_seconds'):
                self.config('source_review_timeout_seconds=' + value + '\n')


if __name__ == '__main__':
    unittest.main()
