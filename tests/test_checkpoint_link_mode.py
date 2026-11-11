"""Checkpoint symlink metadata must survive the Runner's restrictive umask."""
import os
from pathlib import Path
import tempfile
import unittest

from mesh_runner.checkpoint import Checkpoints, fingerprint, inventory
from mesh_runner.workspace import git
from test_workspace import make_repo


class CheckpointLinkMode(unittest.TestCase):
    def test_delta_copy_preserves_link_mode_without_following_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'; source.mkdir()
            outside = root / 'outside'; outside.write_text('unchanged'); outside.chmod(0o640)
            old = os.umask(0o022)
            try:
                (source / 'outside').symlink_to(outside)
                (source / 'dangling').symlink_to('/missing-checkpoint-target')
            finally:
                os.umask(old)
            expected = inventory(source)
            old = os.umask(0o077)
            try:
                Checkpoints(root / 'checkpoints')._copy_delta(
                    source, root / 'copy', expected,
                    {'files': ['outside', 'dangling'], 'dirs': [], 'deleted': []})
            finally:
                os.umask(old)
            self.assertEqual(inventory(root / 'copy'), expected)
            self.assertEqual(outside.read_text(), 'unchanged')
            self.assertEqual(outside.stat().st_mode & 0o777, 0o640)

    def test_slim_capture_verify_restore_under_runner_umask(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = make_repo(root)
            old = os.umask(0o022)
            try:
                (source / 'control').mkdir()
                (source / 'control' / 'base').symlink_to('/missing-checkpoint-target')
            finally:
                os.umask(old)
            expected = fingerprint(source)
            old = os.umask(0o077)
            try:
                checkpoints = Checkpoints(root / 'checkpoints')
                captured = checkpoints.create('link-mode', source, {})
                self.assertIn('delta', checkpoints.read(captured))
                self.assertFalse(checkpoints.read(captured).get('slim_failed', False))
                checkpoints.verify(captured)
                target = root / 'target'; target.mkdir(); git(target, 'init', '--quiet')
                checkpoints.restore(captured, target)
                self.assertEqual(fingerprint(target), expected)
            finally:
                os.umask(old)
