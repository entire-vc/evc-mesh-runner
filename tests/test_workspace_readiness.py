"""Real Store/pool/checkpoint producer boundary; these DB/Git checks run in CI.

FakeMesh substitutes the external HTTP service, never the registry or producer.
"""
import copy
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import time
import tempfile
import unittest
from unittest import mock
import uuid

from mesh_runner import config as config_mod
from mesh_runner import workspace_readiness as ready, workspace_requests
from mesh_runner.store import iso
from mesh_runner.workspace import RepoSpec
from test_runner import Bench
from fakes import FakeMesh, task


class Readiness(Bench):
    def setUp(self):
        super().setUp()
        # workspace_readiness_v1 is no longer a [runner] option (fixed off). The producer still re-reads
        # the config file on every request; each such read gets the switch from `readiness_on` on top of
        # what the shipped loader returned, so turning it off between requests is still observed.
        self.readiness_on = True
        shipped = config_mod.load
        loader = mock.patch.object(config_mod, 'load', side_effect=lambda path: dataclasses.replace(
            shipped(path), workspace_readiness_v1=self.readiness_on))
        loader.start()
        self.addCleanup(loader.stop)

    def parked(self, count=1):
        self.root = Path(tempfile.mkdtemp(dir=self.tmp.name))
        self.cfg.state_dir = self.root / "state"
        base = self.agent.repos[0].base
        self.agent.repos = [RepoSpec('app' if i == 0 else f'app{i}', base, max_slots=4)
                            for i in range(count)]
        self.cfg.workspace_readiness_v1 = self.readiness_on = True
        key = self.root / 'key'
        key.write_text(self.agent.key)
        key.chmod(0o600)
        self.cfg.source = self.root / 'runner.toml'
        self.cfg.source.write_text(
            '[runner]\nmesh_url="http://mesh"\n'
            f'state_dir={json.dumps(str(self.cfg.state_dir))}\n'
            '[[agents]]\nname="builder"\nharness="claude"\n'
            f'key_file={json.dumps(str(key))}\n'
            f'profile_dir={json.dumps(str(self.agent.profile_dir))}\n'
            + ''.join(f'[[agents.repos]]\nname={json.dumps(repo.name)}\n'
                      f'base={json.dumps(str(repo.base))}\n' for repo in self.agent.repos))
        r = self.runner(FakeMesh([task(1, cat='in_progress')]))
        r.refresh_me()
        a = r.store.create('builder', task(1)['id'], 'claude')
        r.mesh.tasks[a.task_id]["checkout_session_id"] = a.id
        paths = [str(r.pool.lease(a.id, repo.name)) for repo in self.agent.repos]
        r.store.update(a, state='waiting', workspaces=paths, session_id=str(uuid.uuid4()),
                       wait={'kind': 'card', 'target': '00000002', 'started': iso()})
        r._suspend_waiting(a, requested=True)
        rows = r.pool._checkpoint_rows(a.id)
        manifest = Path(rows[0]['checkpoint']) / 'manifest.json'
        request = {'version': 1, 'request_id': str(uuid.uuid4()), 'agent': a.agent,
                   'task': a.task_id, 'attempt': a.id, 'session': a.session_id,
                   'generation': workspace_requests.generation(a), 'created': int(time.time()),
                   'expires': int(time.time()) + 60, 'previous': '',
                   'paths': [{'repo': row['repo'], 'source': row['source'],
                              'checkpoint': Path(row['checkpoint']).name,
                              'manifest_sha256': hashlib.sha256((Path(row['checkpoint']) / 'manifest.json').read_bytes()).hexdigest()}
                             for row in rows]}
        return r, a, request, manifest

    def signed(self, request):
        return dict(request, signature=ready.sign(request, self.agent.key))

    def observation(self, r, request):
        ready.submit(self.cfg.state_dir, request, self.agent.key)
        ready.consume(r)
        return ready.receipt(self.cfg.state_dir, request['agent'], request['request_id'])

    def unchanged(self, r):
        return [tuple(row) for row in r.store.db.execute('SELECT * FROM attempts')], [
            tuple(row) for row in r.store.db.execute('SELECT * FROM slots')], [
            tuple(row) for row in r.store.db.execute('SELECT * FROM workspace_checkpoints')]

    def test_native_positive_immutable_delivery_and_revalidation(self):
        r, a, req, manifest = self.parked(count=3)
        before = self.unchanged(r)
        events = r.store.events(a.id)
        result = self.observation(r, req)
        self.assertEqual(result['status'], 'READY', result)
        self.assertEqual(len(result['snapshot']['paths']), 3)
        self.assertEqual(result['snapshot']['session'], a.session_id)
        self.assertEqual(result['snapshot']['paths'][0]['occupancy'], 'free')
        self.assertTrue(result['snapshot']['paths'][0]['continuity'])
        self.assertFalse(result['operational_authority'])
        self.assertEqual(self.unchanged(r), before)
        self.assertEqual(self.observation(r, req), result)
        check = dict(req, request_id=str(uuid.uuid4()), previous=req['request_id'])
        self.assertEqual(self.observation(r, check)['status'], 'READY')
        with self.assertRaises(ready.Refused):
            ready.submit(self.cfg.state_dir, dict(req, expires=req['expires']-1), self.agent.key)
        # No workspace restore, launch, wake or checkout was performed.
        self.assertFalse(r.mesh.checkouts)
        self.assertEqual(r.store.events(a.id), events)
        self.assertEqual(self.unchanged(r), before)

    def test_foreign_conflict_exports_no_foreign_identity_and_changes_nothing(self):
        r, a, req, _ = self.parked()
        foreign = 'private-foreign-attempt-identity'
        r.store.db.execute("UPDATE slots SET state='leased',attempt=? WHERE path=?",
                           (foreign, req['paths'][0]['source']))
        before = self.unchanged(r)
        result = self.observation(r, req)
        self.assertEqual(result['status'], 'NOT_READY')
        self.assertEqual(result['snapshot']['paths'][0]['occupancy'], 'conflict')
        self.assertNotIn(foreign, json.dumps(result))
        self.assertEqual(self.unchanged(r), before)

    def test_missing_slot_or_partial_journal_is_never_ready(self):
        for control in ('slot', 'journal'):
            with self.subTest(control=control):
                r, a, req, _ = self.parked(count=3)
                if control == 'slot':
                    r.store.db.execute('DELETE FROM slots WHERE path=?', (req['paths'][0]['source'],))
                else:
                    r.store.db.execute('DELETE FROM workspace_checkpoints WHERE attempt=? AND repo=?', (a.id, 'app'))
                result = self.observation(r, req)
                self.assertNotEqual(result['status'], 'READY', result)
                if control == 'slot':
                    self.assertEqual(result['snapshot']['paths'][0]['occupancy'], 'unknown')

    def test_checkpoint_pin_and_session_continuity_red_controls(self):
        for control in ('pin', 'session', 'phase', 'inventory'):
            with self.subTest(control=control):
                r, a, req, manifest = self.parked()
                if control == 'pin':
                    req['paths'][0]['manifest_sha256'] = '0' * 64
                elif control == 'phase':
                    r.store.db.execute("UPDATE workspace_checkpoints SET phase='releasing' WHERE attempt=?", (a.id,))
                else:
                    doc = json.loads(manifest.read_bytes())
                    doc['metadata']['session_id' if control == 'session' else 'workspaces'] = (
                        str(uuid.uuid4()) if control == 'session' else ['/other/path'])
                    manifest.write_text(json.dumps(doc))
                    req['paths'][0]['manifest_sha256'] = hashlib.sha256(manifest.read_bytes()).hexdigest()
                result = self.observation(r, req)
                self.assertEqual(result['status'], 'NOT_READY', result)

    def test_authorization_revocation_and_config_drift(self):
        for control in ('signature', 'owner', 'key', 'disabled', 'config', 'checkout', 'checkout_session'):
            with self.subTest(control=control):
                r, a, req, _ = self.parked()
                ready.submit(self.cfg.state_dir, req, 'wrong-key' if control == 'signature' else self.agent.key)
                if control == 'owner':
                    r.mesh.tasks[a.task_id]['assignee_id'] = 'other'
                elif control == 'key':
                    (self.root / 'key').write_text('revoked-key-replacement')
                elif control == 'disabled':
                    self.readiness_on = False
                elif control == 'config':
                    self.cfg.source.write_text(self.cfg.source.read_text() + '\n# drift\n')
                elif control == 'checkout_session':
                    r.mesh.tasks[a.task_id]['checkout_session_id'] = str(uuid.uuid4())
                elif control == 'checkout':
                    r.mesh.tasks[a.task_id]['checked_out_by'] = 'other'
                ready.consume(r)
                result = ready.receipt(self.cfg.state_dir, a.agent, req['request_id'])
                self.assertEqual(result['status'], 'UNKNOWN', result)
                self.assertNotIn('revoked-key-replacement', json.dumps(result))

    def test_restart_durable_receipt_requires_fresh_revalidation(self):
        r, a, req, _ = self.parked()
        old = self.observation(r, req)
        r2 = self.runner(r.mesh)
        r2.refresh_me()
        self.assertEqual(ready.receipt(self.cfg.state_dir, a.agent, req['request_id']), old)
        check = dict(req, request_id=str(uuid.uuid4()), previous=req['request_id'])
        result = self.observation(r2, check)
        self.assertEqual(result['status'], 'STALE', result)
        self.assertNotEqual(result['controller']['instance'], old['controller']['instance'])
        fresh = dict(req, request_id=str(uuid.uuid4()))
        self.assertEqual(self.observation(r2, fresh)['status'], 'READY')

    def test_expiry_and_attempt_generation_rejected(self):
        for control in ('expired', 'generation', 'path'):
            with self.subTest(control=control):
                r, a, req, _ = self.parked()
                if control == 'expired':
                    req['created'] -= 120
                    req['expires'] -= 120
                elif control == 'generation':
                    req['generation'] = '0' * 64
                else:
                    req['paths'][0]['source'] = '/unapproved/path/slot-2'
                self.assertEqual(self.observation(r, req)['status'], 'UNKNOWN')

    def test_native_registry_drift_between_authority_reads_is_unknown(self):
        r, a, req, _ = self.parked()
        native_task = r.mesh.task
        calls = 0

        def changing_service(tid):
            nonlocal calls
            calls += 1
            if calls == 2:
                # A real second registry connection changes the row between captures.
                import sqlite3
                with sqlite3.connect(self.root / 'state' / 'r.sqlite') as db:
                    db.execute("UPDATE slots SET state='held' WHERE path=?", (req['paths'][0]['source'],))
            return native_task(tid)

        r.mesh.task = changing_service  # external HTTP boundary only
        result = self.observation(r, req)
        self.assertEqual(result['status'], 'UNKNOWN', result)
        self.assertEqual(result['reason'], 'observation_drift')

    def test_exact_own_active_journal_is_ready_without_acquiring_anything(self):
        r, a, req, _ = self.parked()
        path = req['paths'][0]['source']
        r.store.db.execute("UPDATE slots SET state='leased',attempt=? WHERE path=?", (a.id, path))
        r.store.db.execute("UPDATE workspace_checkpoints SET phase='active',target=? WHERE attempt=?", (path, a.id))
        before = self.unchanged(r)
        result = self.observation(r, req)
        self.assertEqual(result['status'], 'READY', result)
        self.assertEqual(result['snapshot']['paths'][0]['occupancy'], 'exact-own')
        self.assertEqual(self.unchanged(r), before)

    def test_manifest_symlink_is_unknown_without_following_it(self):
        r, a, req, manifest = self.parked()
        other = self.root / 'private-never-read'
        other.write_text('sensitive sentinel')
        manifest.unlink()
        manifest.symlink_to(other)
        result = self.observation(r, req)
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertNotIn('sensitive sentinel', json.dumps(result))


class Schema(unittest.TestCase):
    """Fast pure controls; safe to run locally without DB/Git fixtures."""
    def request(self):
        return {'version': 1, 'request_id': str(uuid.uuid4()), 'agent': 'builder',
                'task': str(uuid.uuid4()), 'attempt': str(uuid.uuid4()), 'session': str(uuid.uuid4()),
                'generation': 'a' * 64, 'created': 100, 'expires': 160, 'previous': '',
                'paths': [{'repo': 'app', 'source': '/own/app/slot-2', 'checkpoint': 'b' * 32,
                           'manifest_sha256': 'c' * 64}], 'signature': 'd' * 64}

    def test_positive_and_red_schema_controls(self):
        good = self.request()
        ready.validate(good, 'builder')
        for key, value in [('expires', 161), ('version', True), ('paths', []),
                           ('agent', 'foreign'), ('signature', 'g' * 64)]:
            bad = dict(good, **{key: value})
            with self.subTest(key=key), self.assertRaises(ValueError):
                ready.validate(bad, 'builder')
        bad = copy.deepcopy(good)
        bad['paths'][0]['source'] = '/own/../foreign/slot-2'
        with self.assertRaises(ValueError):
            ready.validate(bad, 'builder')
        with self.assertRaises(ValueError):
            ready.validate(dict(good, extra='unapproved'), 'builder')

    def test_signature_binds_every_pin_and_identity(self):
        req = self.request()
        original = ready.sign(req, 'test-key')
        for key in ('task', 'session', 'generation', 'previous', 'expires'):
            bad = dict(req, **{key: 159 if key == 'expires' else str(uuid.uuid4())})
            self.assertNotEqual(ready.sign(bad, 'test-key'), original)
        self.assertNotEqual(ready.sign(req, 'other-key'), original)
