"""Pre-start repository requirement check: exact reasons, one handoff, no useless model turn."""
import json
from pathlib import Path
from unittest import mock

from mesh_runner import repo_requirements as rr
from mesh_runner.workspace import RepoSpec
from fakes import FakeGitLab, FakeMesh, task, calls
from test_runner import Bench

BASE = "https://git.example"


def link(project, iid=7, host="git.example"):
    return {"link_type": "pr", "url": f"https://{host}/{project}/-/merge_requests/{iid}"}


class Classify(Bench):
    def spec(self, name="app", base=None):
        return RepoSpec(name, base or self.agent.repos[0].base)

    def run_check(self, links, mine, approved=None, **kw):
        ident = kw.pop("ident", {})
        return rr.check(links, BASE, agent_repos=mine, approved_repos=approved if approved is not None else mine,
                        project_of=lambda s: ident.get(s.name, ""), **kw)

    def test_links_are_typed_and_host_bound(self):
        got = rr.required_projects([link("g/app"), link("g/other", host="evil.example"),
                                    {"url": "see https://git.example/g/x/-/merge_requests/1"}, "junk"], BASE)
        self.assertEqual(got, ["g/app"])

    def test_only_typed_mr_links_with_plain_paths_count(self):
        got = rr.required_projects([{"link_type": "branch", "url": "https://git.example/g/a/-/merge_requests/1"},
                                    link("g/[x](http://evil)"), link("g/ok")], BASE)
        self.assertEqual(got, ["g/ok"])

    def test_no_repo_is_not_blocked(self):
        v = self.run_check([], [self.spec()])
        self.assertEqual(v.reason, rr.NO_REPO)
        self.assertFalse(v.blocking)

    def test_each_reason_has_actor_and_action(self):
        app = self.spec()
        ident = {"app": "g/app"}
        v = self.run_check([link("g/deploy")], [app], ident=ident)
        self.assertEqual((v.reason, v.next_actor), (rr.UNDECLARED, "operator"))
        self.assertTrue(v.durable and v.action)
        other = RepoSpec("deploy", app.base)
        v = rr.check([link("g/deploy")], BASE, agent_repos=[app], approved_repos=[app, other],
                     project_of=lambda s: {"app": "g/app", "deploy": "g/deploy"}[s.name])
        self.assertEqual(v.reason, rr.NO_MEMBERSHIP)
        gone = RepoSpec("app", self.root / "nope")
        v = self.run_check([link("g/app")], [gone], ident=ident)
        self.assertEqual(v.reason, rr.MISSING_CLONE)
        v = self.run_check([link("g/app")], [app], ident=ident, checkpoint_open=True)
        self.assertEqual(v.reason, rr.CHECKPOINT)
        v = self.run_check([link("g/app")], [app], ident=ident, resuming=True, leased_repos=set())
        self.assertEqual(v.reason, rr.NO_CHECKOUT)
        v = self.run_check([link("g/app")], [app], ident=ident, pool_full=lambda n: True)
        self.assertEqual(v.reason, rr.NO_SLOT)
        self.assertTrue(v.blocking and not v.durable)
        v = self.run_check([link("g/app")], [app], ident=ident)
        self.assertEqual(v.reason, rr.OK)

    def test_own_lease_is_not_a_slot_wait_or_missing_checkout(self):
        app = self.spec()
        v = self.run_check([link("g/app")], [app], ident={"app": "g/app"}, resuming=True,
                           leased_repos={"app"}, pool_full=lambda n: True)
        self.assertEqual(v.reason, rr.OK)

    def test_fingerprint_stable_per_fact_set(self):
        a = self.run_check([link("g/x")], [self.spec()], ident={"app": "g/app"})
        b = self.run_check([link("g/x", 99)], [self.spec()], ident={"app": "g/app"})
        c = self.run_check([link("g/y")], [self.spec()], ident={"app": "g/app"})
        self.assertEqual(a.fingerprint, b.fingerprint)
        self.assertNotEqual(a.fingerprint, c.fingerprint)


class Start(Bench):
    def setup_run(self, project="g/deploy", links=True):
        t = task(1)
        mesh = FakeMesh([t])
        mesh.links = {t["id"]: [link(project)]} if links else {}
        gl = FakeGitLab()
        gl.base_url = BASE
        return t, mesh, self.runner(mesh, gl)

    def ident(self, spec):
        return {"app": "g/app", "deploy": "g/deploy"}.get(spec.name, "")

    def comments(self, mesh, t):
        return [c["body"] for c in mesh.comments_all(t["id"]) if "repository preflight" in c["body"]]

    def test_missing_repo_refused_once_then_admitted_starts_once(self):
        t, mesh, r = self.setup_run()
        with mock.patch.object(rr, "clone_project", lambda b, rem: "g/app"):
            for _ in range(3):
                r.tick()
        self.assertEqual(len(self.comments(mesh, t)), 1)
        self.assertIn("undeclared_repo", self.comments(mesh, t)[0])
        self.assertFalse(calls(self.calls))                      # no model turn
        self.assertEqual(mesh.checkouts, {})                     # refused before any Mesh checkout
        self.assertEqual(r.pool.rows(), [])                      # no lease
        # approved admission: the config now carries the repo for this agent
        extra = RepoSpec("deploy", self.agent.repos[0].base)
        self.agent.repos.append(extra)
        r.pool.repos["deploy"] = extra
        with mock.patch.object(rr, "clone_project", lambda b, rem: "g/app" if False else "g/deploy"):
            r._task_backoff.clear()
            self.settle_all(r)
        self.assertEqual(len(self.comments(mesh, t)), 1)         # no second handoff
        self.assertEqual(len([c for c in calls(self.calls) if not c['resume']]), 1)   # one cold start

    def test_metadata_only_and_no_link_cards_are_not_blocked(self):
        t, mesh, r = self.setup_run(links=False)
        self.settle_all(r)
        self.assertEqual(self.comments(mesh, t), [])
        self.assertEqual(len([c for c in calls(self.calls) if not c['resume']]), 1)

    def test_unauthorized_free_text_url_does_not_block_or_grant(self):
        t, mesh, r = self.setup_run(links=False)
        t["description"] = "see https://git.example/g/deploy/-/merge_requests/3"
        self.settle_all(r)
        self.assertEqual(self.comments(mesh, t), [])

    def test_resume_is_checked_before_checkout_and_stays_ready(self):
        t, mesh, r = self.setup_run()
        t["_status"] = "in_progress"
        r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        r.store.update(a, state="ready", session_id="11111111-1111-4111-8111-111111111111")
        for _ in range(3):
            r.resume_ready(1)
            r._resume_hold.clear()
        self.assertEqual(len(self.comments(mesh, t)), 1)
        self.assertEqual(mesh.checkouts, {})
        self.assertFalse(calls(self.calls))
        self.assertEqual(r.store.get(a.id).state, "ready")
