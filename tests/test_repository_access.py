"""Mesh repository_access read hot each tick, host inventory separate from permission."""
import textwrap
import unittest
from unittest import mock

from mesh_runner import config as config_mod
from mesh_runner import repo_access as ra
from mesh_runner import repo_requirements as rr
from mesh_runner.mesh import MeshError
from mesh_runner.store import Store
from mesh_runner.workspace import RepoSpec
from fakes import FakeGitLab, FakeMesh, task, calls, write_replies
from test_runner import Bench

BASE = "https://git.example"


def link(project, iid=7, host="git.example"):
    return {"link_type": "pr", "url": f"https://{host}/{project}/-/merge_requests/{iid}"}


def access(rev, *projects, host="git.example", provider="gitlab"):
    return {"revision": rev, "repositories": [{"provider": provider, "host": host, "project_path": p}
                                              for p in projects]}


class Identity(unittest.TestCase):
    def test_normalized_on_host_scheme_port_git_suffix_and_slash(self):
        a = ra.identity("GitLab", "HTTPS://Git.Example:443/", "/g/deploy.git/")
        self.assertEqual(a, ra.Identity("gitlab", "git.example", "g/deploy"))
        self.assertEqual(ra.identity("gitlab", "git.example:80", "g/deploy"), a)
        self.assertEqual(ra.card_identity("https://git.example", "g/deploy"), a)
        # a non-default port is part of the host identity
        self.assertNotEqual(ra.identity("gitlab", "git.example:8443", "g/deploy"), a)

    def test_same_path_on_another_host_or_provider_is_another_repository(self):
        mine = ra.identity("gitlab", "git.example", "g/deploy")
        self.assertNotEqual(ra.identity("gitlab", "github.com", "g/deploy"), mine)
        self.assertNotEqual(ra.identity("github", "git.example", "g/deploy"), mine)

    def test_malformed_answers_are_refused_whole(self):
        for bad in (None, [], {"revision": "1", "repositories": []}, {"revision": True, "repositories": []},
                    {"revision": -1, "repositories": []}, {"revision": 1, "repositories": None},
                    {"revision": 1, "repositories": ["g/x"]},
                    {"revision": 1, "repositories": [{"provider": "gitlab", "host": "git.example",
                                                      "project_path": "../x"}]},
                    access(2, "g/ok") | {"repositories": access(2, "g/ok")["repositories"]
                                         + [{"provider": "gitlab", "host": "", "project_path": "g/x"}]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ra.parse(bad)

    def test_empty_list_is_an_explicit_deny_all(self):
        got = ra.parse(access(3))
        self.assertEqual((got.revision, got.repositories), (3, frozenset()))


class Keeper(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "s.sqlite"

    def test_legacy_until_explicit_then_null_or_malformed_never_reopen_it(self):
        k = ra.AccessKeeper(Store(self.path), "builder")
        self.assertFalse(k.observe({"id": "x"}))
        self.assertFalse(k.observe({"repository_access": None}))
        self.assertIsNone(k.current)
        self.assertTrue(k.observe({"repository_access": access(1, "g/app")}))
        self.assertFalse(k.observe({"repository_access": None}))           # field gone: explicit state stays
        self.assertFalse(k.observe({}))
        self.assertFalse(k.observe({"repository_access": {"revision": "x"}}))   # malformed: kept
        self.assertEqual(k.current.revision, 1)
        self.assertTrue(k.observe({"repository_access": access(2)}))       # [] replaces it
        self.assertEqual(k.current.repositories, frozenset())

    def test_older_or_same_revision_never_replaces_the_current_state(self):
        k = ra.AccessKeeper(Store(self.path), "builder")
        self.assertTrue(k.observe({"repository_access": access(5, "g/app")}))
        self.assertTrue(k.observe({"repository_access": access(6)}))          # revoke at 6
        self.assertFalse(k.observe({"repository_access": access(5, "g/app")}))   # delayed grant at 5
        self.assertFalse(k.observe({"repository_access": access(6, "g/app")}))   # same revision, other set
        self.assertEqual((k.current.revision, k.current.repositories), (6, frozenset()))
        again = ra.AccessKeeper(Store(self.path), "builder")                  # the stale answer was not stored
        self.assertEqual((again.current.revision, again.current.repositories), (6, frozenset()))
        self.assertTrue(k.observe({"repository_access": access(7, "g/app")}))
        self.assertEqual(len(k.current.repositories), 1)

    def test_last_known_good_survives_a_new_process(self):
        k = ra.AccessKeeper(Store(self.path), "builder")
        k.observe({"repository_access": access(4, "g/app")})
        again = ra.AccessKeeper(Store(self.path), "builder")               # restart, Mesh not read yet
        self.assertEqual((again.current.revision, len(again.current.repositories)), (4, 1))
        self.assertIsNone(ra.AccessKeeper(Store(self.path), "other").current)   # per lane


class Config(unittest.TestCase):
    def load(self, extra):
        import tempfile
        from pathlib import Path
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = Path(d.name) / "runner.toml"
        p.write_text(textwrap.dedent(f"""
            [runner]
            mesh_url = "http://mesh"
            state_dir = "{d.name}/state"
            [[agents]]
            name = "a"
            key_env = "HOME"
            profile_dir = "{d.name}"
            repos = [{{ name = "app", base = "{d.name}/app" }}]
            """) + textwrap.dedent(extra))
        return config_mod.load(p)

    def test_inventory_section_is_parsed_with_explicit_normalized_project(self):
        cfg = self.load("""
            [[repositories]]
            name = "deploy"
            host = "Git.Example:443"
            project_path = "g/deploy.git"
            base = "/srv/deploy"
            max_slots = 2
            """)
        (spec,) = cfg.host_repositories
        self.assertEqual((spec.name, spec.max_slots, spec.project),
                         ("deploy", 2, ra.Identity("gitlab", "git.example", "g/deploy")))
        self.assertEqual(cfg.agents[0].repos[0].project, ())               # legacy entries carry no project

    def test_inventory_rejects_duplicates_and_missing_identity(self):
        entry = '[[repositories]]\nname = "{n}"\nhost = "git.example"\nproject_path = "{p}"\nbase = "/srv/x"\n'
        for extra in (entry.format(n="x", p="g/x") + entry.format(n="x", p="g/y"),
                      entry.format(n="x", p="g/x") + entry.format(n="y", p="g/x.git"),
                      '[[repositories]]\nname = "x"\nbase = "/srv/x"\n'):
            with self.subTest(extra=extra), self.assertRaises(config_mod.ConfigError):
                self.load(extra)

    def test_no_new_runner_option(self):
        # the inventory is its own section; [runner] keys keep their count
        cfg = self.load("")
        self.assertEqual(cfg.host_repositories, [])
        with open(config_mod.__file__) as f:
            src = f.read()
        self.assertNotIn('r.get("repositories"', src)
        self.assertNotIn('r.get("host_repositories"', src)


class Runner(Bench):
    def setUp(self):
        super().setUp()
        base = self.agent.repos[0].base
        self.deploy = RepoSpec("deploy", base, max_slots=2, project=ra.identity("gitlab", "git.example", "g/deploy"))
        self.cfg.host_repositories = [self.deploy]
        self.grant = None                                  # None = Mesh has no field (legacy)
        self.outage = False

    def setup_run(self, *tasks, project="g/deploy"):
        tasks = tasks or (task(1),)
        mesh = FakeMesh(list(tasks))
        mesh.links = {t["id"]: [link(project)] for t in tasks}
        plain = mesh.me

        def me():
            if self.outage:
                raise MeshError(503, "unavailable")
            out = plain()
            if self.grant is not None:
                out["repository_access"] = self.grant
            return out
        mesh.me = me
        gl = FakeGitLab()
        gl.base_url = BASE
        return mesh, self.runner(mesh, gl)

    def notes(self, mesh, t):
        return [c["body"] for c in mesh.comments_all(t["id"]) if "repository preflight" in c["body"]]

    def starts(self):
        return [c for c in calls(self.calls) if not c["resume"]]

    def leased(self, r, repo):
        return [x for x in r.pool.rows() if x["pool"] == f"builder/{repo}" and x["state"] == "leased"]

    # --- canon §Приёмочные операции 1: four main scenarios ---------------------------------------
    def test_granted_with_clone_starts_ordinarily_on_the_inventory_clone(self):
        self.grant = access(1, "g/deploy")
        t = task(1)
        mesh, r = self.setup_run(t)
        self.settle_all(r)
        self.assertEqual(self.notes(mesh, t), [])
        self.assertEqual(len(self.starts()), 1)
        rows = [x for x in r.pool.rows() if x["pool"] == "builder/deploy"]
        self.assertTrue(rows)                              # leased from the inventory entry
        self.assertFalse([x for x in r.pool.rows() if x["pool"] == "builder/app"])   # legacy list unused
        self.assertEqual(r.status()["repository_access"],
                         {"source": "mesh", "revision": 1, "repositories": ["gitlab:git.example/g/deploy"],
                          "lane_repos": ["deploy"]})

    def test_granted_without_clone_is_missing_approved_clone(self):
        self.grant = access(1, "g/deploy", "g/pipe")
        t = task(1)
        for inventory in ([], [RepoSpec("deploy", self.root / "nope", project=self.deploy.project)]):
            with self.subTest(inventory=len(inventory)):
                self.cfg.host_repositories = inventory
                mesh, r = self.setup_run(t)
                r.tick()
                self.assertEqual(len(self.notes(mesh, t)), 1)
                self.assertIn("missing_approved_clone", self.notes(mesh, t)[0])
                self.assertIn("host inventory", self.notes(mesh, t)[0])
                self.assertFalse(self.starts())
                self.assertEqual(r.pool.rows(), [])

    def test_mesh_denies_is_no_membership_even_when_legacy_config_has_it(self):
        self.agent.repos.append(RepoSpec("deploy", self.agent.repos[0].base))     # legacy says yes
        self.grant = access(1, "g/app")
        t = task(1)
        mesh, r = self.setup_run(t)
        with mock.patch.object(rr, "clone_project", lambda b, rem: "g/deploy"):
            for _ in range(2):
                r._task_backoff.clear()
                r.tick()
        self.assertEqual(len(self.notes(mesh, t)), 1)
        self.assertIn("no_membership", self.notes(mesh, t)[0])
        self.assertIn("Mesh repository access", self.notes(mesh, t)[0])
        self.assertFalse(self.starts())
        self.assertEqual(mesh.checkouts, {})

    def test_mesh_outage_keeps_last_valid_access_without_widening(self):
        self.grant = access(1)                              # explicit deny-all
        t = task(1)
        mesh, r = self.setup_run(t)
        r.tick()
        self.assertIn("no_membership", self.notes(mesh, t)[0])
        self.outage = True
        self.grant = access(2, "g/deploy")                  # granted, but Mesh cannot answer
        for _ in range(2):
            r._task_backoff.clear()
            r.tick()
        self.assertEqual(r.access.current.revision, 1)
        self.assertFalse(self.starts())
        self.assertEqual(r.pool.rows(), [])

    def test_absent_field_is_legacy(self):
        t = task(1)
        mesh, r = self.setup_run(t, project="g/app")       # fixture clone is g/app in legacy config
        self.settle_all(r)
        self.assertEqual(r.status()["repository_access"]["source"], "legacy")
        self.assertEqual(len(self.starts()), 1)
        self.assertTrue([x for x in r.pool.rows() if x["pool"] == "builder/app"])

    # --- additional AC --------------------------------------------------------------------------
    def test_configured_empty_list_denies_the_legacy_repo(self):
        self.grant = access(5)
        t = task(1)
        mesh, r = self.setup_run(t, project="g/app")
        r.tick()
        self.assertIn("no_membership", self.notes(mesh, t)[0])
        self.assertFalse(self.starts())

    def test_malformed_answer_does_not_reset_access(self):
        self.grant = access(1, "g/deploy")
        mesh, r = self.setup_run(task(1))
        r.refresh_me()
        self.grant = {"revision": 2, "repositories": "everything"}
        r.refresh_me()
        self.assertEqual(r.access.current.revision, 1)
        self.grant = None                                   # field disappears after an explicit state
        r.refresh_me()
        self.assertEqual([s.name for s in r.lane_repos], ["deploy"])

    def test_restart_during_outage_keeps_disk_state_not_legacy(self):
        self.grant = access(1)                              # deny-all reaches the Store
        t = task(1)
        mesh, r = self.setup_run(t, project="g/app")
        r.tick()
        # a new process on the same state; Mesh down from the first tick on
        self.outage = True
        r2 = self.runner(mesh, r.gitlab)
        r2.me = {"id": r.me["id"], "workspace_id": "ws1"}  # earlier identity; the tick's read fails
        self.assertTrue(r2.access.mesh)
        self.assertEqual(r2.lane_repos, [])
        r2._task_backoff.clear()
        r2.tick()
        self.assertFalse(self.starts())
        self.assertEqual(r2.pool.rows(), [])

    def test_new_revision_releases_the_cached_hold_in_one_tick(self):
        # no_membership -> missing_approved_clone with the same (empty) local lane: only the revision changed
        self.cfg.host_repositories = []
        self.grant = access(1)
        t = task(1)
        mesh, r = self.setup_run(t)
        r.tick()
        self.assertIn("no_membership", self.notes(mesh, t)[-1])
        r._task_backoff.clear()
        r.tick()                                           # held from cache, nothing new
        self.assertEqual(len(self.notes(mesh, t)), 1)
        self.grant = access(2, "g/deploy")
        r._task_backoff.clear()
        r.tick()
        self.assertEqual(len(self.notes(mesh, t)), 2)
        self.assertIn("missing_approved_clone", self.notes(mesh, t)[-1])
        # and the clone arriving in the inventory + a grant starts the card on the next tick
        self.cfg.host_repositories = [self.deploy]
        self.grant = access(3, "g/deploy")
        r._task_backoff.clear()
        self.settle_all(r)
        self.assertEqual(len(self.starts()), 1)

    def test_grant_with_unnormalized_identity_matches_the_card(self):
        self.grant = access(1, "/g/deploy.git/", host="GIT.EXAMPLE:443")
        t = task(1)
        mesh, r = self.setup_run(t)
        self.settle_all(r)
        self.assertEqual(self.notes(mesh, t), [])
        self.assertEqual(len(self.starts()), 1)

    def test_same_project_path_on_another_host_is_not_granted(self):
        self.grant = access(1, "g/deploy", host="github.com")
        t = task(1)
        mesh, r = self.setup_run(t)
        r.tick()
        self.assertIn("no_membership", self.notes(mesh, t)[0])
        self.assertFalse(self.starts())

    def test_another_lanes_clone_is_not_a_permission(self):
        # the host inventory (and other lanes' configs) know g/deploy; Mesh does not grant it here
        other = RepoSpec("deploy", self.agent.repos[0].base)
        from dataclasses import replace
        self.cfg.agents.append(replace(self.agent, name="Other", repos=[other]))
        self.grant = access(1, "g/app")
        t = task(1)
        mesh, r = self.setup_run(t)
        r.tick()
        self.assertIn("no_membership", self.notes(mesh, t)[0])
        self.assertFalse(self.starts())

    def test_revoke_keeps_the_running_lease_and_blocks_the_next_start(self):
        t1, t2 = task(1), task(2)
        write_replies(self.replies, {t1["id"][:8]: "SLEEP:30:done"})
        self.grant = access(1, "g/deploy")
        mesh, r = self.setup_run(t1)
        r.tick()                                           # t1 starts and keeps running
        running = self.leased(r, "deploy")
        self.assertEqual(len(running), 1)
        attempt = running[0]["attempt"]
        self.assertEqual(r.store.get(attempt).state, "running")
        self.grant = access(2)                             # revoked in Mesh
        mesh.tasks[t2["id"]] = t2
        mesh.links[t2["id"]] = [link("g/deploy")]
        r._task_backoff.clear()
        r.tick()
        self.assertEqual(self.leased(r, "deploy"), running)            # lease kept, same attempt
        self.assertEqual(r.store.get(attempt).state, "running")        # process not killed
        self.assertIn("deploy", r.pool.repos)                          # still managed for release/restore
        self.assertEqual(r.lane_repos, [])
        self.assertIn("no_membership", self.notes(mesh, t2)[0])        # the next start is blocked
        live = r.store.by_state(self.agent.name, ("starting", "running", "unknown", "waiting", "ready"))
        self.assertEqual([a.id for a in live], [attempt])              # no attempt runs for t2
        self.assertFalse([x for x in r.pool.rows() if x["attempt"] and x["attempt"] != attempt])
        self.assertNotIn(t2["id"], mesh.checkouts)


if __name__ == "__main__":
    unittest.main()
