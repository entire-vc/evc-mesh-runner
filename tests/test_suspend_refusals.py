"""A refused snapshot of a card's working copies is a declared hold.

A lane's slot held a submodule whose repository had been cloned into its own path
(`vendor/<kit>/.git` a directory) beside a stale absorbed gitdir. Every settle, prompt-delivery
and workspace-suspend of the card in that slot was refused for three days (hundreds of runner.log lines),
the slot stayed leased, and nothing reached the card or an alert. Now the refusal names the layout,
is counted in a durable streak per card, is reported in status.json (the fleet alert reads it) and
is said once on the card; a snapshot that goes through ends the streak.
"""
import shutil
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

from mesh_runner.workspace import git
from test_runner import Bench
from test_workspace import make_repo
import test_workspace_requests  # module import: its TestCase is not collected here a second time

NOTICE = ('[runner] Workspace snapshot keeps failing ({step}: populated submodule unsupported (embedded .git '
          'directory)); the worktree lease is retained and the step is retried with a growing backoff. '
          'Nothing is lost; the runner owner must repair the cause.')


def _modules(path):
    modules = Path(git(path, 'rev-parse', '--git-path', 'modules'))
    return modules if modules.is_absolute() else path / modules


class SuspendRefusalsAreDeclared(Bench):
    parked = test_workspace_requests.WorkspaceRequests.parked

    def embedded_submodule(self, slot):
        """The layout met in production: the submodule's repository cloned into its own path
        (`.git` a directory), a stale absorbed gitdir left under the slot's modules directory."""
        ext = self.root / 'ext-kit'
        ext.mkdir()
        external = make_repo(ext)
        git(slot, '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(external), 'vendor/kit')
        git(slot, 'commit', '-qm', 'Add kit')
        sub, stale = slot / 'vendor' / 'kit', _modules(slot) / 'vendor' / 'kit'
        (sub / '.git').unlink()
        shutil.copytree(stale, sub / '.git', symlinks=True)
        subprocess.run(['git', 'config', '--file', str(sub / '.git' / 'config'), '--unset', 'core.worktree'], check=True)
        self.assertEqual(git(sub, 'rev-parse', '--absolute-git-dir'), str((sub / '.git').resolve()))
        return sub, stale

    def absorb(self, slot, stale):
        """The repair: the stale gitdir goes away, the embedded one is absorbed into its place."""
        shutil.rmtree(stale)
        git(slot, 'submodule', 'absorbgitdirs', '--', 'vendor/kit')

    def refuse(self, r, a, what, step, rounds=(0,)):
        # mock: external boundary — only the monotonic clock (the step quarantine's growing pause).
        clock = time.monotonic()
        for offset in rounds:
            with patch('mesh_runner.runner.time.monotonic', return_value=clock + offset):
                for _ in range(3):
                    r._guard(r.store.get(a.id), what, step)

    def notices(self, r, a):
        return [c['body'] for c in r.mesh.comments_all(a.task_id) if 'snapshot keeps failing' in c['body']]

    def test_refused_wait_snapshot_is_counted_reported_and_said_once(self):
        r, (a,) = self.parked()
        slot = Path(a.workspaces[0])
        self.embedded_submodule(slot)
        self.assertNotIn('suspend_refusals', r.status())
        self.refuse(r, a, 'workspace-suspend', lambda x: r._suspend_waiting(x, requested=True), rounds=(0, 901))
        rows = r.status()['suspend_refusals']
        self.assertEqual([(x['task_id'], x['task'], x['count'], x['site']) for x in rows],
                         [(a.task_id, a.task8, 6, 'wait')])
        self.assertIn('populated submodule unsupported (embedded .git directory): vendor/kit', rows[0]['error'])
        self.assertEqual(rows[0]['target'], str(slot))           # the slot to repair, not only the path inside it
        self.assertTrue(r.pool.leased_to(a.id))                 # the slot is kept as it is
        self.assertTrue((slot / 'vendor' / 'kit' / '.git').is_dir())
        said = self.notices(r, a)
        self.assertEqual(said, [NOTICE.format(step='workspace-suspend')])
        self.assertNotIn('vendor/kit', said[0])

    def test_refused_ownership_snapshot_in_settle_is_declared(self):
        r, (a,) = self.parked()
        self.embedded_submodule(Path(a.workspaces[0]))
        r.store.update(a, turns=1)
        departed = dict(r.mesh.tasks[a.task_id], assignee_id='agent-lead')
        self.refuse(r, a, 'settle', lambda x: r._note_ownership_loss(x, departed))
        rows = r.status()['suspend_refusals']
        self.assertEqual([(x['task_id'], x['count'], x['site']) for x in rows], [(a.task_id, 3, 'ownership')])
        self.assertEqual(self.notices(r, a), [NOTICE.format(step='settle')])
        self.assertTrue(r.pool.leased_to(a.id))

    def test_streak_survives_a_restart_and_ends_when_the_slot_is_repaired(self):
        r, (a,) = self.parked()
        slot = Path(a.workspaces[0])
        _, stale = self.embedded_submodule(slot)
        step = 'workspace-suspend'
        self.refuse(r, a, step, lambda x: r._suspend_waiting(x, requested=True))
        r = self.runner(r.mesh)                                 # a deploy restarts the runner
        self.refuse(r, a, step, lambda x: r._suspend_waiting(x, requested=True))
        self.assertEqual([x['count'] for x in r.status()['suspend_refusals']], [6])
        self.assertEqual(len(self.notices(r, a)), 1)            # said once per attempt, restart included
        self.absorb(slot, stale)
        r._quarantine.clear()
        r._guard(r.store.get(a.id), step, lambda x: r._suspend_waiting(x, requested=True))
        self.assertEqual(r.pool.leased_to(a.id), [])
        self.assertNotIn('suspend_refusals', r.status())
        self.assertEqual(r.store.mark('builder', a.task_id, 'suspend_refusals')[0], '')

    def test_a_streak_whose_slot_is_no_longer_held_leaves_the_status(self):
        r, (a,) = self.parked()
        self.embedded_submodule(Path(a.workspaces[0]))
        self.refuse(r, a, 'workspace-suspend', lambda x: r._suspend_waiting(x, requested=True))
        self.assertIn('suspend_refusals', r.status())
        r.store.db.execute("UPDATE slots SET attempt='', state='free' WHERE attempt=?", (a.id,))
        self.assertNotIn('suspend_refusals', r.status())
