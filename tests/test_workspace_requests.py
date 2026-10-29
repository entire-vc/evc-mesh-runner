"""Scoped request protocol and real worktree recovery regression tests."""
from dataclasses import replace
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import patch

from mesh_runner import workspace_requests as requests
from mesh_runner.workspace import git
from mesh_runner.store import iso
from test_runner import Bench
from fakes import FakeMesh, task, write_replies, FUTURE_AT


class WorkspaceRequests(Bench):
    def parked(self, count=1):
        self.agent.repos[0] = replace(self.agent.repos[0], max_slots=count)
        tasks = [task(n + 1, cat='in_progress') for n in range(count)]
        r = self.runner(FakeMesh(tasks, max_concurrent=1))
        r.refresh_me()
        attempts = []
        for t in tasks:
            a = r.store.create('builder', t['id'], 'claude')
            p = r.pool.lease(a.id, 'app')
            r.store.update(a, state='waiting', workspaces=[str(p)], session_id=str(uuid.uuid4()),
                           wait={'kind': 'time', 'started': iso(),
                                 'until': '2099-01-01T00:00:00Z'})
            attempts.append(a)
        return r, attempts

    def submit(self, r, a, **changes):
        values = dict(state=self.cfg.state_dir, agent=a.agent, task=a.task_id, attempt=a.id,
                      token=requests.generation(a), request_id=str(uuid.uuid4()), ttl=300)
        values.update(changes)
        return requests.submit(**values)

    def receipt(self, r, a, submitted):
        path = requests.directory(self.cfg.state_dir, a.agent) / (submitted['request']['request_id'] + '.receipt.json')
        return json.loads(path.read_text())['status']

    def test_full_18_wait_pool_admits_one_normally_and_restores(self):
        r, attempts = self.parked(18)
        repair = task(50)
        r.mesh.tasks[repair['id']] = repair
        write_replies(self.replies, {repair['id'][:8]: f'⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 external work',
                                    attempts[0].task8: f'⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 external work'})
        a = attempts[0]
        dependency = task(51, assignee_id='agent-lead')
        r.mesh.tasks[dependency['id']] = dependency
        r.store.update(a, wait={'kind': 'card', 'target': dependency['id'][:8], 'started': iso()})
        source = Path(a.workspaces[0])
        (source / 'README').write_text('staged checkpoint\n')
        git(source, 'add', 'README')
        staged = git(source, 'ls-files', '--stage')
        (source / 'README').write_text('unstaged checkpoint\n')
        (source / 'private-ignored').write_text('fixture untracked\n')
        (source / 'cache').mkdir()
        (source / 'cache' / 'ignored').write_text('fixture ignored\n')
        r.tick()
        self.assertFalse(r.store.by_state('builder', ('running', 'starting')))
        before = {x.id: r.pool.leased_to(x.id) for x in attempts}
        submitted = self.submit(r, a)
        r.tick()
        self.assertEqual(self.receipt(r, a, submitted), 'suspended')
        self.assertEqual(r.store.get(a.id).state, 'waiting')
        self.assertEqual(r.pool.leased_to(a.id), [])
        self.assertEqual(len(r.store.by_state('builder', ('running', 'starting'))), 1)
        for other in attempts[1:]:
            self.assertEqual(r.pool.leased_to(other.id), before[other.id])
        self.settle_all(r)
        repair_attempt = next(x for x in r.store.by_state('builder', ('waiting',)) if x.task_id == repair['id'])
        # Return the repair's workspace through the same supported request path.
        self.submit(r, repair_attempt)
        r.tick()
        old_session = a.session_id
        r.mesh.set_category(dependency['id'], 'done')
        r.tick()  # ordinary check_wakes -> ready -> admission -> checkpoint restore
        restored = r.store.get(a.id)
        self.assertNotEqual(restored.session_id, old_session)
        target = Path(restored.workspaces[0])
        self.assertEqual((target / 'README').read_text(), 'unstaged checkpoint\n')
        self.assertEqual((target / 'private-ignored').read_text(), 'fixture untracked\n')
        self.assertFalse((target / 'cache' / 'ignored').exists())     # git-ignored data is regenerable: not snapshotted
        self.assertEqual(git(target, 'ls-files', '--stage'), staged)
        self.settle_all(r)

    def test_fencing_expiry_and_identity_reject_without_releasing(self):
        for kind in ('expired', 'generation', 'state', 'agent', 'task'):
            with self.subTest(kind=kind):
                r, rows = self.parked()
                a = rows[0]
                if kind == 'expired':
                    with patch('mesh_runner.workspace_requests.time.time', return_value=time.time()-600):
                        sub = self.submit(r, a)
                else:
                    sub = self.submit(r, a, **({'agent': 'other'} if kind == 'agent' else
                                              {'task': str(uuid.uuid4())} if kind == 'task' else {}))
                if kind == 'generation':
                    wait = dict(a.wait, started='2026-10-20T01:00:00Z')
                    r.store.update(a, wait=wait)
                if kind == 'state':
                    r.store.update(a, state='unknown')
                requests.consume(self.cfg.state_dir, 'builder', r._workspace_request)
                self.assertTrue(r.pool.leased_to(a.id))
                # Isolate each subcase's real state/pool, rather than reusing a lease.
                self.tearDown()
                self.setUp()

    def test_live_process_and_disk_budget_preserve_lease(self):
        r, (a,) = self.parked()
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                 start_new_session=True)
        self.addCleanup(child.wait)
        self.addCleanup(child.terminate)
        r.store.update(a, pid=child.pid)
        sub = self.submit(r, a)
        requests.consume(self.cfg.state_dir, a.agent, r._workspace_request)
        self.assertEqual(self.receipt(r, a, sub), 'failed_preserved')
        self.assertTrue(r.pool.leased_to(a.id))
        r.store.update(a, pid=0)
        # Actual capture budget guard with an impossible reserve; no disk mocking.
        from mesh_runner.checkpoint import Checkpoints
        original = Checkpoints._budget
        def reject_budget(cp, target, needed):
            cp.reserve_bytes = 1 << 62
            return original(cp, target, needed)
        sub = self.submit(r, a)
        with patch.object(Checkpoints, '_budget', reject_budget):
            requests.consume(self.cfg.state_dir, a.agent, r._workspace_request)
        self.assertEqual(self.receipt(r, a, sub), 'failed_preserved')
        self.assertTrue(r.pool.leased_to(a.id))

    def test_release_before_receipt_restart_is_idempotent(self):
        r, (a,) = self.parked()
        sub = self.submit(r, a)
        self.assertEqual(r._workspace_request(sub['request']), 'suspended')
        # Simulated crash boundary: release happened, receipt was never written.
        restarted = self.runner(r.mesh)
        restarted.refresh_me()
        requests.consume(self.cfg.state_dir, a.agent, restarted._workspace_request)
        self.assertEqual(self.receipt(restarted, a, sub), 'suspended')
        rows = restarted.pool._checkpoint_rows(a.id)
        repeated = self.submit(restarted, a, request_id=sub['request']['request_id'])
        self.assertEqual(repeated['request'], sub['request'])
        requests.consume(self.cfg.state_dir, a.agent, restarted._workspace_request)
        self.assertEqual(restarted.pool._checkpoint_rows(a.id), rows)

    def test_schema_symlink_and_reused_request_id_fail_closed(self):
        r, (a,) = self.parked()
        sub = self.submit(r, a)
        with self.assertRaises(ValueError):
            self.submit(r, a, task=str(uuid.uuid4()), request_id=sub['request']['request_id'])
        root = requests.directory(self.cfg.state_dir, a.agent)
        bad = root / (str(uuid.uuid4()) + '.request.json')
        bad.symlink_to(root / (sub['request']['request_id'] + '.request.json'))
        requests.consume(self.cfg.state_dir, a.agent, lambda _: 'suspended')
        self.assertTrue(r.pool.leased_to(a.id))

    def test_manual_hold_remains_held_and_partial_capture_can_restore(self):
        from mesh_runner.workspace import RepoSpec
        from test_workspace import make_repo
        self.agent.repos.append(RepoSpec('second', make_repo(self.root / 'second'), max_slots=1))
        r, (a,) = self.parked()
        second = r.pool.lease(a.id, 'second')
        first = Path(a.workspaces[0])
        paths = [str(first), str(second)]
        for path in (first, second):
            (path / 'README').write_text('preserve ' + path.name)
        # Actual unsupported state (a FIFO is not snapshotable), no mocked state/pool/checkpoint.
        fifo = second / 'unsupported.fifo'
        os.mkfifo(fifo)
        r.store.update(a, workspaces=paths)
        r.mesh.tasks[a.task_id]['human_gate'] = True
        r.mesh.tasks[a.task_id]['labels'] = ['no-runner']
        sub = self.submit(r, a)
        requests.consume(self.cfg.state_dir, a.agent, r._workspace_request)
        self.assertEqual(self.receipt(r, a, sub), 'failed_preserved')
        self.assertEqual(r.store.get(a.id).state, 'waiting')
        self.assertEqual(r.pool.leased_to(a.id), [str(second)])
        self.assertEqual((second / 'README').read_text(), 'preserve ' + second.name)
        self.assertFalse(r.mesh.checkouts)
        fifo.unlink()
        restarted = self.runner(r.mesh)
        restarted.refresh_me()
        self.assertEqual(restarted.store.get(a.id).state, 'waiting')
        self.assertTrue(r.mesh.tasks[a.task_id]['human_gate'])
        # Controlled owner clearing is a test fixture action, never request logic.
        r.mesh.tasks[a.task_id]['human_gate'] = False
        r.mesh.tasks[a.task_id]['labels'] = []
        write_replies(self.replies, {a.task8: f'⏳ WAIT {FUTURE_AT}: external=fixture-window source=card:#abcdef12 external work'})
        current = restarted.store.get(a.id)
        restarted.store.update(current, state='ready', wake_text='controlled owner event')
        restarted._resume_one(current)
        resumed = restarted.store.get(a.id)
        self.assertEqual(resumed.state, 'running')
        self.assertEqual({Path(p).joinpath('README').read_text() for p in resumed.workspaces},
                         {'preserve ' + first.name, 'preserve ' + second.name})
        self.settle_all(restarted)

    def test_expired_request_reconciles_already_released_after_crash(self):
        r, (a,) = self.parked()
        sub = self.submit(r, a)
        self.assertEqual(r._workspace_request(sub['request']), 'suspended')
        with patch('mesh_runner.runner.time.time', return_value=time.time()+1000):
            requests.consume(self.cfg.state_dir, a.agent, r._workspace_request)
        self.assertEqual(self.receipt(r, a, sub), 'suspended')

    def test_fifo_cannot_block_owning_tick(self):
        r, (a,) = self.parked()
        sub = self.submit(r, a)
        root = requests.directory(self.cfg.state_dir, a.agent)
        path = root / (sub['request']['request_id'] + '.request.json')
        path.unlink()
        os.mkfifo(path, 0o600)
        # Run in a child so a regression cannot hang the test runner itself.
        script = ('from pathlib import Path; from mesh_runner.workspace_requests import consume; '
                  'consume(Path(__import__("sys").argv[1]), "builder", '
                  'lambda _: (_ for _ in ()).throw(AssertionError("handler called")))')
        subprocess.run([sys.executable, '-c', script, str(self.cfg.state_dir)],
                       check=True, timeout=3)
        self.assertEqual(self.receipt(r, a, sub), 'rejected_schema')
        self.assertTrue(r.pool.leased_to(a.id))

    def test_receipt_io_failure_does_not_stop_tick_and_retries_durably(self):
        r, (a,) = self.parked()
        sub = self.submit(r, a)
        with patch.object(requests, '_publish', side_effect=OSError('full')):
            requests.consume(self.cfg.state_dir, a.agent, r._workspace_request)
        self.assertFalse(r.pool.leased_to(a.id))
        requests.consume(self.cfg.state_dir, a.agent, r._workspace_request)
        self.assertEqual(self.receipt(r, a, sub), 'suspended')
