"""Bounded diagnostic reads never participate in task eligibility or wake."""
import copy
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from mesh_runner.blocking_path import BlockingPath, Budget, MARK
from mesh_runner.effects import Effects, runtime_state
from mesh_runner.mesh import MeshClient, MeshError
from mesh_runner.store import Store
from fakes import task


class Reads:
    def __init__(self, tasks, edges=None, clock=None):
        self.tasks = {t['id']: t for t in tasks}
        self.edges = edges or {}
        self.calls = []
        self.clock = clock
        self.fail = None
        self.runtime = {}

    def bounded_get(self, path, *, timeout):
        self.calls.append((path, timeout))
        if self.clock is not None:
            self.clock[0] += .75
        if self.fail:
            raise MeshError(self.fail, 'synthetic')
        tid = path.split('/')[2]
        if path.endswith('/dependencies'):
            return {'outgoing': self.edges.get(tid, []), 'incoming': []}
        if '/by-short-id/' in path:
            tid = next(k for k in self.tasks if k.startswith(path.rsplit('/', 1)[1]))
        return copy.deepcopy(self.tasks[tid])

    def put_runtime_state(self, tid, state):
        self.runtime[tid] = copy.deepcopy(state)


def node(n, **extra):
    return task(n, status_category='in_progress', human_gate=False, **extra)


def edge(t):
    return {'dependency_type': 'blocks', 'depends_on_task_id': t['id']}


class BlockingPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name)/'state.db')
        self.addCleanup(self.store.db.close)
        self.a = self.store.create('lane', task(1)['id'], 'codex')
        self.b, self.c = node(2), node(3)
        self.store.update(self.a, state='waiting', wait={'kind':'card', 'target':self.b['id'][:8]})

    def resolver(self, mesh):
        return BlockingPath(mesh, self.store, 'lane')

    def test_chain_gate_owner_separate_from_unknown_addressee_ref_and_state_unchanged(self):
        self.c.update(human_gate=True, human_gate_info={'owner_agent_id':'asker-id'}, assignee_id='assignee-id')
        mesh = Reads([self.b,self.c], {self.b['id']:[edge(self.c)]})
        fx = Effects(mesh, None, 'lane', store=self.store)
        original = copy.deepcopy(self.a)
        before = runtime_state(self.a, 'lane')
        fx.publish([self.a])
        shown = mesh.runtime[self.a.task_id]
        self.assertEqual(shown['ref'], before['ref'])
        self.assertEqual(shown['kind'], 'waiting')
        self.assertIn('human_gate=true', shown['reason'])
        self.assertIn('gate_owner=asker-id', shown['reason'])
        self.assertIn('addressee=unknown', shown['reason'])
        self.assertNotIn('assignee-id', shown['reason'])
        self.assertEqual(self.a, original)
        self.assertEqual(self.store.get(self.a.id), original)
        self.assertEqual(len(mesh.calls), 4)
        self.assertTrue(all('/comments' not in p for p,_ in mesh.calls))

    def test_output_budget_keeps_whole_fields_for_eight_paths_and_long_owners(self):
        from mesh_runner.blocking_path import _format_reason
        nodes = [node(n)['id'] for n in range(2, 10)]
        paths = [[self.b['id'], tid] for tid in nodes]
        gates = [(tid, 'x' * 64) for tid in nodes]
        observed = '2026-10-20T17:00:00.000+00:00'
        reason = _format_reason(self.a.task_id, paths, gates, observed)
        self.assertLessEqual(len(reason), 800)
        self.assertIn('UNKNOWN (output overflow)', reason)
        self.assertTrue(reason.endswith('; addressee=unknown; observed_at=' + observed))
        count = int(reason.split('; omitted_fields=')[1].split(';')[0])
        shown = reason.split(': ', 1)[1].split('; omitted_fields=')[0].split('; ')
        expected = [' -> '.join('#' + tid[:8] for tid in [self.a.task_id] + path) for path in paths]
        expected += ['#' + tid[:8] + ' human_gate=true gate_owner=' + owner for tid, owner in gates]
        self.assertGreater(count, 0)
        self.assertEqual(shown, expected[:-count])
        self.assertEqual(len(shown) + count, 16)

    def test_long_eight_node_gate_diagnostic_is_explicitly_incomplete_and_cached(self):
        nodes = [node(n, human_gate_info={'owner_agent_id': 'x' * 64}) for n in range(2, 10)]
        for t in nodes:
            t['human_gate'] = True
        mesh = Reads(nodes, {nodes[0]['id']: [edge(t) for t in nodes[1:]]})
        # Eight nodes need16GETs; the public12GET cap is separately tested.
        reason = self.resolver(mesh).resolve(self.a, Budget(max_gets=16))
        self.assertIn('UNKNOWN (output overflow)', reason)
        self.assertIn('omitted_fields=', reason)
        self.assertIn('addressee=unknown; observed_at=', reason)
        self.assertIn('#' + self.a.task8 + ' -> #' + nodes[0]['id'][:8], reason)
        self.assertLessEqual(len(reason), 800)
        for field in reason.split('; '):
            if 'gate_owner=' in field:
                self.assertTrue(field.endswith('gate_owner=' + 'x' * 64), field)
        calls = len(mesh.calls)
        self.assertEqual(self.resolver(mesh).resolve(self.a, Budget()), reason)
        self.assertEqual(len(mesh.calls), calls)

    def test_false_or_missing_gate_never_inferred_from_labels_owner_or_prose(self):
        for gate in (False, None, 'true', 1):
            with self.subTest(gate=gate):
                t = node(2)
                t.update(human_gate=gate, human_gate_info={'owner_agent_id':'asker'},
                         labels=['awaiting-pavel'], description='Blocking @person')
                mesh = Reads([t])
                result = self.resolver(mesh).resolve(self.a, Budget())
                self.assertNotIn('human_gate=true', result)
                self.assertNotIn('gate_owner=asker', result)
                if gate is not False:
                    self.assertIn('UNKNOWN', result)
                self.store.mark('lane', self.a.task_id, MARK, '')

    def test_cycle_and_429_unknown_cached_including_restart(self):
        for fault in ('cycle', 429):
            with self.subTest(fault=fault):
                self.store.mark('lane', self.a.task_id, MARK, '')
                mesh = Reads([self.b,self.c], {self.b['id']:[edge(self.c)],self.c['id']:[edge(self.b)]})
                if fault == 429:
                    mesh.fail = fault
                first = self.resolver(mesh).resolve(self.a, Budget())
                self.assertIn('UNKNOWN', first)
                self.assertIn(str(fault), first)
                calls = len(mesh.calls)
                self.assertEqual(self.resolver(mesh).resolve(self.a, Budget()), first)
                self.assertEqual(len(mesh.calls), calls)

    def test_deadline_residual_time_and_shared_get_budget(self):
        clock = [10.0]
        nodes = [node(n) for n in range(2,10)]
        mesh = Reads(nodes, {t['id']:[edge(nodes[i+1])] for i,t in enumerate(nodes[:-1])}, clock)
        with mock.patch('mesh_runner.blocking_path.time.monotonic', side_effect=lambda:clock[0]):
            result = self.resolver(mesh).resolve(self.a, Budget())
        self.assertIn('UNKNOWN', result)
        self.assertEqual(len(mesh.calls), 4)
        timeouts = [v for _,v in mesh.calls]
        self.assertEqual(timeouts, sorted(timeouts, reverse=True))
        self.store.mark('lane', self.a.task_id, MARK, '')
        mesh.clock = None
        budget = Budget(max_gets=1)
        result = self.resolver(mesh).resolve(self.a, budget)
        self.assertIn('UNKNOWN', result)
        self.assertEqual(budget.gets, 1)

    def test_failure_recovers_after_300_seconds_but_not_on_each_tick(self):
        mesh = Reads([self.b]);mesh.fail = 503
        resolver = self.resolver(mesh)
        before = dt.datetime(2026,10,20,tzinfo=dt.timezone.utc)
        with mock.patch('mesh_runner.blocking_path.now', return_value=before):
            self.assertIn('503', resolver.resolve(self.a, Budget()))
        mesh.fail = None
        with mock.patch('mesh_runner.blocking_path.now', return_value=before+dt.timedelta(seconds=299)):
            self.assertIn('503', self.resolver(mesh).resolve(self.a, Budget()))
        self.assertEqual(len(mesh.calls),1)
        with mock.patch('mesh_runner.blocking_path.now', return_value=before+dt.timedelta(seconds=301)):
            self.assertNotIn('UNKNOWN', self.resolver(mesh).resolve(self.a, Budget()))
        self.assertEqual(len(mesh.calls),3)

    def test_pagination_and_depth_cuts_unknown(self):
        mesh = Reads([self.b])
        mesh.bounded_get = lambda path,timeout: {'outgoing':[], 'has_more':True} if path.endswith('/dependencies') else self.b
        self.assertIn('UNKNOWN', self.resolver(mesh).resolve(self.a, Budget()))
        self.store.mark('lane', self.a.task_id, MARK, '')
        nodes = [node(n) for n in range(2,8)]
        mesh = Reads(nodes,{t['id']:[edge(nodes[i+1])] for i,t in enumerate(nodes[:-1])})
        self.assertIn('depth', self.resolver(mesh).resolve(self.a, Budget()))
        self.assertLessEqual(len(mesh.calls),12)

    def test_unknown_dependency_types_never_prove_a_complete_path(self):
        for kind in ('future_type', '', 'BLOCKS', None, 1, [], {}):
            with self.subTest(kind=kind):
                self.store.mark('lane', self.a.task_id, MARK, '')
                mesh = Reads([self.b, self.c], {self.b['id']: [
                    {'dependency_type': kind, 'depends_on_task_id': self.c['id']}]})
                self.assertIn('UNKNOWN', self.resolver(mesh).resolve(self.a, Budget()))

    def test_only_known_nonblocking_types_are_ignored_and_legacy_blocker_is_followed(self):
        self.c.update(human_gate=True, human_gate_info={'owner_agent_id': 'asker-id'})
        for kind in ('relates_to', 'is_child_of', 'blocks', 'depends_on'):
            with self.subTest(kind=kind):
                self.store.mark('lane', self.a.task_id, MARK, '')
                mesh = Reads([self.b, self.c], {self.b['id']: [
                    {'dependency_type': kind, 'depends_on_task_id': self.c['id']}]})
                result = self.resolver(mesh).resolve(self.a, Budget())
                self.assertNotIn('UNKNOWN', result)
                blocking = kind in ('blocks', 'depends_on')
                self.assertEqual('human_gate=true' in result, blocking)
                self.assertEqual(len(mesh.calls), 4 if blocking else 2)

    def test_pagination_metadata_must_prove_complete_or_be_absent(self):
        # Current Mesh full-list endpoint has no pagination fields. If any
        # appear, validate their types as well as their completeness claims.
        cases = [({}, True), ({'total': 0}, True), ({'has_more': False}, True),
                 ({'next_page': None}, True)]
        cases += [({'total': value}, False) for value in
                  (True, False, -1, 1, 0.0, '0', '2', None, [], {})]
        cases += [({'has_more': value}, False) for value in (True, 0, 1, 'false', None)]
        cases += [({'next_page': value}, False) for value in (False, True, 0, 1, '', '2')]
        for metadata, complete in cases:
            with self.subTest(metadata=metadata):
                self.store.mark('lane', self.a.task_id, MARK, '')
                mesh = Reads([self.b])
                mesh.bounded_get = lambda path, timeout: (
                    dict(outgoing=[], **metadata) if path.endswith('/dependencies') else self.b)
                result = self.resolver(mesh).resolve(self.a, Budget())
                self.assertEqual('UNKNOWN' not in result, complete)

    def test_total_must_equal_all_outgoing_rows_including_nonblocking_edges(self):
        for total in (0, 1, 2):
            with self.subTest(total=total):
                self.store.mark('lane', self.a.task_id, MARK, '')
                mesh = Reads([self.b])
                mesh.bounded_get = lambda path, timeout: (
                    {'outgoing': [{'dependency_type': 'relates_to'}], 'total': total}
                    if path.endswith('/dependencies') else self.b)
                result = self.resolver(mesh).resolve(self.a, Budget())
                self.assertEqual('UNKNOWN' not in result, total == 1)

    def test_real_store_reopen_and_clock_rollback_refresh(self):
        mesh = Reads([self.b])
        observed = dt.datetime(2026,10,20,13,tzinfo=dt.timezone.utc)
        with mock.patch('mesh_runner.blocking_path.now',return_value=observed):
            first = self.resolver(mesh).resolve(self.a,Budget())
        reopened = Store(Path(self.temp.name)/'state.db')
        try:
            with mock.patch('mesh_runner.blocking_path.now',return_value=observed+dt.timedelta(seconds=10)):
                self.assertEqual(BlockingPath(mesh,reopened,'lane').resolve(self.a,Budget()),first)
            self.assertEqual(len(mesh.calls),2)
            with mock.patch('mesh_runner.blocking_path.now',return_value=observed-dt.timedelta(seconds=10)):
                BlockingPath(mesh,reopened,'lane').resolve(self.a,Budget())
            self.assertEqual(len(mesh.calls),4)
        finally:
            reopened.db.close()

    def test_default_shared_refresh_limit_and_node_cut(self):
        nodes = [node(n) for n in range(2,11)]
        mesh = Reads(nodes)
        budget = Budget()
        for t in nodes:
            self.a.wait['target'] = t['id']
            self.resolver(mesh).resolve(self.a,budget)
        self.assertEqual(len(mesh.calls),12)
        self.assertEqual(budget.gets,12)
        self.store.mark('lane',self.a.task_id,MARK,'')
        self.a.wait['target'] = self.b['id'][:8]
        leaves = [node(n) for n in range(3,12)]
        for t in leaves:
            t['status_category']='done'
        mesh = Reads([self.b]+leaves,{self.b['id']:[edge(t) for t in leaves]})
        budget = Budget()
        self.assertIn('node cut',self.resolver(mesh).resolve(self.a,budget))
        self.assertEqual(budget.nodes,8)
        self.assertLessEqual(len(mesh.calls),12)

    def test_only_card_waits_get_diagnostics_no_normal_get_fallback(self):
        mesh = Reads([self.b])
        fx = Effects(mesh,None,'lane',store=self.store)
        for state,kind in (('running','card'),('waiting','time'),('ready','card')):
            self.store.update(self.a,state=state,wait={'kind':kind,'target':self.b['id'][:8]})
            fx.publish([self.a])
        self.assertEqual(mesh.calls,[])
        del Reads.bounded_get
        try:
            self.store.update(self.a,state='waiting',wait={'kind':'card','target':self.b['id'][:8]})
            fx.publish([self.a])
            self.assertIn('UNKNOWN',mesh.runtime[self.a.task_id]['reason'])
        finally:
            Reads.bounded_get = type(self).saved_bounded_get

    saved_bounded_get = staticmethod(Reads.bounded_get)


class BoundedTransport(unittest.TestCase):
    def test_429_and_transport_errors_not_retried_and_timeout_is_residual(self):
        import urllib.error
        for error in (TimeoutError('synthetic'),urllib.error.HTTPError('https://x',429,'limit',{},io.BytesIO(b'{}'))):
            opener = mock.Mock(side_effect=error)
            client = MeshClient('https://x','synthetic',opener=opener)
            with self.assertRaises(MeshError):
                client._bounded_get_request('/tasks/id',timeout=.25)
            self.assertEqual(opener.call_count,1)
            self.assertLessEqual(opener.call_args.kwargs['timeout'],.25)

    def test_diagnostic_response_bound_and_normal_request_unchanged(self):
        client = MeshClient('https://x','synthetic',opener=lambda *a,**kw:io.BytesIO(b' '*1048577))
        with self.assertRaises(MeshError):
            client._bounded_get_request('/tasks/id',timeout=.25)
        client = MeshClient('https://x','synthetic',opener=lambda *a,**kw:io.BytesIO(b'{"ok":true}'))
        self.assertEqual(client.request('GET','/normal'),{'ok':True})


class WallDeadline(unittest.TestCase):
    def test_stalled_child_is_reaped_within_residual_budget_and_key_not_argv(self):
        import subprocess
        import sys
        import time
        real_popen = subprocess.Popen
        children = []
        def stalled(args, **kwargs):
            self.assertNotIn('private-test-key', repr(args))
            child = real_popen([sys.executable, '-c', 'import time; time.sleep(10)'], **kwargs)
            children.append(child)
            return child
        with mock.patch('mesh_runner.mesh.subprocess.Popen', side_effect=stalled):
            started = time.monotonic()
            with self.assertRaises(MeshError):
                MeshClient('https://x','private-test-key').bounded_get('/tasks/id',timeout=.4)
            self.assertLess(time.monotonic()-started,.6)
        self.assertEqual(len(children),1)
        self.assertIsNotNone(children[0].poll())

    def test_429_child_has_no_retry_and_payload_not_logged(self):
        import subprocess
        import sys
        real_popen = subprocess.Popen
        children = []
        def limited(args, **kwargs):
            child = real_popen([sys.executable, '-c',
                'import sys; sys.stdin.buffer.read(); print(' + repr(json.dumps({'status':429,'body':None})) + ')'], **kwargs)
            children.append(child)
            return child
        with mock.patch('mesh_runner.mesh.subprocess.Popen',side_effect=limited):
            with self.assertRaises(MeshError) as error:
                MeshClient('https://x','private-test-key').bounded_get('/tasks/id',timeout=1)
        self.assertEqual(error.exception.status,429)
        self.assertNotIn('private-test-key',str(error.exception))
        self.assertEqual(len(children),1)
        self.assertIsNotNone(children[0].poll())

    def test_real_worker_invalid_endpoint_returns_unknown_without_network(self):
        with self.assertRaises(MeshError) as error:
            MeshClient('https://unused.invalid','private-test-key').bounded_get('/not-permitted',timeout=1)
        self.assertEqual(error.exception.status,0)
        self.assertNotIn('private-test-key',str(error.exception))

    def test_real_worker_get_uses_identity_without_retries_or_error_body_leak(self):
        import http.server
        import threading
        seen = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path,self.headers.get('X-Agent-Key'),self.headers.get('Authorization')))
                self.send_response(429 if len(seen) == 2 else 200)
                self.end_headers()
                self.wfile.write(b'{"message":"private-response-marker"}' if len(seen) == 2 else b'{"ok":true}')
            def log_message(self,*args):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            client = MeshClient('http://127.0.0.1:'+str(server.server_port),'private-test-key',basic_auth='synthetic:basic')
            path = '/tasks/00000001-0000-4000-8000-000000000001'
            self.assertEqual(client.bounded_get(path,timeout=2),{'ok':True})
            with self.assertRaises(MeshError) as error:
                client.bounded_get(path,timeout=2)
            self.assertEqual(error.exception.status,429)
            self.assertNotIn('private-response-marker',str(error.exception))
            self.assertEqual(len(seen),2)
            self.assertEqual(seen[0][0],'/api/v1'+path)
            self.assertEqual(seen[0][1],'private-test-key')
            self.assertTrue(seen[0][2].startswith('Basic '))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_cleanup_escalates_and_reaps_if_terminate_does_not_stop_child(self):
        import subprocess
        child = mock.Mock()
        child.stdin, child.stdout = io.BytesIO(), io.BytesIO()
        child.poll.return_value = None
        child.communicate.side_effect = subprocess.TimeoutExpired('worker',.2)
        child.wait.side_effect = [subprocess.TimeoutExpired('worker',.05),0]
        with mock.patch('mesh_runner.mesh.subprocess.Popen',return_value=child):
            with self.assertRaises(MeshError):
                MeshClient('https://x','private-test-key').bounded_get('/tasks/id',timeout=.3)
        child.terminate.assert_called_once_with()
        child.kill.assert_called_once_with()
        self.assertEqual(child.wait.call_args_list,[mock.call(timeout=.05),mock.call()])
        self.assertTrue(child.stdin.closed)
        self.assertTrue(child.stdout.closed)
