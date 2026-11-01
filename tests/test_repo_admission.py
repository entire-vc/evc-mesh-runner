"""Scoped additive repo admission: new repo attached, existing state preserved, fail closed."""
import copy
import json
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mesh_runner import repo_admission as admission
from mesh_runner import workspace_requests as requests
from mesh_runner.workspace import RepoSpec
from test_runner import Bench, make_repo
from test_workspace_requests import WorkspaceRequests as _Fixture


class RepoAdmission(Bench):
    parked = _Fixture.parked

    def setUp(self):
        super().setUp()
        self.extra = RepoSpec("bob", make_repo(self.root / "bob-src"), max_slots=2)
        self.cfg.source = self.root / "runner.toml"

    def fresh(self, **agent_changes):
        cfg = copy.deepcopy(self.cfg)
        cfg.agents[0].repos.append(self.extra)
        for k, v in agent_changes.items():
            setattr(cfg.agents[0], k, v)
        return cfg

    def ask(self, r, a, fresh=None, spec=None, **changes):
        values = dict(state=self.cfg.state_dir, agent=a.agent, task=a.task_id, attempt=a.id,
                      token=requests.generation(a), request_id=str(uuid.uuid4()),
                      repo=spec or self.extra, ttl=300)
        values.update(changes)
        sub = admission.submit(**values)
        with patch('mesh_runner.runner.config_mod.load', return_value=fresh or self.fresh()):
            r.tick()
        path = requests.directory(self.cfg.state_dir, a.agent, admission.SPOOL) / (
            sub['request']['request_id'] + '.receipt.json')
        return sub, json.loads(path.read_text())['status'] if path.exists() else None

    def test_new_repo_attached_existing_preserved(self):
        r, (a,) = self.parked()
        before = list(r.store.get(a.id).workspaces)
        sid = r.store.get(a.id).session_id
        _, status = self.ask(r, a)
        self.assertEqual(status, 'admitted')
        got = r.store.get(a.id)
        self.assertEqual(got.workspaces[:1], before)
        self.assertEqual(len(got.workspaces), 2)
        self.assertEqual((got.state, got.session_id), ('waiting', sid))
        self.assertEqual(sorted(r.pool.leased_to(a.id)), sorted(got.workspaces))
        self.assertIn('bob', r.pool.repos)
        self.assertIn('bob', self.agent_names(r))
        self.assertIn(got.workspaces[1], r._admission_notice(got))

    def agent_names(self, r):
        return [x.name for x in r.agent.repos]

    def test_retry_same_request_and_duplicate_request_are_idempotent(self):
        r, (a,) = self.parked()
        sub, status = self.ask(r, a)
        self.assertEqual(status, 'admitted')
        paths = list(r.store.get(a.id).workspaces)
        _, again = self.ask(r, a)       # new request id, repo already attached
        self.assertEqual(again, 'admitted')       # done journal is per attempt+repo
        self.assertEqual(r.store.get(a.id).workspaces, paths)
        self.assertEqual(len(r.pool.leased_to(a.id)), 2)

    def test_duplicate_request_still_validates_leases(self):
        r, (a,) = self.parked()
        self.assertEqual(self.ask(r, a)[1], 'admitted')
        r.pool._set(Path(self.root / 'stray'), 'app', 'leased', a.id)
        self.assertEqual(self.ask(r, a)[1], 'rejected_foreign_lease')
        self.assertEqual(len(r.store.get(a.id).workspaces), 2)

    def test_crash_between_lease_and_commit_is_adopted_on_retry(self):
        r, (a,) = self.parked()
        real = r.store.update_with_marks
        with patch.object(r.store, 'update_with_marks', side_effect=RuntimeError('crash')):
            _, status = self.ask(r, a)
        self.assertEqual(status, 'failed_preserved')
        self.assertEqual(len(r.store.get(a.id).workspaces), 1)
        self.assertEqual(len(r.pool.leased_to(a.id)), 2)          # lease exists, attempt not yet updated
        # A receipt is terminal for that request; a fresh request adopts the lease via the attempt+repo intent.
        _, status = self.ask(r, a)
        self.assertEqual(status, 'admitted')
        self.assertEqual(len(r.store.get(a.id).workspaces), 2)
        self.assertEqual(len(r.pool.leased_to(a.id)), 2)

    def reject(self, want, **kw):
        r, (a,) = self.parked()
        snapshot = (r.store.get(a.id).workspaces, sorted(r.pool.leased_to(a.id)))
        _, status = self.ask(r, a, **kw)
        self.assertEqual(status, want)
        self.assertEqual((r.store.get(a.id).workspaces, sorted(r.pool.leased_to(a.id))), snapshot)
        self.assertNotIn('bob', r.pool.repos)
        self.assertNotIn('bob', self.agent_names(r))

    def test_unrelated_config_drift_rejected(self):
        self.reject('rejected_config', fresh=self.fresh(profile_dir=Path('/elsewhere')))

    def test_credential_rotation_is_not_drift(self):
        r, (a,) = self.parked()
        fresh = self.fresh()
        fresh.mesh_basic_auth = 'rotated:pw'
        _, status = self.ask(r, a, fresh=fresh)
        self.assertEqual(status, 'admitted')

    def test_agent_env_credential_rotation_ok_but_runtime_env_change_is_drift(self):
        r, (a,) = self.parked()
        self.agent.env['API_TOKEN'] = 'old'
        rotated = self.fresh()
        rotated.agents[0].env = {**self.agent.env, 'API_TOKEN': 'new'}
        self.assertEqual(self.ask(r, a, fresh=rotated)[1], 'admitted')

    def test_runtime_env_change_rejected(self):
        changed = self.fresh()
        changed.agents[0].env = {**self.agent.env, 'PATH': '/other'}
        self.reject('rejected_config', fresh=changed)

    def test_unreviewed_spec_rejected(self):
        self.reject('rejected_spec_mismatch', spec=replace(self.extra, max_slots=3))

    def test_stale_generation_rejected(self):
        self.reject('rejected_wait_generation', token='0' * 64)

    def test_active_writer_foreign_lease_and_stale_state_rejected(self):
        r, (a,) = self.parked()
        row = r.store.get(a.id)
        r.store.update(row, state='running')
        _, status = self.ask(r, a)
        self.assertEqual(status, 'rejected_identity_or_state')
        r.store.update(r.store.get(a.id), state='waiting', pid=1)
        with patch('mesh_runner.runner.group_alive', return_value=True):
            _, status = self.ask(r, a)
        self.assertEqual(status, 'rejected_active_writer')
        r.store.update(r.store.get(a.id), pid=0)
        r.pool._set(Path(self.root / 'stray'), 'app', 'leased', a.id)    # lease not in attempt.workspaces
        _, status = self.ask(r, a)
        self.assertEqual(status, 'rejected_foreign_lease')
        self.assertEqual(len(r.store.get(a.id).workspaces), 1)

    def test_full_pool_rejected_without_registering(self):
        r, (a,) = self.parked()
        self.extra = replace(self.extra, max_slots=0)
        _, status = self.ask(r, a)
        self.assertEqual(status, 'rejected_pool_full')
        self.assertNotIn('bob', r.pool.repos)
