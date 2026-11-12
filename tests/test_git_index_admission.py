"""Narrow reviewed Git-index admission: request -> owning tick -> durable receipt -> turn instructions.

Real consumer, store and spool; only Mesh (an external boundary) is the in-memory fake. The target
"live index" is a throw-away fixture repository and is asserted untouched.
"""
import hashlib
import json
import os
import subprocess
import time
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mesh_runner import git_index_admission as gi
from mesh_runner import workspace_requests as requests
from mesh_runner.config import GitIndexAdmissionConfig
from mesh_runner.store import iso
from fakes import FakeMesh, task
from test_runner import Bench

ACTOR = '00000000-0000-4000-8000-0000000000a1'
SOURCE = '00000000-0000-4000-8000-0000000000b2'
PACKAGE = 'abababababababababababababababababababababababababababababababab'


class Mesh(FakeMesh):
    def me(self):
        return {**super().me(), 'id': ACTOR}


class GitIndexAdmission(Bench):
    def setUp(self):
        super().setUp()
        self.live = self.root / 'live-checkout'
        self.live.mkdir()
        for args in (['init', '-q'], ['config', 'user.email', 'a@b'], ['config', 'user.name', 'a']):
            subprocess.run(['git', '-C', str(self.live), *args], check=True)
        (self.live / 'scripts').mkdir()
        for p in gi.ALLOWED_PATHS:
            (self.live / p).write_text('x\n')
        subprocess.run(['git', '-C', str(self.live), 'add', '-A'], check=True)
        subprocess.run(['git', '-C', str(self.live), 'commit', '-qm', 'i'], check=True)
        for p in gi.ALLOWED_PATHS:
            subprocess.run(['git', '-C', str(self.live), 'update-index', '--skip-worktree', p], check=True)
        self.index_before = self.index_state()
        now = int(time.time())
        self.policy = {'version': 1, 'revision': 'rev-1', 'issuer': 'release:reviewed-policy',
                       'review_ref': 'verdict-fixture', 'operation': gi.OPERATION, 'actor': ACTOR,
                       'source_task': SOURCE, 'package_artifact': str(uuid.uuid4()), 'package_sha256': PACKAGE,
                       'paths': sorted(gi.ALLOWED_PATHS), 'target_root': str(self.live),
                       'not_before': now - 60, 'expires': now + 7200, 'grant_seconds': 600}
        self.revocations = {'version': 1, 'revoked': []}
        self.write_policy()
        self.write_revocations()

    def index_state(self):
        return subprocess.run(['git', '-C', str(self.live), 'ls-files', '-v'], capture_output=True, text=True,
                              check=True).stdout + (self.live / '.git' / 'index').read_bytes().hex()

    def write_policy(self, **changes):
        path = self.root / 'policy.json'
        path.write_text(json.dumps({**self.policy, **changes}))
        path.chmod(0o600)
        self.pin = hashlib.sha256(path.read_bytes()).hexdigest()
        self.cfg.git_index_admission = GitIndexAdmissionConfig(path, self.pin, self.root / 'revocations.json')
        return path

    def write_revocations(self, **changes):
        path = self.root / 'revocations.json'
        path.write_text(json.dumps({**self.revocations, **changes}))
        path.chmod(0o600)

    def parked(self, other_task=False):
        # The positive fixture is the policy's source task; a second task of the same actor is the negative.
        t = task(1, cat='in_progress', assignee_id=ACTOR, id=SOURCE) if not other_task else \
            task(1, cat='in_progress', assignee_id=ACTOR)
        r = self.runner(Mesh([t], max_concurrent=1))
        r.refresh_me()
        a = r.store.create('builder', t['id'], 'claude')
        p = r.pool.lease(a.id, 'app')
        r.store.update(a, state='waiting', workspaces=[str(p)], session_id=str(uuid.uuid4()),
                       wait={'kind': 'time', 'started': iso(), 'until': '2099-01-01T00:00:00Z'})
        return r, r.store.get(a.id)

    def ask(self, r, a, tamper=None, **changes):
        values = dict(state=self.cfg.state_dir, cfg=self.cfg.git_index_admission, agent=a.agent, task=a.task_id,
                      attempt=a.id, token=requests.generation(a), request_id=str(uuid.uuid4()), ttl=300)
        values.update(changes)
        sub = gi.submit(**values)
        path = requests.directory(self.cfg.state_dir, a.agent, gi.SPOOL) / (sub['request']['request_id'] + '.request.json')
        if tamper:
            data = json.loads(path.read_text())
            data.update(tamper)
            path.write_text(json.dumps(data))
        self.assertIsNone(sub['receipt'])                   # submit alone never admits anything
        r.tick()
        rec = path.with_name(path.name.replace('.request.json', '.receipt.json'))
        return sub['request']['request_id'], json.loads(rec.read_text()) if rec.exists() else None

    def refused(self, why, r=None, a=None, **kw):
        if r is None:
            r, a = getattr(self, '_p', None) or self.parked()
            self._p = (r, a)
        _, rec = self.ask(r, a, **kw)
        self.assertEqual(rec['status'], why, rec)
        self.assertNotIn('expires', rec)
        self.assertIsNone(gi.load_grant(r.store.mark(a.agent, a.task_id, gi.GRANT_KEY)))
        self.assertEqual(self.index_state(), self.index_before)
        return r, a

    # AC1: finite CLI/request path -> owning tick -> durable scoped receipt -> current attempt text
    def test_request_tick_receipt_instructions(self):
        r, a = self.parked()
        rid, rec = self.ask(r, a)
        self.assertEqual(rec['status'], 'granted')
        self.assertEqual((rec['actor'], rec['task'], rec['attempt'], rec['package_sha256'], rec['paths'],
                          rec['revision'], rec['issuer']),
                         (ACTOR, a.task_id, a.id, PACKAGE, sorted(gi.ALLOWED_PATHS), 'rev-1',
                          'release:reviewed-policy'))
        self.assertEqual(rec['expires'] - rec['issued'], 600)
        self.assertFalse(rec['revoked'])
        text = r._git_index_notice(r.store.get(a.id))
        self.assertIn(rid, text)
        self.assertIn('scripts/macmini-disk-cleanup.sh', text)
        self.assertIn('does NOT permit editing source in the shared fleet checkout', text)
        self.assertEqual(self.index_state(), self.index_before)      # the consumer never touches a live index

    def test_cli_command_submits_only(self):
        from mesh_runner import cli
        r, a = self.parked()
        rid = str(uuid.uuid4())
        with patch.object(cli.config_mod, 'load', return_value=self.cfg), \
                patch('sys.stdout') as out:
            code = cli.main(['admit-git-index', '--config', 'x', '--agent', a.agent, '--task', a.task_id,
                             '--attempt', a.id, '--wait-generation', requests.generation(a), '--request-id', rid])
        self.assertEqual(code, 0)
        written = ''.join(c.args[0] for c in out.write.call_args_list)
        self.assertIn(rid, written)
        root = requests.directory(self.cfg.state_dir, a.agent, gi.SPOOL)
        self.assertFalse((root / (rid + '.receipt.json')).exists())
        self.assertIsNone(r.store.mark(a.agent, a.task_id, gi.GRANT_KEY))

    # AC2: refusals through the real consumer
    def test_refuses_foreign_actor(self):
        self.write_policy(actor=str(uuid.uuid4()))
        self.refused('rejected_actor')

    def test_refuses_foreign_agent_in_request(self):
        self.refused('rejected_schema', tamper={'agent': 'someone-else'})

    def test_refuses_actors_other_task_with_valid_attempt(self):
        # same actor, a real task it owns, a valid attempt and generation, but not the policy's source task
        r, a = self.parked(other_task=True)
        self.assertNotEqual(a.task_id, SOURCE)
        self.refused('rejected_task', r, a)
        self.assertEqual(r._git_index_notice(r.store.get(a.id)), '')
        # a hand-written request that also claims the policy's source_task is refused the same way
        self.refused('rejected_task', r, a, tamper={'source_task': SOURCE})

    def test_refuses_other_task_attempt_generation(self):
        r, a = self.parked()
        self.refused('rejected_identity_or_state', r, a, task=str(uuid.uuid4()))
        self.refused('rejected_identity_or_state', r, a, attempt=str(uuid.uuid4()))
        self.refused('rejected_wait_generation', r, a, token='0' * 64)

    def test_refuses_unrecognised_or_changed_policy(self):
        r, a = self.parked()
        path = self.root / 'policy.json'
        path.write_text(json.dumps({**self.policy, 'revision': 'forged'}))      # not the pinned digest
        values = dict(state=self.cfg.state_dir, cfg=self.cfg.git_index_admission, agent=a.agent, task=a.task_id,
                      attempt=a.id, token=requests.generation(a), request_id=str(uuid.uuid4()), ttl=300)
        with self.assertRaises(gi.Unverifiable):
            gi.submit(**values)                                                  # the CLI side refuses too
        # a pre-published request still reaches the consumer, which refuses it on the same ground
        self.write_policy()
        sub = gi.submit(**values)
        path.write_text(json.dumps({**self.policy, 'revision': 'forged'}))
        r.tick()
        rec = json.loads((requests.directory(self.cfg.state_dir, a.agent, gi.SPOOL)
                          / (values['request_id'] + '.receipt.json')).read_text())
        self.assertEqual((sub['receipt'], rec['status']), (None, 'rejected_unverifiable'))

    def test_refuses_unknown_policy_pin_in_request(self):
        self.refused('rejected_policy_pin', tamper={'policy': 'a' * 64})

    def test_refuses_changed_package_paths_operation(self):
        self.refused('rejected_package', tamper={'package_sha256': 'b' * 64})
        self.refused('rejected_paths', tamper={'paths': ['scripts/macmini-disk-cleanup.sh']})
        self.refused('rejected_paths', tamper={'paths': ['scripts/*']})
        self.refused('rejected_operation', tamper={'operation': 'git-index-anything'})
        self.refused('rejected_source_task', tamper={'source_task': str(uuid.uuid4())})

    def test_policy_with_wildcard_or_wrong_paths_is_unverifiable(self):
        r, a = self.parked()
        self.write_policy(paths=['scripts/*'])
        with self.assertRaises(gi.Unverifiable):
            self.ask(r, a)

    def test_revocation_list_unknown_or_revoked_fails_closed(self):
        (self.root / 'revocations.json').unlink()
        self.refused('rejected_unverifiable')
        self.write_revocations(revoked=['rev-1'])
        self.refused('rejected_revoked')
        self.write_revocations(revoked=[])
        (self.root / 'revocations.json').chmod(0o666)                  # unsafe file = UNKNOWN
        self.refused('rejected_unverifiable')

    def test_expired_and_not_yet_valid_policy(self):
        now = int(time.time())
        self.write_policy(expires=now - 1, not_before=now - 100)
        self.refused('rejected_policy_expired')
        self.write_policy(not_before=now + 600, expires=now + 900)
        self.refused('rejected_not_yet_valid')

    def test_expired_request_refused(self):
        r, a = self.parked()
        values = dict(state=self.cfg.state_dir, cfg=self.cfg.git_index_admission, agent=a.agent, task=a.task_id,
                      attempt=a.id, token=requests.generation(a), request_id=str(uuid.uuid4()), ttl=5)
        sub = gi.submit(**values)
        path = requests.directory(self.cfg.state_dir, a.agent, gi.SPOOL) / (values['request_id'] + '.request.json')
        data = json.loads(path.read_text())
        data.update(created=data['created'] - 100, expires=data['expires'] - 100)
        path.write_text(json.dumps(data))
        r.tick()
        rec = json.loads(path.with_name(values['request_id'] + '.receipt.json').read_text())
        self.assertEqual(rec['status'], 'expired')

    def test_refuses_lost_ownership(self):
        r, a = self.parked()
        r.mesh.tasks[a.task_id]['assignee_id'] = 'agent-lead'
        sub = gi.submit(self.cfg.state_dir, self.cfg.git_index_admission, a.agent, a.task_id, a.id,
                        requests.generation(a), str(uuid.uuid4()), 300)
        self.assertEqual(r._git_index_request(sub['request'])['status'], 'rejected_ownership')
        self.assertIsNone(r.store.mark(a.agent, a.task_id, gi.GRANT_KEY))

    def test_refuses_unknown_ownership(self):
        r, a = self.parked()
        r.mesh.broken_tasks.add(a.task_id)
        sub = gi.submit(self.cfg.state_dir, self.cfg.git_index_admission, a.agent, a.task_id, a.id,
                        requests.generation(a), str(uuid.uuid4()), 300)
        self.assertEqual(r._git_index_request(sub['request'])['status'], 'rejected_ownership_unknown')

    def test_refuses_conflicting_attempt_and_foreign_lease_and_maintenance(self):
        r, a = self.parked()
        other = r.store.create('other-lane', a.task_id, 'claude')
        self.refused('rejected_conflicting_attempt', r, a)
        r.store.update(other, state='failed')
        row = next(x for x in r.pool.rows() if x['attempt'] == a.id)
        r.store.db.execute('UPDATE slots SET path=? WHERE path=?', (str(self.live / 'sub'), row['path']))
        self.refused('rejected_foreign_lease', r, a)
        r.store.db.execute('UPDATE slots SET path=? WHERE path=?', (row['path'], str(self.live / 'sub')))
        r.store.db.execute(
            "INSERT INTO maintenance_windows(window,agent,task,attempt,generation,pool,slot,lease,state,opened_at) "
            "VALUES(?,?,?,?,?,?,?,?, 'held', 1)", (str(uuid.uuid4()), a.agent, a.task_id, a.id, '0' * 64, 'p',
                                                     'path-x', '1' * 64))
        self.refused('rejected_maintenance_hold', r, a)

    def test_foreign_lease_check_resolves_dotdot_and_symlink_aliases(self):
        r, a = self.parked()
        row = next(x for x in r.pool.rows() if x['attempt'] == a.id)
        link = self.root / 'alias-of-live'
        link.symlink_to(self.live)
        for alias in (str(self.live / 'zz' / '..' / 'sub'), str(link / 'sub'), str(link)):
            with self.subTest(alias=alias):
                r.store.db.execute('UPDATE slots SET path=? WHERE attempt=?', (alias, a.id))
                self.refused('rejected_foreign_lease', r, a)
        r.store.db.execute('UPDATE slots SET path=? WHERE attempt=?', (row['path'], a.id))

    def test_unknown_during_a_new_request_ends_a_live_grant_for_good(self):
        r, a = self.parked()
        self.ask(r, a)
        (self.root / 'revocations.json').unlink()
        _, rec = self.ask(r, a)                                   # a second request while the files are unreadable
        self.assertEqual(rec['status'], 'rejected_unverifiable')
        self.write_revocations()                                  # readable again before any prompt is built
        self.assertIn('has ENDED', r._git_index_notice(r.store.get(a.id)))

    def test_not_configured_is_refused(self):
        r, a = self.parked()
        self.write_policy()
        sub = gi.submit(self.cfg.state_dir, self.cfg.git_index_admission, a.agent, a.task_id, a.id,
                        requests.generation(a), str(uuid.uuid4()), 300)
        self.cfg.git_index_admission = None
        r.tick()
        rec = json.loads((requests.directory(self.cfg.state_dir, a.agent, gi.SPOOL)
                          / (sub['request']['request_id'] + '.receipt.json')).read_text())
        self.assertEqual(rec['status'], 'rejected_unverifiable')

    # AC3: idempotency, restart, revoke, attempt change
    def test_repeat_is_idempotent_and_never_extends_ttl(self):
        r, a = self.parked()
        _, first = self.ask(r, a)
        with patch('time.time', return_value=time.time() + 100):
            _, second = self.ask(r, a)
        self.assertEqual(second['status'], 'already_granted')
        self.assertEqual((second['issued'], second['expires']), (first['issued'], first['expires']))
        # the same request id replayed (crash before the receipt file): same grant, same expiry
        saved = gi.load_grant(r.store.mark(a.agent, a.task_id, gi.GRANT_KEY))
        self.assertEqual((saved['issued'], saved['expires']), (first['issued'], first['expires']))

    def test_replay_of_same_request_after_crash_keeps_expiry(self):
        r, a = self.parked()
        rid, first = self.ask(r, a)
        receipt = requests.directory(self.cfg.state_dir, a.agent, gi.SPOOL) / (rid + '.receipt.json')
        receipt.unlink()                                         # crash after the mark, before the receipt
        with patch('time.time', return_value=time.time() + 100):
            r.tick()
        again = json.loads(receipt.read_text())
        self.assertEqual((again['status'], again['issued'], again['expires']), ('granted', first['issued'], first['expires']))

    def test_crash_between_request_id_and_grant_never_leaves_a_renewable_id(self):
        r, a = self.parked()
        real = r.store.mark
        def crash(agent, task, key, value=None):
            if key == gi.GRANT_KEY and value is not None:
                raise OSError('crash after the id row, before the grant mark')
            return real(agent, task, key, value)
        values = dict(state=self.cfg.state_dir, cfg=self.cfg.git_index_admission, agent=a.agent, task=a.task_id,
                      attempt=a.id, token=requests.generation(a), request_id=str(uuid.uuid4()), ttl=900)
        rid = gi.submit(**values)['request']['request_id']
        with patch.object(r.store, 'mark', side_effect=crash):
            try:
                r.tick()
            except OSError:
                pass                                    # the tick may surface or contain the crash
        self.assertEqual(r.store.db.execute('SELECT COUNT(*) FROM git_index_requests WHERE request_id=?',
                                            (rid,)).fetchone()[0], 1)
        receipt = requests.directory(self.cfg.state_dir, a.agent, gi.SPOOL) / (rid + '.receipt.json')
        receipt.unlink(missing_ok=True)
        _, again = self.ask(r, a, request_id=rid, ttl=900)      # the same id is submitted again
        self.assertEqual(again['status'], 'expired')
        self.assertIsNone(r.store.mark(a.agent, a.task_id, gi.GRANT_KEY))

    def test_id_answered_already_granted_is_used_and_cannot_issue_later(self):
        self.write_policy(grant_seconds=30)
        r, a = self.parked()
        _, first = self.ask(r, a)
        rid, second = self.ask(r, a, ttl=900)                    # arrives during the live grant
        self.assertEqual(second['status'], 'already_granted')
        receipt = requests.directory(self.cfg.state_dir, a.agent, gi.SPOOL) / (rid + '.receipt.json')
        receipt.unlink()
        _, again = self.ask(r, a, request_id=rid, ttl=900)
        self.assertEqual(again['status'], 'already_granted')
        receipt.unlink()
        with patch('time.time', return_value=first['expires'] + 5):    # grant over, the request still unexpired
            _, late = self.ask(r, a, request_id=rid, ttl=900)
        self.assertEqual(late['status'], 'expired')

    def test_replay_after_expiry_is_not_granted(self):
        r, a = self.parked()
        rid, first = self.ask(r, a)
        receipt = requests.directory(self.cfg.state_dir, a.agent, gi.SPOOL) / (rid + '.receipt.json')
        receipt.unlink()                                         # crash before the receipt, then the grant expires
        with patch('time.time', return_value=first['expires'] + 5):
            r.tick()
        again = json.loads(receipt.read_text())
        self.assertEqual(again['status'], 'expired')
        self.assertNotIn('expires', again)

    def test_replay_of_expired_request_with_live_grant_is_refused_and_ends_it(self):
        r, a = self.parked()
        rid, first = self.ask(r, a, ttl=5)                       # request expires long before the 600 s grant
        receipt = requests.directory(self.cfg.state_dir, a.agent, gi.SPOOL) / (rid + '.receipt.json')
        receipt.unlink()                                         # crash before the receipt
        with patch('time.time', return_value=first['issued'] + 30):
            r.tick()
        again = json.loads(receipt.read_text())
        self.assertEqual(again['status'], 'expired')
        self.assertNotIn('expires', again)
        self.assertEqual(gi.load_grant(r.store.mark(a.agent, a.task_id, gi.GRANT_KEY))['state'], 'ended')
        self.assertIn('has ENDED', r._git_index_notice(r.store.get(a.id)))

    def test_replay_rechecks_current_state_and_ends_the_grant(self):
        for change in ('ownership', 'conflict', 'lease'):
            with self.subTest(change):
                self.tearDown(); self.setUp()
                r, a = self.parked()
                rid, first = self.ask(r, a)
                self.assertEqual(first['status'], 'granted')
                if change == 'ownership':
                    r.mesh.tasks[a.task_id]['assignee_id'] = 'agent-lead'
                elif change == 'conflict':
                    r.store.create('other-lane', a.task_id, 'claude')
                else:
                    row = next(x for x in r.pool.rows() if x['attempt'] == a.id)
                    r.store.db.execute('UPDATE slots SET path=? WHERE path=?', (str(self.live / 'sub'), row['path']))
                receipt = requests.directory(self.cfg.state_dir, a.agent, gi.SPOOL) / (rid + '.receipt.json')
                receipt.unlink()                                   # same request replayed (crash before receipt)
                sub = gi.submit(self.cfg.state_dir, self.cfg.git_index_admission, a.agent, a.task_id, a.id,
                                requests.generation(a), str(uuid.uuid4()), 300)      # and a second request
                for request in (first_request(self, receipt, rid), sub['request']):
                    rec = r._git_index_request(request)
                    self.assertTrue(rec['status'].startswith('rejected_'), (change, rec))
                    self.assertNotIn('expires', rec)
                self.assertEqual(gi.load_grant(r.store.mark(a.agent, a.task_id, gi.GRANT_KEY))['state'], 'ended')

    def test_delivery_rechecks_conflict_and_lease(self):
        for change in ('conflict', 'lease'):
            with self.subTest(change):
                self.tearDown(); self.setUp()
                r, a = self.parked()
                self.ask(r, a)
                self.assertIn('is in force', r._git_index_notice(r.store.get(a.id)))
                if change == 'conflict':
                    r.store.create('other-lane', a.task_id, 'claude')
                else:
                    row = next(x for x in r.pool.rows() if x['attempt'] == a.id)
                    r.store.db.execute('UPDATE slots SET path=? WHERE path=?', (str(self.live / 'sub'), row['path']))
                self.assertIn('has ENDED', r._git_index_notice(r.store.get(a.id)))
                self.assertEqual(gi.load_grant(r.store.mark(a.agent, a.task_id, gi.GRANT_KEY))['state'], 'ended')
                self.assertIn('has ENDED', r._git_index_notice(r.store.get(a.id)))

    def test_live_request_replayed_after_its_grant_expired_never_renews(self):
        self.write_policy(grant_seconds=30)                       # shorter than the 300 s request TTL
        r, a = self.parked()
        rid, first = self.ask(r, a, ttl=300)
        receipt = requests.directory(self.cfg.state_dir, a.agent, gi.SPOOL) / (rid + '.receipt.json')
        receipt.unlink()                                         # crash before the receipt
        with patch('time.time', return_value=first['expires'] + 5):
            r.tick()
        again = json.loads(receipt.read_text())
        self.assertEqual(again['status'], 'expired')
        self.assertNotIn('expires', again)
        self.assertEqual(gi.load_grant(r.store.mark(a.agent, a.task_id, gi.GRANT_KEY))['expires'], first['expires'])

    def test_replay_of_request_whose_grant_ended_never_reissues(self):
        r, a = self.parked()
        rid, first = self.ask(r, a, ttl=300)
        self.write_revocations(revoked=['rev-1'])
        self.assertIn('has ENDED', r._git_index_notice(r.store.get(a.id)))   # grant ended, state != granted
        self.write_revocations(revoked=[])
        receipt = requests.directory(self.cfg.state_dir, a.agent, gi.SPOOL) / (rid + '.receipt.json')
        receipt.unlink()
        r.tick()
        self.assertEqual(json.loads(receipt.read_text())['status'], 'expired')
        self.assertIn('has ENDED', r._git_index_notice(r.store.get(a.id)))

    def test_issued_request_ids_are_durable_and_unbounded(self):
        r, a = self.parked()
        rid, first = self.ask(r, a)
        # many later grants would have pushed any bounded history out; the mark no longer names this id at all
        grant = gi.load_grant(r.store.mark(a.agent, a.task_id, gi.GRANT_KEY))
        grant.update(request_id=str(uuid.uuid4()), state='ended', notified=True)
        grant.pop('used', None)
        r.store.mark(a.agent, a.task_id, gi.GRANT_KEY, json.dumps(grant))
        receipt = requests.directory(self.cfg.state_dir, a.agent, gi.SPOOL) / (rid + '.receipt.json')
        receipt.unlink()
        r.tick()
        self.assertEqual(json.loads(receipt.read_text())['status'], 'expired')
        self.assertEqual(r.store.db.execute('SELECT COUNT(*) FROM git_index_requests').fetchone()[0], 1)
        r2 = self.runner(r.mesh)                                   # survives a restart (same sqlite store)
        r2.refresh_me()
        self.assertEqual(r2.store.db.execute('SELECT request_id FROM git_index_requests').fetchone()[0], rid)

    def test_older_request_id_cannot_reissue_after_a_newer_grant(self):
        self.write_policy(grant_seconds=30)
        r, a = self.parked()
        rid1, first = self.ask(r, a, ttl=300)
        with patch('time.time', return_value=first['expires'] + 5):
            rid2, second = self.ask(r, a, ttl=300)
            self.assertEqual(second['status'], 'granted')
        receipt = requests.directory(self.cfg.state_dir, a.agent, gi.SPOOL) / (rid1 + '.receipt.json')
        receipt.unlink()
        with patch('time.time', return_value=second['expires'] + 5):    # the newer grant has expired too
            r.tick()
        self.assertEqual(json.loads(receipt.read_text())['status'], 'expired')
        self.assertEqual(gi.load_grant(r.store.mark(a.agent, a.task_id, gi.GRANT_KEY))['request_id'], rid2)

    def test_new_request_after_expiry_issues_a_fresh_grant(self):
        r, a = self.parked()
        _, first = self.ask(r, a)
        with patch('time.time', return_value=first['expires'] + 5):
            _, second = self.ask(r, a)
        self.assertEqual(second['status'], 'granted')
        self.assertGreater(second['expires'], first['expires'])

    def test_restart_keeps_binding_and_revoke_ends_it_once(self):
        r, a = self.parked()
        rid, _ = self.ask(r, a)
        r2 = self.runner(r.mesh)
        r2.refresh_me()
        self.assertIn(rid, r2._git_index_notice(r2.store.get(a.id)))      # a restarted runner re-proves it
        self.write_revocations(revoked=['rev-1'])
        text = r2._git_index_notice(r2.store.get(a.id))
        self.assertIn('has ENDED', text)
        self.assertIn('do not change any Git index', text)
        self.assertIn('has ENDED', r2._git_index_notice(r2.store.get(a.id)))    # nothing permissive remains
        self.write_revocations(revoked=[])                                # un-revoking does not resurrect it
        self.assertIn('has ENDED', r2._git_index_notice(r2.store.get(a.id)))
        self.assertEqual(self.index_state(), self.index_before)

    def test_expiry_ends_grant_and_new_request_needs_new_admission(self):
        r, a = self.parked()
        self.ask(r, a)
        with patch('time.time', return_value=time.time() + 601):
            self.assertIn('has ENDED', r._git_index_notice(r.store.get(a.id)))

    def test_other_attempt_never_inherits(self):
        r, a = self.parked()
        self.ask(r, a)
        r.store.update(a, state='failed')
        b = r.store.create('builder', a.task_id, 'claude')
        self.assertEqual(r._git_index_notice(r.store.get(b.id)), '')

    def test_unverifiable_state_at_delivery_ends_the_grant_for_good(self):
        r, a = self.parked()
        self.ask(r, a)
        (self.root / 'revocations.json').unlink()
        self.assertIn('has ENDED', r._git_index_notice(r.store.get(a.id)))
        self.write_revocations()                                  # readable again: still needs a new admission
        self.assertIn('has ENDED', r._git_index_notice(r.store.get(a.id)))
        _, rec = self.ask(r, a)                                   # explicit new request issues a fresh grant
        self.assertEqual(rec['status'], 'granted')

    def test_unknown_ownership_at_delivery_ends_the_grant(self):
        r, a = self.parked()
        self.ask(r, a)
        r.mesh.broken_tasks.add(a.task_id)
        self.assertIn('has ENDED', r._git_index_notice(r.store.get(a.id)))
        r.mesh.broken_tasks.discard(a.task_id)
        self.assertIn('has ENDED', r._git_index_notice(r.store.get(a.id)))

    def test_lost_ownership_at_delivery(self):
        r, a = self.parked()
        self.ask(r, a)
        r.mesh.tasks[a.task_id]['assignee_id'] = 'agent-lead'
        self.assertIn('has ENDED', r._git_index_notice(r.store.get(a.id)))

    def test_normal_repo_admission_unchanged(self):
        r, a = self.parked()
        self.assertEqual(r._git_index_notice(r.store.get(a.id)), '')
        self.assertEqual(r._admission_notice(r.store.get(a.id)), '')

    # Every launched prompt carries the proven state (resume, no-session start, persisted retry prompt)
    def launched(self, r, a, **update):
        from fakes import calls
        r.store.update(r.store.get(a.id), state='ready', wait={}, **update)
        before = len(calls(self.calls))
        r.tick()
        self.settle_all(r)
        return [c['prompt'] for c in calls(self.calls)[before:]]

    def test_resume_prompt_carries_notice(self):
        r, a = self.parked()
        rid, _ = self.ask(r, a)
        self.assertTrue(any(rid in p and 'is in force' in p for p in self.launched(r, a)))

    def test_no_session_start_prompt_carries_notice(self):
        r, a = self.parked()
        rid, _ = self.ask(r, a)
        self.assertTrue(any(rid in p and 'is in force' in p for p in self.launched(r, a, session_id='')))

    def test_stale_permissive_text_is_dropped_on_retry(self):
        stale = gi.instructions({'request_id': 'old', 'revision': 'r', 'issuer': 'i', 'expires': 1,
                                 'paths': ['p'], 'target_root': '/x', 'package_sha256': 'p'})
        self.assertNotIn('Scoped admission', gi.strip('before' + stale + 'after'))
        self.assertEqual(gi.strip('before' + stale + 'after'), 'before\nafter')

    def test_ended_grant_is_told_on_launched_prompt(self):
        r, a = self.parked()
        self.ask(r, a)
        self.write_revocations(revoked=['rev-1'])
        prompts = self.launched(r, a)
        self.assertTrue(any('has ENDED' in p and 'is in force' not in p for p in prompts))


def first_request(case, receipt, rid):
    return json.loads(receipt.with_name(rid + '.request.json').read_text())
