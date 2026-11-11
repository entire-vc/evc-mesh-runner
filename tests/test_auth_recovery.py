"""Real signed ingress/SQLite; only external provider HTTPS is redirected to a fixture."""
import contextlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.request
import uuid

from mesh_runner import auth, auth_recovery
from mesh_runner.cli import main
from mesh_runner.config import load
from mesh_runner.extension_service import ExtensionService, SecretStore, sign
from mesh_runner.runner import Runner


class Recovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.status, self.body, self.calls, self.during = 200, {"data": []}, [], None
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith('/v1/models'):
                    owner.calls.append((self.path, self.headers.get('x-api-key'), self.headers.get('anthropic-version')))
                    if owner.during:
                        owner.during()
                    code, body = owner.status, owner.body
                elif '/agents/me/tasks' in self.path:
                    if getattr(owner, 'on_tasks', None):
                        owner.on_tasks()
                    code, body = 200, {'tasks': [], 'has_more': False}
                else:
                    code, body = 200, {'agent': {'id': 'own-actor'}}
                self.send_response(code)
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())
            def log_message(self, *_):
                pass
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.workspace = '11111111-2222-4333-8444-555555555555'
        self.svc = ExtensionService(root/'state', lambda *_: None, self.workspace)
        self.server = self.svc.serve('127.0.0.1', 0)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        SecretStore(root/'state').save(dict(signing_secret='fixture-signature', workspace_id=self.workspace))
        key = root/'key'; key.write_text('fixture-key'); key.chmod(0o600)
        self.config = root/'runner.toml'
        self.config.write_text('[runner]\nmesh_url='+json.dumps(f'http://127.0.0.1:{self.http.server_port}')+
            '\nstate_dir='+json.dumps(str(root/'state'))+'\n[extension]\nenabled=true\ntrust_first_install=true\nworkspace_id='+
            json.dumps(self.workspace)+'\nport='+str(self.server.server_port)+'\n[[agents]]\nname="lane"\nharness="claude"\nkey_file='+
            json.dumps(str(key))+'\nprofile_dir='+json.dumps(str(root/'profile'))+
            '\n[agents.env]\nANTHROPIC_API_KEY="fixture-api-not-real"\nCLAUDE_CONFIG_DIR='+json.dumps(str(root/'claude'))+'\n')
        self.runner = Runner(load(self.config), lock=False)
        self.ar = self.runner.agents[0]
        self.ar.refresh_me()
        self.addCleanup(self.runner.store.db.close)
        self.addCleanup(self.ar.store.db.close)
        self.svc.wake = self.runner.wake_event
        self.profile = auth.profile_id(auth_recovery.environment(self.ar), self.ar.agent.profile_dir)
        self.hold()
        original = urllib.request.OpenerDirector.open
        def transport(opener, request, *args, **kwargs):
            # TRUE external boundary: provider TLS transport, not probe/consumer logic.
            if isinstance(request, urllib.request.Request) and request.full_url.startswith('https://api.anthropic.com/'):
                request = urllib.request.Request(f'http://127.0.0.1:{self.http.server_port}/v1/models?limit=1',
                                                 headers=dict(request.header_items()))
            return original(opener, request, *args, **kwargs)
        self.transport = patch('urllib.request.OpenerDirector.open', new=transport)
        self.transport.start()
        self.addCleanup(self.transport.stop)

    def hold(self, verification=False):
        auth.hold(self.runner.store, self.profile, verification=verification)

    def event(self):
        generation = self.runner.store.mark(auth.OWNER, auth.PREFIX+self.profile, 'auth_hold_scope')[0]
        return dict(event='auth.recheck', version=1, event_id=str(uuid.uuid4()), workspace_id=self.workspace,
                    lane='lane', mesh_actor='own-actor', profile=self.profile, generation=generation, observed_at=time.time())

    def emit(self, value, secret='fixture-signature'):
        body = json.dumps(value).encode(); stamp = str(int(time.time()))
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=20)
        conn.request('POST', '/mesh/events', body, {'X-Mesh-Timestamp':stamp,
                     'X-Mesh-Signature':sign(secret,stamp,body)})
        response = conn.getresponse(); response.read(); conn.close()
        return response.status

    def test_actual_cli_signed_consumer_single_wake_and_no_secret_output(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = main(['recheck-auth','--config',str(self.config),'--agent','lane','--profile',self.profile])
        self.assertEqual(rc, 0)
        self.assertFalse(auth.held(self.runner.store, self.profile))
        self.assertEqual(self.runner._wake_gen, {'lane': 1})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0], ('/v1/models?limit=1','fixture-api-not-real','2023-06-01'))
        self.assertNotIn('fixture-', output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['model_capability'], 'unknown')

    def test_negative_bindings_do_not_probe_or_wake(self):
        for field, value in [('version', True), ('workspace_id','other'), ('mesh_actor','foreign'),
                             ('profile','a'*32), ('generation','foreign'), ('lane','other'),
                             ('observed_at',time.time()-301), ('observed_at',float('nan'))]:
            with self.subTest(field=field, value=value):
                payload=self.event(); payload[field]=value; self.emit(payload)
                self.assertTrue(auth.held(self.runner.store, self.profile))
        self.assertEqual(self.emit(self.event(),secret='foreign'),401)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.runner._wake_gen,{})

    def test_deny_unknown_and_replay_retain_then_new_event_releases(self):
        for status in (401,403,429,500):
            with self.subTest(status=status):
                self.status=status; payload=self.event(); self.emit(payload)
                self.status=200; n=len(self.calls); self.emit(payload)
                self.assertEqual(len(self.calls),n)
                self.assertTrue(auth.held(self.runner.store,self.profile))
        self.body={'unexpected':'not authenticated proof'}
        self.emit(self.event()); self.assertTrue(auth.held(self.runner.store,self.profile))
        self.body={'data':[]}
        payload=self.event(); self.emit(payload); self.emit(payload)
        self.assertFalse(auth.held(self.runner.store,self.profile))
        self.assertEqual(self.runner._wake_gen,{'lane':1})

    def test_new_generation_during_probe_cannot_be_cleared(self):
        payload=self.event()
        # A real competing SQLite writer records a new failure during network I/O.
        from mesh_runner.store import Store
        def fail_again():
            store=Store(self.runner.cfg.state_dir/'runner.sqlite')
            try:
                auth.hold(store,self.profile)
            finally:
                store.db.close()
        self.during=fail_again
        self.emit(payload)
        self.assertTrue(auth.held(self.runner.store,self.profile))
        self.assertNotEqual(self.event()['generation'],payload['generation'])
        self.assertEqual(self.runner._wake_gen,{})
        self.during=None
        self.emit(payload); self.assertTrue(auth.held(self.runner.store,self.profile))
        self.emit(self.event()); self.assertFalse(auth.held(self.runner.store,self.profile))

    def test_native_custom_mixed_verification_and_legacy_are_unknown(self):
        for change in ({'ANTHROPIC_API_KEY':''}, {'CLAUDE_CODE_OAUTH_TOKEN':'fixture-oauth'},
                       {'ANTHROPIC_BASE_URL':'https://foreign.invalid'}, {'CLAUDE_CODE_USE_BEDROCK':'1'}):
            with self.subTest(change=change):
                saved=dict(self.ar.agent.inline_env)
                self.ar.agent.inline_env.update(change)
                self.emit(self.event())
                self.assertTrue(auth.held(self.runner.store,self.profile))
                self.ar.agent.inline_env=saved
        self.hold(verification=True); self.emit(self.event())
        self.assertTrue(auth.held(self.runner.store,self.profile))
        self.runner.store.mark(auth.OWNER,auth.PREFIX+self.profile,'auth_hold_scope','')
        self.emit(self.event()); self.assertTrue(auth.held(self.runner.store,self.profile))
        self.assertEqual(self.calls,[])

    def test_manual_clear_remains_supported_for_unknown(self):
        self.hold(verification=True)
        self.assertTrue(auth.clear(self.runner.store,self.profile))
        self.assertFalse(auth.clear(self.runner.store,self.profile))

    def test_unsupported_auth_routes_do_not_make_a_provider_request(self):
        for env in ({}, {'CLAUDE_CODE_OAUTH_TOKEN':'fixture'},
                    {'ANTHROPIC_API_KEY':'fixture','ANTHROPIC_BASE_URL':'https://custom.invalid'},
                    {'ANTHROPIC_API_KEY':'fixture','ANTHROPIC_AUTH_TOKEN':'fixture'},
                    {'ANTHROPIC_API_KEY':'fixture','CLAUDE_CODE_USE_VERTEX':'1'}):
            with self.subTest(keys=list(env)):
                self.assertEqual(auth_recovery.probe(env),'unknown')
        self.assertEqual(self.calls,[])

    def test_same_timestamp_new_failure_has_distinct_epoch(self):
        # System time is the external boundary; generations must not be clocks.
        with patch('mesh_runner.auth.iso', return_value='2026-11-01T00:00:00.000+00:00'):
            self.hold(); first=self.event()['generation']
            self.hold(); second=self.event()['generation']
        self.assertNotEqual(first,second)

    def test_two_fresh_signed_events_only_one_can_wake(self):
        barrier=threading.Barrier(2)
        self.during=lambda: barrier.wait(timeout=5)
        outcomes=[]
        threads=[threading.Thread(target=lambda e=e: outcomes.append(self.emit(e)))
                 for e in (self.event(), self.event())]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=15)
        self.assertTrue(all(not t.is_alive() for t in threads))
        self.assertEqual(outcomes,[202,202])
        self.assertFalse(auth.held(self.runner.store,self.profile))
        self.assertEqual(self.runner._wake_gen,{'lane':1})

    def test_routing_changes_during_probe_retain_hold(self):
        self.during=lambda: self.ar.agent.inline_env.update(ANTHROPIC_API_KEY='different-fixture')
        self.emit(self.event())
        self.assertTrue(auth.held(self.runner.store,self.profile))
        self.assertEqual(self.runner._wake_gen,{})

    def test_default_ordinary_task_events_cannot_clear_auth_hold(self):
        self.runner.wake_event('task.updated',{'task_id':'task','restored':True})
        self.assertTrue(auth.held(self.runner.store,self.profile))
        self.assertEqual(self.calls,[])

    def test_cas_retains_durable_wake_before_delivery(self):
        payload = self.event()
        self.assertEqual(auth_recovery.consume(self.runner, payload), ['lane'])
        self.assertFalse(auth.held(self.runner.store, self.profile))
        row = self.runner.store.mark(auth.OWNER, auth.PREFIX+self.profile, 'auth_wake_v1')
        self.assertIsNotNone(row)
        self.assertEqual(json.loads(row[0])['generation'], payload['generation'])
        self.assertEqual(self.runner._wake_gen, {})

    def test_outbox_write_failure_rolls_back_hold_clear(self):
        # Real SQLite failure at the atomic durability boundary, no mocked CAS.
        self.runner.store.db.execute("CREATE TRIGGER reject_auth_wake BEFORE INSERT ON marks "
            "WHEN NEW.key='auth_wake_v1' BEGIN SELECT RAISE(ABORT, 'injected storage failure'); END")
        self.emit(self.event())
        self.assertTrue(auth.held(self.runner.store, self.profile))
        self.assertEqual(self.runner._wake_gen, {})

    def test_restart_drains_pending_once_and_acknowledges_exact_generation(self):
        payload = self.event()
        auth_recovery.consume(self.runner, payload)  # death before RAM scheduling
        other = Runner(load(self.config), lock=False)
        try:
            other.agents[0].refresh_me()
            self.assertEqual(auth_recovery.deliver(other), ['lane'])
            self.assertEqual(auth_recovery.deliver(other), [])
            self.assertEqual(other._wake_gen, {'lane': 1})
            pending = auth_recovery.pending(other, other.agents[0])
            self.assertEqual(len(pending), 1)
            auth_recovery.acknowledge(other, other.agents[0], pending)
            self.assertEqual(auth_recovery.pending(other, other.agents[0]), {})
            self.assertEqual(len(self.calls), 1)  # delivery never probes the provider
        finally:
            other.agents[0].store.db.close()
            other.store.db.close()

    def test_new_failure_invalidates_undelivered_wake(self):
        auth_recovery.consume(self.runner, self.event())
        self.hold()
        self.assertEqual(auth_recovery.deliver(self.runner), [])
        self.assertTrue(auth.held(self.runner.store, self.profile))
        self.assertEqual(self.runner._wake_gen, {})

    def test_old_ack_cannot_delete_a_later_generation(self):
        auth_recovery.consume(self.runner, self.event())
        old = auth_recovery.pending(self.runner, self.ar)
        self.hold()
        auth_recovery.consume(self.runner, self.event())
        auth_recovery.acknowledge(self.runner, self.ar, old)
        self.assertEqual(len(auth_recovery.pending(self.runner, self.ar)), 1)

    def test_notify_auth_change_uses_actual_configured_profile_and_ingress(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = main(['notify-auth-change', '--config', str(self.config), '--agent', 'lane'])
        self.assertEqual(rc, 0)
        self.assertFalse(auth.held(self.runner.store, self.profile))
        self.assertEqual(self.runner._wake_gen, {'lane': 1})
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn('fixture-', output.getvalue())

    def test_actual_agent_loop_acknowledges_restored_obligation_after_tick(self):
        auth_recovery.consume(self.runner, self.event())
        # Restart between CAS and scheduling; the real loop uses only empty
        # remote Mesh fixtures. No product task or harness can be launched.
        other = Runner(load(self.config), lock=False)
        stop = threading.Event()
        try:
            ar = other.agents[0]
            ar.refresh_me()
            other.cfg.mesh_effects = False
            from mesh_runner.interactive import Tmux
            ar._tmux = Tmux('/nonexistent-auth-fixture-tmux')
            self.on_tasks = stop.set  # external Mesh response ends one full tick
            other._agent_loop(ar, stop)
            self.assertNotIn('error', other.snapshot()[0])
            self.assertEqual(other._wake_gen, {'lane': 1})
            self.assertEqual(auth_recovery.pending(other, ar), {})
            self.assertEqual(other.store.by_state('lane', ('starting','running')), [])
            self.assertEqual(len(self.calls), 1)
        finally:
            self.on_tasks = None
            other.agents[0].store.db.close()
            other.store.db.close()

    def test_foreign_event_cannot_drain_an_existing_valid_obligation(self):
        auth_recovery.consume(self.runner, self.event())
        foreign = self.event()
        foreign['mesh_actor'] = 'foreign'
        self.assertEqual(self.runner.wake_event('auth.recheck', foreign), [])
        self.assertEqual(self.runner._wake_gen, {})
        self.assertEqual(len(auth_recovery.pending(self.runner, self.ar)), 1)


if __name__ == '__main__':
    unittest.main()
