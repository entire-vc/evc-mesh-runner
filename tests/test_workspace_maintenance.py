"""Owner-side maintenance window: acquire -> hold -> finish on real Store, WorkspacePool and spool."""
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mesh_runner import maintenance
from mesh_runner import repo_admission
from mesh_runner import workspace_requests as requests
from mesh_runner.adapters.base import proc_stamp
from mesh_runner.workspace import WorkspaceError, base_lock
from test_workspace_requests import WorkspaceRequests


class Maintenance(WorkspaceRequests):
    def parked(self, count=1):
        r, attempts = super().parked(count)
        r.tick()                         # the first tick annotates each WAIT once; start from the settled state
        return r, [r.store.get(a.id) for a in attempts]

    def slot(self, r, a):
        row = r.store.db.execute("SELECT * FROM slots WHERE attempt=? AND state='leased'", (a.id,)).fetchone()
        return dict(row)

    def ask(self, r, a, op='acquire', **changes):
        slot = self.slot(r, a)
        rid = changes.pop('request_id', str(uuid.uuid4()))
        values = dict(state=self.cfg.state_dir, agent=a.agent, op=op, task=a.task_id, attempt=a.id,
                      token=requests.generation(a), request_id=rid, pool=slot['pool'], slot=slot['path'],
                      lease=maintenance.lease_token(slot), ttl=300)
        values.update(changes)
        return maintenance.submit(**values)

    def answer(self, r, a, submitted):
        r.tick()
        path = requests.directory(self.cfg.state_dir, a.agent, maintenance.SPOOL) / (
            submitted['request']['request_id'] + '.receipt.json')
        return json.loads(path.read_text())

    def status(self, r, a, op='acquire', **changes):
        return self.answer(r, a, self.ask(r, a, op, **changes))['status']

    def snapshot(self, r, a):
        """Everything a refused request must leave alone."""
        cur = r.store.get(a.id)
        return (cur.state, cur.wait, cur.workspaces, self.slot(r, a),
                [dict(x) for x in r.store.db.execute("SELECT * FROM maintenance_windows")],
                [dict(x) for x in r.pool._checkpoint_rows(a.id)])

    def other_target_works(self, r, other):
        self.assertEqual(self.receipt(r, other, self.submit_and_tick(r, other)), 'suspended')
        self.assertEqual(r.pool.leased_to(other.id), [])

    def submit_and_tick(self, r, a):
        s = self.submit(r, a)
        r.tick()
        return s

    # ---------------------------------------------------------------- positive
    def test_acquire_hold_finish_then_ordinary_capture(self):
        r, (a, other) = self.parked(2)
        before = self.snapshot(r, a)
        got = self.answer(r, a, self.ask(r, a))
        self.assertEqual((got['status'], got['barrier']), ('ready', 'held'))
        self.assertEqual((got['task'], got['attempt'], got['pool'], got['slot']),
                         (a.task_id, a.id, self.slot(r, a)['pool'], self.slot(r, a)['path']))
        after = self.snapshot(r, a)
        self.assertEqual(after[:4], before[:4])                       # WAIT, lease, workspaces untouched
        self.assertEqual([w['state'] for w in after[4]], ['held'])
        self.assertTrue((Path(a.workspaces[0]) / '.git').exists())
        # durable receipt event, delivered without timer polling
        events = [json.loads(x) for x in (self.cfg.state_dir / 'events.jsonl').read_text().splitlines()]
        self.assertIn(('maintenance', 'ready'), [(e['kind'], e.get('status')) for e in events])
        self.assertTrue([e for _, k, e in r.store.events(a.id) if k == 'maintenance_ready'])
        # the window blocks this attempt's capture; the other target keeps working
        self.assertEqual(self.answer_plain(r, a), 'rejected_maintenance_hold')
        self.other_target_works(r, other)
        win = self.slot(r, a)
        done = self.answer(r, a, self.ask(r, a, 'finish', window_id=got['window']))
        self.assertEqual((done['status'], done['barrier']), ('finished', 'finished'))
        self.assertEqual(self.slot(r, a), win)
        self.assertEqual(self.receipt(r, a, self.submit_and_tick(r, a)), 'suspended')   # ordinary capture again
        self.assertTrue(r.pool._checkpoint_rows(a.id))

    def answer_plain(self, r, a):
        return self.receipt(r, a, self.submit_and_tick(r, a))

    # ---------------------------------------------------------------- identity negatives
    def test_wrong_stale_identity_refused_without_side_effects(self):
        r, (a, other) = self.parked(2)
        before = self.snapshot(r, a)
        bad = dict(
            task=str(uuid.uuid4()), attempt=str(uuid.uuid4()), token='0' * 64, lease='1' * 64,
            pool='builder/elsewhere', slot=self.slot(r, other)['path'])
        expected = dict(task='rejected_identity_or_state', attempt='rejected_identity_or_state',
                        token='rejected_wait_generation', lease='rejected_stale_lease',
                        pool='rejected_pool', slot='rejected_lease_drift')
        for key, value in bad.items():
            with self.subTest(field=key):
                self.assertEqual(self.status(r, a, **{key: value}), expected[key])
                self.assertEqual(self.snapshot(r, a), before)
        self.other_target_works(r, other)

    def test_expired_request_is_not_accepted(self):
        r, (a,) = self.parked()
        s = self.ask(r, a)
        with patch('time.time', lambda t=time.time: t() + 4000):
            self.assertEqual(self.answer(r, a, s)['status'], 'expired')
        self.assertEqual(r.store.db.execute("SELECT count(*) FROM maintenance_windows").fetchone()[0], 0)

    def test_expired_finish_does_not_lift_the_hold(self):
        r, (a,) = self.parked()
        self.assertEqual(self.status(r, a), 'ready')
        before = self.snapshot(r, a)
        s = self.ask(r, a, 'finish', window_id=before[4][0]['window'])
        with patch('time.time', lambda t=time.time: t() + 4000):
            receipt = self.answer(r, a, s)
        self.assertEqual((receipt['status'], receipt['barrier']), ('expired', 'held'))
        self.assertEqual(self.snapshot(r, a), before)
        self.assertEqual(self.status(r, a, 'finish', window_id=before[4][0]['window']), 'finished')

    # ---------------------------------------------------------------- in-flight operations
    def test_live_writer_refused(self):
        r, (a, other) = self.parked(2)
        proc = subprocess.Popen(['sleep', '60'], start_new_session=True)
        try:
            r.store.update(a, pid=proc.pid, stamp=proc_stamp(proc.pid))
            before = self.snapshot(r, a)
            self.assertEqual(self.status(r, a), 'rejected_active_writer')
            self.assertEqual(self.snapshot(r, a), before)
            self.other_target_works(r, other)
        finally:
            proc.kill()
            proc.wait()

    def test_pre_journal_capture_in_flight_refused(self):
        """A capture that holds the repository lock has not written its journal yet."""
        r, (a,) = self.parked()
        repo = self.agent.repos[0]
        taken, free = threading.Event(), threading.Event()

        def capture():
            with base_lock(repo.base):
                taken.set()
                free.wait(10)

        worker = threading.Thread(target=capture)
        worker.start()
        try:
            self.assertTrue(taken.wait(10))
            before = self.snapshot(r, a)
            self.assertEqual(self.status(r, a), 'rejected_operation_in_flight')
            self.assertEqual(self.snapshot(r, a), before)
        finally:
            free.set()
            worker.join()
        self.assertEqual(self.status(r, a), 'ready')                   # same lease, once the operation ended

    def test_restore_or_reconcile_journal_refused(self):
        r, (a,) = self.parked()
        r.pool._checkpoints()
        for phase in ('verified', 'releasing', 'restoring', 'restored'):
            with self.subTest(phase=phase):
                r.store.db.execute("DELETE FROM workspace_checkpoints")
                r.store.db.execute("INSERT INTO workspace_checkpoints VALUES(?,?,?,?,?,?,?)",
                                   (r.pool.owner, a.id, 'app', '/cp', a.workspaces[0], phase, a.workspaces[0]))
                before = self.snapshot(r, a)
                self.assertEqual(self.status(r, a), 'rejected_checkpoint_in_flight')
                self.assertEqual(self.snapshot(r, a), before)

    # ---------------------------------------------------------------- duplicates, receipt loss, restart
    def test_duplicate_replay_returns_same_window_and_second_is_refused(self):
        r, (a,) = self.parked()
        first = self.ask(r, a)
        self.assertEqual(self.answer(r, a, first)['status'], 'ready')
        rid = first['request']['request_id']
        spool = requests.directory(self.cfg.state_dir, a.agent, maintenance.SPOOL)
        (spool / (rid + '.receipt.json')).unlink()                    # receipt lost; request resubmitted
        again = self.ask(r, a, request_id=rid)
        self.assertEqual(again['request']['expires'], first['request']['expires'])    # no new deadline
        self.assertEqual(self.answer(r, a, again)['status'], 'ready')
        self.assertEqual(r.store.db.execute("SELECT count(*) FROM maintenance_windows").fetchone()[0], 1)
        self.assertEqual(self.status(r, a), 'rejected_duplicate_window')
        self.assertEqual(r.store.db.execute("SELECT count(*) FROM maintenance_windows").fetchone()[0], 1)

    def test_receipt_write_failure_keeps_durable_hold(self):
        r, (a,) = self.parked()
        s = self.ask(r, a)
        spool = requests.directory(self.cfg.state_dir, a.agent, maintenance.SPOOL)
        with patch('mesh_runner.workspace_requests.os.fsync', side_effect=OSError('disk full')):
            r.tick()                                                  # filesystem boundary fails: receipt cannot be published
        rid = s['request']['request_id']
        self.assertFalse((spool / (rid + '.receipt.json')).exists())
        self.assertEqual(maintenance.held(r.store, attempt=a.id)['window'], rid)   # hold precedes receipt
        self.assertEqual(self.answer(r, a, s)['status'], 'ready')                  # next tick reports it
        self.assertEqual(r.store.db.execute("SELECT count(*) FROM maintenance_windows").fetchone()[0], 1)

    def test_restart_and_late_time_keep_hold_until_exact_finish(self):
        r, (a, other) = self.parked(2)
        got = self.answer(r, a, self.ask(r, a))
        window = got['window']
        restarted = self.runner(r.mesh)
        restarted.refresh_me()
        later = lambda t=time.time: t() + 7 * 86400
        with patch('time.time', later):                               # a held window never expires by itself
            restarted.tick()
            restarted.tick()
        self.assertEqual(maintenance.held(restarted.store, attempt=a.id)['window'], window)
        before = self.snapshot(restarted, a)
        foreign = [dict(window_id=window, lease='2' * 64),            # stale lease
                   dict(window_id=str(uuid.uuid4())),                 # unknown window
                   dict(window_id=window, pool='builder/elsewhere')]  # wrong pool
        for change in foreign:
            with self.subTest(change=change):
                self.assertIn(self.status(restarted, a, 'finish', **change),
                              ('rejected_foreign_finish', 'rejected_stale_lease', 'rejected_lease_drift'))
                self.assertEqual(self.snapshot(restarted, a), before)
        # a finish pinned to another attempt's identity cannot lift this window
        s = self.ask(restarted, other, 'finish', window_id=window)
        self.assertEqual(self.answer(restarted, other, s)['status'], 'rejected_foreign_finish')
        self.assertEqual(maintenance.held(restarted.store, attempt=a.id)['window'], window)
        self.assertEqual(self.status(restarted, a, 'finish', window_id=window), 'finished')
        self.assertIsNone(maintenance.held(restarted.store, attempt=a.id))
        again = self.status(restarted, a, 'finish', window_id=window)
        self.assertEqual(again, 'already_finished')

    def test_finish_refused_when_a_writer_appeared(self):
        r, (a,) = self.parked()
        window = self.answer(r, a, self.ask(r, a))['window']
        proc = subprocess.Popen(['sleep', '60'], start_new_session=True)
        try:
            r.store.update(a, pid=proc.pid, stamp=proc_stamp(proc.pid))
            self.assertEqual(self.status(r, a, 'finish', window_id=window), 'rejected_active_writer')
            self.assertIsNotNone(maintenance.held(r.store, attempt=a.id))
        finally:
            proc.kill()
            proc.wait()
        r.store.update(r.store.get(a.id), pid=0, stamp='')
        self.assertEqual(self.status(r, a, 'finish', window_id=window), 'finished')

    # ---------------------------------------------------------------- barrier coverage
    def test_hold_closes_wake_suspend_resume_admission_capture_restore_free(self):
        r, (a, other) = self.parked(2)
        self.answer(r, a, self.ask(r, a))
        r.store.update(a, wait={'kind': 'time', 'started': a.wait['started'], 'until': '2000-01-01T00:00:00Z'})
        a = r.store.get(a.id)               # the WAIT is due while the window is open
        before = self.snapshot(r, a)
        r.agent.workspace_wait_release_minutes = 0
        for _ in range(2):
            r.tick()                                                  # wake poll, suspend policy, admission
        self.assertEqual(self.snapshot(r, a), before)                 # a due WAIT did not wake; nothing captured
        self.assertEqual(self.status(r, a, request_id=str(uuid.uuid4())), 'rejected_duplicate_window')
        # explicit paths: pool primitives refuse
        with self.assertRaises(WorkspaceError):
            r.pool.suspend(a.id, {}, writer_stopped=True)
        with self.assertRaises(WorkspaceError):
            r.pool.restore(a.id)
        with self.assertRaises(WorkspaceError):
            r.pool.release(a.id)
        with self.assertRaises(WorkspaceError):
            r.pool.archive_and_free(a.workspaces[0])
        self.assertIn('not held', r.pool.free_path(a.workspaces[0]))       # a leased slot is never freed by the operator CLI
        self.assertEqual(r.pool.recheck_held({a.id}), [])
        self.assertEqual(self.answer_plain(r, a), 'rejected_maintenance_hold')
        # admission of an added repository
        spec = replace(self.agent.repos[0], name='app')
        sub = repo_admission.submit(self.cfg.state_dir, a.agent, a.task_id, a.id, requests.generation(a),
                                    str(uuid.uuid4()), spec, 300)
        r.tick()
        path = requests.directory(self.cfg.state_dir, a.agent, repo_admission.SPOOL) / (
            sub['request']['request_id'] + '.receipt.json')
        self.assertEqual(json.loads(path.read_text())['status'], 'rejected_maintenance_hold')
        # resume of a ready attempt
        r.store.update(r.store.get(a.id), state='ready')
        r.resume_ready(5)
        r.tick()
        self.assertEqual(r.store.get(a.id).state, 'ready')
        self.assertEqual(r.pool.leased_to(a.id), [self.slot(r, a)['path']])
        self.assertEqual(r.pool._checkpoint_rows(a.id), [])
        self.assertEqual(r.store.get(a.id).workspaces, before[2])
        self.other_target_works(r, other)

    # ---------------------------------------------------------------- races past the precheck
    def open_window_row(self, r, a, slot):
        r.store.db.execute(
            "INSERT INTO maintenance_windows(window,agent,task,attempt,generation,pool,slot,lease,state,opened_at) "
            "VALUES(?,?,?,?,?,?,?,?,'held',?)",
            (str(uuid.uuid4()), a.agent, a.task_id, a.id, requests.generation(a), slot['pool'], slot['path'],
             maintenance.lease_token(slot), int(time.time())))

    def race(self, r, a, operation, slot, name, during=None):
        """Run ``operation`` so that it passes its barrier precheck, then blocks on the repository lock;
        the window opens while it waits, under that same lock, as acquire does."""
        repo = self.agent.repos[0]
        result = {}

        def work():
            try:
                result['value'] = operation()
            except WorkspaceError as e:
                result['error'] = e

        def parked_on_lock(thread):
            """Observed, not injected: the worker is inside ``operation`` and waits in the lock's
            ``__enter__`` — it is past every check that precedes the lock."""
            frame = sys._current_frames().get(thread.ident)
            names = []
            while frame:
                names.append(frame.f_code.co_name)
                frame = frame.f_back
            return '__enter__' in names and name in names and '_maintenance_guard' not in names

        with base_lock(repo.base):
            worker = threading.Thread(target=work)
            worker.start()
            deadline = time.monotonic() + 10
            while not parked_on_lock(worker):
                self.assertTrue(worker.is_alive() and time.monotonic() < deadline, 'worker never reached the lock')
                worker.join(0.001)               # a yield bounded by the deadline, not a timing proof
            (during or (lambda: self.open_window_row(r, a, slot)))()
        worker.join(30)
        self.assertFalse(worker.is_alive())
        return result

    def test_release_that_passed_its_precheck_cannot_return_the_slot(self):
        r, (a,) = self.parked()
        before = self.snapshot(r, a)
        out = self.race(r, a, lambda: r.pool.release(a.id), before[3], 'release')
        self.assertIn('error', out)
        self.assertEqual(self.slot(r, a), before[3])                 # still leased, not held, not freed
        self.assertEqual(r.pool.leased_to(a.id), [before[3]['path']])

    def test_release_of_an_unsafe_slot_cannot_rewrite_a_window_lease_to_held(self):
        r, (a,) = self.parked()
        before = self.snapshot(r, a)
        (Path(before[3]['path']) / 'README').write_text('uncommitted work\n')     # unsafe: release would hold it
        out = self.race(r, a, lambda: r.pool.release(a.id), before[3], 'release')
        self.assertIn('error', out)
        self.assertEqual(self.slot(r, a), before[3])                  # still the pinned lease, never held

    def test_free_path_that_passed_its_precheck_cannot_free_the_slot(self):
        r, (a,) = self.parked()
        slot = self.slot(r, a)
        path = slot['path']
        r.pool._set(Path(path), 'app', 'held', a.id, 'fixture hold')        # clean slot, held: freeable but for the window
        out = self.race(r, a, lambda: r.pool.free_path(path), slot, 'free_path')
        self.assertIn('maintenance window', out.get('value', ''))
        row = r.store.db.execute("SELECT state FROM slots WHERE path=?", (path,)).fetchone()
        self.assertEqual(row[0], 'held')

    def test_inspect_rejects_a_request_id_that_is_a_path(self):
        r, (a,) = self.parked()
        with self.assertRaises(ValueError):
            maintenance.inspect(self.cfg.state_dir, r.store, a.agent, '../../runner')
        self.assertIn('windows', maintenance.inspect(self.cfg.state_dir, r.store, a.agent, str(uuid.uuid4())))

    def test_operator_free_slot_never_frees_a_leased_target_even_without_a_window(self):
        """`free-slot` is another process: the only ordering it shares with the Runner is the slot row."""
        r, (a,) = self.parked()
        slot = self.slot(r, a)
        self.assertIn('not held', r.pool.free_path(slot['path']))
        self.assertIn('not held', r.pool.free_path(slot['path'], archive=True))
        self.assertEqual(self.slot(r, a), slot)
        self.assertEqual(self.status(r, a), 'ready')                  # the window is then taken on the same untouched lease
        self.assertIn('not held', r.pool.free_path(slot['path']))
        self.assertEqual(self.slot(r, a), slot)

    def test_free_slot_command_in_another_process_leaves_a_leased_target_alone(self):
        """The real CLI, a real second process, against the Runner's own state: lease and window stay."""
        r, (a,) = self.parked()
        slot = self.slot(r, a)
        (self.cfg.state_dir / 'runner.sqlite').symlink_to(self.cfg.state_dir / 'r.sqlite')   # the CLI's store is the Runner's
        key = self.root / 'builder.key'
        key.write_text('k\n')
        key.chmod(0o600)
        conf = self.root / 'runner.toml'
        repo = self.agent.repos[0]
        conf.write_text(f'''[runner]
mesh_url = "http://mesh"
state_dir = "{self.cfg.state_dir}"

[[agents]]
name = "{a.agent}"
key_file = "{key}"
harness = "claude"
profile_dir = "{self.agent.profile_dir}"

  [[agents.repos]]
  name = "{repo.name}"
  base = "{repo.base}"
  max_slots = {repo.max_slots}
''')
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
        for extra in ([], ['--archive']):
            out = subprocess.run([sys.executable, '-m', 'mesh_runner.cli', 'free-slot', '--config', str(conf),
                                  '--path', slot['path'], *extra], env=env, capture_output=True, text=True, timeout=120)
            self.assertIn('not held', out.stdout, out.stdout + out.stderr)
        self.assertEqual(self.slot(r, a), slot)
        self.assertEqual(r.pool.leased_to(a.id), [slot['path']])

    def test_free_path_loses_to_a_runner_that_reused_the_slot_after_its_read(self):
        """The operator process read ``held``; before it acts, the Runner frees and re-leases the slot."""
        r, (a,) = self.parked()
        slot = self.slot(r, a)
        path = slot['path']
        r.pool._set(Path(path), 'app', 'held', a.id, 'fixture hold')
        newcomer = str(uuid.uuid4())

        def runner_reuses():
            r.store.db.execute("UPDATE slots SET state='leased', attempt=?, updated_at=? WHERE path=?",
                               (newcomer, '2099-01-01T00:00:00Z', path))

        out = self.race(r, a, lambda: r.pool.free_path(path), slot, 'free_path', during=runner_reuses)
        self.assertIn('changed', out.get('value', ''))
        row = r.store.db.execute("SELECT state, attempt FROM slots WHERE path=?", (path,)).fetchone()
        self.assertEqual(tuple(row), ('leased', newcomer))

    def test_recheck_held_loses_to_an_operator_or_reuse_after_its_snapshot(self):
        """The Runner's own sweep snapshots held rows before the lock; a row that changed since is not its."""
        r, (a,) = self.parked()
        slot = self.slot(r, a)
        path = slot['path']
        r.pool._set(Path(path), 'app', 'held', a.id, 'fixture hold')
        newcomer = str(uuid.uuid4())

        def reused():
            r.store.db.execute("UPDATE slots SET state='leased', attempt=?, updated_at=? WHERE path=?",
                               (newcomer, '2099-01-01T00:00:00Z', path))

        out = self.race(r, a, lambda: r.pool.recheck_held(set()), slot, 'recheck_held', during=reused)
        self.assertEqual(out.get('value'), [])
        row = r.store.db.execute("SELECT state, attempt FROM slots WHERE path=?", (path,)).fetchone()
        self.assertEqual(tuple(row), ('leased', newcomer))
