"""Scoped additive repo admission: new repo attached, existing state preserved, fail closed."""
import copy
import json
import os
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import PropertyMock, patch

from mesh_runner import config as config_mod
from mesh_runner import repo_admission as admission
from mesh_runner import workspace_requests as requests
from mesh_runner.store import iso
from mesh_runner.workspace import RepoSpec
from fakes import calls, task, wait_time, write_replies
from test_runner import Bench, make_repo
from test_workspace_requests import WorkspaceRequests as _Fixture


class AdmissionFixture(Bench):
    parked = _Fixture.parked

    def setUp(self):
        super().setUp()
        self.extra = RepoSpec("extra", make_repo(self.root / "extra-src"), max_slots=2)
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


class AdmissionSpec(AdmissionFixture):
    def cache(self, repositories):
        from mesh_runner.repo_access import AccessKeeper
        from mesh_runner.store import Store
        store = Store(self.cfg.state_dir / 'runner.sqlite')
        self.addCleanup(store.close)
        AccessKeeper(store, self.agent.name).observe({'repository_access': {
            'revision': 3, 'repositories': [s.project._asdict() for s in repositories]}})
        return store

    def inventory(self):
        from mesh_runner.repo_access import identity
        spec = replace(self.extra, max_slots=18,
                       project=identity('gitlab', 'git.example', 'group/extra'))
        self.cfg.host_repositories = [spec]
        return spec

    def test_never_migrated_uses_legacy_without_creating_registry(self):
        self.agent.repos.append(self.extra)
        self.inventory()
        self.assertEqual(admission.configured_spec(self.cfg, self.agent, 'extra'), self.extra)
        self.assertFalse((self.cfg.state_dir / 'runner.sqlite').exists())

    def test_explicit_permission_uses_inventory_even_without_legacy_entry(self):
        spec = self.inventory()
        self.cache([spec])
        self.assertEqual(admission.configured_spec(self.cfg, self.agent, 'extra'), spec)

    def test_explicit_deny_does_not_fall_back_to_legacy(self):
        self.agent.repos.append(self.extra)
        self.inventory()
        self.cache([])
        self.assertIsNone(admission.configured_spec(self.cfg, self.agent, 'extra'))

    def test_corrupt_permission_does_not_fall_back_to_legacy(self):
        from mesh_runner import repo_access
        store = self.cache([])
        store.mark(self.agent.name, repo_access.STORE_TASK, repo_access.STORE_KEY, 'broken')
        with self.assertRaises(ValueError):
            admission.configured_spec(self.cfg, self.agent, 'app')


class RepoAdmission(AdmissionFixture):
    def test_failure_diagnostics_mask_known_credentials(self):
        r, (a,) = self.parked()
        secret = 'fixture-value'
        r.env['TEST_TOKEN'] = secret
        self.extra = replace(self.extra, remote='missing-' + secret)
        with self.assertLogs('mesh_runner', level='WARNING') as logs:
            sub, status = self.ask(r, a)
        self.assertEqual(status, 'failed_preserved')
        receipt = requests._read(requests.directory(self.cfg.state_dir, a.agent, admission.SPOOL)
                                 / (sub['request']['request_id'] + '.receipt.json'))
        for out in (json.dumps(receipt), '\n'.join(logs.output)):
            self.assertNotIn(secret, out)
            self.assertIn('[redacted]', out)
        self.assertLessEqual(len(receipt['error']['message']), 300)

    def test_failed_preconfigured_fetch_keeps_pool_and_existing_work(self):
        r, (a,) = self.parked()
        r.tick()  # settle ordinary wait metadata before the preservation snapshot
        self.extra = replace(self.extra, remote='missing-remote')
        r.agent.repos.append(self.extra)
        r.pool.repos['extra'] = self.extra
        old = r.store.get(a.id)
        path = Path(old.workspaces[0]) / 'README'
        path.write_text('uncommitted work\n')
        rows = r.pool.rows()
        with self.assertLogs('mesh_runner', level='WARNING') as logs:
            sub, status = self.ask(r, a, fresh=copy.deepcopy(self.cfg))
        self.assertEqual(status, 'failed_preserved')
        self.assertEqual(r.pool.repos['extra'], self.extra)
        self.assertIsInstance(r._pool_full(), bool)
        self.assertEqual(r.pool.rows(), rows)
        got = r.store.get(a.id)
        self.assertEqual((got.workspaces, got.session_id, got.wait),
                         (old.workspaces, old.session_id, old.wait))
        self.assertEqual(path.read_text(), 'uncommitted work\n')
        receipt = requests._read(requests.directory(self.cfg.state_dir, a.agent, admission.SPOOL)
                                 / (sub['request']['request_id'] + '.receipt.json'))
        self.assertEqual(receipt['error']['stage'], 'lease')
        self.assertEqual(receipt['error']['type'], 'WorkspaceError')
        self.assertIn('missing-remote', receipt['error']['message'])
        self.assertIn(sub['request']['request_id'], '\n'.join(logs.output))
        # The operator repairs the remote; the ordinary request can retry without losing the pool.
        self.extra = replace(self.extra, remote='origin')
        r.agent.repos[-1] = self.extra
        r.pool.repos['extra'] = self.extra
        self.assertEqual(self.ask(r, got, fresh=copy.deepcopy(self.cfg))[1], 'admitted')

    def test_failed_new_registration_rolls_back_only_the_new_entry(self):
        r, (a,) = self.parked()
        self.extra = replace(self.extra, remote='missing-remote')
        before = dict(r.pool.repos)
        _, status = self.ask(r, a)
        self.assertEqual(status, 'failed_preserved')
        self.assertEqual(r.pool.repos, before)
        self.assertFalse(r.store.mark(a.agent, a.task_id, f'repo_admission:{a.id}:extra')[0])

    def test_mesh_admission_uses_inventory_spec_instead_of_legacy(self):
        from mesh_runner.repo_access import identity
        r, (a,) = self.parked()
        legacy = replace(self.extra, max_slots=12)
        inventory = replace(self.extra, max_slots=18,
                            project=identity('gitlab', 'git.example', 'group/extra'))
        r.agent.repos.append(legacy)
        self.cfg.host_repositories = [inventory]
        r.access.observe({'repository_access': {'revision': 3, 'repositories': [
            inventory.project._asdict()]}})
        r._apply_access()
        _, status = self.ask(r, a, fresh=copy.deepcopy(self.cfg), spec=inventory)
        self.assertEqual(status, 'admitted')
        self.assertEqual(r.pool.repos['extra'], inventory)

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
        self.assertIn('extra', r.pool.repos)
        self.assertIn('extra', self.agent_names(r))
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
        self.assertNotIn('extra', r.pool.repos)
        self.assertNotIn('extra', self.agent_names(r))

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
        self.assertNotIn('extra', r.pool.repos)


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


class AdmissionCLI(RealConfigFixture):
    def test_cli_pins_inventory_spec_and_tick_admits_it(self):
        from contextlib import redirect_stdout
        from io import StringIO
        import logging
        from mesh_runner import cli, repo_access
        from mesh_runner.store import Store
        root_log = logging.getLogger()
        self.addCleanup(root_log.setLevel, root_log.level)
        self.addCleanup(setattr, root_log, 'handlers', root_log.handlers[:])
        self.write_toml(extra=True)
        inventory_text = (f'\n[[repositories]]\nname = "extra"\nbase = "{self.extra.base}"\n'
                          'max_slots = 18\nprovider = "gitlab"\nhost = "git.example"\n'
                          'project_path = "group/extra"\n')
        with self.toml.open('a') as out:
            out.write(inventory_text)
        self.reload()
        r, (a,) = self.parked()
        # parked() adjusts the first repo's capacity; keep the canonical TOML in sync.
        self.write_toml(extra=True)
        with self.toml.open('a') as out:
            out.write(inventory_text)
        spec = self.cfg.host_repositories[0]
        grant = {'repository_access': {'revision': 3, 'repositories': [spec.project._asdict()]}}
        r.access.observe(grant)
        r._apply_access()
        store = Store(self.cfg.state_dir / 'runner.sqlite')
        try:
            repo_access.AccessKeeper(store, a.agent).observe(grant)
        finally:
            store.close()
        output = StringIO()
        with redirect_stdout(output):
            result = cli.main(['admit-workspace-repo', '--config', str(self.toml), '--agent', a.agent,
                               '--task', a.task_id, '--attempt', a.id,
                               '--wait-generation', requests.generation(a),
                               '--request-id', str(uuid.uuid4()), '--repo', 'extra'])
        self.assertEqual(result, 0)
        submitted = json.loads(output.getvalue())
        self.assertEqual(submitted['request']['spec'], admission.spec_digest(spec))
        r.tick()
        receipt = requests._read(requests.directory(self.cfg.state_dir, a.agent, admission.SPOOL)
                                 / (submitted['request']['request_id'] + '.receipt.json'))
        self.assertEqual(receipt['status'], 'admitted')
        self.assertEqual(len(r.store.get(a.id).workspaces), 2)
        self.assertEqual(r.store.get(a.id).session_id, a.session_id)


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
        self.assertEqual(sorted(self.rows(r, a)), ['app', 'extra'])
        self.assertEqual(r.pool.leased_to(a.id), [])
        paths, _ = r.pool.restore(a.id, expected_repos={'app', 'extra'})
        self.assertEqual(len(paths), 2)
        self.assertEqual(Path(paths[0] if 'extra' not in paths[0] else paths[1]).joinpath('README').read_text(), 'edit')

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
        paths, _ = again.pool.restore(a.id, expected_repos={'app', 'extra'})
        self.assertEqual(len(paths), 2)

    def refused_with(self, sql, *args):
        r, a, _ = self.restored()
        r.store.db.execute(sql, (*args, r.pool.owner, a.id))
        self.assertEqual(self.ask(r, a)[1], 'rejected_checkpointed')
        self.assertEqual(len(r.pool.leased_to(a.id)), 1)
        self.assertNotIn('extra', r.pool.repos)

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
        self.refused_with("UPDATE workspace_checkpoints SET repo='extra' WHERE owner=? AND attempt=?")

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


class ResumeAdmitsLaneRepos(AdmissionFixture):
    """A repository added to the lane while an attempt waited reaches that attempt on its
    resume (live leases or a checkpoint restore), by admit-workspace-repo's own journal, without an
    operator request. The journal still fixes what a restore brings back."""

    def setUp(self):
        super().setUp()
        self.agent.repos.append(self.extra)            # the lane's config grew after the attempt was admitted

    def wake(self, r, a):
        """The ordinary path: the awaited card closes, the tick wakes and resumes the attempt."""
        dependency = task(51, assignee_id='agent-lead')
        r.mesh.tasks[dependency['id']] = dependency
        r.store.update(r.store.get(a.id), turns=1,
                       wait={'kind': 'card', 'target': dependency['id'][:8], 'started': iso()})
        write_replies(self.replies, {a.task8: wait_time(why='external work')})
        r.mesh.set_category(dependency['id'], 'done')
        r.tick()
        self.settle_all(r)
        return r.store.get(a.id)

    def extra_slot(self, path):
        return Path(path).parent.name == 'extra'

    def test_waiting_attempt_gets_the_added_repo_on_resume(self):
        r, (a,) = self.parked()
        before = list(a.workspaces)
        self.assertEqual(r.pool.leased_to(a.id), before)          # admitted with app only
        got = self.wake(r, a)
        self.assertEqual(got.workspaces[:1], before)               # existing workspace kept, first
        self.assertEqual(len(got.workspaces), 2)
        self.assertTrue(self.extra_slot(got.workspaces[1]))
        self.assertEqual(got.session_id, a.session_id)             # same session resumed
        self.assertEqual(sorted(r.pool.leased_to(a.id)), sorted(got.workspaces))
        last = calls(self.calls)[-1]
        self.assertTrue(last['resume'])
        self.assertIn('Repository extra was added to this task', last['prompt'])
        self.assertIn(got.workspaces[1], last['prompt'])
        mark = json.loads(r.store.mark(a.agent, a.task_id, f'repo_admission:{a.id}:extra')[0])
        self.assertEqual((mark['phase'], mark['path']), ('done', got.workspaces[1]))
        self.assertFalse(r.store.mark(a.agent, a.task_id, r.ADMISSION_NOTICE)[0])   # told once
        self.assertEqual([json.loads(d)['repo'] for _, k, d in r.store.events(a.id)
                          if k == 'repo_admitted_on_resume'], ['extra'])

    def test_checkpointed_attempt_gets_the_added_repo_after_restore(self):
        r, (a,) = self.parked()
        source = list(a.workspaces)
        (Path(source[0]) / 'README').write_text('edit before the wait\n')
        r._suspend_waiting(r.store.get(a.id), requested=True)   # the WAIT released its worktree
        self.assertEqual(r.pool.leased_to(a.id), [])
        self.assertEqual({x['repo']: x['phase'] for x in r.pool._checkpoint_rows(a.id)}, {'app': 'released'})
        # the operator's request is refused for exactly this state: the reason the lane asked again and again
        self.assertTrue(r._admission_journal_unsafe(r.store.get(a.id), 'extra'))
        got = self.wake(r, a)
        ev = [json.loads(d) for _, k, d in r.store.events(a.id) if k == 'restore_session']
        self.assertEqual([(e['intent'], e['reason'], e['workspaces']) for e in ev],
                         [('resume', 'workspace_restore_same_path', 1)])   # verdict on the journaled set
        self.assertEqual(got.workspaces[:1], source)
        self.assertEqual(len(got.workspaces), 2)
        self.assertTrue(self.extra_slot(got.workspaces[1]))
        self.assertEqual(got.session_id, a.session_id)
        self.assertEqual((Path(source[0]) / 'README').read_text(), 'edit before the wait\n')
        self.assertEqual({x['repo']: x['phase'] for x in r.pool._checkpoint_rows(a.id)}, {'app': 'active'})
        last = calls(self.calls)[-1]
        self.assertTrue(last['resume'])
        self.assertIn('Repository extra was added to this task', last['prompt'])
        # the grown inventory survives the next WAIT: both repositories are journaled and restored
        r._suspend_waiting(r.store.get(a.id), requested=True)
        self.assertEqual(sorted(x['repo'] for x in r.pool._checkpoint_rows(a.id)), ['app', 'extra'])

    def test_repo_outside_lane_repos_is_not_added(self):
        r, (a,) = self.parked()
        app = [s for s in self.agent.repos if s.name == 'app']
        self.assertIn('extra', r.pool.repos)                       # registered in the pool, not granted
        with patch.object(type(r), 'lane_repos', new_callable=PropertyMock, return_value=app):
            got = self.wake(r, a)
        self.assertEqual(got.workspaces, a.workspaces)
        self.assertEqual(r.pool.leased_to(a.id), a.workspaces)
        self.assertNotIn('was added to this task', calls(self.calls)[-1]['prompt'])

    def test_lease_outside_inventory_without_intent_adds_nothing(self):
        r, (a,) = self.parked()
        stray = self.root / 'stray'
        r.pool._set(stray, 'extra', 'leased', a.id)                # not ours to adopt: no admission intent
        self.assertEqual(r._admit_lane_repos(r.store.get(a.id)), [])
        self.assertEqual(r.store.get(a.id).workspaces, a.workspaces)
        self.assertEqual(sorted(r.pool.leased_to(a.id)), sorted(a.workspaces + [str(stray)]))

    def test_crash_between_lease_and_commit_is_adopted_on_next_resume(self):
        r, (a,) = self.parked()
        with patch.object(r.store, 'update_with_marks', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                r._admit_lane_repos(r.store.get(a.id))
        self.assertEqual(len(r.pool.leased_to(a.id)), 2)            # leased, not yet in the inventory
        self.assertEqual(r.store.get(a.id).workspaces, a.workspaces)
        added = r._admit_lane_repos(r.store.get(a.id))
        self.assertEqual(len(added), 1)
        self.assertEqual(r.store.get(a.id).workspaces, a.workspaces + added)
        self.assertEqual(len(r.pool.leased_to(a.id)), 2)            # adopted, no second lease

    def test_full_pool_skips_the_repo_and_resumes(self):
        self.agent.repos[-1] = replace(self.extra, max_slots=0)
        r, (a,) = self.parked()
        got = self.wake(r, a)
        self.assertEqual(got.workspaces, a.workspaces)
        self.assertTrue(calls(self.calls)[-1]['resume'])
        self.assertNotIn('extra', [Path(p).parent.name for p in r.pool.leased_to(a.id)])

    def test_operator_request_after_auto_admission_is_idempotent(self):
        r, (a,) = self.parked()
        added = r._admit_lane_repos(r.store.get(a.id))
        self.assertEqual(len(added), 1)
        r.store.mark(a.agent, a.task_id, r.ADMISSION_NOTICE, '')
        _, status = self.ask(r, r.store.get(a.id))
        self.assertEqual(status, 'admitted')                       # same journal key: nothing leased twice
        self.assertEqual(len(r.pool.leased_to(a.id)), 2)
