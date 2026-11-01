"""Portable Runner regression: durable holds prevent periodic/restart retries."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
import contextlib
import io
import threading
import time
import uuid
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mesh_runner import access_recovery as access
from mesh_runner.config import AgentConfig, RunnerConfig
from mesh_runner.runner import Runner
from mesh_runner.extension_service import ExtensionService, SecretStore, sign
from mesh_runner.config import load


class OwnedRecovery(unittest.TestCase):
    def test_real_cli_identity_probe_signed_consumer_and_notifier(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            actor = ['own-mesh']
            requests = []
            class MeshHandler(BaseHTTPRequestHandler):
                # External authenticated Mesh HTTP boundary; no Runner internals replaced.
                def do_GET(self):
                    requests.append(self.path)
                    self.send_response(503 if actor[0] is None else 200)
                    self.end_headers()
                    self.wfile.write(json.dumps({'agent': {'id': actor[0]}}).encode())
                def log_message(self, *_):
                    pass
            mesh = ThreadingHTTPServer(('127.0.0.1', 0), MeshHandler)
            thread = threading.Thread(target=mesh.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(mesh.server_close)
            self.addCleanup(mesh.shutdown)
            state = root/'state'
            workspace = '11111111-2222-4333-8444-555555555555'
            svc = ExtensionService(state, lambda *_: None, workspace)
            server = svc.serve('127.0.0.1', 0)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            value = dict(version=1, task_id='task', lane='reviewer', mesh_actor='own-mesh',
                         gitlab_actor=70, project='g/p', capability='independent-account-note-label/v1',
                         head='a'*40, route_sha256='b'*64)
            allowed = root/'allowed'
            calls = root/'calls'
            hook = root/'hook.py'
            hook.write_text('import os,json\nfrom pathlib import Path\n'
                'value='+repr(value)+'\n'
                'with Path('+repr(str(calls))+').open("a") as f: f.write(os.environ.get("MESH_RUNNER_AGENT_ID", "")+"\\n")\n'
                'ok=Path('+repr(str(allowed))+').exists() and os.environ.get("MESH_RUNNER_AGENT_ID")==value["mesh_actor"]\n'
                'print(("ACCESS_ALLOW=" if ok else "ACCESS_HOLD=")+json.dumps(value))\n'
                'raise SystemExit(0 if ok else 76)\n')
            key = root/'key'; key.write_text('fixture-key'); key.chmod(0o600)
            config = root/'runner.toml'
            config.write_text('[runner]\nmesh_url='+json.dumps(f'http://127.0.0.1:{mesh.server_port}')+
                '\nstate_dir='+json.dumps(str(state))+'\n[extension]\nenabled=true\nworkspace_id='+
                json.dumps(workspace)+'\ntrust_first_install=true\nport='+str(server.server_port)+'\n[[agents]]\nname="reviewer"\nkey_file='+
                json.dumps(str(key))+'\nharness="claude"\nprofile_dir='+json.dumps(str(root/'profile'))+
                '\nstart_gate_command='+json.dumps([sys.executable,str(hook)])+'\n')
            runner = Runner(load(config), lock=False)
            ar = runner.agents[0]
            ar.refresh_me()  # Existing running-agent lifecycle; producer must do its own GET.
            svc.wake = runner.wake_event
            SecretStore(state).save(dict(signing_secret='fixture-secret-not-real', workspace_id=workspace))
            self.assertTrue(ar._gate('reserve','task','review')[0])
            raw = access.held(runner.store,'reviewer','task')
            def event():
                return dict(event='access.restored', version=1, event_id=str(uuid.uuid4()),
                            workspace_id=workspace, lane='reviewer', task_id='task',
                            hold=json.loads(raw), observed_at=time.time())
            def emit(payload, secret='fixture-secret-not-real', timestamp=None):
                body = json.dumps(payload).encode()
                ts = str(int(time.time()) if timestamp is None else timestamp)
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
                conn.request('POST','/mesh/events',body,
                             {'X-Mesh-Timestamp':ts,'X-Mesh-Signature':sign(secret,ts,body)})
                response = conn.getresponse(); response.read(); conn.close()
            denied_event = event()
            emit(denied_event)  # Correct signature is insufficient while own capability is denied.
            allowed.touch()
            emit(denied_event)  # A denied event cannot be replayed after restoration.
            self.assertEqual(access.held(runner.store,'reviewer','task'), raw)
            for change in ('signature','timestamp','workspace','epoch','actor','task','head'):
                with self.subTest(change=change):
                    payload = event()
                    if change=='workspace': payload['workspace_id']='foreign'
                    if change=='epoch': payload['hold']['epoch']='stale'
                    if change=='actor': payload['hold']['binding']['mesh_actor']='foreign'
                    if change=='head': payload['hold']['binding']['head']='c'*40
                    if change=='task': payload['task_id']='foreign'
                    emit(payload, secret='wrong' if change=='signature' else 'fixture-secret-not-real',
                         timestamp=int(time.time())-301 if change=='timestamp' else None)
                    self.assertEqual(access.held(runner.store,'reviewer','task'), raw)
                    self.assertFalse(runner._wake_gen)
            allowed.unlink()
            args = ['--config',str(config),'--agent','reviewer','--task','task']
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(access.command(args), 1)  # Own denied probe retains epoch.
                allowed.touch()
                self.assertEqual(access.command(args), 0)  # Fresh producer identity + signed consumer.
            self.assertEqual(runner._wake_gen, {'reviewer':1})
            self.assertFalse(access.held(runner.store,'reviewer','task'))
            self.assertGreaterEqual(requests.count('/api/v1/agents/me'), 3)
            self.assertTrue(all(line=='own-mesh' for line in calls.read_text().splitlines()))
            self.assertNotIn('fixture-key', output.getvalue())
            allowed.unlink()
            self.assertTrue(ar._gate('reserve','task','review')[0])
            raw = access.held(runner.store,'reviewer','task')
            allowed.touch()
            for identity in (None, 'foreign-mesh'):
                actor[0] = identity
                before = calls.read_text()
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(access.command(args), 1)
                self.assertEqual(access.held(runner.store,'reviewer','task'), raw)
                self.assertEqual(calls.read_text(), before)
            actor[0] = 'own-mesh'
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(access.notify_command(['--config',str(config),'--gitlab-actor','3','--project','g/p']), 0)
                self.assertTrue(access.held(runner.store,'reviewer','task'))
                self.assertEqual(access.notify_command(['--config',str(config),'--gitlab-actor','70','--project','g/p']), 0)
            self.assertFalse(access.held(runner.store,'reviewer','task'))
            self.assertEqual(runner._wake_gen, {'reviewer':2})
            self.assertFalse(runner.store.by_state('reviewer', ('ready','starting','running')))


class DurableAccess(unittest.TestCase):
    def test_actual_gate_persists_epoch_across_restart_and_task_update(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            value=dict(version=1,task_id='task',lane='reviewer',mesh_actor='own-mesh',gitlab_actor=70,
                       project='g/p',capability='independent-account-note-label/v1',head='a'*40,route_sha256='b'*64)
            hook=root/'gate.py';calls=root/'calls'
            hook.write_text('from pathlib import Path\nPath('+repr(str(calls))+').write_text("called")\n'
                            'print('+repr('ACCESS_HOLD='+json.dumps(value))+')\nraise SystemExit(76)\n')
            agent=AgentConfig('reviewer','fixture-key','claude',root/'profile',[],
                              start_gate_command=[sys.executable,str(hook)])
            cfg=RunnerConfig('https://mesh.invalid',root/'state',[agent])
            runner=Runner(cfg,lock=False)
            self.assertTrue(runner.agents[0]._gate('reserve','task','review')[0])
            raw=access.held(runner.store,'reviewer','task')
            self.assertEqual(json.loads(raw)['binding'],value)
            calls.unlink()
            restarted=Runner(cfg,lock=False)
            restarted.agents[0]._gate_event(dict(id='task',updated_at='changed'))
            for phase in ('reserve','admit'):
                self.assertEqual(restarted.agents[0]._gate(phase,'task','review')[0],access.REASON)
            self.assertFalse(calls.exists())
            self.assertEqual(access.held(restarted.store,'reviewer','task'),raw)
            # Removing the hook cannot silently turn a persisted denial into admission.
            agent.start_gate_command = []
            for phase in ("reserve", "admit"):
                self.assertEqual(restarted.agents[0]._gate(phase,"task","review")[0],access.REASON)
            newer=json.loads(raw);newer['epoch']='new-epoch'
            self.assertTrue(restarted.store.compare_mark('reviewer','task',access.KEY,raw,access.packed(newer)))
            self.assertFalse(runner.store.compare_mark('reviewer','task',access.KEY,raw,''))

    def test_unstructured_legacy_gate_stays_out_of_durable_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            agent=AgentConfig('generic','fixture-key','claude',root/'profile',[],
                              start_gate_command=[sys.executable,'-c','print("ordinary task hold");raise SystemExit(76)'])
            runner=Runner(RunnerConfig('https://mesh.invalid',root/'state',[agent]),lock=False)
            self.assertTrue(runner.agents[0]._gate('reserve','task','work')[0])
            self.assertFalse(access.held(runner.store,'generic','task'))


if __name__=='__main__':
    unittest.main()
