"""Checkpoint path migration retains an unsent request through ordinary admission."""
import json
import sys
from pathlib import Path

from mesh_runner import interactive, launch_guard
from test_runner import Bench
import test_legacy_unsent_handoff as legacy_tests
from fakes import calls


class UnknownRestore(Bench):
    prepared = legacy_tests.LegacyHandoff.prepared

    def unknown(self):
        r, a, prompt, directory, capacity = self.prepared()
        r.pool.suspend(a.id, r._suspend_metadata(a), writer_stopped=True)
        other = r.store.create('builder', 'other-task', 'claude')
        r.pool.lease(other.id, 'app')
        r._resume_one(a)
        a = r.store.get(a.id)
        self.assertEqual(a.state, 'unknown')
        self.assertEqual(a.session_id, '')
        self.assertNotEqual(a.workspaces, a.wait['interactive_prompt_intent']['workspaces'])
        self.assertEqual([x['phase'] for x in r.pool._checkpoint_rows(a.id)], ['active'])
        return r, a, prompt, directory, capacity

    def test_owner_recovers_checkpoint_scope_without_ack_and_delivers_once(self):
        r, a, prompt, directory, _ = self.unknown()
        original = a.wait['interactive_prompt_intent']
        journal = interactive.read_delivery(directory, a.id)
        source = a.source_read_at
        r.settle_finished()
        a = r.store.get(a.id)
        self.assertEqual(a.state, 'ready')
        self.assertNotIn('interactive_prompt_intent', a.wait)
        self.assertEqual(a.wait['unsent_scope_recovery']['intent'], original)
        self.assertEqual(interactive.read_delivery(directory, a.id), journal)
        self.assertEqual(a.source_read_at, source)
        self.assertFalse(r.store.mark('builder', a.task_id, 'queue-key'))
        self.assertEqual(json.loads(r.store.mark('builder', a.task_id,
            'unsent_scope_archive:' + a.id + ':' + original['token'])[0]), original)
        r._resume_one(a)
        current = r.store.get(a.id)
        self.assertEqual(current.state, 'running')
        self.assertEqual(current.harness, 'codex')
        pending = current.wait['interactive_prompt_intent']
        self.assertNotEqual(pending['token'], original['token'])
        self.assertEqual(pending['prompt'].count(prompt), 1)
        self.assertEqual(pending['prompt'].count('pending wake'), 1)
        self.assertIn(current.workspaces[0], pending['prompt'])
        self.assertIn('current leased workspaces', pending['prompt'])
        self.assertEqual((Path(current.workspaces[0]) / 'pending-work.txt').read_text(), 'previous uncommitted work')
        self.assertFalse(r.store.mark('builder', a.task_id, 'queue-key'))
        self.assertEqual(r.procs[a.id].wait(timeout=10), 0)
        self.assertEqual(len(calls(self.calls)), 1)
        self.assertTrue(r._reconcile_interactive_prompt(r.store.get(a.id)))
        self.assertNotIn('unsent_scope_recovery', r.store.get(a.id).wait)
        self.assertEqual(r.store.mark('builder', a.task_id, 'queue-key')[0], 'pending')
        self.assertFalse((self.root / 'untruthful-quota').exists())

    def test_ambiguous_old_release_preserves_unknown_intent(self):
        r, a, _, directory, _ = self.unknown()
        before = dict(a.wait)
        self.agent.turn_end_command = [sys.executable, '-c', 'pass']
        r.settle_finished()
        self.assertEqual(r.store.get(a.id).state, 'unknown')
        self.assertEqual(r.store.get(a.id).wait, before)

    def test_capacity_denial_keeps_recovery_context_and_pending_marks(self):
        r, a, _, directory, capacity = self.unknown()
        r.settle_finished()
        self.assertEqual(r.store.get(a.id).state, 'ready')
        capacity.write_text('0')
        before = dict(r.store.get(a.id).wait)
        r._resume_one(r.store.get(a.id))
        self.assertEqual(r.store.get(a.id).wait, before)
        self.assertFalse(calls(self.calls))
        self.assertFalse(r.store.mark('builder', a.task_id, 'queue-key'))

    def test_live_writer_partial_metadata_and_changed_proofs_stay_unknown(self):
        r, a, _, directory, _ = self.unknown()
        original = json.loads(json.dumps(a.wait))
        journal = interactive.read_delivery(directory, a.id)
        meta = interactive.meta_path(directory, a.id).read_bytes()
        progress = interactive.progress_path(directory, a.id).read_bytes()
        for change in ('live', 'partial-meta', 'send-intent', 'bytes', 'missing-checkpoint', 'foreign-lease'):
            with self.subTest(change=change):
                wait = json.loads(json.dumps(original))
                self.windows = []
                interactive.write_delivery(directory, a.id, journal)
                interactive.meta_path(directory, a.id).write_bytes(meta)
                interactive.progress_path(directory, a.id).write_bytes(progress)
                r.store.db.execute("UPDATE workspace_checkpoints SET phase='active' WHERE attempt=?", (a.id,))
                r.store.db.execute("UPDATE slots SET attempt=? WHERE path=?", (a.id, a.workspaces[0]))
                if change == 'live': self.windows = [(interactive.session_name('builder'), a.id)]
                if change == 'partial-meta': interactive.progress_path(directory, a.id).unlink()
                if change == 'send-intent': interactive.write_delivery(directory, a.id, journal | {'phase': 'send-intent'})
                if change == 'bytes': wait['interactive_prompt_intent']['prompt'] += ' changed'
                if change == 'missing-checkpoint': r.store.db.execute("UPDATE workspace_checkpoints SET phase='released' WHERE attempt=?", (a.id,))
                if change == 'foreign-lease': r.store.db.execute("UPDATE slots SET attempt='foreign' WHERE path=?", (a.workspaces[0],))
                r.store.update(r.store.get(a.id), wait=wait)
                before = r.store.mark('builder', a.task_id, 'gate')
                r.settle_finished()
                self.assertEqual(r.store.get(a.id).state, 'unknown')
                self.assertEqual(r.store.mark('builder', a.task_id, 'gate'), before)
                self.assertFalse(calls(self.calls))

    def test_restart_before_new_outbox_keeps_exact_pending_context(self):
        r, a, prompt, directory, _ = self.unknown()
        r.settle_finished()
        # _spawn's existing pre-outbox crash state after selecting a new harness.
        r.store.update(r.store.get(a.id), harness='codex')
        r._resume_one(r.store.get(a.id))
        pending = r.store.get(a.id).wait['interactive_prompt_intent']
        self.assertEqual(pending['prompt'].count(prompt), 1)
        self.assertEqual(pending['prompt'].count('pending wake'), 1)

    def test_target_guard_carries_unknown_without_settling_or_changing_it(self):
        import sqlite3
        r, a, _, directory, _ = self.unknown()
        target_db = self.cfg.state_dir / 'runner.sqlite'
        with sqlite3.connect(target_db) as dest:
            r.store.db.backup(dest)
        before = r.store.get(a.id).wait
        source = Path(launch_guard.__file__).resolve().parents[2]
        self.assertTrue(launch_guard.pending(self.cfg.state_dir))
        # The target's own guard takes the row over from a release with the same journal contract;
        # the row is neither settled nor changed by that.
        binding = {'version': 1, 'release': str(source), 'tree': 'new', 'config': 'c', 'config_sha256': 's',
                   'state': str(self.cfg.state_dir)}
        previous = dict(binding, tree='old')
        self.assertTrue(launch_guard.journal_compatible(self.cfg.state_dir, binding, previous, source))
        self.assertTrue(launch_guard.pending(self.cfg.state_dir))
        self.assertEqual(r.store.get(a.id).wait, before)
        # ... and not from another state, nor for a target this guard does not belong to
        self.assertFalse(launch_guard.journal_compatible(self.cfg.state_dir, binding, dict(previous, state='/elsewhere'), source))
        self.assertFalse(launch_guard.journal_compatible(self.cfg.state_dir, binding, previous, source.parent))

    def test_crash_after_reserved_journal_before_outbox_retries_once(self):
        import os
        from unittest import mock
        r, a, prompt, directory, _ = self.unknown()
        r.settle_finished()
        original = r.store.get(a.id).wait['unsent_scope_recovery']['intent']
        replace = os.replace
        def disk_fault(source, destination, *args, **kwargs):
            result = replace(source, destination, *args, **kwargs)
            if Path(destination) == interactive.delivery_path(directory, a.id):
                raise OSError('simulated crash after atomic journal rename')
            return result
        with mock.patch.object(os, 'replace', side_effect=disk_fault):
            with self.assertRaisesRegex(OSError, 'simulated crash'):
                r._resume_one(r.store.get(a.id))
        held = r.store.get(a.id)
        self.assertEqual(held.state, 'ready')
        self.assertNotIn('interactive_prompt_intent', held.wait)
        journal = interactive.read_delivery(directory, a.id)
        self.assertEqual(journal['phase'], 'reserved')
        self.assertNotEqual(journal['token'], original['token'])
        self.assertFalse(calls(self.calls))
        r._resume_one(held)
        active = r.store.get(a.id)
        self.assertEqual(active.state, 'running')
        self.assertEqual(active.wait['interactive_prompt_intent']['prompt'].count(prompt), 1)
        self.assertEqual(active.wait['interactive_prompt_intent']['prompt'].count('pending wake'), 1)
        self.assertEqual(r.procs[a.id].wait(timeout=10), 0)
        self.assertEqual(len(calls(self.calls)), 1)
        self.assertTrue(r._reconcile_interactive_prompt(r.store.get(a.id)))

    def test_nonreserved_orphan_or_changed_archive_cannot_start(self):
        r, a, _, directory, _ = self.unknown()
        r.settle_finished()
        current = r.store.get(a.id)
        original = json.loads(json.dumps(current.wait))
        journal = interactive.read_delivery(directory, a.id)
        for variant in ('received', 'send-intent', 'malformed', 'archive', 'live'):
            with self.subTest(variant=variant):
                self.windows = []
                r.store.update(r.store.get(a.id), wait=json.loads(json.dumps(original)))
                orphan = dict(journal, token='orphan', phase='reserved')
                if variant in ('received', 'send-intent'): orphan['phase'] = variant
                if variant == 'malformed': orphan['sha256'] = 'invalid'
                if variant == 'archive':
                    wait = json.loads(json.dumps(original))
                    wait['unsent_scope_recovery']['journal']['token'] = 'changed'
                    r.store.update(r.store.get(a.id), wait=wait)
                if variant == 'live': self.windows = [(interactive.session_name('builder'), a.id)]
                interactive.write_delivery(directory, a.id, orphan)
                r._resume_one(r.store.get(a.id))
                self.assertEqual(r.store.get(a.id).state, 'ready')
                self.assertFalse(calls(self.calls))

    def test_new_outbox_during_pre_spawn_scan_is_not_overwritten(self):
        from unittest import mock
        r, a, _, directory, _ = self.unknown()
        r.settle_finished()
        reads = []
        def external_windows(_tmux):
            reads.append(1)
            if len(reads) == 1:
                current = r.store.get(a.id)
                r.store.update(current, wait=current.wait | {'interactive_prompt_intent': {}})
            return []
        with mock.patch.object(interactive.Tmux, 'windows_strict', external_windows):
            with self.assertRaisesRegex(RuntimeError, 'evidence changed'):
                r._resume_one(r.store.get(a.id))
        self.assertIn('interactive_prompt_intent', r.store.get(a.id).wait)
        self.assertEqual(r.store.get(a.id).wait['interactive_prompt_intent'], {})
        self.assertFalse(calls(self.calls))

    def test_changed_wait_during_release_cas_keeps_new_owner_data(self):
        r, a, _, directory, _ = self.unknown()
        db = self.root / 'state' / 'r.sqlite'
        self.agent.turn_end_command = [sys.executable, '-c',
            'import sqlite3,json;db=sqlite3.connect(' + repr(str(db)) + ');'
            'row=db.execute("SELECT wait FROM attempts WHERE id=?",(' + repr(a.id) + ',)).fetchone();'
            'w=json.loads(row[0]);w["owner_change"]="preserved";'
            'db.execute("UPDATE attempts SET wait=? WHERE id=?",(json.dumps(w),' + repr(a.id) + '));'
            'db.commit();print("RELEASE_CONFIRMED=1")']
        r.settle_finished()
        current = r.store.get(a.id)
        self.assertEqual(current.state, 'unknown')
        self.assertEqual(current.wait['owner_change'], 'preserved')
        self.assertIn('interactive_prompt_intent', current.wait)
        self.assertFalse(r.store.mark('builder', a.task_id,
            'unsent_scope_archive:' + a.id + ':' + a.wait['interactive_prompt_intent']['token']))

    def test_normal_idle_partial_cleanup_requires_all_writer_absence(self):
        r, a, _, directory, _ = self.unknown()
        interactive.progress_path(directory, a.id).unlink()
        original = interactive.read_meta(directory, a.id)
        for changes in ({'session_id': 'other'}, {'harness': 'codex'}, {'busy_pid': True}, {'busy_pid': 1}):
            with self.subTest(changes=changes):
                interactive.write_meta(directory, a.id, original | {'busy_pid': 0} | changes)
                r.settle_finished()
                self.assertEqual(r.store.get(a.id).state, 'unknown')
        interactive.write_meta(directory, a.id, original | {'busy_pid': 0})
        self.native_argv = ['claude', '--session-id', original['session_id']]
        r.settle_finished()
        self.assertEqual(r.store.get(a.id).state, 'unknown')
        self.native_argv = ['fixture-idle-process']
        r.settle_finished()
        self.assertEqual(r.store.get(a.id).state, 'ready')

    def test_capacity_return_after_expired_hold_keeps_admitted_paths(self):
        r, a, _, directory, capacity = self.unknown()
        r.settle_finished()
        self.agent.workspace_wait_release_minutes = 1
        r.store.mark('builder', a.task_id, 'workspace_ready_hold',
                     json.dumps({'attempt': a.id, 'since': '2020-01-01T00:00:00Z'}))
        capacity.write_text('0')
        r._resume_one(r.store.get(a.id))
        self.assertEqual([x['phase'] for x in r.pool._checkpoint_rows(a.id)], ['active'])
        self.assertTrue(r.pool.leased_to(a.id))
        capacity.write_text('1')
        r._resume_one(r.store.get(a.id))
        self.assertEqual(r.store.get(a.id).state, 'running')
