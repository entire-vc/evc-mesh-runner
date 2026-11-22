"""Ownership returns go through admission, real git worktrees and the durable registry."""
import json
from dataclasses import replace
from pathlib import Path

from fakes import AGENT_ID, FakeMesh, calls, task, wait_time, write_replies
from mesh_runner.adapters.base import group_alive
from mesh_runner.workspace import git
from test_runner import Bench


class OwnershipWorkspaceReturn(Bench):
    def departed(self, *, dirty=False):
        original, other = task(51), task(52)
        self.agent.repos[0] = replace(self.agent.repos[0], max_slots=1)
        write_replies(self.replies, {t['id'][:8]: wait_time(why='external fixture event')
                                     for t in (original, other)})
        r = self.runner(FakeMesh([original]))
        r.tick()
        self.settle_all(r)
        first = r.store.by_state('builder', ('waiting',))[0]
        path = Path(first.workspaces[0])
        branch = 'builder/original-work'
        git(path, 'checkout', '-q', '-b', branch)
        (path / 'README').write_text('original task commit\n')
        git(path, 'commit', '-qam', 'original work')
        head = git(path, 'rev-parse', 'HEAD')
        self.expected_tree = 'original task commit\n'
        if dirty:
            self.expected_tree = 'original task uncommitted work\n'
            (path / 'README').write_text(self.expected_tree)
        r.mesh.tasks[original['id']]['assignee_id'] = 'agent-lead'
        r.tick()
        self.assertEqual(r.store.get(first.id).state, 'completed')
        self.assertFalse(group_alive(first.pid, first.stamp))
        r.mesh.tasks[other['id']] = other
        r.tick()
        self.settle_all(r)
        holder = next(a for a in r.store.by_state('builder', ('waiting',)) if a.task_id == other['id'])
        self.assertEqual(holder.workspaces, first.workspaces)
        self.agent.repos[0] = replace(self.agent.repos[0], max_slots=2)
        r.pool.repos['app'] = self.agent.repos[0]
        return r, original, first, holder, branch, head

    def give_back(self, r, original):
        r.mesh.tasks[original['id']]['assignee_id'] = AGENT_ID
        r.mesh.tasks[original['id']]['updated_at'] = '2026-11-01T00:00:00Z'
        r.tick()
        self.settle_all(r)
        return next(a for a in r.store.on_task(original['id']) if a.state != 'completed')

    def assert_binding(self, r, attempt, branch, head):
        self.assertEqual(len(attempt.workspaces), 1)
        path = Path(attempt.workspaces[0])
        self.assertEqual(git(path, 'symbolic-ref', '--short', 'HEAD'), branch)
        self.assertEqual(git(path, 'rev-parse', 'HEAD'), head)
        self.assertEqual((path / 'README').read_text(), self.expected_tree)
        self.assertEqual(r.pool.leased_paths(attempt.id), attempt.workspaces)
        holders = [p for p, ref in r.pool._worktree_branches(r.pool.repos['app'])
                   if ref == 'refs/heads/' + branch]
        self.assertEqual([p.resolve() for p in holders], [path.resolve()])
        self.assertEqual(len([a for a in r.store.on_task(attempt.task_id)
                              if a.state in ('starting', 'running', 'ready', 'waiting')]), 1)

    def test_busy_previous_slot_restores_branch_and_head(self):
        r, original, first, holder, branch, head = self.departed()
        later = self.give_back(r, original)
        self.assertNotEqual(later.workspaces, first.workspaces)
        self.assert_binding(r, later, branch, head)
        self.assertTrue(r.pool.attempt_holds_slot(holder.id, holder.workspaces[0]))
        self.assertEqual(git(Path(holder.workspaces[0]), 'rev-parse', '--abbrev-ref', 'HEAD'), 'HEAD')
        # Repeated ordinary ticks retain one attempt, its HEAD and its existing lease.
        before = len(calls(self.calls))
        for _ in range(3):
            r.tick()
        self.assertEqual(len(calls(self.calls)), before)
        self.assert_binding(r, r.store.get(later.id), branch, head)

    def test_live_foreign_branch_blocks_without_writer_or_lease_then_retries(self):
        r, original, first, holder, branch, head = self.departed()
        git(Path(holder.workspaces[0]), 'checkout', '-q', branch)
        before = len(calls(self.calls))
        later = self.give_back(r, original)
        self.assertEqual(later.state, 'ready')
        self.assertEqual(later.turns, 0)
        self.assertEqual(later.pid, 0)
        self.assertEqual(r.pool.leased_to(later.id), [])
        self.assertEqual(len(calls(self.calls)), before)
        r = self.runner(r.mesh)  # The branch hold and checkpoint survive daemon replacement.
        for _ in range(3):
            r.tick()
        self.assertEqual(len(calls(self.calls)), before)
        self.assertTrue(r.pool.attempt_holds_slot(holder.id, holder.workspaces[0]))
        # Completion releases the holder through the ordinary ownership path.
        r.mesh.tasks[holder.task_id]['assignee_id'] = 'agent-lead'
        r.tick()
        self.settle_all(r)
        later = r.store.get(later.id)
        self.assertEqual(later.state, 'waiting')
        self.assert_binding(r, later, branch, head)
        self.assertEqual(len(calls(self.calls)), before + 1)
        self.assertEqual(r.store.mark('builder', original['id'], 'ownership_loss')[0], '')

    def test_available_previous_slot_keeps_session_and_binding(self):
        r, original, first, holder, branch, head = self.departed()
        r.mesh.tasks[holder.task_id]['assignee_id'] = 'agent-lead'
        r.tick()
        later = self.give_back(r, original)
        self.assertEqual(later.workspaces, first.workspaces)
        self.assertEqual(later.session_id, first.session_id)
        self.assert_binding(r, later, branch, head)

    def test_dirty_work_is_preserved_when_the_previous_slot_is_reused(self):
        r, original, first, holder, branch, head = self.departed(dirty=True)
        later = self.give_back(r, original)
        self.assertNotEqual(later.workspaces, first.workspaces)
        self.assert_binding(r, later, branch, head)

    def test_another_ownership_roundtrip_preserves_the_new_owner_commit(self):
        r, original, first, holder, branch, head = self.departed()
        later = self.give_back(r, original)
        path = Path(later.workspaces[0])
        self.expected_tree = 'new owner commit\n'
        (path / 'README').write_text(self.expected_tree)
        git(path, 'commit', '-qam', 'continued work')
        new_head = git(path, 'rev-parse', 'HEAD')
        self.assertNotEqual(new_head, head)
        r.mesh.tasks[original['id']]['assignee_id'] = 'agent-lead'
        r.tick()
        self.assertEqual(r.store.get(later.id).state, 'completed')
        returned = self.give_back(r, original)
        self.assert_binding(r, returned, branch, new_head)

    def test_adoption_persists_recovery_inventory_before_any_restore(self):
        r, original, first, holder, branch, head = self.departed()
        later = r.store.create('builder', original['id'], self.agent.harness)
        rows = r.pool.adopt_checkpoints(first.id, later.id)
        self.assertEqual(r.store.get(later.id).workspaces, [row['source'] for row in rows])
        self.assertEqual(r.pool.leased_to(later.id), [])
        r = self.runner(r.mesh)
        self.assertEqual(r.pool.adopt_checkpoints(first.id, later.id), rows)
        self.assertEqual(r.store.get(later.id).workspaces, first.workspaces)

    # A returning card's adoption replayed every snapshot into scratch under the process-wide checkpoint
    # IO lock, and the restore right after replayed it again: the start step `task` took up to 147 s
    # (20.11; a lane with 13 repositories: 91 s). The restore proves the stored tree and the bundle before it
    # touches the target and the result after; the capture proved the replay too.
    def test_adoption_replays_no_snapshot(self):
        from unittest import mock
        from mesh_runner.checkpoint import Checkpoints
        r, original, first, holder, branch, head = self.departed()
        later = r.store.create('builder', original['id'], self.agent.harness)
        with mock.patch.object(Checkpoints, '_replay', autospec=True, side_effect=Checkpoints._replay) as replay:
            rows = r.pool.adopt_checkpoints(first.id, later.id)
            self.assertEqual(replay.call_count, 0)
            paths, _ = r.pool.restore(later.id)
            self.assertEqual(replay.call_count, len(rows))
        self.assertEqual(git(Path(paths[0]), 'rev-parse', 'HEAD'), head)
        self.assertEqual((Path(paths[0]) / 'README').read_text(), self.expected_tree)

    def test_a_damaged_snapshot_is_still_refused_before_its_target_is_touched(self):
        from mesh_runner.workspace import WorkspaceError
        r, original, first, holder, branch, head = self.departed()
        later = r.store.create('builder', original['id'], self.agent.harness)
        rows = r.pool.adopt_checkpoints(first.id, later.id)
        snapshot = Path(rows[0]['checkpoint'])
        (snapshot / 'tree' / 'bit-rot').write_text('x\n')         # a slim tree holds only what differs from HEAD
        with self.assertRaisesRegex(WorkspaceError, 'snapshot hash mismatch'):
            r.pool.restore(later.id)
        target = Path(r.pool._checkpoint_rows(later.id)[0]['target'])
        self.assertFalse((target / 'bit-rot').exists())                          # target untouched
        self.assertNotEqual(git(target, 'rev-parse', 'HEAD'), head)
        self.assertTrue((snapshot / 'manifest.json').is_file())                  # the only copy is kept
        self.assertEqual(r.pool._checkpoint_rows(first.id)[0]['phase'], 'transferred')

    def damaged_start_heals(self, damage, heal):
        """A snapshot damaged after its capture is found before the start leases a slot: three refused
        starts leave the pool as it was and the lane starting, the snapshot is kept, and the card starts
        once it is whole."""
        r, original, first, holder, branch, head = self.departed()
        snapshot = Path(r.pool._checkpoint_rows(first.id)[0]['checkpoint'])
        damage(snapshot)
        r.mesh.tasks[original['id']]['assignee_id'] = AGENT_ID
        r.mesh.tasks[original['id']]['updated_at'] = '2026-11-01T00:00:00Z'
        app = lambda: sorted((row['path'], row['state']) for row in r.pool.rows() if row['pool'] == 'builder/app')
        before = app()
        for _ in range(3):
            r._task_backoff.clear()
            r.tick()
            self.settle_all(r)
            self.assertEqual(app(), before)                    # no lease, no held slot per retry
            self.assertEqual(r._starts_paused_until, 0.0)      # the card is held, not the lane
        self.assertTrue((snapshot / 'manifest.json').is_file())
        heal(snapshot)
        r._task_backoff.clear()
        r.tick()
        self.settle_all(r)
        later = next(a for a in r.store.on_task(original['id']) if a.state not in ('completed', 'aborted',
                                                                                   'aborted-notified'))
        self.assert_binding(r, later, branch, head)

    def test_a_stray_file_in_a_snapshot_holds_the_card_before_any_lease_and_heals(self):
        self.damaged_start_heals(lambda s: (s / 'tree' / 'bit-rot').write_text('x\n'),
                                 lambda s: (s / 'tree' / 'bit-rot').unlink())

    # The index is copied into the target before the result is checked; a damaged one
    # cost a held slot per retry and the card never started again
    def test_a_damaged_index_holds_the_card_before_any_lease_and_heals(self):
        def damage(s):
            (s / 'index').rename(s / 'index-good')
            (s / 'index').write_text('broken index\n')
        self.damaged_start_heals(damage, lambda s: (s / 'index-good').replace(s / 'index'))

    def test_a_truncated_index_is_refused_at_adoption(self):
        from mesh_runner.workspace import InventoryError
        r, original, first, holder, branch, head = self.departed()
        index = Path(r.pool._checkpoint_rows(first.id)[0]['checkpoint']) / 'index'
        index.write_bytes(index.read_bytes()[:-7])
        later = r.store.create('builder', original['id'], self.agent.harness)
        with self.assertRaisesRegex(InventoryError, 'index'):
            r.pool.adopt_checkpoints(first.id, later.id)
        self.assertEqual(r.pool._checkpoint_rows(later.id), [])          # nothing transferred
        self.assertEqual(r.pool.leased_to(later.id), [])

    # Git's `index.skipHash` writes an all-zero trailer, which proves nothing; an index cut
    # to its header behind that trailer passed the check, and the restore held a slot per retry
    SKIPHASH = {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'index.skipHash', 'GIT_CONFIG_VALUE_0': 'true'}

    @staticmethod
    def drop_digests(snapshot):
        """The manifest as a runner wrote it before it recorded index digests: every snapshot on disk at
        the rollout."""
        manifest = json.loads((snapshot / 'manifest.json').read_text())
        manifest.pop('index_sha256')
        (snapshot / 'manifest.json').write_text(json.dumps(manifest))

    def skiphash_damage_heals(self, damage, legacy=False):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, self.SKIPHASH):
            kept = {}

            def cut(s):
                if legacy:
                    self.drop_digests(s)
                kept['index'] = (s / 'index').read_bytes()
                self.assertEqual(kept['index'][-20:], bytes(20))           # captured as git wrote it
                (s / 'index').write_bytes(damage(kept['index']))
            self.damaged_start_heals(cut, lambda s: (s / 'index').write_bytes(kept['index']))

    def test_a_skiphash_index_cut_to_its_header_holds_the_card_before_any_lease_and_heals(self):
        self.skiphash_damage_heals(lambda raw: raw[:12] + bytes(20))

    def test_a_skiphash_index_with_a_changed_object_id_holds_the_card_before_any_lease_and_heals(self):
        # whole in form (every entry parses), wrong in content: only the capture's own record tells
        self.skiphash_damage_heals(lambda raw: raw[:12 + 40] + bytes([raw[52] ^ 1]) + raw[53:])

    # A snapshot taken before the digests has nothing to compare a skipHash index with, and
    # its form does not show a changed object id: such a snapshot is proven by a replay into scratch
    def test_a_legacy_skiphash_index_with_a_changed_object_id_holds_the_card_before_any_lease_and_heals(self):
        self.skiphash_damage_heals(lambda raw: raw[:12 + 40] + bytes([raw[52] ^ 1]) + raw[53:], legacy=True)

    def test_a_legacy_skiphash_index_cut_to_its_header_holds_the_card_before_any_lease_and_heals(self):
        self.skiphash_damage_heals(lambda raw: raw[:12] + bytes(20), legacy=True)

    def legacy_adoption_replays(self, env, replays):
        import os
        from unittest import mock
        from mesh_runner.checkpoint import Checkpoints
        with mock.patch.dict(os.environ, env):
            r, original, first, holder, branch, head = self.departed()
            self.drop_digests(Path(r.pool._checkpoint_rows(first.id)[0]['checkpoint']))
            later = r.store.create('builder', original['id'], self.agent.harness)
            with mock.patch.object(Checkpoints, '_replay', autospec=True, side_effect=Checkpoints._replay) as replay:
                r.pool.adopt_checkpoints(first.id, later.id)
                self.assertEqual(replay.call_count, replays)
                paths, _ = r.pool.restore(later.id)
            self.assertEqual(git(Path(paths[0]), 'rev-parse', 'HEAD'), head)
            self.assertEqual((Path(paths[0]) / 'README').read_text(), self.expected_tree)

    def test_a_legacy_snapshot_is_adopted_without_a_replay_when_its_index_checksum_proves_it(self):
        self.legacy_adoption_replays({}, 0)

    def test_a_sound_legacy_skiphash_snapshot_is_proven_by_one_replay_and_starts(self):
        self.legacy_adoption_replays(self.SKIPHASH, 1)

    def test_a_sound_skiphash_snapshot_starts(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, self.SKIPHASH):
            r, original, first, holder, branch, head = self.departed()
            index = Path(r.pool._checkpoint_rows(first.id)[0]['checkpoint']) / 'index'
            self.assertEqual(index.read_bytes()[-20:], bytes(20))
            r.mesh.tasks[original['id']]['assignee_id'] = AGENT_ID
            r.mesh.tasks[original['id']]['updated_at'] = '2026-11-01T00:00:00Z'
            r.tick()
            self.settle_all(r)
            later = next(a for a in r.store.on_task(original['id']) if a.state not in ('completed', 'aborted',
                                                                                       'aborted-notified'))
            self.assert_binding(r, later, branch, head)


class CheckpointFaultsStayWithTheCard(OwnershipWorkspaceReturn):
    """A checkpoint fault of one card holds that card, never the lane, and never repeats forever."""

    def test_released_restore_can_be_restored_again(self):
        r, original, first, holder, branch, head = self.departed()
        later = r.store.create('builder', original['id'], self.agent.harness)
        r.pool.adopt_checkpoints(first.id, later.id)
        paths, _ = r.pool.restore(later.id)
        # the start is refused after the restore (admission): the slot goes back to the pool
        self.assertEqual(r.pool.release(later.id, paths=paths), [])
        r.store.update(later, state='completed', note='admission refused')
        again = r.store.create('builder', original['id'], self.agent.harness)
        r.pool.adopt_checkpoints(first.id, again.id)
        restored, _ = r.pool.restore(again.id)
        self.assertEqual(restored, paths)
        self.assertEqual(git(Path(restored[0]), 'rev-parse', 'HEAD'), head)
        self.assertEqual((Path(restored[0]) / 'README').read_text(), self.expected_tree)

    def test_checkpoint_pass_keeps_snapshots_of_a_departed_card(self):
        from mesh_runner.pool_ops import prune_checkpoints
        r, original, first, holder, branch, head = self.departed()
        before = r.pool._checkpoint_rows(first.id)
        self.assertTrue(before)
        r.store.db.execute("UPDATE attempts SET updated_at='2026-01-01T00:00:00.000+00:00' WHERE id=?", (first.id,))
        prune_checkpoints(r.pool, grace_hours=0, apply=True)
        self.assertEqual(r.pool._checkpoint_rows(first.id), before)
        self.assertTrue(all(Path(row['checkpoint']).is_dir() for row in before))
        later = self.give_back(r, original)
        self.assert_binding(r, later, branch, head)

    def test_spent_note_no_longer_keeps_snapshots(self):
        from mesh_runner.pool_ops import prune_checkpoints
        r, original, first, holder, branch, head = self.departed()
        r.store.mark('builder', original['id'], 'ownership_loss', '')
        r.store.db.execute("UPDATE attempts SET updated_at='2026-01-01T00:00:00.000+00:00' WHERE id=?", (first.id,))
        prune_checkpoints(r.pool, grace_hours=0, apply=True)
        self.assertEqual(r.pool._checkpoint_rows(first.id), [])

    def test_lost_snapshots_start_fresh_with_one_comment(self):
        r, original, first, holder, branch, head = self.departed()
        r.store.db.execute("DELETE FROM workspace_checkpoints WHERE attempt=?", (first.id,))
        later = self.give_back(r, original)
        self.assertNotIn(later.state, ('aborted', 'aborted-notified'))
        self.assertEqual(r._starts_paused_until, 0.0)
        self.assertEqual(len(later.workspaces), 1)
        notes = [c for c in r.mesh.comments_all(original['id']) if 'snapshots' in str(c)]
        self.assertEqual(len(notes), 1)

    def test_inventory_fault_holds_the_card_not_the_lane(self):
        from mesh_runner.workspace import InventoryError
        r, original, first, holder, branch, head = self.departed()

        def refuse(*_a, **_k):
            raise InventoryError('checkpoint handover: incomplete source inventory')
        r.pool.adopt_checkpoints = refuse
        r.mesh.tasks[original['id']]['assignee_id'] = AGENT_ID
        r.mesh.tasks[original['id']]['updated_at'] = '2026-11-01T00:00:00Z'
        r.tick()
        self.assertEqual(r._starts_paused_until, 0.0)
        self.assertIn(original['id'], r._task_backoff)
        self.assertEqual(r._start_error, '')

    def test_partly_lost_snapshots_start_fresh_too(self):
        r, original, first, holder, branch, head = self.departed()
        r.store.update(r.store.get(first.id), workspaces=first.workspaces + ['/gone/slot-9'])
        later = self.give_back(r, original)
        self.assertNotIn(later.state, ('aborted', 'aborted-notified'))
        self.assertEqual(len(later.workspaces), 1)
        self.assertEqual(r._starts_paused_until, 0.0)

    def test_same_fault_is_said_once_across_retries(self):
        from mesh_runner.workspace import InventoryError
        r, original, first, holder, branch, head = self.departed()

        def refuse(*_a, **_k):
            raise InventoryError('checkpoint handover: incomplete source inventory')
        r.pool.adopt_checkpoints = refuse
        r.mesh.tasks[original['id']]['assignee_id'] = AGENT_ID
        r.mesh.tasks[original['id']]['updated_at'] = '2026-11-01T00:00:00Z'
        for _ in range(3):
            r._task_backoff.clear()
            r.tick()
        notes = [c for c in r.mesh.comments_all(original['id']) if 'cannot start this card' in str(c)]
        self.assertEqual(len(notes), 1)
        # the card keeps retrying after its backoff: a fault healed meanwhile lets it start
        tries = [a for a in r.store.on_task(original['id']) if a.id != first.id]
        self.assertGreaterEqual(len(tries), 2)
        self.assertNotIn('aborted-notified', {a.state for a in tries})
        self.assertEqual(r._starts_paused_until, 0.0)


class RestoreRefusalsAreCountedAndReported(OwnershipWorkspaceReturn):
    """A refused restore of one card's working copies holds that card, is counted in a durable
    streak and reported in status.json (the fleet alert reads it), with the slot and the cause."""

    REFUSAL = 'checkpoint: submodule gitdir differs in target (local tag v0.1.0): vendor/kit; lease retained'

    def refuse_restores(self):
        from unittest.mock import patch
        from mesh_runner.workspace import RestoreRefused
        return patch('mesh_runner.checkpoint.Checkpoints.restore', autospec=True,
                     side_effect=RestoreRefused(self.REFUSAL))

    def ask_again(self, r, original):
        r._task_backoff.clear()
        r.mesh.tasks[original['id']]['assignee_id'] = AGENT_ID
        r.mesh.tasks[original['id']]['updated_at'] = '2026-11-01T00:00:00Z'
        r.tick()

    def test_refused_start_holds_the_card_and_reports_slot_and_cause(self):
        r, original, first, holder, branch, head = self.departed()
        self.assertNotIn('restore_refusals', r.status())
        with self.refuse_restores():
            self.ask_again(r, original)
        self.assertEqual(r._starts_paused_until, 0.0)          # the lane goes on
        self.assertEqual(r._start_error, '')
        self.assertIn(original['id'], r._task_backoff)          # this card waits
        rows = r.status()['restore_refusals']
        self.assertEqual([(x['task_id'], x['task'], x['count']) for x in rows],
                         [(original['id'], original['id'][:8], 1)])
        self.assertEqual(rows[0]['error'], self.REFUSAL)
        leased = [p for row in r.pool._checkpoint_rows(r.store.on_task(original['id'])[-1].id)
                  for p in [row['target']] if p]
        self.assertTrue(rows[0]['target'])
        self.assertIn(rows[0]['target'], leased)

    def test_streak_survives_a_restart_and_ends_when_the_card_gets_its_copies(self):
        r, original, first, holder, branch, head = self.departed()
        with self.refuse_restores():
            for _ in range(2):
                self.ask_again(r, original)
        r = self.runner(r.mesh)                                 # a deploy restarts the runner
        with self.refuse_restores():
            for _ in range(2):
                self.ask_again(r, original)
        self.assertEqual([x['count'] for x in r.status()['restore_refusals']], [4])
        self.ask_again(r, original)                             # the slot is repaired: the restore goes through
        self.settle_all(r)
        self.assertNotIn('restore_refusals', r.status())
        self.assertEqual(r.store.mark('builder', original['id'], 'restore_refusals')[0], '')

    def test_an_old_streak_leaves_the_status(self):
        r, original, first, holder, branch, head = self.departed()
        with self.refuse_restores():
            self.ask_again(r, original)
        value = json.loads(r.store.mark('builder', original['id'], 'restore_refusals')[0])
        value['last'] = '2026-01-01T00:00:00.000+00:00'
        r.store.mark('builder', original['id'], 'restore_refusals', json.dumps(value))
        self.assertNotIn('restore_refusals', r.status())


class IndexWhole(Bench):
    """The index check at adoption accepts every index git itself writes and refuses a damaged one."""

    def indexes(self, d):
        """Real indexes: versions 2 to 4 (3 from an intent-to-add entry, 4 with shared path prefixes),
        SHA-1 and SHA-256 repositories, each with git's checksum trailer and with `index.skipHash`."""
        import subprocess
        for fmt in ('sha1', 'sha256'):
            for version in ('2', '3', '4'):
                for skip in ('false', 'true'):
                    repo = Path(d) / f'r-{fmt}-{version}-{skip}'
                    subprocess.run(['git', 'init', '-q', f'--object-format={fmt}', str(repo)], check=True)
                    for name in ('dir/alpha', 'dir/alphabet', 'dir/beta', 'top'):
                        (repo / name).parent.mkdir(parents=True, exist_ok=True)
                        (repo / name).write_text(name + '\n')
                    cfg = ['-c', f'index.skipHash={skip}', '-c', f'index.version={4 if version == "4" else 2}']
                    subprocess.run(['git', '-C', str(repo), *cfg, 'add', '.'], check=True)
                    # a name longer than the 12-bit length field: index only, no file system holds it
                    blob = subprocess.run(['git', '-C', str(repo), 'hash-object', '-w', 'top'], check=True,
                                          capture_output=True, text=True).stdout.strip()
                    subprocess.run(['git', '-C', str(repo), *cfg, 'update-index', '--add', '--cacheinfo',
                                    f"100644,{blob},{'/'.join(['d' * 200] * 21)}"], check=True)
                    if version == '3':
                        (repo / 'later').write_text('later\n')
                        subprocess.run(['git', '-C', str(repo), *cfg, 'add', '-N', 'later'], check=True)
                    subprocess.run(['git', '-C', str(repo), *cfg, 'update-index', '--force-write-index'],
                                   check=True)
                    index = repo / '.git' / 'index'
                    raw = index.read_bytes()
                    self.assertEqual(int.from_bytes(raw[4:8], 'big'), int(version), repo.name)
                    size = 32 if fmt == 'sha256' else 20
                    self.assertEqual(raw[-size:] == bytes(size), skip == 'true', repo.name)
                    yield repo.name, index, raw

    def test_every_index_git_writes_is_whole_and_damage_is_not(self):
        import hashlib
        import tempfile
        from mesh_runner.checkpoint import _index_whole
        with tempfile.TemporaryDirectory() as d:
            seen = 0
            for name, index, raw in self.indexes(d):
                seen += 1
                self.assertTrue(_index_whole(index), name)
                self.assertTrue(_index_whole(index, hashlib.sha256(raw).hexdigest()), name)
                size = 32 if '-sha256-' in name else 20
                broken = [raw[:3] + b'X' + raw[4:],                       # signature
                          raw[:12] + bytes(size),                         # cut to its header
                          raw[:-size - 1] + raw[-size:],                  # one byte short
                          raw[:-size] + b'\0' + raw[-size:],              # one byte long
                          raw[:8] + (int.from_bytes(raw[8:12], 'big') + 1).to_bytes(4, 'big') + raw[12:]]
                for n, bad in enumerate(broken):
                    index.write_bytes(bad)
                    self.assertFalse(_index_whole(index), (name, n))
                # whole in form, other content: only the capture's digest tells
                flipped = raw[:52] + bytes([raw[52] ^ 1]) + raw[53:]
                index.write_bytes(flipped)
                self.assertFalse(_index_whole(index, hashlib.sha256(raw).hexdigest()), name)
            self.assertEqual(seen, 12)
            fake = Path(d) / 'fake'
            fake.write_bytes(b'DIRC' + b'\x00\x00\x00\x02' + bytes(4) + b'entry' + bytes(19) + b'\x01')
            self.assertFalse(_index_whole(fake))
            self.assertFalse(_index_whole(Path(d) / 'missing'))
            self.assertFalse(_index_whole(Path(d) / 'missing', 'whatever'))
