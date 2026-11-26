"""Crash-idempotent restore and git-ignored/cache exclusion for populated submodules.

Covers:
- Retrying restore() after a mid-_replay_submodule interruption (missing files, 0-byte truncated
  file, un-restored modes, pre-delta HEAD content, no completed restore marker) succeeds and
  reconstructs the exact recorded fingerprint.
- Real foreign or newer work in a target submodule (foreign file, modified content, staged index
  change, or any mutation after a completed restore) is still refused with RestoreRefused.
- Atomic staging during _replay_submodule does not leave a half-populated worktree on interruption.
- Git-ignored regenerable caches (node_modules, cache/) inside populated submodules are left out of
  slim/full checkpoints while tracked and untracked files are preserved and proven in place.
"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mesh_runner.checkpoint import Checkpoints, fingerprint, submodule_plan
from mesh_runner.workspace import RESTORE_MARKER, RestoreRefused, WorkspaceError, git
from test_workspace import make_repo

FILE_PROTOCOL = ('-c', 'protocol.file.allow=always')


class SubmoduleRestoreCrashAndExcludes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.user = self.root / 'user.gitconfig'
        self.user.write_text('')
        isolation = patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(self.user), 'GIT_CONFIG_NOSYSTEM': '1'})
        isolation.start()
        self.addCleanup(isolation.stop)
        for var in ('GIT_CONFIG', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS', 'XDG_CONFIG_HOME', 'GIT_TEMPLATE_DIR'):
            os.environ.pop(var, None)
        self.base = make_repo(self.root)
        (self.root / 'ext').mkdir()
        self.ext = make_repo(self.root / 'ext')  # .gitignore ignores cache/

    def tearDown(self):
        self.tmp.cleanup()

    def _setup_submodule(self, *, gitignore: str = 'cache/\nnode_modules/\n') -> Path:
        (self.ext / '.gitignore').write_text(gitignore)
        for name in ('alpha.txt', 'beta.txt', 'gamma.txt', 'to-delete.txt'):
            (self.ext / name).write_text(f'head {name}\n')
        (self.ext / 'target').mkdir()
        (self.ext / 'target' / 'tracked.txt').write_text('tracked inside target\n')
        git(self.ext, 'add', '.')
        git(self.ext, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'seed ext')
        git(self.ext, 'push', '-q', 'origin', 'HEAD:main')

        git(self.base, *FILE_PROTOCOL, 'submodule', 'add', '-q',
            str(self.root / 'ext' / 'origin.git'), 'vendor/kit')
        git(self.base, 'commit', '-q', '-m', 'add kit')
        git(self.base, 'push', '-q', 'origin', 'HEAD:main')

        source = self.root / 'source'
        git(self.root, 'clone', '-q', str(self.root / 'origin.git'), str(source))
        git(source, *FILE_PROTOCOL, 'submodule', 'update', '-q', '--init')
        return source

    def _dirty_submodule(self, source: Path) -> Path:
        sub = source / 'vendor' / 'kit'
        (sub / 'alpha.txt').write_text('modified tracked content in submodule\n')
        (sub / 'to-delete.txt').unlink()
        (sub / 'extra_dir').mkdir()
        (sub / 'extra_dir' / 'one.js').write_text('export const one = 1;\n' * 20)
        (sub / 'extra_dir' / 'two.js').write_text('export const two = 2;\n' * 20)
        (sub / 'tool.sh').write_text('#!/bin/sh\necho ok\n')
        (sub / 'tool.sh').chmod(0o751)
        (sub / 'link').symlink_to('tool.sh')
        (sub / 'empty_dir').mkdir()
        return sub

    def _fresh_target(self, name: str = 'target') -> Path:
        target = self.root / name
        git(self.root, 'clone', '-q', str(self.root / 'origin.git'), str(target))
        return target

    def _marker_path(self, target: Path) -> Path:
        p = Path(git(target, 'rev-parse', '--git-path', RESTORE_MARKER))
        return p if p.is_absolute() else target / p

    def test_retry_after_partial_submodule_replay_without_marker_succeeds(self):
        source = self._setup_submodule()
        self._dirty_submodule(source)
        cps = Checkpoints(self.root / 'cps')
        cp = cps.create('cp1', source, {'base_sha': git(source, 'rev-parse', 'origin/main')})
        manifest = cps.read(cp)
        target = self._fresh_target()
        cps.restore(cp, target)

        # Simulate an older runner killed mid-_apply_delta: marker already removed,
        # incomplete tree, 0-byte truncated file, un-chmodded modes, pre-delta HEAD file.
        self._marker_path(target).unlink()
        sub = target / 'vendor' / 'kit'
        (sub / 'extra_dir' / 'two.js').unlink()          # missing file
        (sub / 'empty_dir').rmdir()                       # missing empty dir
        (sub / 'extra_dir' / 'one.js').write_bytes(b'')   # 0-byte truncated copy
        (sub / 'tool.sh').chmod(0o600)                    # mode not restored yet
        (sub / 'extra_dir').chmod(0o700)                  # dir mode not restored yet
        (sub / 'alpha.txt').write_text('head alpha.txt\n')  # still HEAD before delta reached it
        (sub / 'to-delete.txt').write_text('head to-delete.txt\n')  # not unlinked yet

        cps.restore(cp, target)
        self.assertEqual(
            fingerprint(target, submodule_plan(target), tuple(manifest['excludes']),
                        tuple(manifest.get('ignored', []))),
            manifest['fingerprint'],
        )
        self.assertEqual((sub / 'extra_dir' / 'one.js').read_text(), 'export const one = 1;\n' * 20)
        self.assertEqual((sub / 'tool.sh').stat().st_mode & 0o777, 0o751)
        self.assertFalse((sub / 'to-delete.txt').exists())

    def test_interrupted_apply_delta_leaves_target_retryable(self):
        source = self._setup_submodule()
        self._dirty_submodule(source)
        cps = Checkpoints(self.root / 'cps')
        cp = cps.create('cp2', source, {'base_sha': git(source, 'rev-parse', 'origin/main')})
        manifest = cps.read(cp)
        target = self._fresh_target()

        real_copy2 = shutil.copy2
        calls = {'n': 0}

        def interrupted_copy2(src, dst, *a, **k):
            if 'vendor/kit/extra_dir' in str(src):
                calls['n'] += 1
                if calls['n'] == 1:
                    Path(dst).write_bytes(b'')
                    raise RuntimeError('simulated mid-replay kill')
            return real_copy2(src, dst, *a, **k)

        with patch('mesh_runner.checkpoint.shutil.copy2', side_effect=interrupted_copy2):
            with self.assertRaisesRegex(RuntimeError, 'simulated mid-replay kill'):
                cps.restore(cp, target)

        # In-progress marker uses 'replaying' (never 'checkpoint') and atomic staging left target/sub empty.
        marker = json.loads(self._marker_path(target).read_text())
        self.assertEqual(marker.get('replaying'), cp.name)
        self.assertNotIn('checkpoint', marker)
        self.assertEqual(list((target / 'vendor' / 'kit').iterdir()), [])

        # Even with an in-progress marker, real foreign work dropped into the submodule is refused.
        (target / 'vendor' / 'kit' / 'foreign.txt').write_text('foreign during retry\n')
        with self.assertRaisesRegex(RestoreRefused, 'work in target differs'):
            cps.restore(cp, target)
        (target / 'vendor' / 'kit' / 'foreign.txt').unlink()

        # Retry succeeds cleanly and reaches the exact recorded fingerprint.
        cps.restore(cp, target)
        done = json.loads(self._marker_path(target).read_text())
        self.assertEqual(done.get('checkpoint'), cp.name)
        self.assertNotIn('replaying', done)
        self.assertEqual(
            fingerprint(target, submodule_plan(target), tuple(manifest['excludes']),
                        tuple(manifest.get('ignored', []))),
            manifest['fingerprint'],
        )

    def test_foreign_or_modified_work_in_submodule_is_still_refused(self):
        source = self._setup_submodule()
        self._dirty_submodule(source)
        cps = Checkpoints(self.root / 'cps')
        cp = cps.create('cp3', source, {'base_sha': git(source, 'rev-parse', 'origin/main')})
        target = self._fresh_target()
        cps.restore(cp, target)
        sub = target / 'vendor' / 'kit'

        # 1. With completed marker: even a deletion, mode change, 0-byte truncation, or a
        # completed marker recording another result digest is refused as newer work.
        (sub / 'extra_dir' / 'one.js').write_bytes(b'')
        with self.assertRaisesRegex(RestoreRefused, 'work in target differs'):
            cps.restore(cp, target)
        (sub / 'extra_dir' / 'one.js').write_text('export const one = 1;\n' * 20)

        (sub / 'extra_dir' / 'two.js').unlink()
        with self.assertRaisesRegex(RestoreRefused, 'work in target differs'):
            cps.restore(cp, target)
        (sub / 'extra_dir' / 'two.js').write_text('export const two = 2;\n' * 20)

        marker_path = self._marker_path(target)
        saved_marker = marker_path.read_text()
        marker_data = json.loads(saved_marker)
        marker_data['result'] = '0' * 64
        marker_path.write_text(json.dumps(marker_data))
        with self.assertRaisesRegex(RestoreRefused, 'newer work since completed restore'):
            cps.restore(cp, target)
        # Mutating the checkpoint id, writing an unrelated replaying marker, or corrupting the
        # marker must all fail closed rather than bypassing the completed-restore guard.
        (target / 'superproject-edit.txt').write_text('keep me\n')
        for bad_marker, pattern in (
            (json.dumps({'checkpoint': 'other-checkpoint', 'result': json.loads(saved_marker)['result']}),
             'another checkpoint or state'),
            (json.dumps({'replaying': cp.name, 'result': '0' * 64}),
             'another checkpoint or state'),
            (json.dumps({'replaying': 'other-checkpoint', 'result': json.loads(saved_marker)['result']}),
             'another checkpoint or state'),
            ('{corrupt',
             'unexpected or unreadable restore marker'),
        ):
            marker_path.write_text(bad_marker)
            with self.assertRaisesRegex(RestoreRefused, pattern):
                cps.restore(cp, target)
        self.assertEqual((target / 'superproject-edit.txt').read_text(), 'keep me\n')
        (target / 'superproject-edit.txt').unlink()
        marker_path.write_text(saved_marker)

        # 2. Without marker, a foreign file in the submodule must still be refused.
        self._marker_path(target).unlink()
        (sub / 'extra_dir' / 'two.js').unlink()
        (sub / 'foreign.txt').write_text('real foreign work\n')
        with self.assertRaisesRegex(RestoreRefused, 'work in target differs'):
            cps.restore(cp, target)
        self.assertEqual((sub / 'foreign.txt').read_text(), 'real foreign work\n')
        (sub / 'foreign.txt').unlink()

        # 3. Without marker, a file with real new content (not 0-byte/prefix/HEAD) must be refused.
        (sub / 'beta.txt').write_text('real user modification\n')
        with self.assertRaisesRegex(RestoreRefused, 'work in target differs'):
            cps.restore(cp, target)
        self.assertEqual((sub / 'beta.txt').read_text(), 'real user modification\n')
        (sub / 'beta.txt').write_text('head beta.txt\n')

        # 4. Without marker, staged index change in submodule must be refused.
        (sub / 'beta.txt').write_text('staged edit\n')
        git(sub, 'add', 'beta.txt')
        with self.assertRaisesRegex(RestoreRefused, 'work in target differs'):
            cps.restore(cp, target)

    def test_submodule_ignored_node_modules_and_cache_excluded_in_slim_and_full(self):
        source = self._setup_submodule()
        sub = self._dirty_submodule(source)
        nm = sub / 'node_modules' / '.pnpm' / 'pkg@1.0.0'
        nm.mkdir(parents=True)
        for i in range(25):
            (nm / f'f{i}.js').write_text(f'module.exports = {i};\n')
        (sub / 'cache').mkdir()
        (sub / 'cache' / 'build.log').write_text('regenerable log\n')

        # Slim capture: proven in place, excludes node_modules and ignores cache/.
        replays = []
        real_replay = Checkpoints._replay
        cps_slim = Checkpoints(self.root / 'cps-slim', slim=True)
        with patch.object(Checkpoints, '_replay', lambda s, *a, **k: replays.append(a) or real_replay(s, *a, **k)):
            cp_slim = cps_slim.create('slim1', source, {'base_sha': git(source, 'rev-parse', 'origin/main')})
            self.assertEqual(replays, [])
            manifest = cps_slim.read(cp_slim)
            self.assertIn('vendor/kit/node_modules', manifest['excludes'])
            self.assertIn('vendor/kit/cache', manifest.get('ignored', []))
            self.assertFalse(any('node_modules' in f for f in manifest['delta']['files']))
            self.assertFalse(any('vendor/kit/cache' in f for f in manifest['delta']['files']))
            self.assertFalse((cp_slim / 'tree' / 'vendor' / 'kit' / 'node_modules').exists())
            self.assertFalse((cp_slim / 'tree' / 'vendor' / 'kit' / 'cache').exists())
            cps_slim.verify(cp_slim)
        self.assertEqual(len(replays), 1)

        target_slim = self._fresh_target('target-slim')
        cps_slim.restore(cp_slim, target_slim)
        self.assertEqual(
            fingerprint(target_slim, submodule_plan(target_slim),
                        tuple(manifest['excludes']), tuple(manifest.get('ignored', []))),
            manifest['fingerprint'],
        )
        self.assertFalse((target_slim / 'vendor' / 'kit' / 'node_modules').exists())
        self.assertFalse((target_slim / 'vendor' / 'kit' / 'cache').exists())
        self.assertEqual((target_slim / 'vendor' / 'kit' / 'target' / 'tracked.txt').read_text(),
                         'tracked inside target\n')
        self.assertEqual((target_slim / 'vendor' / 'kit' / 'extra_dir' / 'one.js').read_text(),
                         'export const one = 1;\n' * 20)

        # Regenerating ignored dirs after completed restore does not break idempotent re-restore.
        (target_slim / 'vendor' / 'kit' / 'node_modules').mkdir()
        (target_slim / 'vendor' / 'kit' / 'node_modules' / 'fresh.js').write_text('x\n')
        (target_slim / 'vendor' / 'kit' / 'cache').mkdir()
        (target_slim / 'vendor' / 'kit' / 'cache' / 'new.log').write_text('y\n')
        cps_slim.restore(cp_slim, target_slim)

        # Full capture: still excludes git-ignored DEFAULT_EXCLUDE_NAMES (node_modules).
        cps_full = Checkpoints(self.root / 'cps-full', slim=False)
        cp_full = cps_full.create('full1', source, {'base_sha': git(source, 'rev-parse', 'origin/main')})
        full_manifest = cps_full.read(cp_full)
        self.assertIn('vendor/kit/node_modules', full_manifest['excludes'])
        self.assertFalse((cp_full / 'tree' / 'vendor' / 'kit' / 'node_modules').exists())
        target_full = self._fresh_target('target-full')
        cps_full.restore(cp_full, target_full)
        self.assertEqual(
            fingerprint(target_full, submodule_plan(target_full), tuple(full_manifest['excludes'])),
            full_manifest['fingerprint'],
        )

    def test_tracked_file_inside_submodule_node_modules_is_preserved(self):
        source = self._setup_submodule()
        sub = source / 'vendor' / 'kit'
        (sub / 'node_modules').mkdir()
        (sub / 'node_modules' / 'kept.js').write_text('tracked\n')
        git(sub, 'add', '-f', 'node_modules/kept.js')
        (sub / 'node_modules' / 'dep').mkdir()
        (sub / 'node_modules' / 'dep' / 'ignored.js').write_text('ignored\n')

        cps = Checkpoints(self.root / 'cps-tracked-nm')
        cp = cps.create('tnm1', source, {'base_sha': git(source, 'rev-parse', 'origin/main')})
        manifest = cps.read(cp)
        self.assertNotIn('vendor/kit/node_modules', manifest['excludes'])
        self.assertIn('vendor/kit/node_modules/dep', manifest.get('ignored', []))
        cps.verify(cp)
        target = self._fresh_target('target-tracked-nm')
        cps.restore(cp, target)
        self.assertEqual((target / 'vendor' / 'kit' / 'node_modules' / 'kept.js').read_text(), 'tracked\n')
        self.assertFalse((target / 'vendor' / 'kit' / 'node_modules' / 'dep').exists())

    def test_unignored_node_modules_inside_submodule_still_verifies_and_restores(self):
        # Without node_modules in the submodule's .gitignore, superproject git status reports the
        # submodule dirty; capture must not break verification by dropping an unignored dir.
        source = self._setup_submodule(gitignore='cache/\n')
        sub = source / 'vendor' / 'kit'
        (sub / 'node_modules').mkdir()
        (sub / 'node_modules' / 'unignored.js').write_text('not ignored\n')

        cps = Checkpoints(self.root / 'cps-unignored-nm')
        cp = cps.create('unm1', source, {'base_sha': git(source, 'rev-parse', 'origin/main')})
        manifest = cps.read(cp)
        self.assertNotIn('vendor/kit/node_modules', manifest['excludes'])
        cps.verify(cp)
        target = self._fresh_target('target-unignored-nm')
        cps.restore(cp, target)
        self.assertEqual((target / 'vendor' / 'kit' / 'node_modules' / 'unignored.js').read_text(), 'not ignored\n')

    def test_unignored_submodule_cache_with_pathspec_metacharacters_is_not_excluded(self):
        # A path containing Git glob/pathspec metacharacters (e.g. `[slug]` or `:magic`) must be
        # checked literally so unignored files inside it are never mistaken for clean/ignored.
        source = self._setup_submodule(gitignore='cache/\n')
        sub = source / 'vendor' / 'kit'
        for rel in ('app/[slug]/node_modules/unignored.js', ':pkg/node_modules/unignored.js'):
            p = sub / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f'kept {rel}\n')

        cps = Checkpoints(self.root / 'cps-meta-nm')
        cp = cps.create('mnm1', source, {'base_sha': git(source, 'rev-parse', 'origin/main')})
        manifest = cps.read(cp)
        self.assertNotIn('vendor/kit/app/[slug]/node_modules', manifest['excludes'])
        self.assertNotIn('vendor/kit/:pkg/node_modules', manifest['excludes'])
        cps.verify(cp)
        target = self._fresh_target('target-meta-nm')
        cps.restore(cp, target)
        for rel in ('app/[slug]/node_modules/unignored.js', ':pkg/node_modules/unignored.js'):
            self.assertEqual((target / 'vendor' / 'kit' / rel).read_text(), f'kept {rel}\n')

    def test_interrupted_submodule_gitdir_creation_leaves_target_retryable(self):
        source = self._setup_submodule()
        self._dirty_submodule(source)
        cps = Checkpoints(self.root / 'cps-gd')
        cp = cps.create('gd1', source, {'base_sha': git(source, 'rev-parse', 'origin/main')})
        manifest = cps.read(cp)
        target = self._fresh_target('target-gd')

        from mesh_runner import checkpoint as cp_mod
        real_gitdir = cp_mod._gitdir

        def fail_pack_refs(gd, *args, **kwargs):
            if args and args[0] == 'pack-refs':
                raise RuntimeError('simulated kill during gitdir init')
            return real_gitdir(gd, *args, **kwargs)

        with patch('mesh_runner.checkpoint._gitdir', side_effect=fail_pack_refs):
            with self.assertRaisesRegex(RuntimeError, 'simulated kill during gitdir init'):
                cps.restore(cp, target)

        # Final submodule gitdir was never published half-initialized; retry succeeds cleanly.
        modules = Path(git(target, 'rev-parse', '--git-path', 'modules'))
        if not modules.is_absolute():
            modules = target / modules
        self.assertFalse((modules / 'vendor/kit').exists())
        cps.restore(cp, target)
        self.assertEqual(
            fingerprint(target, submodule_plan(target), tuple(manifest['excludes']),
                        tuple(manifest.get('ignored', []))),
            manifest['fingerprint'],
        )

    def test_prefix_truncated_file_recovers_while_non_prefix_edit_is_refused(self):
        source = self._setup_submodule()
        self._dirty_submodule(source)
        cps = Checkpoints(self.root / 'cps-prefix')
        cp = cps.create('pref1', source, {'base_sha': git(source, 'rev-parse', 'origin/main')})
        manifest = cps.read(cp)
        target = self._fresh_target('target-prefix')
        cps.restore(cp, target)
        self._marker_path(target).unlink()
        sub = target / 'vendor' / 'kit'
        one = sub / 'extra_dir' / 'one.js'
        full_bytes = one.read_bytes()

        # Non-prefix shorter content is refused (real user edit).
        one.write_bytes(b'edited!')
        with self.assertRaisesRegex(RestoreRefused, 'work in target differs'):
            cps.restore(cp, target)

        # Strict prefix of checkpoint file (interrupted copy mid-file) recovers.
        one.write_bytes(full_bytes[:40])
        cps.restore(cp, target)
        self.assertEqual(one.read_bytes(), full_bytes)
        self.assertEqual(
            fingerprint(target, submodule_plan(target), tuple(manifest['excludes']),
                        tuple(manifest.get('ignored', []))),
            manifest['fingerprint'],
        )


if __name__ == '__main__':
    unittest.main()
