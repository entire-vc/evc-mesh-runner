"""Scoped additive repo admission: new repo attached, existing state preserved, fail closed."""
import copy
import json
import os
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mesh_runner import config as config_mod
from mesh_runner import repo_admission as admission
from mesh_runner import workspace_requests as requests
from mesh_runner.workspace import RepoSpec
from test_runner import Bench, make_repo
from test_workspace_requests import WorkspaceRequests as _Fixture


class AdmissionFixture(Bench):
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


class RepoAdmission(AdmissionFixture):
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

    def reject(self, want, session_env=None, **kw):
        r, (a,) = self.parked()
        if session_env:
            r._apply_env({**os.environ, **session_env})
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
        self.agent.env['API_TOKEN'] = self.agent.declared_env['API_TOKEN'] = 'old'
        rotated = self.fresh()
        rotated.agents[0].env = rotated.agents[0].declared_env = {**self.agent.env, 'API_TOKEN': 'new'}
        self.assertEqual(self.ask(r, a, fresh=rotated)[1], 'admitted')

    def test_inline_credential_rotation_is_not_drift(self):
        r, (a,) = self.parked()
        self.agent.inline_env = {'API_TOKEN': 'old'}
        rotated = self.fresh()
        rotated.agents[0].inline_env = {'API_TOKEN': 'new'}
        self.assertEqual(self.ask(r, a, fresh=rotated)[1], 'admitted')

    def test_inline_path_change_rejected(self):
        changed = self.fresh()
        changed.agents[0].inline_env = {'PATH': '/other'}
        self.reject('rejected_config', fresh=changed)

    def test_runtime_env_change_rejected(self):
        changed = self.fresh()
        changed.agents[0].env = changed.agents[0].declared_env = {**self.agent.env, 'PATH': '/other'}
        self.reject('rejected_config', fresh=changed)

    def test_session_env_applied_by_a_turn_is_not_drift(self):
        # the real parser path: env command output -> _read_env -> _apply_env mutates agent.env
        r, (a,) = self.parked()
        out = f'PATH={os.environ["PATH"]}:/routed/bin\nCLAUDE_CONFIG_DIR=/acct\nHTTPS_PROXY=http://p\nMESH_RUNNER_LANE=x\n'
        done = type('P', (), {'returncode': 0, 'stdout': out, 'stderr': ''})()
        declared = dict(r.agent.env)             # what a reload of the config file declares
        r.agent.env_command = ['env-cmd']
        with patch('mesh_runner.runner.subprocess.run', return_value=done):
            r._fresh_env()
        self.assertTrue(r.agent.env['PATH'].endswith('/routed/bin'))
        before = list(r.store.get(a.id).workspaces)
        fresh = self.fresh(env_command=['env-cmd'])
        fresh.agents[0].env = declared
        _, status = self.ask(r, a, fresh=fresh)
        self.assertEqual(status, 'admitted')
        self.assertEqual(r.store.get(a.id).workspaces[:1], before)

    def test_session_env_still_leaves_declared_drift_rejected(self):
        changed = self.fresh()
        changed.agents[0].env = changed.agents[0].declared_env = {**self.agent.declared_env, 'PATH': '/other'}
        self.reject('rejected_config', fresh=changed, session_env={'PATH': '/routed/bin'})

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


class RealConfigFixture(AdmissionFixture):
    """Runner config comes from a real TOML read by the shipped loader: the first runner, the
    restarted runner and the admission's fresh read all use it. Nothing replaces the loader: every read
    goes through it, and only the two bench timings below are laid on top of its result."""

    def setUp(self):
        super().setUp()
        # wake_poll_seconds and heartbeat_seconds are no longer [runner] options; the bench polls and
        # beats on every tick, so each read of the file (the fixture's and the admission's fresh read
        # alike) gets the same two values on top of what the shipped loader returned
        shipped = config_mod.load
        loader = patch.object(config_mod, 'load', side_effect=lambda path: replace(
            shipped(path), wake_poll_seconds=0, heartbeat_seconds=0))
        loader.start()
        self.addCleanup(loader.stop)
        key = self.root / 'agent.key'
        key.write_text('k')
        key.chmod(0o600)
        self.key = key
        self.toml = self.root / 'runner.toml'
        self.write_toml()
        self.reload()

    def write_toml(self, extra=False, profile_dir=None):
        a = self.agent
        repos = [(r.name, r.base, r.max_slots) for r in a.repos if r.name != self.extra.name]
        if extra:
            repos.append((self.extra.name, self.extra.base, self.extra.max_slots))
        text = (f'[runner]\nmesh_url = "http://mesh"\nstate_dir = "{self.cfg.state_dir}"\n'
                'load_hold_per_cpu = 0\n\n'
                f'[[agents]]\nname = "{a.name}"\nkey_file = "{self.key}"\nharness = "claude"\n'
                f'profile_dir = "{profile_dir or a.profile_dir}"\n'
                f'\n[agents.env]\nFAKE_REPLIES = "{self.replies}"\nFAKE_CALLS = "{self.calls}"\n')
        for name, base, slots in repos:
            text += f'\n[[agents.repos]]\nname = "{name}"\nbase = "{base}"\nmax_slots = {slots}\n'
        self.toml.write_text(text)

    def reload(self):
        """What a (re)started runner does: load the file with the shipped loader."""
        self.cfg = config_mod.load(self.toml)
        self.agent = self.cfg.agents[0]
        self.assertEqual(self.cfg.source, self.toml)

    def fresh(self, **agent_changes):
        self.write_toml(extra=True, **agent_changes)

    def ask(self, r, a, fresh=True, spec=None, **changes):
        if fresh is True:
            self.write_toml(extra=True)             # the additive repo, as an operator would add it
        values = dict(state=self.cfg.state_dir, agent=a.agent, task=a.task_id, attempt=a.id,
                      token=requests.generation(a), request_id=str(uuid.uuid4()),
                      repo=spec or self.extra, ttl=300)
        values.update(changes)
        sub = admission.submit(**values)
        r.tick()
        path = requests.directory(self.cfg.state_dir, a.agent, admission.SPOOL) / (
            sub['request']['request_id'] + '.receipt.json')
        return sub, json.loads(path.read_text())['status'] if path.exists() else None


class ActiveJournalAdmission(RealConfigFixture):
    """Waiting attempt after a real suspend -> restore -> resume: the journal rows stay `active`."""

    def restored(self):
        r, (a,) = self.parked()
        (Path(r.store.get(a.id).workspaces[0]) / 'README').write_text('edit')
        meta = {'session_id': 's', 'harness': 'claude', 'cwd': '', 'workspaces': [], 'wait': {}}
        r.pool.suspend(a.id, meta, writer_stopped=True)
        paths, _ = r.pool.restore(a.id, expected_repos={'app'})
        r.store.update(r.store.get(a.id), workspaces=paths)
        r.store.db.execute("UPDATE workspace_checkpoints SET phase='active' WHERE owner=? AND attempt=?",
                           (r.pool.owner, a.id))
        return r, a, meta

    def rows(self, r, a):
        return {x['repo']: (x['phase'], x['target']) for x in r.pool._checkpoint_rows(a.id)}

    def test_active_journal_admits_and_preserves_journal_edits_and_session(self):
        r, a, meta = self.restored()
        before = self.rows(r, a)
        got = r.store.get(a.id)
        sid, old = got.session_id, list(got.workspaces)
        self.assertEqual(self.ask(r, a)[1], 'admitted')
        got = r.store.get(a.id)
        self.assertEqual((got.session_id, got.workspaces[:1], len(got.workspaces)), (sid, old, 2))
        self.assertEqual(self.rows(r, a), before)                       # journal untouched
        self.assertEqual((Path(old[0]) / 'README').read_text(), 'edit')
        self.assertEqual(len(r.pool.leased_to(a.id)), 2)
        # idempotent: a new request adds no lease and no row
        self.assertEqual(self.ask(r, a)[1], 'admitted')
        self.assertEqual((len(r.pool.leased_to(a.id)), self.rows(r, a)), (2, before))
        # expanded inventory survives resuspend/restore
        r.pool.suspend(a.id, meta, writer_stopped=True)
        self.assertEqual(sorted(self.rows(r, a)), ['app', 'bob'])
        self.assertEqual(r.pool.leased_to(a.id), [])
        paths, _ = r.pool.restore(a.id, expected_repos={'app', 'bob'})
        self.assertEqual(len(paths), 2)
        self.assertEqual(Path(paths[0] if 'bob' not in paths[0] else paths[1]).joinpath('README').read_text(), 'edit')

    def test_crash_after_lease_is_adopted_with_active_journal(self):
        r, a, _ = self.restored()
        before = self.rows(r, a)
        with patch.object(r.store, 'update_with_marks', side_effect=RuntimeError('crash')):
            self.assertEqual(self.ask(r, a)[1], 'failed_preserved')
        self.assertEqual(len(r.pool.leased_to(a.id)), 2)
        self.assertEqual(self.ask(r, a)[1], 'admitted')
        self.assertEqual((len(r.pool.leased_to(a.id)), self.rows(r, a)), (2, before))

    def test_runner_restart_after_crash_adopts_lease_and_keeps_journal(self):
        # The crash is injected at the commit point (the only seam between lease and inventory
        # update); the recovery path is a real new runner over the same sqlite state.
        r, a, meta = self.restored()
        before = self.rows(r, a)
        old = list(r.store.get(a.id).workspaces)
        with patch.object(r.store, 'update_with_marks', side_effect=RuntimeError('crash')):
            self.assertEqual(self.ask(r, a)[1], 'failed_preserved')
        self.reload()                                   # process restart: the loader reads the file again
        restarted = self.runner(r.mesh)
        restarted.refresh_me()
        self.assertEqual(self.ask(restarted, a)[1], 'admitted')
        got = restarted.store.get(a.id)
        self.assertEqual((got.workspaces[:1], len(got.workspaces)), (old, 2))
        self.assertEqual((len(restarted.pool.leased_to(a.id)), self.rows(restarted, a)), (2, before))
        self.assertEqual((Path(old[0]) / 'README').read_text(), 'edit')
        # restart again, then resuspend/restore the expanded inventory
        self.reload()                                   # now the expanded file is the boot config
        again = self.runner(r.mesh)
        again.refresh_me()
        again.pool.suspend(a.id, meta, writer_stopped=True)
        paths, _ = again.pool.restore(a.id, expected_repos={'app', 'bob'})
        self.assertEqual(len(paths), 2)

    def refused_with(self, sql, *args):
        r, a, _ = self.restored()
        r.store.db.execute(sql, (*args, r.pool.owner, a.id))
        self.assertEqual(self.ask(r, a)[1], 'rejected_checkpointed')
        self.assertEqual(len(r.pool.leased_to(a.id)), 1)
        self.assertNotIn('bob', r.pool.repos)

    def test_non_active_phases_refused(self):
        self.refused_with("UPDATE workspace_checkpoints SET phase='released' WHERE owner=? AND attempt=?")

    def test_restoring_phase_refused(self):
        self.refused_with("UPDATE workspace_checkpoints SET phase='restoring' WHERE owner=? AND attempt=?")

    def test_verified_phase_refused(self):
        self.refused_with("UPDATE workspace_checkpoints SET phase='verified' WHERE owner=? AND attempt=?")

    def test_restored_phase_refused(self):
        self.refused_with("UPDATE workspace_checkpoints SET phase='restored' WHERE owner=? AND attempt=?")

    def test_journal_target_not_live_lease_refused(self):
        self.refused_with("UPDATE workspace_checkpoints SET target='/elsewhere' WHERE owner=? AND attempt=?")

    def test_journal_naming_requested_repo_refused(self):
        self.refused_with("UPDATE workspace_checkpoints SET repo='bob' WHERE owner=? AND attempt=?")

    def test_foreign_lease_with_active_journal_refused(self):
        r, a, _ = self.restored()
        r.pool._set(Path(self.root / 'stray'), 'app', 'leased', a.id)
        self.assertEqual(self.ask(r, a)[1], 'rejected_foreign_lease')    # fail closed by the lease check

    def test_active_writer_with_active_journal_refused(self):
        r, a, _ = self.restored()
        r.store.update(r.store.get(a.id), pid=1)
        try:
            with patch('mesh_runner.runner.group_alive', return_value=True):
                self.assertEqual(self.ask(r, a)[1], 'rejected_active_writer')
        finally:
            # The planted pid is fictitious: teardown's _kill would killpg(1) in CI.
            r.store.update(r.store.get(a.id), pid=0)

    def test_config_drift_and_stale_generation_with_active_journal_refused(self):
        r, a, _ = self.restored()
        self.fresh(profile_dir=Path('/elsewhere'))
        self.assertEqual(self.ask(r, a, fresh=False)[1], 'rejected_config')
        self.assertEqual(self.ask(r, a, token='0' * 64)[1], 'rejected_wait_generation')
