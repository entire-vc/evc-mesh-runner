"""A repository with a configured submodule is captured in the slim form and proven in place.

Before, any `.gitmodules` sent the capture to the full form: the whole worktree copied into `.pending-*`
and, since the in-place proof refused it (`form`), the whole checkout laid out again in `.verify-*` and
removed. That was thousands of file-system events per suspension of such a slot, a clean
one included. Now the superproject is captured as a delta over HEAD and so is every populated submodule
(over its own HEAD, from its own bundle), and the proof decides in place. Real git, real filesystem."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mesh_runner.checkpoint import Checkpoints, fingerprint, submodule_plan
from mesh_runner.store import Store
from mesh_runner.workspace import RepoSpec, WorkspacePool, WorkspaceError, git
from test_checkpoint_proof_in_place import FileCount
from test_workspace import make_repo

FILE_PROTOCOL = ('-c', 'protocol.file.allow=always')


class SubmoduleSlim(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # The replay reads the user-wide config: an empty one here, so the host's own settings never
        # decide these tests. A local submodule URL needs the file protocol: given per command
        # (`FILE_PROTOCOL`), never in that config, which the proof in place would refuse.
        self.user = self.root / 'user.gitconfig'
        self.user.write_text('')
        isolation = patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(self.user), 'GIT_CONFIG_NOSYSTEM': '1'})
        isolation.start()
        self.addCleanup(isolation.stop)
        for var in ('GIT_CONFIG', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS', 'XDG_CONFIG_HOME', 'GIT_TEMPLATE_DIR'):
            os.environ.pop(var, None)
        self.base = make_repo(self.root)
        (self.root / 'ext').mkdir()
        self.ext = make_repo(self.root / 'ext')      # its `.gitignore` ignores cache/

    def tearDown(self):
        self.tmp.cleanup()

    def _commit_many(self, repo: Path, count: int, prefix: str) -> None:
        for i in range(count):
            d = repo / prefix / f'd{i // 100:03d}'
            d.mkdir(parents=True, exist_ok=True)
            (d / f'f{i:05d}.txt').write_text(f'{prefix} {i}\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '-q', '-m', f'many {prefix}')
        git(repo, 'push', '-q', 'origin', 'HEAD:main')

    def _with_submodule(self, outer: int = 0, inner: int = 0) -> None:
        if inner:
            self._commit_many(self.ext, inner, 'kit')
        if outer:
            self._commit_many(self.base, outer, 'src')
        git(self.base, *FILE_PROTOCOL, 'submodule', 'add', '-q', str(self.root / 'ext' / 'origin.git'), 'vendor/kit')
        git(self.base, 'commit', '-q', '-m', 'add kit')
        git(self.base, 'push', '-q', 'origin', 'HEAD:main')

    def _pool(self):
        return WorkspacePool(Store(self.root / 's.sqlite'), self.root / 'pool',
                             [RepoSpec('app', self.base, max_slots=1)], owner='agent')

    def _slot(self, populate: bool):
        pool = self._pool()
        slot = pool.lease('attempt', 'app')
        if populate:
            git(slot, *FILE_PROTOCOL, 'submodule', 'update', '-q', '--init')
        return pool, slot

    def _suspend_counted(self, pool, slot, populated: bool):
        plan = submodule_plan(slot)
        self.assertEqual([(e['path'], e['populated'], e['gitdir']) for e in plan],
                         [('vendor/kit', populated, populated)])
        expected = fingerprint(slot, plan)
        root = pool._checkpoints().root
        with FileCount(root) as count:
            pool.suspend('attempt', {'session_id': 's', 'wait': {'target': 'card'}}, writer_stopped=True)
        self.assertTrue(any(p.name == 'manifest.json' for p in root.rglob('*')))   # it did capture here
        return plan, expected, count

    def test_clean_superproject_with_a_populated_submodule_creates_at_most_50_files(self):
        self._with_submodule(outer=1200, inner=1200)
        pool, slot = self._slot(populate=True)
        self.assertGreaterEqual(sum(1 for p in slot.rglob('*') if p.is_file() and '.git' not in p.parts), 2000)
        plan, expected, count = self._suspend_counted(pool, slot, populated=True)
        self.assertLessEqual(count.created, 50, f'{count.created} files, scratch {count.scratch}')
        self.assertFalse([n for n in count.scratch if n.startswith('.verify-')])
        paths, manifests = pool.restore('attempt')
        self.assertIn('delta', manifests[0])
        self.assertEqual(fingerprint(Path(paths[0]), plan), expected)

    def test_clean_superproject_with_a_placeholder_submodule_creates_at_most_50_files(self):
        # A placeholder submodule: `.gitmodules`, the gitlink, no gitdir, an empty directory.
        self._with_submodule(outer=2000)
        pool, slot = self._slot(populate=False)
        plan, expected, count = self._suspend_counted(pool, slot, populated=False)
        self.assertLessEqual(count.created, 50, f'{count.created} files, scratch {count.scratch}')
        self.assertFalse([n for n in count.scratch if n.startswith('.verify-')])
        paths, manifests = pool.restore('attempt')
        self.assertIn('delta', manifests[0])
        self.assertEqual(fingerprint(Path(paths[0]), plan), expected)


    # -- A dirty superproject with a dirty submodule: decided in place, and the scratch replay agrees.

    def _dirty_slot(self):
        self._with_submodule(outer=30, inner=30)
        pool, slot = self._slot(populate=True)
        sub = slot / 'vendor' / 'kit'
        (slot / 'README').write_text('unstaged in the superproject\n')
        (slot / 'notes.txt').write_text('untracked in the superproject\n')
        (sub / 'kit' / 'd000' / 'f00000.txt').write_text('staged in the submodule\n')
        git(sub, 'add', '.')
        git(sub, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'unpublished in the submodule')
        (sub / 'kit' / 'd000' / 'f00001.txt').write_text('staged after the commit\n')
        git(sub, 'add', 'kit/d000/f00001.txt')
        (sub / 'kit' / 'd000' / 'f00001.txt').write_text('then changed again\n')
        (sub / 'kit' / 'd000' / 'f00002.txt').unlink()
        (sub / 'tool').write_text('echo\n'); (sub / 'tool').chmod(0o751)
        (sub / 'link').symlink_to('tool')
        (sub / 'empty').mkdir()
        (sub / 'cache').mkdir(); (sub / 'cache' / 'ignored').write_text('ignored in the submodule\n')
        return pool, slot

    def _capture(self, slot: Path, key: str = 'cp'):
        cps = Checkpoints(self.root / 'cps')
        return cps, cps.create(key, slot, {'base_sha': git(slot, 'rev-parse', 'origin/main')})

    def _restores_into_a_fresh_clone(self, cps, cp, name: str) -> Path:
        target = self.root / name
        git(self.root, 'clone', '-q', str(self.root / 'origin.git'), str(target))
        cps.restore(cp, target)
        manifest = cps.read(cp)
        self.assertEqual(fingerprint(target, submodule_plan(target), tuple(manifest['excludes']),
                                     tuple(manifest.get('ignored', []))), manifest['fingerprint'])
        return target

    def test_dirty_submodule_is_proven_in_place_and_the_replay_agrees(self):
        pool, slot = self._dirty_slot()
        replays = []
        real = Checkpoints._replay
        with patch.object(Checkpoints, '_replay', lambda s, *a, **k: replays.append(a) or real(s, *a, **k)):
            cps, cp = self._capture(slot)
            self.assertEqual(replays, [])                 # no scratch replay at capture
            manifest = cps.read(cp)
            self.assertEqual(manifest['version'], 3)
            self.assertIn('vendor/kit/cache', manifest.get('ignored', []))
            self.assertNotIn('vendor/kit/cache/ignored', manifest['delta']['files'])
            self.assertIn('vendor/kit/kit/d000/f00002.txt', manifest['delta']['deleted'])
            self.assertIn('vendor/kit/empty', manifest['delta']['dirs'])
            self.assertIn('notes.txt', manifest['delta']['files'])
            self.assertFalse(any(f.startswith('vendor/kit/kit/d001/') for f in manifest['delta']['files']))
            cps.verify(cp)                                # the authority, the scratch replay, agrees
        self.assertEqual(len(replays), 1)
        target = self._restores_into_a_fresh_clone(cps, cp, 'restored')
        sub = target / 'vendor' / 'kit'
        self.assertFalse((sub / 'cache').exists())
        self.assertEqual((sub / 'kit' / 'd000' / 'f00001.txt').read_text(), 'then changed again\n')
        self.assertFalse((sub / 'kit' / 'd000' / 'f00002.txt').exists())
        self.assertEqual((sub / 'tool').stat().st_mode & 0o777, 0o751)
        self.assertEqual(os.readlink(sub / 'link'), 'tool')
        self.assertIn('unpublished in the submodule', git(sub, 'log', '-1', '--format=%s'))

    def test_a_tampered_submodule_part_is_refused(self):
        pool, slot = self._dirty_slot()
        cps, cp = self._capture(slot)
        part = cp / 'tree' / 'vendor' / 'kit' / 'tool'
        part.write_text('tampered\n')
        with self.assertRaisesRegex(WorkspaceError, 'snapshot hash mismatch'):
            cps.verify(cp)
        part.write_text('echo\n'); part.chmod(0o751)
        cps.verify(cp)                                                      # positive control
        bundle = cp / 'submodules' / '0' / 'commits.bundle'
        whole = bundle.read_bytes()
        target = self.root / 'target'
        git(self.root, 'clone', '-q', str(self.root / 'origin.git'), str(target))
        (target / 'sentinel').write_text('keep me')
        for broken in (b'garbage', whole[:-10], whole[:-20] + bytes(20)):
            with self.subTest(size=len(broken)):
                bundle.write_bytes(broken)
                with self.assertRaisesRegex(WorkspaceError, 'bundle invalid'):
                    cps.restore(cp, target)
                self.assertEqual((target / 'sentinel').read_text(), 'keep me')
                self.assertFalse((target / 'vendor' / 'kit' / '.git').exists())
        bundle.write_bytes(whole)
        self._restores_into_a_fresh_clone(cps, cp, 'restored')

    def test_a_bad_submodule_bundle_at_capture_is_not_accepted_in_place(self):
        pool, slot = self._dirty_slot()
        real = Checkpoints._replay_submodules

        def broken(s, checkpoint, *a, **k):
            bundle = checkpoint / 'submodules' / '0' / 'commits.bundle'
            bundle.write_bytes(bundle.read_bytes()[:-20] + bytes(20))
            return real(s, checkpoint, *a, **k)
        with patch.object(Checkpoints, '_replay_submodules', broken), \
                self.assertLogs('mesh_runner', 'INFO') as logs:
            cps, cp = self._capture(slot)
        self.assertTrue(any('not decided (submodule-bundle)' in line for line in logs.output), logs.output)
        # The scratch replay refuses that slim snapshot too; the capture keeps the full form, as before.
        manifest = cps.read(cp)
        self.assertTrue(manifest.get('slim_failed'))
        self.assertNotIn('delta', manifest)
        cps.verify(cp)
        self._restores_into_a_fresh_clone(cps, cp, 'restored')

    # -- The source's submodule gitdir reads the superproject's view: it must read like the replay's.

    def test_a_source_submodule_gitdir_that_reads_differently_hands_over(self):
        for name, setup, reason in (
                ('info/exclude', lambda gd: (gd / 'info').mkdir(exist_ok=True) or
                 (gd / 'info' / 'exclude').write_text('local.log\n'), 'submodule-info'),
                ('config', lambda gd: git(gd, 'config', 'status.showUntrackedFiles', 'no'), 'submodule-config')):
            with self.subTest(name):
                self.tearDown(); self.setUp()
                self._with_submodule(outer=5, inner=5)
                pool, slot = self._slot(populate=True)
                gd = Path(git(slot / 'vendor' / 'kit', 'rev-parse', '--absolute-git-dir'))
                (slot / 'vendor' / 'kit' / 'local.log').write_text('only the source hides this\n')
                setup(gd)
                with self.assertLogs('mesh_runner', 'INFO') as logs:
                    try:
                        cps, cp = self._capture(slot)
                    except WorkspaceError as e:              # as before: the scratch replay decides
                        self.assertIn('replay verification failed', str(e))
                    else:
                        cps.verify(cp)
                self.assertTrue(any(f'not decided ({reason})' in line for line in logs.output), logs.output)

    def test_a_template_that_gives_a_gitdir_something_is_used_for_the_proof(self):
        # The replay makes the submodule gitdir with `git init --bare`, so with the user's template. An
        # `info/exclude` there hides an untracked file the source shows: the replay's status differs
        # from the recorded one. A proof without the template would see the source's view and accept.
        self._with_submodule(outer=5, inner=5)
        pool, slot = self._slot(populate=True)
        (slot / 'vendor' / 'kit' / 'local.log').write_text('the replay would hide this\n')
        template = self.root / 'template'
        (template / 'info').mkdir(parents=True)
        (template / 'info' / 'exclude').write_text('*.log\n')
        replays = []
        real = Checkpoints._replay
        with patch.dict(os.environ, {'GIT_TEMPLATE_DIR': str(template)}), \
                patch.object(Checkpoints, '_replay', lambda s, *a, **k: replays.append(a) or real(s, *a, **k)), \
                self.assertLogs('mesh_runner', 'INFO') as logs:
            try:
                self._capture(slot)
            except WorkspaceError as e:
                self.assertIn('replay verification failed', str(e))
        self.assertTrue(any('not decided (submodule-info)' in line for line in logs.output), logs.output)
        self.assertTrue(replays)                          # the scratch replay decided, not the proof

    def test_template_inert_reads_what_git_init_copied(self):
        def made(template: Path | None) -> Path:
            view = Path(tempfile.mkdtemp(dir=self.root)) / 'view'
            view.mkdir()
            env = dict(os.environ, **({'GIT_TEMPLATE_DIR': str(template)} if template else {}))
            subprocess.run(['git', '-C', str(view), 'init', '--quiet'], check=True, env=env)
            return view / '.git'
        self.assertTrue(Checkpoints._template_inert(made(None)))          # git's own template
        cases = {
            'hooks/post-checkout': '#!/bin/sh\n',
            'info/exclude': '# comment\n*.log\n',
            'info/attributes': '* -text\n',
            'config': '[core]\n\tautocrlf = true\n',
            'packed-refs': '',
        }
        for rel, text in cases.items():
            with self.subTest(rel):
                template = Path(tempfile.mkdtemp(dir=self.root))
                (template / rel).parent.mkdir(parents=True, exist_ok=True)
                (template / rel).write_text(text)
                self.assertFalse(Checkpoints._template_inert(made(template)))
        quiet = Path(tempfile.mkdtemp(dir=self.root))
        (quiet / 'info').mkdir(); (quiet / 'info' / 'exclude').write_text('# only a comment\n\n')
        (quiet / 'hooks').mkdir(); (quiet / 'hooks' / 'pre-commit.sample').write_text('#!/bin/sh\n')
        (quiet / 'description').write_text('x\n')
        self.assertTrue(Checkpoints._template_inert(made(quiet)))


if __name__ == '__main__':
    unittest.main()
