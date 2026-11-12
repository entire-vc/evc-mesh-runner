"""Storage path without a registry/DB: real git, filesystem and native clone API."""
import argparse
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mesh_runner.checkpoint import (SLIM_GIT_COMMANDS, SLIM_REASONS, SLIM_STAGES, Checkpoints, _copy,
                                    _copy_file, _slim_failure_reason, fingerprint, manifest_digest,
                                    slim_failure_cause)
from mesh_runner.cli import _checkpoint_pin_arg
from mesh_runner.pool_ops import _bounded_rmtree
from mesh_runner.workspace import WorkspaceError, git
from test_workspace import make_repo


class CheckpointStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_copy_uses_no_child_process_and_preserves_symlinks_modes_excludes(self):
        source = self.root / 'source'; source.mkdir()
        (source / 'readonly').mkdir()
        (source / 'readonly' / 'file').write_bytes(b'bytes\0')
        (source / 'readonly' / 'file').chmod(0o640)
        (source / 'readonly').chmod(0o555)
        (source / 'link').symlink_to('/absent')
        (source / '.git').mkdir()
        (source / 'excluded').write_text('out')
        dest = self.root / 'dest'
        # mock: external process boundary — copying must not start per-file cp.
        with patch('subprocess.Popen', side_effect=AssertionError('child process')):
            _copy(source, dest, frozenset({'excluded'}))
        self.assertEqual((dest / 'readonly' / 'file').read_bytes(), b'bytes\0')
        self.assertEqual((dest / 'readonly' / 'file').stat().st_mode & 0o777, 0o640)
        self.assertEqual((dest / 'readonly').stat().st_mode & 0o777, 0o555)
        self.assertEqual(os.readlink(dest / 'link'), '/absent')
        self.assertFalse((dest / '.git').exists()); self.assertFalse((dest / 'excluded').exists())
        # Existing destination makes native clonefile fail with EEXIST: portable fallback still copies.
        (source / 'excluded').chmod(0o751)
        _copy_file(source / 'excluded', dest / 'readonly' / 'file')
        self.assertEqual((dest / 'readonly' / 'file').read_text(), 'out')
        self.assertEqual((dest / 'readonly' / 'file').stat().st_mode & 0o777, 0o751)

    def test_capture_verify_restore_staged_deleted_modes_and_links(self):
        source = make_repo(self.root)
        (source / 'deleted').write_bytes(b'tracked'); git(source, 'add', 'deleted'); git(source, 'commit', '-qm', 'file')
        (source / 'deleted').unlink()
        (source / 'README').write_bytes(b'staged\0'); git(source, 'add', 'README')
        (source / 'README').write_bytes(b'unstaged\0')
        (source / 'exec').write_bytes(b'new'); (source / 'exec').chmod(0o751)
        (source / 'link').symlink_to('exec')
        (source / 'empty').mkdir(); (source / 'empty').chmod(0o750)
        expected = fingerprint(source)
        cp = Checkpoints(self.root / 'cp')
        path = cp.create('roundtrip', source, {})
        cp.verify(path)
        target = self.root / 'target'; target.mkdir(); git(target, 'init', '--quiet')
        cp.restore(path, target)
        self.assertEqual(fingerprint(target), expected)
        self.assertFalse((target / 'deleted').exists())
        self.assertEqual(git(target, 'show', ':README'), 'staged\0')

    def test_real_slim_failure_is_classified_without_exception_details(self):
        source = make_repo(self.root)
        git(source, 'update-index', '--skip-worktree', 'README')
        (source / 'README').write_text('hidden change')
        cp = Checkpoints(self.root / 'cp')
        path = cp.create('fallback', source, {})
        data = cp.read(path)
        self.assertTrue(data['slim_failed'])
        self.assertEqual(data['slim_failure_reason'], 'replay-mismatch')
        self.assertEqual(data['slim_failure_stage'], 'verify-fingerprint')
        self.assertNotIn('slim_failure_git', data)
        self.assertFalse(cp.is_old_format(path))
        self.assertEqual(_slim_failure_reason(WorkspaceError('/private/token=secret\noutput')), 'other')

    def test_slim_failure_cause_is_finite_and_names_the_git_command(self):
        cases = {
            'git checkout --quiet --force in /private/secret/slot: error: unable to read sha1 file of a (b)':
                {'reason': 'git-object-unreadable', 'git': 'checkout'},
            "git update-ref refs/runner-checkpoints/k abc in /p: fatal: cannot lock ref 'refs/x': Unable to create '/p/x.lock'":
                {'reason': 'git-lock', 'git': 'update-ref'},
            'git fetch --quiet --no-recurse-submodules in /p: timed out': {'reason': 'git-timeout', 'git': 'fetch'},
            'git init in /p: token=abc': {'reason': 'git-other', 'git': 'init'},
            'git made-up --x in /p: whatever': {'reason': 'git-other', 'git': 'other'},
            'git timed out after 600s': {'reason': 'timeout', 'git': None},
            'checkpoint: snapshot hash mismatch': {'reason': 'snapshot-mismatch', 'git': None},
            'checkpoint: base commit missing in target; target untouched': {'reason': 'base-commit-missing', 'git': None},
            'checkpoint: a new message /private/token=secret': {'reason': 'other', 'git': None},
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                cause = slim_failure_cause(WorkspaceError(message), 'verify-checkout')
                self.assertEqual({k: cause[k] for k in expected}, expected)
                self.assertEqual(cause['stage'], 'verify-checkout')
                self.assertIn(cause['reason'], SLIM_REASONS)
                self.assertTrue(cause['git'] is None or cause['git'] in SLIM_GIT_COMMANDS or cause['git'] == 'other')
        self.assertEqual(slim_failure_cause(WorkspaceError('x'), '/private/stage')['stage'], 'unknown')

    def test_injected_git_failure_records_stage_and_command_and_logs_one_line(self):
        source = make_repo(self.root)
        (source / 'note.txt').write_text('untracked work')
        cp = Checkpoints(self.root / 'cp')
        secret = 'git checkout-index --all --force in /private/secret-path: fatal: unable to read 0123abcd'
        with patch.object(Checkpoints, '_overlay_delta', side_effect=WorkspaceError(secret)), \
                self.assertLogs('mesh_runner', 'WARNING') as logs:
            path = cp.create('injected', source, {'task': '0000aaaa-0000-4000-8000-000000000001',
                                                  'attempt': 'a1b2c3d4-0000', 'repo': 'steward-workspace'})
        data = cp.read(path)
        self.assertEqual((data['slim_failure_stage'], data['slim_failure_reason'], data['slim_failure_git']),
                         ('verify-overlay', 'git-object-unreadable', 'checkout-index'))
        self.assertIn(data['slim_failure_stage'], SLIM_STAGES)
        lines = [l for l in logs.output if 'checkpoint slim fallback' in l]
        self.assertEqual(len(lines), 1)
        self.assertIn('agent=cp card=0000aaaa attempt=a1b2c3d4 repo=steward-workspace stage=verify-overlay '
                      'reason=git-object-unreadable git=checkout-index exit=- outcome=full-form', lines[0])
        for leak in ('/private', 'secret', '0123abcd'):
            self.assertNotIn(leak, lines[0])
            self.assertNotIn(leak, (path / 'manifest.json').read_text())

    def test_cross_volume_slim_failure_keeps_the_lease_instead_of_a_full_copy(self):
        source = make_repo(self.root)
        git(source, 'update-index', '--skip-worktree', 'README')
        (source / 'README').write_text('hidden change')
        cp = Checkpoints(self.root / 'cp')
        with patch('mesh_runner.checkpoint._same_volume', return_value=False), \
                self.assertLogs('mesh_runner', 'WARNING') as logs, \
                self.assertRaisesRegex(WorkspaceError, r'verify-fingerprint/replay-mismatch.*lease retained'):
            cp.create('crossvol', source, {'repo': 'app'})
        self.assertEqual([p.name for p in (self.root / 'cp').iterdir()], [])     # no full copy, no pending
        self.assertTrue(any('outcome=lease-retained' in l for l in logs.output))
        self.assertEqual((source / 'README').read_text(), 'hidden change')        # source untouched

    def test_manifest_rejects_symlink_and_pin_parser_rejects_paths(self):
        path = self.root / 'cp'; path.mkdir(); (path / 'manifest.json').write_text('{}')
        self.assertEqual(len(manifest_digest(path)), 64)
        (path / 'manifest.json').unlink(); (path / 'manifest.json').symlink_to('/absent')
        with self.assertRaises(OSError): manifest_digest(path)
        for value in ('../escape:' + 'a'*64, 'id:bad', '.trash-id:' + 'a'*64):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                _checkpoint_pin_arg(value)
        self.assertEqual(_checkpoint_pin_arg('id:'+'a'*64), ('id', 'a'*64))

    def test_bounded_cleanup_readonly_tree_never_follows_symlinks(self):
        outside = self.root / 'outside'; outside.mkdir(); (outside / 'keep').write_text('safe')
        trash = self.root / 'trash'; trash.mkdir(); nested = trash / 'readonly'; nested.mkdir()
        (nested / 'payload').write_text('remove'); (nested / 'link').symlink_to(outside, target_is_directory=True)
        nested.chmod(0o555); trash.chmod(0o555)
        with self.assertRaises(WorkspaceError):
            _bounded_rmtree(trash, time.monotonic() - 1, anchor=self.root)
        self.assertTrue((nested / 'payload').exists())
        _bounded_rmtree(trash, time.monotonic() + 10, anchor=self.root)
        self.assertFalse(trash.exists()); self.assertEqual((outside / 'keep').read_text(), 'safe')
        (self.root / 'escape').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            _bounded_rmtree(self.root / 'escape', time.monotonic() + 10, anchor=self.root)
        self.assertEqual((outside / 'keep').read_text(), 'safe')
