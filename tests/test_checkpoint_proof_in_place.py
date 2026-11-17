"""A routine capture is proven without laying the checkout out in the checkpoint directory.

Before, every capture replayed the whole tree into `.verify-*` scratch and removed it: a slot of tens
of thousands of files wrote and deleted as many in the checkpoint directory on every WAIT: a large
source of file-system events on the host. Real git, real filesystem."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mesh_runner.checkpoint import Checkpoints, _NotProvable, fingerprint
from mesh_runner.store import Store
from mesh_runner.workspace import RepoSpec, WorkspacePool, WorkspaceError, git
from test_workspace import make_repo


def _files_under(path: Path) -> int:
    return sum(len(files) for _, _, files in os.walk(path))


class FileCount:
    """Files a block creates under `root`: what is left at the end plus what any rmtree removed there
    (the scratch replay lives in a temporary directory that is gone by the time the call returns)."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.removed, self.scratch = 0, []

    def __enter__(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.before = _files_under(self.root)
        real = shutil.rmtree

        def counting(path, *args, **kwargs):
            p = Path(path).resolve()
            if p == self.root or self.root in p.parents:
                self.removed += _files_under(p)
                self.scratch.append(p.name)
            return real(path, *args, **kwargs)
        self.patch = patch('shutil.rmtree', counting)      # tempfile's cleanup calls it through the module
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()
        self.created = _files_under(self.root) - self.before + self.removed


class ProofInPlace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # The replay reads the user-wide config: a fixed empty one here, so the host's own settings (a
        # key the proof does not model hands over by name) never decide these tests.
        empty = self.root / 'empty.gitconfig'
        empty.write_text('')
        isolation = patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(empty), 'GIT_CONFIG_NOSYSTEM': '1'})
        isolation.start()
        self.addCleanup(isolation.stop)
        for var in ('GIT_CONFIG', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS', 'XDG_CONFIG_HOME', 'GIT_TEMPLATE_DIR'):
            os.environ.pop(var, None)
        self.base = make_repo(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _pool(self):
        return WorkspacePool(Store(self.root / 's.sqlite'), self.root / 'pool',
                             [RepoSpec('app', self.base, max_slots=1)], owner='agent')

    def _commit_many(self, count: int) -> None:
        for i in range(count):
            d = self.base / 'src' / f'd{i // 100:03d}'
            d.mkdir(parents=True, exist_ok=True)
            (d / f'f{i:05d}.txt').write_text(f'file {i}\n')
        git(self.base, 'add', '.')
        git(self.base, 'commit', '-q', '-m', 'many')
        git(self.base, 'push', '-q', 'origin', 'HEAD:main')

    def test_clean_slot_of_5000_files_creates_at_most_50_files_in_the_checkpoint_root(self):
        self._commit_many(5000)
        pool = self._pool()
        slot = pool.lease('attempt', 'app')
        expected = fingerprint(slot)
        root = pool._checkpoints().root                     # where this pool keeps its checkpoints
        with FileCount(root) as count:
            pool.suspend('attempt', {'session_id': 's', 'wait': {'target': 'card'}}, writer_stopped=True)
        self.assertTrue(any(p.name == 'manifest.json' for p in root.rglob('*')))   # it did capture here
        self.assertLessEqual(count.created, 50, f'{count.created} files, scratch {count.scratch}')
        self.assertFalse([n for n in count.scratch if n.startswith('.verify-')])
        paths, _ = pool.restore('attempt')
        self.assertEqual(fingerprint(Path(paths[0])), expected)

    def _dirty(self, source: Path) -> None:
        """Every kind of difference a slim capture records: staged, unstaged, deleted, renamed, new
        (executable, symlink, empty directory, nested repository), a commit beyond the base."""
        (source / 'tracked').write_text('one\n'); (source / 'gone').write_text('two\n')
        (source / 'sub').mkdir(); (source / 'sub' / 'moved').write_text('three\n')
        (source / 'sub' / 'stays').write_text('four\n')
        (source / 'link').symlink_to('tracked')
        git(source, 'add', '.'); git(source, 'commit', '-q', '-m', 'beyond base')
        (source / 'README').write_bytes(b'staged\0'); git(source, 'add', 'README')
        (source / 'README').write_bytes(b'unstaged\0')
        (source / 'gone').unlink()
        git(source, 'mv', 'sub/moved', 'renamed')
        (source / 'exec').write_text('echo\n'); (source / 'exec').chmod(0o751)
        (source / 'newlink').symlink_to('/absent')
        (source / 'empty').mkdir(); (source / 'empty').chmod(0o750)
        nested = source / 'evidence'
        nested.mkdir()
        git(nested, 'init', '-q'); (nested / 'f').write_text('nested\n')
        (source / 'cache').mkdir(); (source / 'cache' / 'log').write_text('ignored\n')

    def _capture(self, source: Path, key: str = 'cp') -> tuple:
        cps = Checkpoints(self.root / 'cps')
        base = git(source, 'rev-parse', 'origin/main')
        return cps, cps.create(key, source, {'base_sha': base})

    def test_dirty_capture_is_proven_in_place_and_the_replay_agrees(self):
        self._dirty(self.base)
        replays = []
        real = Checkpoints._replay
        with patch.object(Checkpoints, '_replay', lambda s, *a, **k: replays.append(a) or real(s, *a, **k)):
            cps, cp = self._capture(self.base)
            self.assertEqual(replays, [])                 # no scratch replay at capture
            manifest = cps.read(cp)
            self.assertEqual(manifest['bundle']['mode'], 'incremental')
            self.assertTrue(any(f.startswith('evidence/.git/') for f in manifest['delta']['files']))
            cps.verify(cp)                                # the authority, the scratch replay, agrees
        self.assertEqual(len(replays), 1)
        target = self.root / 'target'
        git(self.root, 'clone', '-q', str(self.root / 'origin.git'), str(target))
        cps.restore(cp, target)
        self.assertEqual(fingerprint(target, None, tuple(manifest['excludes']), tuple(manifest['ignored'])),
                         manifest['fingerprint'])

    def _simple_dirty(self, source: Path) -> None:
        """A commit beyond the base (so a bundle), an unstaged change, an untracked executable."""
        (source / 'tracked').write_text('one\n')
        git(source, 'add', '.'); git(source, 'commit', '-q', '-m', 'beyond base')
        (source / 'README').write_text('unstaged\n')
        (source / 'exec').write_text('echo\n'); (source / 'exec').chmod(0o751)

    def test_a_corrupt_bundle_or_delta_file_is_still_refused(self):
        self._dirty(self.base)
        cps, cp = self._capture(self.base)
        delta = cp / 'tree' / 'exec'
        delta.write_text('tampered\n')
        with self.assertRaisesRegex(WorkspaceError, 'snapshot hash mismatch'):
            cps.verify(cp)
        delta.write_text('echo\n'); delta.chmod(0o751)
        cps.verify(cp)
        # Every tip of this bundle is in the source's store, so a fetch would never read the pack.
        bundle = cp / 'commits.bundle'
        whole = bundle.read_bytes()
        for broken in (whole[:-40], whole[:-20] + bytes(20)):
            with self.subTest(size=len(broken)):
                bundle.write_bytes(broken)
                with self.assertRaisesRegex(WorkspaceError, 'bundle invalid'):
                    cps.verify(cp)
        bundle.write_bytes(whole)
        cps.verify(cp)                                                      # positive control
        bundle.unlink()                                       # a missing bundle is refused the same way
        with self.assertRaisesRegex(WorkspaceError, 'bundle invalid'):
            cps.verify(cp)
        bundle.write_bytes(whole)
        cps.verify(cp)

    def test_corruption_during_capture_rejects_the_slim_snapshot(self):
        self._simple_dirty(self.base)
        real_copy, real_bundle = Checkpoints._copy_delta, Checkpoints._bundle

        def bad_copy(s, source, dst, full, delta):
            real_copy(s, source, dst, full, delta)
            (dst / 'exec').write_text('tampered\n')

        def bad_bundle(s, source, pending, refs, base_sha, stash_ref):
            info = real_bundle(s, source, pending, refs, base_sha, stash_ref)
            if base_sha:            # only the slim form; the full form must stay restorable
                out = pending / 'commits.bundle'
                out.write_bytes(out.read_bytes()[:-40])
            return info

        def no_bundle(s, source, pending, refs, base_sha, stash_ref):
            info = real_bundle(s, source, pending, refs, base_sha, stash_ref)
            if base_sha:            # a read error is a refusal like any other, never an escaping OSError
                (pending / 'commits.bundle').unlink()
            return info
        def attributes_changed():
            (self.base / '.gitattributes').write_text('# a comment\n')   # the proof hands over at once
        cases = (('_copy_delta', bad_copy, 'snapshot-mismatch', None),
                 ('_bundle', bad_bundle, 'bundle-invalid', None),
                 ('_bundle', no_bundle, 'bundle-invalid', None),
                 ('_bundle', bad_bundle, 'bundle-invalid', attributes_changed))
        for i, (name, fake, reason, before) in enumerate(cases):
            with self.subTest(name, handed_over=before is not None), patch.object(Checkpoints, name, fake):
                if before:
                    before()
                cps, cp = self._capture(self.base, key=f'cp{i}')
                manifest = cps.read(cp)
                self.assertTrue(manifest.get('slim_failed'))
                self.assertEqual(manifest.get('slim_failure_reason'), reason)
                self.assertNotIn('delta', manifest)
        with self.assertLogs('mesh_runner', 'INFO') as logs:
            cps, cp = self._capture(self.base, key='cp-intact')               # positive control
        self.assertTrue(any('not decided (attributes)' in line for line in logs.output))
        self.assertFalse(cps.read(cp).get('slim_failed'))
        self.assertIn('delta', cps.read(cp))

    def test_a_git_view_only_the_source_has_is_not_accepted(self):
        # With core.filemode=false the source's status hides a mode change; the replay's repository (a
        # fresh `git init`, like a fresh clone at restore) shows it, so the replay refuses the snapshot.
        # The proof must not accept it: as before, the capture refuses and keeps the lease.
        git(self.base, 'config', 'core.filemode', 'false')
        (self.base / 'README').chmod(0o755)
        self.assertEqual(git(self.base, 'status', '--porcelain'), '')
        with self.assertLogs('mesh_runner', 'INFO') as logs:
            with self.assertRaisesRegex(WorkspaceError, 'replay verification failed'):
                self._capture(self.base)
        self.assertTrue(any('not decided (git-view)' in line for line in logs.output))
        (self.base / 'README').chmod(0o644)                                  # positive control
        replays = []
        real = Checkpoints._replay
        with patch.object(Checkpoints, '_replay', lambda s, *a, **k: replays.append(a) or real(s, *a, **k)):
            cps, cp = self._capture(self.base, key='cp-644')
        self.assertEqual(replays, [])
        self.assertFalse(cps.read(cp).get('slim_failed'))

    def test_checkout_conversion_is_left_to_the_replay(self):
        # The worktree holds the blob's bytes, but checkout would write CRLF: the proof must not
        # accept what the replay refuses. Same outcome as before: the full form.
        (self.base / '.gitattributes').write_text('*.txt eol=crlf\n')
        (self.base / 'a.txt').write_bytes(b'line\n')
        git(self.base, 'add', '.'); git(self.base, 'commit', '-q', '-m', 'crlf')
        (self.base / 'a.txt').write_bytes(b'line\n')      # LF, as committed: git status calls it clean
        git(self.base, 'update-index', '--refresh')
        self.assertEqual(git(self.base, 'status', '--porcelain'), '')
        with self.assertLogs('mesh_runner', 'INFO') as logs:
            cps, cp = self._capture(self.base)
        self.assertTrue(any('proof in place not decided (conversion)' in line for line in logs.output))
        self.assertTrue(cps.read(cp).get('slim_failed'))

    def _restores_into_a_fresh_clone(self, cps, cp, name: str) -> None:
        target = self.root / name
        git(self.root, 'clone', '-q', str(self.root / 'origin.git'), str(target))
        cps.restore(cp, target)
        manifest = cps.read(cp)
        self.assertEqual(fingerprint(target, None, tuple(manifest['excludes']), tuple(manifest.get('ignored', []))),
                         manifest['fingerprint'])

    def test_attributes_are_read_where_the_replay_reads_them(self):
        # The replay checks out in a fresh repository: HEAD's `.gitattributes`, the user's attributes
        # file and that repository's own `info/attributes`, never the source's. A local override in the
        # source must neither hide a conversion from the proof nor invent one.
        (self.base / '.gitattributes').write_text('*.txt text eol=crlf\n')
        (self.base / 'a.txt').write_bytes(b'line\n')
        git(self.base, 'add', '.'); git(self.base, 'commit', '-q', '-m', 'attributes')
        info = self.base / '.git' / 'info' / 'attributes'
        info.parent.mkdir(exist_ok=True)
        info.write_text('*.txt eol=lf\n')                         # only the source checks out LF
        self.assertEqual(git(self.base, 'status', '--porcelain'), '')
        with self.assertLogs('mesh_runner', 'INFO') as logs:
            cps, cp = self._capture(self.base)
        self.assertTrue(any('not decided (conversion)' in line for line in logs.output))
        self.assertTrue(cps.read(cp).get('slim_failed'))          # as before the proof: the full form
        cps.verify(cp)
        self._restores_into_a_fresh_clone(cps, cp, 'restored')
        # The other way round: HEAD converts nothing, only the source's override would. The replay
        # writes the blobs as they are, so the proof decides in place.
        (self.base / '.gitattributes').write_text('*.txt text\n')
        git(self.base, 'add', '.'); git(self.base, 'commit', '-q', '-m', 'no eol')
        info.write_text('*.txt eol=crlf\n')
        self.assertEqual(git(self.base, 'status', '--porcelain'), '')
        replays = []
        real = Checkpoints._replay
        with patch.object(Checkpoints, '_replay', lambda s, *a, **k: replays.append(a) or real(s, *a, **k)):
            cps, cp = self._capture(self.base, key='cp-inverse')
        self.assertEqual(replays, [])
        self.assertFalse(cps.read(cp).get('slim_failed'))
        self._restores_into_a_fresh_clone(cps, cp, 'restored-inverse')

    def test_config_is_read_where_the_replay_reads_it(self):
        # The source's own config is not the replay's: a user-wide core.autocrlf that the source turns
        # off locally still converts on the replay's checkout.
        (self.base / 'a.txt').write_bytes(b'line\n')
        git(self.base, 'add', '.'); git(self.base, 'commit', '-q', '-m', 'text')
        git(self.base, 'config', 'core.autocrlf', 'false')
        user = self.root / 'user.gitconfig'
        user.write_text('[core]\n\tautocrlf = true\n')
        with patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(user)}):
            self.assertEqual(git(self.base, 'status', '--porcelain'), '')
            with self.assertLogs('mesh_runner', 'INFO') as logs:
                cps, cp = self._capture(self.base)
            self.assertTrue(any('not decided (conversion)' in line for line in logs.output))
            self.assertTrue(cps.read(cp).get('slim_failed'))
            cps.verify(cp)
        user.write_text('[core]\n\tautocrlf = input\n')                      # positive control
        with patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(user)}):
            cps, cp = self._capture(self.base, key='cp-input')
            self.assertFalse(cps.read(cp).get('slim_failed'))
            cps.verify(cp)

    def test_a_checkout_without_symlinks_is_left_to_the_replay(self):
        # With core.symlinks=false the replay's checkout writes a link as a file holding its target;
        # the proof predicts a link from HEAD's mode, so it hands over (the source's local
        # symlinks=true is not the replay's config), and the replay decides (here it refuses).
        (self.base / 'link').symlink_to('README')
        git(self.base, 'add', '.'); git(self.base, 'commit', '-q', '-m', 'link')
        git(self.base, 'config', 'core.symlinks', 'true')
        user = self.root / 'user.gitconfig'
        user.write_text('[core]\n\tsymlinks = false\n')
        with patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(user)}):
            self.assertEqual(git(self.base, 'status', '--porcelain'), '')
            with self.assertLogs('mesh_runner', 'INFO') as logs:
                cps, cp = self._capture(self.base)
            self.assertTrue(any('not decided (symlinks)' in line for line in logs.output))
            self.assertTrue(cps.read(cp).get('slim_failed'))
            cps.verify(cp)
            self._restores_into_a_fresh_clone(cps, cp, 'restored')
        user.write_text('[core]\n\tsymlinks = true\n')                       # positive control
        replays = []
        real = Checkpoints._replay
        with patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(user)}), \
                patch.object(Checkpoints, '_replay', lambda s, *a, **k: replays.append(a) or real(s, *a, **k)):
            cps, cp = self._capture(self.base, key='cp-symlinks')
        self.assertEqual(replays, [])
        self.assertFalse(cps.read(cp).get('slim_failed'))

    def test_git_config_does_not_hide_the_replays_config(self):
        # `GIT_CONFIG` redirects only the `git config` command; the replay's checkout still reads the
        # user-wide config. An empty one must not hide its `core.symlinks=false` from the proof.
        (self.base / 'link').symlink_to('README')
        git(self.base, 'add', '.'); git(self.base, 'commit', '-q', '-m', 'link')
        git(self.base, 'config', 'core.symlinks', 'true')
        user = self.root / 'user.gitconfig'
        user.write_text('[core]\n\tsymlinks = false\n')
        mask = self.root / 'mask.gitconfig'
        mask.write_text('')
        with patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(user), 'GIT_CONFIG': str(mask)}):
            with self.assertLogs('mesh_runner', 'INFO') as logs:
                cps, cp = self._capture(self.base)
            self.assertTrue(any('not decided (symlinks)' in line for line in logs.output), logs.output)
            self.assertTrue(cps.read(cp).get('slim_failed'))
            cps.verify(cp)
            self._restores_into_a_fresh_clone(cps, cp, 'restored')
        # Positive controls: with the user-wide config the replay accepts, neither an empty `GIT_CONFIG`
        # nor one holding what would hand over (symlinks=false, an unmodelled key) reaches the proof.
        user.write_text('[core]\n\tsymlinks = true\n')
        real = Checkpoints._replay
        for n, masked in enumerate(('', '[core]\n\tsymlinks = false\n[diff]\n\torderFile = ../order\n')):
            with self.subTest(masked=masked):
                mask.write_text(masked)
                replays = []
                with patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(user), 'GIT_CONFIG': str(mask)}), \
                        patch.object(Checkpoints, '_replay', lambda s, *a, **k: replays.append(a) or real(s, *a, **k)):
                    cps, cp = self._capture(self.base, key=f'cp-git-config-{n}')
                    self.assertEqual(replays, [])
                    self.assertFalse(cps.read(cp).get('slim_failed'))
                    cps.verify(cp)
                    self._restores_into_a_fresh_clone(cps, cp, f'restored-{n}')

    def test_unlisted_checkout_config_is_left_to_the_replay(self):
        # Only the checkout settings the proof models pass: any other key in `core`, `checkout` or
        # `submodule` hands over by name, so a setting not thought of costs a replay, never a slim
        # snapshot the replay refuses.
        self._dirty(self.base)
        cps, cp = self._capture(self.base)
        data = cps.read(cp)
        user = self.root / 'user.gitconfig'
        cases = (('[core]\n\tsparseCheckout = true\n', 'config core.sparsecheckout'),
                 ('[core]\n\tsomethingNew = 1\n', 'config core.somethingnew'),
                 ('[submodule]\n\trecurse = true\n', 'config submodule.recurse'),
                 ('[core]\n\teol = crlf\n', 'conversion'),
                 ('[core]\n\tsymlinks\n\tsymlinks = off\n', 'symlinks'))      # the last value counts
        for config, reason in cases:
            with self.subTest(reason):
                user.write_text(config)
                with patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(user)}), \
                        self.assertRaises(_NotProvable) as caught:
                    cps._prove_in_place(cp, data)
                self.assertEqual(caught.exception.reason, reason)
        # Positive control: listed keys with modelled values are decided in place.
        user.write_text('[core]\n\teditor = vi\n\tsymlinks\n\tautocrlf = input\n\teol = lf\n'
                        '[checkout]\n\tworkers = 2\n[user]\n\tname = t\n')
        with patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(user)}):
            cps._prove_in_place(cp, data)

    def test_conditional_config_is_left_to_the_replay(self):
        # A conditional include resolves by the repository it is read in: `onbranch` matches the
        # replay's fresh repository (HEAD on its unborn default branch at checkout, on the recorded
        # branch at the fingerprint) and not a detached probe; `gitdir` by directory. The proof does
        # not model that: it hands over, and the replay decides (here it refuses: the full form).
        (self.base / 'a.txt').write_bytes(b'line\n')
        git(self.base, 'add', '.'); git(self.base, 'commit', '-q', '-m', 'text')
        git(self.base, 'config', 'core.autocrlf', 'false')
        self.assertEqual(git(self.base, 'symbolic-ref', 'HEAD'), 'refs/heads/main')
        crlf = self.root / 'crlf.gitconfig'
        crlf.write_text('[core]\n\tautocrlf = true\n')
        user = self.root / 'user.gitconfig'
        nested = self.root / 'nested.gitconfig'                # reached through a plain include
        nested.write_text(f'[includeIf "onbranch:main"]\n\tpath = {crlf}\n')
        user.write_text(f'[include]\n\tpath = {nested}\n')
        with patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(user)}):
            self.assertEqual(git(self.base, 'status', '--porcelain'), '')
            with self.assertLogs('mesh_runner', 'INFO') as logs:
                cps, cp = self._capture(self.base)
            self.assertTrue(any('not decided (conditional-config)' in line for line in logs.output))
            self.assertTrue(cps.read(cp).get('slim_failed'))
            cps.verify(cp)
            self._restores_into_a_fresh_clone(cps, cp, 'restored')
        # Positive control: a plain include is the same file wherever it is read: decided in place.
        nested.write_text('[core]\n\tautocrlf = input\n')
        replays = []
        real = Checkpoints._replay
        with patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(user)}), \
                patch.object(Checkpoints, '_replay', lambda s, *a, **k: replays.append(a) or real(s, *a, **k)):
            cps, cp = self._capture(self.base, key='cp-plain-include')
        self.assertEqual(replays, [])
        self.assertFalse(cps.read(cp).get('slim_failed'))

    def test_programs_from_config_never_run_in_the_source(self):
        # A hook the replay runs or an fsmonitor starts in the work tree it runs for: for the proof that
        # is the capture source. The proof decides them before its first index read and hands over; the
        # replay runs them in its scratch target (the marker proves they are live). The capture's own
        # Git commands in the source start them as any Git command there does: not the proof's doing.
        (self.base / 'a.txt').write_bytes(b'line\n')
        git(self.base, 'add', '.'); git(self.base, 'commit', '-q', '-m', 'text')
        marker = self.root / 'ran-in'
        hooks = self.root / 'hooks'
        hooks.mkdir()
        script = self.root / 'record.sh'
        script.write_text(f'#!/bin/sh\npwd -P >> "{marker}"\nexit 1\n')
        script.chmod(0o755)
        cases = (('hooks', f'[core]\n\thooksPath = {hooks}\n', 'post-index-change'),
                 ('fsmonitor', f'[core]\n\tfsmonitor = {script}\n', None))
        for reason, config, hook in cases:
            with self.subTest(reason):
                if hook:
                    shutil.copy2(script, hooks / hook)
                user = self.root / f'user-{reason}.gitconfig'
                user.write_text(config)
                cps, cp = self._capture(self.base, key=f'cp-{reason}')
                marker.write_text('')
                with patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(user)}):
                    with self.assertRaises(_NotProvable) as caught:
                        cps._prove_in_place(cp, cps.read(cp))
                    self.assertEqual(caught.exception.reason, reason)
                    self.assertEqual(marker.read_text(), '', 'the proof started nothing')
                    with self.assertLogs('mesh_runner', 'INFO') as logs:
                        self._capture(self.base, key=f'cp-{reason}-live')
                    self.assertTrue(any(f'not decided ({reason})' in line for line in logs.output))
                    marker.write_text('')
                    cps.verify(cp)
                ran = marker.read_text().split()
                self.assertTrue(ran and all('/.verify-' in r for r in ran),
                                f'positive control: the program ran, in the replay target: {ran}')
        # Positive control: a hook none of these commands runs is not a reason to hand over.
        (hooks / 'post-index-change').unlink()
        shutil.copy2(script, hooks / 'pre-commit')
        replays = []
        real = Checkpoints._replay
        with patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(self.root / 'user-hooks.gitconfig')}), \
                patch.object(Checkpoints, '_replay', lambda s, *a, **k: replays.append(a) or real(s, *a, **k)):
            cps, cp = self._capture(self.base, key='cp-other-hook')
        self.assertEqual(replays, [])
        self.assertFalse(cps.read(cp).get('slim_failed'))

    def test_a_relative_attributes_file_is_left_to_the_replay(self):
        # Git resolves a relative core.attributesFile against the work tree: next to the source for the
        # proof, next to the scratch target (at restore, the restore target) for the replay. Here the two
        # name different files: the source sees eol=lf, the replay's checkout writes CRLF. The proof hands
        # over; the replay refuses the slim form and the full form restores.
        (self.base / 'a.txt').write_text('line\n')
        git(self.base, 'add', '.'); git(self.base, 'commit', '-q', '-m', 'text')
        cps = Checkpoints(self.root / 'cps')
        cps.root.mkdir(exist_ok=True)
        (self.root / 'attrs').write_text('*.txt text eol=lf\n')
        (cps.root / 'attrs').write_text('*.txt text eol=crlf\n')
        parent = self.root / 'restore-parent'
        parent.mkdir()
        (parent / 'attrs').write_text('*.txt text eol=crlf\n')
        user = self.root / 'user.gitconfig'
        user.write_text('[core]\n\tattributesFile = ../attrs\n')
        base = git(self.base, 'rev-parse', 'origin/main')
        with patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(user)}):
            self.assertEqual(git(self.base, 'status', '--porcelain'), '')
            with self.assertLogs('mesh_runner', 'INFO') as logs:
                cp = cps.create('cp', self.base, {'base_sha': base})
            self.assertTrue(any('not decided (relative core.attributesfile)' in line for line in logs.output))
            self.assertTrue(cps.read(cp).get('slim_failed'))
            cps.verify(cp)
            target = parent / 'fresh'
            git(self.root, 'clone', '-q', str(self.root / 'origin.git'), str(target))
            cps.restore(cp, target)
            self.assertEqual((target / 'a.txt').read_bytes(), b'line\n')
        user.write_text(f'[core]\n\tattributesFile = {self.root / "attrs"}\n')     # positive control
        replays = []
        real = Checkpoints._replay
        with patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(user)}), \
                patch.object(Checkpoints, '_replay', lambda s, *a, **k: replays.append(a) or real(s, *a, **k)):
            cp = cps.create('cp-absolute', self.base, {'base_sha': base})
        self.assertEqual(replays, [])
        self.assertFalse(cps.read(cp).get('slim_failed'))

    def test_settings_resolved_against_the_work_tree_are_left_to_the_replay(self):
        # A relative path in the environment or the config names a file next to the work tree: the
        # source for the proof, the scratch target for the replay. Each case arranges the two sides to
        # differ where it can, and then the replay refuses (the control that the case is real). The proof
        # hands over by name. Config outside the modelled set (a driver Git starts from attributes, a URL
        # rewrite) hands over by section and key, never the subsection: it may hold a URL.
        (self.base / '.gitattributes').write_text('a.txt diff=conv\n')
        (self.base / 'a.txt').write_text('one\n')
        (self.base / 'b.txt').write_text('two\n')
        git(self.base, 'add', '.'); git(self.base, 'commit', '-q', '-m', 'text')
        (self.base / 'a.txt').write_text('one more\n')
        (self.base / 'new.txt').write_text('new\n')
        cps, cp = self._capture(self.base)
        data = cps.read(cp)
        self.assertFalse(data.get('slim_failed'))
        cps._prove_in_place(cp, data)                                         # positive controls
        cps.verify(cp)
        conv = self.root / 'conv.sh'                    # the source side of `../`: present, a no-op
        conv.write_text('#!/bin/sh\ncat "$1"\n')
        conv.chmod(0o755)
        (self.root / 'order').write_text('a.txt\n')
        crlf = '*.txt text eol=crlf\n'
        cases = (   # reason, user config, environment, files next to the scratch target, the replay refuses
            ('relative core.attributesfile', '[core]\n\tattributesFile = ../attrs\n', {}, {'attrs': crlf}),
            ('relative core.excludesfile', '[core]\n\texcludesFile = ../ignore\n', {}, {'ignore': 'new.txt\n'}),
            ('relative init.templatedir', '[init]\n\ttemplateDir = ../tmpl\n', {},
             {'tmpl/info/attributes': crlf}),
            ('relative GIT_TEMPLATE_DIR', None, {'GIT_TEMPLATE_DIR': '../tmpl'}, {'tmpl/info/attributes': crlf}),
            ('relative XDG_CONFIG_HOME', None, {'XDG_CONFIG_HOME': '..'}, {'git/attributes': crlf}),
            ('relative HOME', None, {'HOME': '..'}, {'.config/git/attributes': crlf}),
            ('relative GIT_CONFIG_GLOBAL', None, {'GIT_CONFIG_GLOBAL': '../user.gitconfig'},
             {'user.gitconfig': '[core]\n\tautocrlf = true\n'}),
            ('config diff.orderfile', '[diff]\n\torderFile = ../order\n', {}, {}),
            ('config diff.textconv', '[diff "conv"]\n\ttextconv = ../conv.sh\n', {}, {}),
            ('config filter.clean', '[filter "tidy"]\n\tclean = cat\n', {}, None),
            ('config url.insteadof', '[url "https://example.invalid/"]\n\tinsteadOf = /x/\n', {}, None),
        )
        for reason, config, environment, files in cases:
            with self.subTest(reason):
                env = dict(environment)
                if config is not None:
                    user = self.root / 'case.gitconfig'
                    user.write_text(config)
                    env['GIT_CONFIG_GLOBAL'] = str(user)
                for rel, text in (files or {}).items():
                    (cps.root / rel).parent.mkdir(parents=True, exist_ok=True)
                    (cps.root / rel).write_text(text)
                try:
                    with patch.dict(os.environ, env):
                        with self.assertRaises(_NotProvable) as caught:
                            cps._prove_in_place(cp, data)
                        self.assertEqual(caught.exception.reason, reason)
                        if files is not None:
                            with self.assertRaises(WorkspaceError):
                                cps.verify(cp)
                finally:
                    for rel in files or {}:
                        top = cps.root / rel.split('/')[0]
                        shutil.rmtree(top) if top.is_dir() else top.unlink()
        cps.verify(cp)
        # Positive control: the settings a fleet host has, with absolute paths, are decided in place.
        (self.root / 'tmpl').mkdir()
        included = self.root / 'included.gitconfig'
        included.write_text('[user]\n\temail = t@example.invalid\n')
        user = self.root / 'case.gitconfig'
        user.write_text('[credential]\n\thelper = osxkeychain\n'
                        '[credential "https://example.invalid"]\n\thelper = !true\n'
                        '[safe]\n\tdirectory = *\n[user]\n\tname = t\n'
                        f'[init]\n\tdefaultBranch = main\n\ttemplateDir = {self.root / "tmpl"}\n'
                        f'[include]\n\tpath = {included}\n'
                        f'[core]\n\tattributesFile = {self.root / "attrs"}\n\texcludesFile = ~/absent-ignore\n')
        with patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(user), 'XDG_CONFIG_HOME': str(self.root)}):
            cps._prove_in_place(cp, data)

    def test_the_proof_names_what_it_cannot_decide(self):
        self._dirty(self.base)
        cps, cp = self._capture(self.base)
        data = cps.read(cp)
        cps._prove_in_place(cp, data)                                         # positive control
        cases = (
            ('content', lambda d: d['fingerprint']['tree']['tracked'].__setitem__(3, '0' * 64)),
            ('layout', lambda d: d['fingerprint']['tree'].pop('sub')),
            ('git-view', lambda d: d['fingerprint'].__setitem__('staged', '0' * 64)),
            ('git-view', lambda d: d['fingerprint'].__setitem__('unstaged', '0' * 64)),
            ('git-view', lambda d: d['fingerprint'].__setitem__('status', '0' * 64)),
            ('skip-tracked', lambda d: d.__setitem__('ignored', ['sub'])),     # sub/stays is tracked
            ('form', lambda d: d.pop('delta')),
            ('objects', lambda d: d.__setitem__('objects', str(self.root))),
        )
        for i, (reason, mutate) in enumerate(cases):
            with self.subTest(reason, case=i):
                broken = json.loads(json.dumps(data))
                mutate(broken)
                with self.assertRaises(_NotProvable) as caught:
                    cps._prove_in_place(cp, broken)
                self.assertEqual(caught.exception.reason, reason)


if __name__ == '__main__':
    unittest.main()
