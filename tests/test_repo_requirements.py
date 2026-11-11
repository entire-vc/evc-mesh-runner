"""Pre-start repository requirement check: exact reasons, one handoff, no useless model turn."""
import json
from pathlib import Path
from unittest import mock

from mesh_runner import repo_requirements as rr
from mesh_runner.workspace import RepoSpec
from fakes import FakeGitLab, FakeMesh, task, calls
from test_runner import Bench

BASE = "https://git.example"


def link(project, iid=7, host="git.example", status=None, **meta):
    out = {"link_type": "pr", "url": f"https://{host}/{project}/-/merge_requests/{iid}"}
    if status is not None:
        out["status"] = status
    if meta:
        out["metadata"] = meta
    return out


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

    def test_all_missing_repos_named_in_one_verdict(self):
        app = self.spec()
        v = self.run_check([link("g/deploy"), link("g/pipe"), link("g/app")], [app], ident={"app": "g/app"})
        self.assertEqual((v.reason, v.project), (rr.UNDECLARED, "g/deploy"))
        self.assertEqual([p for _, p in v.also], ["g/pipe"])
        self.assertIn("g/pipe", v.text())
        self.assertEqual(v.as_dict()["also"], [{"reason": rr.UNDECLARED, "project": "g/pipe"}])
        single = self.run_check([link("g/deploy"), link("g/app")], [app], ident={"app": "g/app"})
        self.assertNotEqual(v.fingerprint, single.fingerprint)

    def test_link_order_does_not_change_the_handoff(self):
        app = self.spec()
        a = self.run_check([link("g/deploy"), link("g/pipe")], [app], ident={"app": "g/app"})
        b = self.run_check([link("g/pipe"), link("g/deploy")], [app], ident={"app": "g/app"})
        self.assertEqual((a.project, a.also, a.fingerprint), (b.project, b.also, b.fingerprint))

    def test_durable_blocker_leads_over_slot_wait(self):
        app = self.spec()
        v = self.run_check([link("g/app"), link("g/deploy")], [app], ident={"app": "g/app"},
                           pool_full=lambda n: True)
        self.assertEqual((v.reason, v.project), (rr.UNDECLARED, "g/deploy"))
        self.assertEqual(v.also, ((rr.NO_SLOT, "g/app"),))

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


class TerminalLinks(Classify):
    """A merged/closed MR link is history; it must not hold the start of new work."""

    def own_and_foreign(self):
        app = self.spec()
        deploy = RepoSpec("deploy", app.base)
        return app, deploy, (lambda s: {"app": "g/app", "deploy": "g/deploy"}[s.name])

    def test_single_merged_foreign_link_starts(self):
        # A historical merged foreign link must not require current repository membership.
        app, deploy, ident = self.own_and_foreign()
        fixture = [{"link_type": "pr", "provider": "gitlab", "status": "merged", "status_synced_at": None,
                    "metadata": {"status_declared_manually": {"status": "merged"}},
                    "url": "https://git.example/g/deploy/-/merge_requests/61"}]
        v = rr.check(fixture, BASE, agent_repos=[app], approved_repos=[app, deploy], project_of=ident)
        self.assertEqual(v.reason, rr.NO_REPO)
        self.assertFalse(v.blocking)
        self.assertEqual(v.historical, ("g/deploy",))
        self.assertEqual(v.as_dict()["historical"], ["g/deploy"])
        self.assertEqual(rr.required_projects(fixture, BASE), [])

    def test_open_foreign_link_still_no_membership(self):
        app, deploy, ident = self.own_and_foreign()
        for status in ("open", "opened", "draft", "", None):
            v = rr.check([link("g/deploy", status=status)], BASE, agent_repos=[app],
                         approved_repos=[app, deploy], project_of=ident)
            self.assertEqual((v.reason, v.project), (rr.NO_MEMBERSHIP, "g/deploy"), status)

    def test_open_review_role_link_stays_required(self):
        app, deploy, ident = self.own_and_foreign()
        v = rr.check([link("g/deploy", status="open", role="review")], BASE, agent_repos=[app],
                     approved_repos=[app, deploy], project_of=ident)
        self.assertEqual(v.reason, rr.NO_MEMBERSHIP)

    def test_open_own_plus_merged_foreign_is_ok(self):
        app, deploy, ident = self.own_and_foreign()
        v = rr.check([link("g/app", status="open"), link("g/deploy", iid=9, status="merged")], BASE,
                     agent_repos=[app], approved_repos=[app, deploy], project_of=ident)
        self.assertEqual((v.reason, v.project, v.historical), (rr.OK, "g/app", ("g/deploy",)))

    def test_closed_only_is_not_blocked(self):
        app, deploy, ident = self.own_and_foreign()
        v = rr.check([link("g/deploy", status="closed"), link("g/pipe", iid=3, status="Closed")], BASE,
                     agent_repos=[app], approved_repos=[app, deploy], project_of=ident)
        self.assertEqual((v.reason, v.historical), (rr.NO_REPO, ("g/deploy", "g/pipe")))

    def test_project_with_one_live_link_stays_required(self):
        # all links of a project must be finished before its absence stops blocking
        app, deploy, ident = self.own_and_foreign()
        v = rr.check([link("g/deploy", iid=1, status="merged"), link("g/deploy", iid=2, status="open")], BASE,
                     agent_repos=[app], approved_repos=[app, deploy], project_of=ident)
        self.assertEqual((v.reason, v.historical), (rr.NO_MEMBERSHIP, ()))

    def test_negative_control_open_undeclared_repo_still_refused(self):
        app, _, _ = self.own_and_foreign()
        v = self.run_check([link("g/elsewhere", status="open"), link("g/deploy", iid=2, status="merged")],
                           [app], ident={"app": "g/app"})
        self.assertEqual((v.reason, v.project, v.historical), (rr.UNDECLARED, "g/elsewhere", ("g/deploy",)))
        self.assertTrue(v.durable)

    def test_history_does_not_change_the_handoff_fingerprint(self):
        app, _, _ = self.own_and_foreign()
        a = self.run_check([link("g/elsewhere")], [app], ident={"app": "g/app"})
        b = self.run_check([link("g/elsewhere"), link("g/deploy", iid=2, status="merged")], [app],
                           ident={"app": "g/app"})
        self.assertEqual(a.fingerprint, b.fingerprint)


class Start(Bench):
    def test_mcpgate_author_membership_does_not_admit_independent_reviewer(self):
        # The author owns the only configured repository admission.
        # Admission must refuse the selected independent reviewer, not borrow that
        # lane's worktree/key or dispatch the author's review to the author.
        from dataclasses import replace
        t, mesh, r = self.setup_run("entire-vc/example-gate")
        author = replace(self.agent, name="Dana", repos=[RepoSpec("example-gate", self.agent.repos[0].base)])
        self.cfg.agents.append(author)
        # Each clone has an actual native remote; distinguish the two specs by base.
        author.repos[0] = RepoSpec("example-gate", self.root / "author-clone")
        with mock.patch.object(rr, "clone_project", lambda b, rem: (
                "entire-vc/example-gate" if b == author.repos[0].base else "g/app")):
            for _ in range(3):
                r.tick()
        self.assertEqual(len(self.comments(mesh, t)), 1)
        self.assertIn("no_membership", self.comments(mesh, t)[0])
        self.assertIn("next_actor=operator", self.comments(mesh, t)[0])
        self.assertFalse(calls(self.calls))
        self.assertFalse(mesh.checkouts)
        self.assertEqual(t["status_id"], "s-todo")

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

    def test_a_refused_card_is_held_without_a_new_attempt_each_backoff(self):
        # A durable refusal must not create another attempt on every backoff.
        t, mesh, r = self.setup_run()
        with mock.patch.object(rr, "clone_project", lambda b, rem: "g/app"):
            for _ in range(6):
                r._task_backoff.clear()                          # every backoff period ends
                r.tick()
            aborted = [a for a in r.store.by_state(self.agent.name, ("aborted",)) if a.task_id == t["id"]]
            self.assertEqual(len(aborted), 1)                    # the first refusal only
            self.assertEqual(len(self.comments(mesh, t)), 1)
            self.assertFalse(calls(self.calls))
            self.assertEqual(mesh.checkouts, {})
            reason = r._hold_reason(t, "work")
            self.assertEqual(reason, "repository preflight: undeclared_repo; required=g/deploy")
            from mesh_runner.effects import skip_state
            shown = skip_state(reason, self.agent.name)
            self.assertEqual((shown["kind"], shown["reason_code"]), ("stopped", "outside_lane_scope"))
            self.assertIn("g/deploy", shown["reason"])
        # the operator adds the repository: the hold ends at once and the card starts, with no second handoff
        extra = RepoSpec("deploy", self.agent.repos[0].base)
        self.agent.repos.append(extra)
        r.pool.repos["deploy"] = extra
        with mock.patch.object(rr, "clone_project", lambda b, rem: "g/deploy"):
            self.assertEqual(r._hold_reason(t, "work"), "")
            self.assertEqual(r.store.mark(self.agent.name, t["id"], r.REPO_PREFLIGHT)[0], "")
        self.assertEqual(len(self.comments(mesh, t)), 1)

    def test_changed_facts_of_a_held_card_are_handed_off_once(self):
        t, mesh, r = self.setup_run()
        with mock.patch.object(rr, "clone_project", lambda b, rem: "g/app"):
            r.tick()
            self.assertEqual(len(self.comments(mesh, t)), 1)
            mesh.links[t["id"]] = [link("g/other")]              # the card now needs another repository
            t["updated_at"] = "2026-11-02T00:00:00Z"
            for _ in range(3):
                r._task_backoff.clear()
                r.tick()
        notes = self.comments(mesh, t)
        self.assertEqual(len(notes), 2)
        self.assertIn("required=g/other", notes[1])
        self.assertEqual(len([a for a in r.store.by_state(self.agent.name, ("aborted",)) if a.task_id == t["id"]]), 1)

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

    def test_held_card_whose_link_merged_is_released_and_starts(self):
        t, mesh, r = self.setup_run()
        with mock.patch.object(rr, "clone_project", lambda b, rem: "g/app"):
            r.tick()
            self.assertEqual(len(self.comments(mesh, t)), 1)
            mesh.links[t["id"]] = [link("g/deploy", status="merged")]
            t["updated_at"] = "2026-11-10T20:00:00Z"
            r._task_backoff.clear()                              # the failed-start backoff period ends
            self.assertEqual(r._hold_reason(t, "work"), "")
            self.assertEqual(r.store.mark(self.agent.name, t["id"], r.REPO_PREFLIGHT)[0], "")
            self.settle_all(r)
        self.assertEqual(len(self.comments(mesh, t)), 1)
        self.assertEqual(len([c for c in calls(self.calls) if not c['resume']]), 1)
