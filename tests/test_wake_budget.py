"""One shared deadline for a WAIT observation, and a safe cause when a read fails.

Real MeshClient over a local HTTP fixture, real runner, Store and SQLite; the only fake is the
Mesh server itself (the true external boundary).
"""
import datetime as dt
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fakes import AGENT_ID, STATUSES, task
from mesh_runner import observation_budget as budget
from mesh_runner.config import load
from mesh_runner.mesh import MeshClient, MeshError
from mesh_runner.runner import AgentRunner
from mesh_runner.store import Store
from mesh_runner.wake import GitLab
from test_runner import Bench

KEY = 'fixture-agent-key-7f3a'


class Server:
    """GET fixture. ``mode`` per path prefix: ok / slow / reset / badjson / 403."""

    def __init__(self, tasks):
        self.tasks = {t['id']: t for t in tasks}
        self.target_id = tasks[-1]['id']
        self.modes = {}                 # task id (or '*') -> mode
        self.delay = 0.6
        self.seq = []                   # per-request delays for mode 'seq'
        self.log = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.log.append(('GET', self.path))
                path = self.path.split('?', 1)[0]
                mode = next((m for k, m in outer.modes.items() if k == '*' or k in path), 'ok')
                if mode == 'seq':
                    time.sleep(outer.seq.pop(0) if outer.seq else 0)
                elif mode == 'slow':
                    time.sleep(outer.delay)
                elif mode == 'stall':
                    # headers late (0.7s), then the body never comes
                    time.sleep(0.7)
                    self.send_response(200)
                    self.send_header('Content-Length', '10')
                    self.end_headers()
                    self.wfile.flush()
                    time.sleep(5)
                    return
                elif mode == 'trickle':
                    # each byte arrives well inside the socket timeout, the whole body never ends in time
                    self.send_response(200)
                    self.send_header('Content-Length', '100')
                    self.end_headers()
                    try:
                        for _ in range(100):
                            self.wfile.write(b' ')
                            self.wfile.flush()
                            time.sleep(0.2)
                    except OSError:
                        pass
                    return
                elif mode == 'reset':
                    self.connection.close()
                    return
                elif mode == 'badjson':
                    return self.send('{not json', raw=True)
                elif mode == '403':
                    return self.send({'message': 'forbidden'}, 403)
                self.send(outer.route(path))

            def do_POST(self):
                outer.log.append(('POST', self.path))
                self.rfile.read(int(self.headers.get('Content-Length') or 0))
                if outer.modes.get('*') == 'reset':
                    self.connection.close()
                    return
                self.send({}, 200)

            do_PATCH = do_PUT = do_DELETE = do_POST

            def send(self, body, status=200, raw=False):
                data = (body if raw else json.dumps(body)).encode()
                try:
                    self.send_response(status)
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

        self.http = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.http.daemon_threads = True
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.http.server_port}'

    def route(self, path):
        path = path.removeprefix('/api/v1')
        if path == '/agents/me':
            return {'id': AGENT_ID, 'name': 'builder'}
        if path == '/projects/p1/statuses':
            return STATUSES
        if path.startswith('/tasks/by-short-id/'):
            short = path.rsplit('/', 1)[1]
            return next((t for t in self.tasks.values() if t['id'].startswith(short)), {})
        if path.endswith('/comments'):
            return {'items': [], 'total_pages': 1}
        if path.endswith('/dependencies'):
            tid = path.split('/')[-2]
            edge = {'id': 'edge-' + tid[:4], 'task_id': tid, 'depends_on_task_id': self.target_id,
                    'dependency_type': 'blocks', 'condition': 'task_done', 'condition_satisfied': False}
            return {'outgoing': [edge], 'incoming': []}
        if path.endswith('/vcs-links'):
            return []
        return self.tasks.get(path.rsplit('/', 1)[1], {})

    def stop(self):
        self.http.shutdown()
        self.http.server_close()


class WakeBudget(Bench):
    n = iter(range(1000))

    def flow(self, budget_s=1.5, lanes=1):
        owned = [task(0x10 + i, cat='in_progress') for i in range(lanes)]
        target = task(0x99, cat='todo')
        self.srv = Server(owned + [target])
        self.addCleanup(self.srv.stop)
        self.cfg.wake_budget_seconds = budget_s
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        mesh = MeshClient(self.srv.url, KEY, timeout=1)
        r = AgentRunner(self.cfg, self.agent, Store(self.root / 'state' / f'wb{next(self.n)}.sqlite'), mesh=mesh,
                        adapter=self.adapter)
        self.addCleanup(r.store.db.close)
        r.refresh_me()
        attempts = []
        for t in owned:
            a = r.store.create(self.agent.name, t['id'], 'claude')
            wait = {'kind': 'card', 'target': target['id'][:8], 'since': '2026-10-01T00:00:00Z',
                    'started': '2026-10-01T00:00:00Z', 'errors': 0}
            r.store.update(a, state='waiting', session_id='native-' + t['id'][:4],
                           workspaces=['/leased/' + t['id'][:4]], wait=wait)
            attempts.append(r.store.get(a.id))
            t['checkout_session_id'] = a.id
        return r, attempts, target

    def snapshot(self, r, a):
        """The attempt's whole row (state, session, workspaces lease, checkout, wait) and its marks
        (checkpoint and delivery receipts), straight from SQLite."""
        row = tuple(r.store.db.execute('SELECT * FROM attempts WHERE id=?', (a.id,)).fetchone())
        marks = tuple(tuple(m) for m in r.store.db.execute(   # the failure-bound counter is bookkeeping
            'SELECT * FROM marks WHERE agent=? AND task_id=? AND key!=? ORDER BY key',
            (a.agent, a.task_id, AgentRunner.READ_FAILURES)))
        return row, marks

    def failed_events(self, r, a):
        rows = r.store.db.execute('SELECT kind, detail FROM events WHERE attempt=? AND kind=?',
                                  (a.id, 'wake_read_failed')).fetchall()
        return [row[1] for row in rows]

    # A1 -------------------------------------------------------------------------------------
    def test_one_deadline_covers_several_requests_and_backoff(self):
        r, (a,), _ = self.flow(budget_s=1.5)
        self.srv.modes['*'] = 'slow'
        self.srv.delay = 0.8
        t0 = time.monotonic()
        r.check_wakes()
        elapsed = time.monotonic() - t0
        # unbounded: 1s timeout + 1s backoff + 1s + 3s + 1s per request, several requests
        self.assertLess(elapsed, 3.0)
        (cause,) = self.failed_events(r, a)
        self.assertIn('kind=timeout', cause)
        self.assertIn('budget=1.5s', cause)
        self.assertRegex(cause, r'elapsed=\d+\.\d+s')
        self.assertRegex(cause, r'retries=\d+')
        self.assertIn('stage=GET /', cause)

    def test_backoff_shares_the_deadline(self):
        # timeout 1s + transport backoff 1s (no jitter on this path) leave 2.2s of 3.2s for the retry;
        # the 3s backoff after it is refused. Unbudgeted the same reads cost 1+1+1+3+1 = 7s per request.
        r, (a,), _ = self.flow(budget_s=3.2)
        self.srv.modes['*'] = 'slow'
        self.srv.delay = 1.5
        t0 = time.monotonic()
        r.check_wakes()
        self.assertLess(time.monotonic() - t0, 4.2)
        (cause,) = self.failed_events(r, a)
        self.assertIn('kind=timeout', cause)
        self.assertIn('retries=1', cause)
        slept = [e for e in self.srv.log if e[1].endswith('/api/v1/tasks/' + a.task_id)]
        self.assertEqual(len(slept), 2)            # first try + one retry, no third

    def test_distinct_reads_and_backoff_share_one_deadline(self):
        # read 1 answers in 0.6s, read 2 times out at 1s (t=1.6) and backs off 1s (t=2.6); the retry
        # is cut to the 0.4s left. Unbudgeted this wake would run far past 3s.
        r, (a,), _ = self.flow(budget_s=3.0)
        self.srv.modes['*'] = 'seq'
        self.srv.seq = [0.6, 1.5, 1.5, 1.5, 1.5]
        t0 = time.monotonic()
        r.check_wakes()
        self.assertLess(time.monotonic() - t0, 3.8)
        (cause,) = self.failed_events(r, a)
        self.assertIn('retries=1', cause)
        self.assertGreaterEqual(len({p.split('?')[0] for _, p in self.srv.log}), 2)    # different reads

    def test_repeated_channel_failures_never_become_a_wake(self):
        r, (a,), target = self.flow(budget_s=3)
        before = self.snapshot(r, a)
        self.srv.modes[target['id'][:8]] = 'reset'     # the target read inside wake.check (a 403 there is the
        #                                                 separate, documented unreadable-target path)
        for _ in range(8):                              # past max_errors (5)
            r.check_wakes()
        self.assertEqual(self.snapshot(r, a), before)   # no ready state, no wake text, no errors counter
        self.assertEqual(len(self.failed_events(r, a)), 8)

    def test_channel_failures_in_a_row_over_the_bound_wake_the_owner_once(self):
        r, (a,), target = self.flow(budget_s=3)
        self.srv.modes[target['id'][:8]] = 'reset'
        clock = {'t': dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)}
        with mock.patch('mesh_runner.runner.now', lambda: clock['t']):
            for _ in range(5):                          # many failures, but within minutes: no wake
                r.check_wakes()
                clock['t'] += dt.timedelta(minutes=10)
            self.assertEqual(r.store.get(a.id).state, 'waiting')
            clock['t'] += dt.timedelta(hours=6)
            r.check_wakes()                             # the 6th failure in a row, 6h after the first
        woke = r.store.get(a.id)
        self.assertEqual(woke.state, 'ready')
        self.assertIn('failed 6 times in a row over 6.8h', woke.wake_text)
        self.assertIn('kind=transport', woke.wake_text)
        self.assertIn('UNKNOWN', woke.wake_text)
        self.assertNotIn(KEY, woke.wake_text)
        self.assertEqual(len(self.failed_events(r, a)), 6)
        self.assertEqual(r.store.mark(a.agent, a.task_id, AgentRunner.READ_FAILURES)[0], '')

    def test_a_successful_check_resets_the_failure_count(self):
        r, (a,), target = self.flow(budget_s=3)
        clock = {'t': dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)}
        with mock.patch('mesh_runner.runner.now', lambda: clock['t']):
            for step in ('reset', 'reset', 'ok', 'reset', 'reset'):
                if step == 'ok':
                    self.srv.modes.clear()
                else:
                    self.srv.modes[target['id'][:8]] = 'reset'
                r.check_wakes()
                clock['t'] += dt.timedelta(hours=3)
            self.assertEqual(r.store.get(a.id).state, 'waiting')   # 2 in a row over 3h: below the bound
            self.srv.modes[target['id'][:8]] = 'reset'
            r.check_wakes()                                           # 3 in a row over 6h
        self.assertEqual(r.store.get(a.id).state, 'ready')

    def test_the_failure_count_survives_a_restart(self):
        r, (a,), target = self.flow(budget_s=3)
        self.srv.modes[target['id'][:8]] = 'reset'
        clock = {'t': dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)}
        with mock.patch('mesh_runner.runner.now', lambda: clock['t']):
            r.check_wakes(); clock['t'] += dt.timedelta(hours=4)
            r.check_wakes(); clock['t'] += dt.timedelta(hours=4)
            r._read_failures.clear()                   # a new runner process: memory gone, the mark stays
            r.check_wakes()
        self.assertEqual(r.store.get(a.id).state, 'ready')

    def test_retry_is_still_allowed_inside_the_budget(self):
        r, (a,), _ = self.flow(budget_s=20)
        calls = {'n': 0}
        real = self.srv.route

        def flaky(path):
            if path.endswith('/tasks/' + a.task_id):
                calls['n'] += 1
            return real(path)
        self.srv.route = flaky
        self.srv.modes[a.task_id] = 'reset'
        threading.Timer(0.5, lambda: self.srv.modes.clear()).start()
        r.check_wakes()
        self.assertGreaterEqual(calls['n'], 1)          # the GET was repeated after the reset
        self.assertEqual(self.failed_events(r, a), [])

    def test_mutation_is_not_repeated_on_transport_failure(self):
        r, _, _ = self.flow(budget_s=20)
        self.srv.modes['*'] = 'reset'
        with budget.observe(20):
            with self.assertRaises(MeshError) as caught:
                r.mesh.request('POST', '/tasks/x/comments', {'body': 'b'})
        self.assertEqual(caught.exception.kind, 'transport')
        self.assertEqual(len([m for m, _ in self.srv.log if m == 'POST']), 1)

    def test_trickled_body_cannot_outlive_the_deadline(self):
        r, (a,), _ = self.flow(budget_s=1.5)
        self.srv.modes['*'] = 'trickle'
        t0 = time.monotonic()
        r.check_wakes()
        self.assertLess(time.monotonic() - t0, 3.0)          # 100 bytes x 0.2s would be 20s
        (cause,) = self.failed_events(r, a)
        self.assertIn('kind=timeout', cause)

    def test_stalled_body_after_late_headers_respects_the_remaining_budget(self):
        r, (a,), _ = self.flow(budget_s=1.2)        # the client's own timeout is 1s
        self.srv.modes['*'] = 'stall'
        t0 = time.monotonic()
        r.check_wakes()
        self.assertLess(time.monotonic() - t0, 1.7)  # without the retime: 0.7s + a full 1s read timeout
        (cause,) = self.failed_events(r, a)
        self.assertIn('kind=timeout', cause)

    def test_vcs_http_status_is_kept_in_the_cause(self):
        r, _, _ = self.flow()
        self.srv.modes['*'] = '403'
        gitlab = GitLab(self.srv.url, 'glpat-fixture')
        with budget.observe(5) as obs:
            with self.assertRaises(Exception) as caught:
                gitlab.get('projects/x')
            cause = budget.describe(caught.exception, obs)
        self.assertIn('kind=http', cause)
        self.assertIn('status=403', cause)
        self.assertNotIn('glpat', cause)

    # A2 -------------------------------------------------------------------------------------
    def test_failure_kinds_are_distinguishable_and_never_a_dependency_done(self):
        for mode, kind in (('slow', 'timeout'), ('reset', 'transport'), ('badjson', 'parse'), ('403', 'http')):
            with self.subTest(mode=mode):
                r, (a,), _ = self.flow(budget_s=3)
                self.srv.modes['*'] = mode
                self.srv.delay = 1.5
                r.check_wakes()
                (cause,) = self.failed_events(r, a)
                self.assertIn('kind=' + kind, cause)
                if kind == 'http':
                    self.assertIn('status=403', cause)
                self.assertEqual(r.store.get(a.id).state, 'waiting')
                self.assertEqual([m for m, _ in self.srv.log if m != 'GET'], [])

    # A3 -------------------------------------------------------------------------------------
    def test_wait_lease_checkpoint_survive_then_one_wake_and_other_lanes_continue(self):
        r, (a, b), target = self.flow(budget_s=1.5, lanes=2)
        self.srv.delay = 0.8
        self.srv.modes[a.task_id] = 'slow'
        r.store.mark(a.agent, a.task_id, 'checkpoint', 'ckpt-1')
        r.store.update(a, checkout={'token': 'lease-tok', 'generation': 3})
        before_a = self.snapshot(r, a)
        self.assertEqual(len(before_a[1]), 1)
        r.check_wakes()
        self.assertEqual(self.snapshot(r, a), before_a)              # wait, lease, session untouched
        self.assertEqual(len(self.failed_events(r, a)), 1)
        self.assertEqual(self.failed_events(r, b), [])                # the other lane's tick ran
        # the target closes and Mesh recovers: own successful observation, exactly one wake
        self.srv.modes.clear()
        self.srv.tasks[target['id']]['status_id'] = next(s['id'] for s in STATUSES if s['category'] == 'done')
        r.check_wakes()
        woke = r.store.get(a.id)
        self.assertEqual(woke.state, 'ready')
        self.assertEqual(woke.wake_text.count('is done'), 1)
        again = self.snapshot(r, a)
        r.check_wakes()
        self.assertEqual(self.snapshot(r, a), again)

    # A4 -------------------------------------------------------------------------------------
    def test_no_secret_or_query_in_events_status_or_cause(self):
        r, (a,), _ = self.flow(budget_s=1.5)
        self.srv.modes['*'] = 'slow'
        self.srv.delay = 0.8
        r.check_wakes()
        dump = json.dumps([tuple(x) for x in r.store.db.execute('SELECT * FROM events').fetchall()]) + json.dumps(
            [list(x) for x in r.store.db.execute('SELECT * FROM attempts').fetchall()], default=str)
        self.assertNotIn(KEY, dump)
        for _, detail in [(0, d) for d in self.failed_events(r, a)]:
            self.assertNotIn('?', detail)
            self.assertNotIn('127.0.0.1', detail)

    def test_describe_strips_ids_and_queries(self):
        self.assertEqual(budget.stage_of('GET', '/tasks/00000000-0000-4000-8000-000000000005/comments?limit=40&page=2'),
                         'GET /tasks/:id/comments')


class BudgetConfig(unittest.TestCase):
    def load(self, line=''):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / 'k'
            key.write_text('x')
            key.chmod(0o600)
            path = Path(tmp) / 'r.toml'
            path.write_text(f'[runner]\nmesh_url = "http://m"\nstate_dir = "{tmp}"\n{line}\n'
                            f'[[agents]]\nname = "a"\nkey_file = "{key}"\nprofile_dir = "{tmp}"\n')
            return load(path)

    def test_default_value_and_a_retired_key_is_ignored(self):
        # wake_budget_seconds is no longer a [runner] option: the budget stays 45 s whatever a file says
        self.assertEqual(self.load().wake_budget_seconds, 45.0)
        for value in ('20', '0.2', 'inf', 'nan', '-5', '0', '"x"'):
            with self.subTest(value=value), self.assertLogs('mesh_runner', 'WARNING') as logs:
                self.assertEqual(self.load('wake_budget_seconds = ' + value).wake_budget_seconds, 45.0)
            self.assertIn('wake_budget_seconds is no longer an option', '\n'.join(logs.output))


class StageRedaction(unittest.TestCase):
    def test_unknown_segments_are_never_copied(self):
        stage = budget.stage_of('GET', '/tasks/my-secret-slug/comments?token=abc')
        self.assertEqual(stage, 'GET /tasks/:id/comments')
        self.assertEqual(budget.stage_of('GET', '/projects/Acme_Corp/statuses'), 'GET /projects/:id/statuses')
        self.assertNotIn('Acme', budget.stage_of('GET', '/workspaces/ws-prod-1/mentionables'))
