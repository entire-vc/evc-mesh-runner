"""One shared deadline for a WAIT observation, and a safe cause when a read fails.

Real MeshClient over a local HTTP fixture, real runner, Store and SQLite; the only fake is the
Mesh server itself (the true external boundary).
"""
import datetime as dt
import errno
import json
import re
import socket
import ssl
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fakes import AGENT_ID, STATUSES, task
from mesh_runner import coalesced_http
from mesh_runner import observation_budget as budget
from mesh_runner import wake as wake_mod
from mesh_runner.config import load
from mesh_runner.mesh import MeshClient, MeshError
from mesh_runner.runner import AgentRunner
from mesh_runner.store import Store
from mesh_runner.wake import GitHub, GitLab
from test_coalesced_http import _cert
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
                elif mode == 'trickle_head':
                    # the status line arrives whole, the header block one byte at a time: every byte lands
                    # well inside the socket's idle timeout, the head never ends in time
                    self.wfile.write(b'HTTP/1.1 200 OK\r\n')
                    try:
                        for b in b'X-Pad: ' + b'p' * 60 + b'\r\nContent-Length: 2\r\n\r\n{}':
                            self.wfile.write(bytes([b]))
                            self.wfile.flush()
                            time.sleep(0.05)
                    except OSError:
                        pass
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
                elif mode == '503':
                    return self.send({'message': 'unavailable'}, 503)
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

    def test_trickled_headers_cannot_outlive_the_deadline(self):
        # the socket timeout is an idle timeout per recv; a head sent byte by byte reset it
        r, (a,), _ = self.flow(budget_s=1.5)
        self.srv.modes['*'] = 'trickle_head'
        t0 = time.monotonic()
        r.check_wakes()
        self.assertLess(time.monotonic() - t0, 2.5)          # 80 header bytes x 0.05s would be 4s
        (cause,) = self.failed_events(r, a)
        self.assertIn('kind=timeout', cause)
        elapsed = float(re.search(r'elapsed=([\d.]+)s', cause)[1])
        self.assertLessEqual(elapsed, 1.5 + 1)

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
        self.assertIn('cause=HTTPError', cause)
        self.assertNotIn('glpat', cause)
        self.assertEqual(obs.retries, 0)                                 # 4xx is never retried

    def test_vcs_get_retries_transient_reset_and_succeeds_within_budget(self):
        self.flow()
        self.srv.modes['*'] = 'reset'
        orig_route = self.srv.route
        attempts = {'n': 0}

        # Override mode to clear after the first dropped connection
        real_get = self.srv.http.RequestHandlerClass.do_GET

        def flaky_do_GET(handler):
            attempts['n'] += 1
            if attempts['n'] >= 2:
                self.srv.modes.clear()
            return real_get(handler)

        self.srv.route = lambda path: {'state': 'open', 'head': {'sha': 'abc'}, 'base': {'ref': 'main'}}
        gh = GitHub(api_url=self.srv.url)
        with mock.patch.object(self.srv.http.RequestHandlerClass, 'do_GET', flaky_do_GET), \
                mock.patch.object(wake_mod, 'VCS_RETRY_DELAYS', (0.05, 0.05)):
            with budget.observe(5) as obs:
                mr = gh.get_mr('github:acme/widgets!5733')
        self.assertEqual(mr['state'], 'opened')
        self.assertEqual(obs.retries, 1)
        self.assertEqual(attempts['n'], 2)
        self.srv.route = orig_route

    def test_vcs_get_retries_a_5xx_gateway_answer_and_succeeds(self):
        self.flow()
        self.srv.modes['*'] = '503'
        real_get = self.srv.http.RequestHandlerClass.do_GET
        attempts = {'n': 0}

        def flaky_do_GET(handler):
            attempts['n'] += 1
            if attempts['n'] >= 2:
                self.srv.modes.clear()
            return real_get(handler)

        orig_route = self.srv.route
        self.srv.route = lambda path: {'id': 39965, 'status': 'success'}
        gl = GitLab(self.srv.url, 'glpat-fixture')
        with mock.patch.object(self.srv.http.RequestHandlerClass, 'do_GET', flaky_do_GET), \
                mock.patch.object(wake_mod, 'VCS_RETRY_DELAYS', (0.05, 0.05)):
            with budget.observe(5) as obs:
                row = gl.get_pipeline('acme/widgets#39965')
        self.srv.route = orig_route
        self.assertEqual(row['status'], 'success')
        self.assertEqual((attempts['n'], obs.retries), (2, 1))

    def test_vcs_5xx_on_every_attempt_is_retried_then_described(self):
        self.flow()
        self.srv.modes['*'] = '503'
        gl = GitLab(self.srv.url, 'glpat-fixture')
        before = len(self.srv.log)
        with mock.patch.object(wake_mod, 'VCS_RETRY_DELAYS', (0.05, 0.05)):
            with budget.observe(5) as obs:
                with self.assertRaises(Exception) as caught:
                    gl.get_pipeline('secret-org/secret-repo#39965')
                cause = budget.describe(caught.exception, obs)
        self.assertEqual(len(self.srv.log) - before, 3)                  # first try and both retries
        for part in ('kind=http', 'status=503', 'stage=GET vcs/gitlab/projects/:id/pipelines/:id',
                     'cause=HTTPError', 'retries=2'):
            self.assertIn(part, cause)
        for leaked in ('secret-org', '39965', 'glpat', 'unavailable'):
            self.assertNotIn(leaked, cause)

    def test_vcs_get_retries_per_attempt_timeout_within_budget(self):
        self.flow()
        self.srv.modes['*'] = 'seq'
        self.srv.seq = [1.5, 0.0]                                        # first attempt stalls, retry answers fast
        self.srv.route = lambda path: {'id': 39965, 'status': 'success'}
        gl = GitLab(self.srv.url, 'glpat-fixture')
        self.assertLessEqual(wake_mod.VCS_TIMEOUT, 15.0)
        self.assertEqual(wake_mod.VCS_RETRY_DELAYS, (1.0, 3.0))
        with mock.patch.object(wake_mod, 'VCS_TIMEOUT', 0.4), \
                mock.patch.object(wake_mod, 'VCS_RETRY_DELAYS', (0.05, 0.05)):
            with budget.observe(5) as obs:
                row = gl.get_pipeline('acme/widgets#39965')
        self.assertEqual(row['status'], 'success')
        self.assertEqual(obs.retries, 1)

    def test_vcs_stall_on_every_attempt_names_endpoint_cause_and_retries(self):
        # one 30s read and no retry was logged as `stage=GET vcs elapsed=31s`
        self.flow()
        self.srv.modes['*'] = 'slow'
        self.srv.delay = 1.0
        gh = GitHub(api_url=self.srv.url)
        with mock.patch.object(wake_mod, 'VCS_TIMEOUT', 0.2), \
                mock.patch.object(wake_mod, 'VCS_RETRY_DELAYS', (0.05, 0.05)):
            with budget.observe(5) as obs:
                with self.assertRaises(Exception) as caught:
                    gh.get_mr('github:acme/widgets!5733')
                cause = budget.describe(caught.exception, obs)
        self.assertIn('kind=timeout', cause)
        self.assertIn('stage=GET vcs/github/repos/:id/:id/pulls/:id', cause)
        self.assertRegex(cause, r' cause=[A-Za-z/]*Timeout')
        self.assertIn('retries=2', cause)
        self.assertLess(obs.elapsed(), 1.5)                              # three short attempts, not one long one
        self.assertNotIn('widgets', cause)

    def test_stage_redacts_project_slugs_that_spell_a_path_word(self):
        self.assertEqual(budget.stage_of('GET', 'vcs/github/repos/actions/runs/pulls/5733'),
                         'GET vcs/github/repos/:id/:id/pulls/:id')
        self.assertEqual(budget.stage_of('GET', 'vcs/github/repos/jobs/trace/actions/runs/77'),
                         'GET vcs/github/repos/:id/:id/actions/runs/:id')
        self.assertEqual(budget.stage_of('GET', 'vcs/gitlab/projects/pipelines/merge_requests/9?x=1'),
                         'GET vcs/gitlab/projects/:id/merge_requests/:id')
        self.assertEqual(budget.stage_of('GET', '/api/v1/projects/tasks/tasks'), 'GET /api/v1/projects/:id/tasks')

    def test_a_bare_network_os_error_is_transport_and_a_local_one_is_not(self):
        for exc, kind in ((OSError(errno.ENETUNREACH, 'x'), 'transport'), (OSError(errno.EHOSTUNREACH, 'x'), 'transport'),
                          (OSError(errno.ENETDOWN, 'x'), 'transport'), (OSError(errno.ETIMEDOUT, 'x'), 'timeout'),
                          (FileNotFoundError(errno.ENOENT, 'x'), ''), (PermissionError(errno.EACCES, 'x'), ''),
                          (OSError(errno.ENOSPC, 'x'), ''), (OSError('no errno'), '')):
            with self.subTest(exc=repr(exc)):
                self.assertEqual(budget.classify(exc), kind)
        self.assertIn('kind=transport', budget.describe(OSError(errno.ENETUNREACH, 'x')))
        self.assertIn('cause=OSError:ENETUNREACH', budget.describe(OSError(errno.ENETUNREACH, 'x')))

    def test_vcs_get_retries_a_bare_socket_error_raised_mid_read_and_succeeds(self):
        self.flow()
        self.srv.route = lambda path: {'state': 'open', 'head': {'sha': 'abc'}, 'base': {'ref': 'main'}}
        real_read = budget.read
        calls = {'n': 0}

        def flaky_read(resp, max_bytes=None):
            calls['n'] += 1
            if calls['n'] == 1:
                raise OSError(errno.ENETUNREACH, 'Network is unreachable')   # not wrapped by urllib
            return real_read(resp, max_bytes)

        gh = GitHub(api_url=self.srv.url)
        with mock.patch.object(wake_mod.budget, 'read', flaky_read), \
                mock.patch.object(wake_mod, 'VCS_RETRY_DELAYS', (0.05, 0.05)):
            with budget.observe(5) as obs:
                mr = gh.get_mr('github:acme/widgets!5733')
        self.assertEqual(mr['state'], 'opened')
        self.assertEqual(calls['n'], 2)
        self.assertEqual(obs.retries, 1)

    def test_cause_names_errno_only_where_it_is_an_errno(self):
        import socket, ssl
        self.assertEqual(budget.cause_of(urllib.error.URLError(ConnectionResetError(errno.ECONNRESET, 'peer'))),
                         'URLError/ConnectionResetError:ECONNRESET')
        self.assertEqual(budget.cause_of(urllib.error.URLError(socket.gaierror(socket.EAI_NONAME, 'host'))),
                         'URLError/gaierror')                            # EAI_NONAME is 8 = ENOEXEC on macOS
        self.assertEqual(budget.cause_of(urllib.error.URLError(ssl.SSLError(1, 'tls'))), 'URLError/SSLError')
        self.assertEqual(budget.cause_of(TimeoutError('timed out')), 'TimeoutError')

    def test_vcs_stage_names_provider_and_endpoint_without_leaking_slugs_or_ids(self):
        self.flow()
        self.srv.modes['*'] = '403'
        gh = GitHub(api_url=self.srv.url)
        gl = GitLab(self.srv.url, 'glpat-fixture', gh)
        with budget.observe(5) as obs:
            with self.assertRaises(Exception) as caught:
                gh.get_mr('github:secret-org/secret-repo!5733')
            gh_cause = budget.describe(caught.exception, obs)
        self.assertIn('stage=GET vcs/github/repos/:id/:id/pulls/:id', gh_cause)
        for leaked in ('secret-org', 'secret-repo', '5733', '127.0.0.1'):
            self.assertNotIn(leaked, gh_cause)

        with budget.observe(5) as obs:
            with self.assertRaises(Exception) as caught:
                gl.get_pipeline('secret-org/secret-repo#39965')
            gl_cause = budget.describe(caught.exception, obs)
        self.assertIn('stage=GET vcs/gitlab/projects/:id/pipelines/:id', gl_cause)
        for leaked in ('secret-org', 'secret-repo', '39965', 'glpat', '127.0.0.1'):
            self.assertNotIn(leaked, gl_cause)

        with budget.observe(5) as obs:
            with self.assertRaises(Exception) as caught:
                gl.job_trace_tail('secret-org/secret-repo#39965', 897945)
            tr_cause = budget.describe(caught.exception, obs)
        self.assertIn('stage=GET vcs/gitlab/projects/:id/jobs/:id/trace', tr_cause)
        for leaked in ('secret-org', '897945', 'glpat', '127.0.0.1'):
            self.assertNotIn(leaked, tr_cause)

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
                if kind in ('timeout', 'transport'):
                    self.assertRegex(cause, r' cause=[A-Za-z]+')         # the class names the fault
                else:
                    self.assertNotIn('cause=', cause)
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


class Trickle:
    """Raw HTTP/1.1 fixture, plain or TLS. ``shape`` says where the answer starts to trickle, one byte per
    ``gap``: ``status`` (the status line itself), ``headers`` (status line whole, then the header block),
    ``body`` (the head at once, then the body), ``ok`` (everything at once). Every gap is far inside the
    client's idle timeout, so only a wall deadline can end the read. ``chunk`` bytes go per gap: a loaded host
    stretches each short wait, so a positive control sends a few chunks rather than many single bytes."""

    def __init__(self, shape, ctx=None, gap=0.04, body=b'{"ok": true}', pre_tls=0.0, pre_head=0.0, chunk=1):
        self.shape, self.ctx, self.gap, self.body, self.chunk = shape, ctx, gap, body, chunk
        self.pre_tls, self.pre_head = pre_tls, pre_head        # whole stalls before the handshake / the head
        self.stop = threading.Event()
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        scheme, host = ('https', 'localhost') if ctx else ('http', '127.0.0.1')
        self.url = f'{scheme}://{host}:{self.port}'
        threading.Thread(target=self._accept, daemon=True).start()

    def answer(self) -> bytes:
        # the length header comes last: a head cut short reads as a 200 with an empty body
        return (b'HTTP/1.1 200 OK\r\n' + b''.join(b'X-Pad-%d: %s\r\n' % (i, b'p' * 12) for i in range(3))
                + b'Content-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n'
                % len(self.body) + self.body)

    def _accept(self):
        while True:
            try:
                raw, _ = self.sock.accept()
            except OSError:
                return                                  # the listener was closed
            threading.Thread(target=self._serve, args=(raw,), daemon=True).start()

    def _serve(self, raw):
        conn = raw
        try:
            if self.ctx:
                if self.pre_tls and self.stop.wait(self.pre_tls):
                    return
                conn = self.ctx.wrap_socket(raw, server_side=True)
            buf = b''
            while b'\r\n\r\n' not in buf:
                part = conn.recv(65536)
                if not part:
                    return
                buf += part
            if self.pre_head and self.stop.wait(self.pre_head):
                return
            out = self.answer()
            at = {'status': 0, 'headers': out.index(b'\r\n') + 2,
                  'body': out.index(b'\r\n\r\n') + 4, 'ok': len(out)}[self.shape]
            conn.sendall(out[:at])
            for i in range(at, len(out), self.chunk):
                if self.stop.wait(self.gap):
                    return
                conn.sendall(out[i:i + self.chunk])
        except OSError:
            pass                                        # the client gave up: the point of the test
        finally:
            conn.close()

    def close(self):
        self.stop.set()
        self.sock.close()


def watchdogs() -> list:
    return [t for t in threading.enumerate() if t.name == 'observation-watchdog' and t.is_alive()]


class HeaderDeadline(unittest.TestCase):
    """The observation deadline is a wall deadline from connect to the last header byte, not only
    an idle timeout per recv. Real MeshClient and VCS readers, real sockets; the server is the only fake."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.crt, cls.key = _cert(cls.tmp.name)
        cls.server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.server_ctx.load_cert_chain(cls.crt, cls.key)
        cls.client_ctx = ssl.create_default_context(cafile=cls.crt)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def serve(self, shape, tls=False, **kw):
        srv = Trickle(shape, self.server_ctx if tls else None, **kw)
        self.addCleanup(srv.close)
        return srv

    def mesh(self, srv, timeout=30.0):
        opener = None
        if srv.ctx:      # the production HTTPS path, trusting the fixture's certificate
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                coalesced_http.CoalescingHTTPSHandler(context=self.client_ctx)).open
        return MeshClient(srv.url, KEY, timeout=timeout, opener=opener)

    def timed(self, seconds, call):
        """(result or exception, wall seconds) of ``call`` inside a ``seconds`` observation."""
        t0 = time.monotonic()
        with budget.observe(seconds):
            try:
                out = call()
            except Exception as e:      # noqa: BLE001 - the test inspects what came out
                out = e
        return out, time.monotonic() - t0

    # AC1 -------------------------------------------------------------------------------------
    def test_mesh_read_with_a_trickled_status_line_ends_at_the_deadline(self):
        srv = self.serve('status')
        out, took = self.timed(1.5, lambda: self.mesh(srv).request('GET', '/tasks/by-short-id/0000aaaa'))
        self.assertLessEqual(took, 1.5 + 1)             # 160 head bytes x 0.04s would be over 6s
        self.assertIsInstance(out, MeshError)
        self.assertEqual(out.kind, 'timeout')
        # a read cut at the deadline names its fault like a socket timeout does: it was attempted
        self.assertIn('cause=TimeoutError', budget.describe(out))

    def test_mesh_read_with_a_trickled_header_block_ends_at_the_deadline(self):
        # a head cut short by the watchdog must not pass for a 200 with an empty body
        srv = self.serve('headers')
        out, took = self.timed(1.5, lambda: self.mesh(srv).request('GET', '/tasks/x/comments'))
        self.assertLessEqual(took, 1.5 + 1)
        self.assertIsInstance(out, MeshError)
        self.assertEqual(out.kind, 'timeout')

    def test_mesh_read_over_tls_with_a_trickled_header_block_ends_at_the_deadline(self):
        srv = self.serve('headers', tls=True)
        out, took = self.timed(1.5, lambda: self.mesh(srv).request('GET', '/tasks/x'))
        self.assertLessEqual(took, 1.5 + 1)
        self.assertIsInstance(out, MeshError)
        self.assertEqual(out.kind, 'timeout')

    def test_stalls_split_across_handshake_and_head_end_at_the_deadline(self):
        # each stall is under the 2s idle timeout, together they pass the budget: only a wall limit ends it
        srv = self.serve('ok', tls=True, pre_tls=1.8, pre_head=1.8)
        out, took = self.timed(2, lambda: self.mesh(srv).request('GET', '/tasks/by-short-id/0000aaaa'))
        self.assertLessEqual(took, 2 + 1)
        self.assertIsInstance(out, MeshError)
        self.assertEqual(out.kind, 'timeout')

    def test_no_watchdog_thread_reads_unguarded_and_leaves_no_guard_behind(self):
        srv = self.serve('ok')
        client = self.mesh(srv)
        with mock.patch.object(threading.Timer, 'start', side_effect=RuntimeError("can't start new thread")):
            out, _ = self.timed(5, lambda: client.request('GET', '/tasks/x'))
        self.assertEqual(out, {'ok': True})
        self.assertIsNone(getattr(budget._local, 'guard', None))
        out, _ = self.timed(5, lambda: client.request('GET', '/tasks/x'))      # the next guarded read is whole
        self.assertEqual(out, {'ok': True})
        self.assertEqual(client.request('GET', '/tasks/x'), {'ok': True})      # and outside an observation

    def test_attempt_limit_bounds_the_head_inside_a_longer_budget(self):
        srv = self.serve('headers')
        out, took = self.timed(10, lambda: self.mesh(srv, timeout=0.6)._request('GET', '/tasks/x'))
        self.assertLessEqual(took, 0.6 + 1)
        self.assertIsInstance(out, MeshError)
        self.assertEqual(out.kind, 'timeout')
        self.assertIn('attempt limit', str(out))        # the attempt ran out, not the observation

    # AC2 -------------------------------------------------------------------------------------
    def test_github_pull_request_read_with_trickled_headers_ends_at_the_deadline(self):
        srv = self.serve('headers')
        out, took = self.timed(1.5, lambda: GitHub(api_url=srv.url).get_mr('org/repo!7'))
        self.assertLessEqual(took, 1.5 + 1)
        self.assertIsInstance(out, Exception)
        self.assertEqual(budget.classify(out), 'timeout')

    def test_gitlab_read_with_a_trickled_status_line_ends_at_the_deadline(self):
        srv = self.serve('status')
        out, took = self.timed(1.5, lambda: GitLab(srv.url, 'glpat-fixture').get('projects/x/merge_requests/7'))
        self.assertLessEqual(took, 1.5 + 1)
        self.assertIsInstance(out, Exception)
        self.assertEqual(budget.classify(out), 'timeout')

    # AC3: positive controls -----------------------------------------------------------------
    def test_plain_answers_still_read(self):
        for tls in (False, True):
            with self.subTest(tls=tls):
                srv = self.serve('ok', tls=tls)
                out, _ = self.timed(5, lambda: self.mesh(srv).request('GET', '/tasks/x'))
                self.assertEqual(out, {'ok': True})
        srv = self.serve('ok')
        out, _ = self.timed(5, lambda: GitHub(api_url=srv.url).get_mr('org/repo!7'))
        self.assertEqual(out['state'], 'opened')
        out, _ = self.timed(5, lambda: GitLab(srv.url, 'glpat-fixture').get('projects/x'))
        self.assertEqual(out, {'ok': True})
        self.assertEqual(watchdogs(), [])               # no timer outlives its read

    def test_slow_body_inside_the_budget_reads_past_the_attempt_limit(self):
        # the attempt limit (0.6s) bounds the head only; a body that keeps coming inside the budget is read
        srv = self.serve('body', gap=0.4, chunk=12, body=b'{"ok": true, "pad": "' + b'q' * 12 + b'"}')
        out, took = self.timed(10, lambda: self.mesh(srv, timeout=0.6).request('GET', '/tasks/x'))
        self.assertEqual(out, {'ok': True, 'pad': 'q' * 12})
        self.assertGreater(took, 0.6 + 0.3)             # three body chunks 0.4s apart
        srv = self.serve('body', gap=0.4, chunk=5)
        out, _ = self.timed(10, lambda: GitLab(srv.url, 'glpat-fixture').get('projects/x'))
        self.assertEqual(out, {'ok': True})
        self.assertEqual(watchdogs(), [])

    def test_late_but_whole_head_inside_the_budget_reads(self):
        srv = self.serve('headers', gap=0.25, chunk=40)  # the head in four pieces, about 1s, inside the budget
        out, took = self.timed(10, lambda: self.mesh(srv, timeout=3).request('GET', '/tasks/x'))
        self.assertGreater(took, 0.6)
        self.assertEqual(out, {'ok': True})
