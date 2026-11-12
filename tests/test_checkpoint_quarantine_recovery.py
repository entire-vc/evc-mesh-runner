"""Real SQLite, Git worktrees, checkpoint failures and ordinary tick admission."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

from mesh_runner.mesh import MeshError
from mesh_runner.adapters.base import proc_stamp
from mesh_runner.workspace import RepoSpec, WorkspaceError
from mesh_runner.workspace_scope import RECEIPT_KEY
from test_runner import Bench
from test_workspace import make_repo
from test_workspace_requests import WorkspaceRequests
from fakes import write_replies, FUTURE_AT, wait_time


class CheckpointQuarantineRecovery(Bench):
    parked = WorkspaceRequests.parked

    def failed_resume(self):
        extra = self.root / 'extra'
        extra.mkdir()
        self.agent.repos.append(RepoSpec('extra', make_repo(extra), max_slots=1))
        r, (a,) = self.parked()
        path = r.pool.lease(a.id, 'extra')
        r.store.update(a, state='ready', workspaces=[*a.workspaces, str(path)])
        (Path(a.workspaces[0]) / 'README').write_text('preserved progress\n')
        fifo = path / 'unsupported.fifo'
        os.mkfifo(fifo)   # real unsupported inventory: capture fails, retains this lease
        with self.assertRaises(WorkspaceError):
            r.pool.suspend(a.id, r._suspend_metadata(a), writer_stopped=True)
        self.assertEqual([row['phase'] for row in r.pool._checkpoint_rows(a.id)], ['released'])
        # mock: external boundary — only the monotonic clock, no internal service/DB mocks.
        clock = time.monotonic()
        with patch('mesh_runner.runner.time.monotonic', return_value=clock):
            for _ in range(3):
                r._guard(a, 'resume', r._resume_one)
        with patch('mesh_runner.runner.time.monotonic', return_value=clock + 901):
            for _ in range(3):
                r._guard(a, 'resume', r._resume_one)
            self.assertAlmostEqual(r._quarantine[(a.id, 'resume')] - (clock + 901), 1800)
        self.assertIn((a.id, 'resume'), r._quarantine)
        self.assertTrue(r.pool.leased_to(a.id))
        return r, a, fifo

    def recover(self, r, a, fifo):
        fifo.unlink()
        r.pool.suspend(a.id, r._suspend_metadata(a), writer_stopped=True)
        self.assertEqual(r.pool.leased_to(a.id), [])
        self.assertTrue(all(row['phase'] == 'released' for row in r.pool._checkpoint_rows(a.id)))

    def events(self, r, a, kind):
        return [detail for _, event, detail in r.store.events(a.id) if event == kind]

    def test_next_ordinary_tick_admits_once_without_quarantine_timer(self):
        r, a, fifo = self.failed_resume()
        deadline = r._quarantine[(a.id, 'resume')]
        self.recover(r, a, fifo)
        write_replies(self.replies, {a.task8: wait_time(why="work", event_ref="fixture")})
        r.tick()
        self.assertEqual(r.store.get(a.id).state, 'running')
        self.assertEqual(len(self.events(r, a, 'checkpoint_quarantine_recheck')), 1)
        self.assertEqual(Path(r.store.get(a.id).workspaces[0], 'README').read_text(), 'preserved progress\n')
        self.assertNotIn((a.id, 'resume'), r._quarantine)
        self.assertGreater(deadline, time.monotonic())
        self.finish_turn(r)   # real child, native ACK and output, no second writer
        r.tick()
        self.assertEqual(len(self.events(r, a, 'checkpoint_quarantine_recheck')), 1)
        self.assertEqual(len(self.events(r, a, 'launch')), 1)

    def test_failed_or_partial_recovery_keeps_quarantine(self):
        r, a, fifo = self.failed_resume()
        for _ in range(2):
            with self.assertRaises(WorkspaceError):
                r.pool.suspend(a.id, r._suspend_metadata(a), writer_stopped=True)
            r.tick()
        self.assertEqual(r.store.get(a.id).state, 'ready')
        self.assertFalse(self.events(r, a, 'checkpoint_quarantine_recheck'))

    def test_changed_attempt_generation_keeps_quarantine(self):
        r, a, fifo = self.failed_resume()
        self.recover(r, a, fifo)
        r.store.update(a, turns=a.turns + 1)
        r.tick()
        self.assertEqual(r.store.get(a.id).state, 'ready')
        self.assertFalse(self.events(r, a, 'checkpoint_quarantine_recheck'))

    def test_stale_manifest_or_replaced_checkpoint_is_not_recovery(self):
        for changed in ('manifest', 'checkpoint'):
            with self.subTest(changed=changed):
                r, a, fifo = self.failed_resume()
                self.recover(r, a, fifo)
                row = r.pool._checkpoint_rows(a.id)[0]
                if changed == 'manifest':
                    manifest = Path(row['checkpoint']) / 'manifest.json'
                    data = json.loads(manifest.read_text())
                    data['metadata']['checkpoint_generation'] = 'stale'
                    manifest.write_text(json.dumps(data))
                else:
                    # Corrupt journal control only: no actual lease or checkpoint is removed.
                    r.store.db.execute('UPDATE workspace_checkpoints SET checkpoint=? WHERE attempt=? AND repo=?',
                                       (row['checkpoint'] + '-foreign', a.id, row['repo']))
                r.tick()
                self.assertEqual(r.store.get(a.id).state, 'ready')
                self.assertFalse(self.events(r, a, 'checkpoint_quarantine_recheck'))
                self.tearDown(); self.setUp()

    def test_unrelated_resume_failure_and_other_step_are_not_cleared(self):
        r, a, fifo = self.failed_resume()
        other_key = (a.id, 'wake')
        r._quarantine[other_key] = r._quarantine[(a.id, 'resume')] + 100
        self.recover(r, a, fifo)
        r.tick()
        self.assertIn(other_key, r._quarantine)
        self.assertEqual(len(self.events(r, a, 'checkpoint_quarantine_recheck')), 1)
        self.finish_turn(r)

    def test_human_gate_survives_recovery_and_recheck_is_consumed_once(self):
        r, a, fifo = self.failed_resume()
        self.recover(r, a, fifo)
        r.mesh.tasks[a.task_id]['human_gate'] = True
        # parked() creates the attempt directly; ordinary admission records this
        # scope receipt before a later human gate can hold its resume.
        r.store.mark(a.agent, a.task_id, RECEIPT_KEY,
                     json.dumps({'attempt': a.id, 'scope': 'workspace'}))
        r.tick()
        r.tick()
        self.assertTrue(r.mesh.tasks[a.task_id]['human_gate'])
        self.assertEqual(r.store.get(a.id).state, 'ready')
        self.assertFalse(r.procs)
        self.assertEqual(len(self.events(r, a, 'checkpoint_quarantine_recheck')), 1)

    def test_unknown_attempt_does_not_recheck_or_launch(self):
        r, a, fifo = self.failed_resume()
        self.recover(r, a, fifo)
        r.store.update(a, state='unknown', note='native acceptance unresolved')
        r.resume_ready(1)
        self.assertEqual(r.store.get(a.id).state, 'unknown')
        self.assertFalse(self.events(r, a, 'checkpoint_quarantine_recheck'))

    def test_live_writer_cannot_consume_recovery(self):
        r, a, fifo = self.failed_resume()
        self.recover(r, a, fifo)
        writer = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
        try:
            r.store.update(a, pid=writer.pid, stamp=proc_stamp(writer.pid))
            r.resume_ready(1)
            self.assertIsNone(writer.poll())
            self.assertFalse(self.events(r, a, 'checkpoint_quarantine_recheck'))
            self.assertEqual(r.store.get(a.id).state, 'ready')
        finally:
            writer.terminate()
            writer.wait(timeout=5)

    def test_foreign_lease_is_preserved_when_recovery_pool_is_busy(self):
        r, a, fifo = self.failed_resume()
        self.recover(r, a, fifo)
        foreign = r.pool.lease('foreign-attempt', 'app')
        r.tick()
        self.assertEqual(r.pool.leased_to('foreign-attempt'), [str(foreign)])
        self.assertEqual(r.store.get(a.id).state, 'ready')
        self.assertFalse(r.procs)
        self.assertEqual(len(self.events(r, a, 'checkpoint_quarantine_recheck')), 1)
        r.tick()
        self.assertEqual(len(self.events(r, a, 'checkpoint_quarantine_recheck')), 1)

    def test_new_quarantine_reason_is_not_replaced_by_old_recovery(self):
        r, a, fifo = self.failed_resume()
        self.recover(r, a, fifo)
        # A newer exception hold supersedes the old bound hold, even on the same step.
        key = (a.id, 'resume')
        r._quarantine[key] += 100
        r.tick()
        self.assertIn(key, r._quarantine)
        self.assertFalse(self.events(r, a, 'checkpoint_quarantine_recheck'))

    def test_new_admission_hold_survives_checkpoint_recovery(self):
        r, a, fifo = self.failed_resume()
        self.recover(r, a, fifo)
        newer_hold = r._resume_hold[a.id] + 100
        r._resume_hold[a.id] = newer_hold
        r.tick()
        self.assertEqual(r._resume_hold[a.id], newer_hold)
        self.assertNotIn((a.id, 'resume'), r._quarantine)
        self.assertEqual(r.store.get(a.id).state, 'ready')
        self.assertFalse(r.procs)
        self.assertEqual(len(self.events(r, a, 'checkpoint_quarantine_recheck')), 1)

    def test_cross_volume_capture_failure_is_said_on_the_card_once(self):
        extra = self.root / 'extra'
        extra.mkdir()
        self.agent.repos.append(RepoSpec('extra', make_repo(extra), max_slots=1))
        r, (a,) = self.parked()
        path = r.pool.lease(a.id, 'extra')
        r.store.update(a, state='ready', workspaces=[*a.workspaces, str(path)])
        subprocess.run(['git', '-C', str(path), 'update-index', '--skip-worktree', 'README'], check=True)
        (path / 'README').write_text('hidden change\n')   # real slim replay mismatch, retains the lease
        clock = time.monotonic()
        # mock: external boundaries — the monotonic clock and the volume of the checkpoint store.
        with patch('mesh_runner.checkpoint._same_volume', return_value=False):
            with self.assertRaisesRegex(WorkspaceError, 'cross volumes; lease retained'):
                r.pool.suspend(a.id, r._suspend_metadata(a), writer_stopped=True)
            for offset in (0, 901):
                with patch('mesh_runner.runner.time.monotonic', return_value=clock + offset):
                    for _ in range(3):
                        r._guard(a, 'resume', r._resume_one)
            # A runner restart loses the in-memory strikes and quarantine; the store keeps the notice.
            r._quarantine.clear(); r._strikes.clear(); r._errors.clear()
            with patch('mesh_runner.runner.time.monotonic', return_value=clock + 5000):
                for _ in range(3):
                    r._guard(a, 'resume', r._resume_one)
        self.assertEqual(len(self.events(r, a, 'quarantine:resume')), 3)
        said = [c['body'] for c in r.mesh.comments_all(a.task_id) if 'snapshot keeps failing' in c['body']]
        self.assertEqual(said, ['[runner] Workspace snapshot keeps failing (resume: verify-fingerprint/'
                                'replay-mismatch); the worktree lease is retained and the step is retried with '
                                'a growing backoff. Nothing is lost; the runner owner must repair the cause.'])
        self.assertNotIn(str(path), said[0])
        self.assertTrue(r.pool.leased_to(a.id))

    def test_retained_lease_notice_that_mesh_refused_is_said_on_the_next_failure(self):
        extra = self.root / 'extra'
        extra.mkdir()
        self.agent.repos.append(RepoSpec('extra', make_repo(extra), max_slots=1))
        r, (a,) = self.parked()
        path = r.pool.lease(a.id, 'extra')
        r.store.update(a, state='ready', workspaces=[*a.workspaces, str(path)])
        subprocess.run(['git', '-C', str(path), 'update-index', '--skip-worktree', 'README'], check=True)
        (path / 'README').write_text('hidden change\n')
        add_comment, refused = r.mesh.add_comment, []

        def flaky(tid, body, *args, **kw):
            if 'snapshot keeps failing' in body and not refused:
                refused.append(body)
                raise MeshError(503, 'unavailable')
            return add_comment(tid, body, *args, **kw)

        clock = time.monotonic()
        # mock: external boundaries — the Mesh API (one refused write), the monotonic clock, the volume.
        with patch('mesh_runner.checkpoint._same_volume', return_value=False), \
                patch.object(r.mesh, 'add_comment', side_effect=flaky):
            with self.assertRaisesRegex(WorkspaceError, 'cross volumes; lease retained'):
                r.pool.suspend(a.id, r._suspend_metadata(a), writer_stopped=True)
            for offset in (0, 901, 5000):
                with patch('mesh_runner.runner.time.monotonic', return_value=clock + offset):
                    for _ in range(3):
                        r._guard(a, 'resume', r._resume_one)
        self.assertEqual(len(refused), 1)
        said = [c['body'] for c in r.mesh.comments_all(a.task_id) if 'snapshot keeps failing' in c['body']]
        self.assertEqual(len(said), 1)
        self.assertEqual(r.store.mark(a.agent, a.task_id, f'lease_retained_notice:{a.id}')[0],
                         'verify-fingerprint/replay-mismatch')
