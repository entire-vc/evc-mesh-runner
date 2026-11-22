"""A plain ``wait_for on=card`` waits for the target's code: it ends when the target's merge request is
merged, not when somebody closes the target card later. A target without a merge request still ends the
wait by closing (unchanged). FakeMesh and FakeGitLab are the external boundaries; Runner, Store and the
observer are real."""
from fakes import CAT_TO_STATUS, FakeGitLab, FakeMesh, task, wait_card, write_replies
from mesh_runner import wake
from test_runner import Bench

MR = "grp/proj!7"
MR_URL = "https://git.example/grp/proj/-/merge_requests/7"
MR2 = "grp/proj!8"
MR2_URL = "https://git.example/grp/proj/-/merge_requests/8"


class Down(FakeGitLab):
    def get_mr(self, target):
        raise OSError("gitlab down")


def link(url, status):
    return {"link_type": "pr", "url": url, "status": status, "status_synced_at": "2026-11-16T10:00:00Z"}


class TargetMerged(Bench):
    def flow(self, links=(), states=None, observer=True):
        t, x = task(1), task(2)             # x: the target, stays open
        mesh, gl = FakeMesh([t, x], max_concurrent=1), FakeGitLab()
        mesh.links = {x["id"]: [dict(row) for row in links]}
        for target, state in (states or {}).items():
            gl.mr_rows[target] = {"state": state}
        r = self.runner(mesh, gl if observer else None)
        self.addCleanup(r.store.db.close)
        r.refresh_me()
        return r, t, x

    def declare(self, r, t, x, first=True):
        line = wait_card(x["id"])
        write_replies(self.replies, {t["id"][:8]: line, t["id"][:8] + ":resume": line})
        r.start_new(1) if first else r.resume_ready(1)
        a = self.finish_turn(r)
        self.assertEqual((a.state, a.wait.get("kind")), ("waiting", "card"), a.wake_text)
        return a

    def observe(self, r, a):
        r._wake_budgeted(r.store.get(a.id))
        return r.store.get(a.id)

    def test_already_merged_target_releases_the_wait_at_once(self):
        r, t, x = self.flow([link(MR_URL, "merged")], {MR: "merged"})
        a = self.declare(r, t, x)
        self.assertTrue(r.mesh.dependencies(t["id"])["outgoing"])   # the blocks edge the wait wrote
        s = self.observe(r, a)
        self.assertEqual((s.state, s.id), ("ready", a.id))
        self.assertIn(f"task #{x['id'][:8]} is merged ({MR})", s.wake_text)
        self.assertEqual(r.store.mark(self.agent.name, t["id"], "met_wake")[0], a.id)   # the awaited thing
        self.assertNotIn("edge", s.wait)
        self.assertEqual(r.mesh.dependencies(t["id"])["outgoing"], [])        # the wait's edge is gone

    def test_open_mr_holds_then_its_merge_wakes(self):
        r, t, x = self.flow([link(MR_URL, "open")], {MR: "opened"})
        a = self.declare(r, t, x)
        self.assertEqual(self.observe(r, a).state, "waiting")
        r.mesh.links[x["id"]] = [link(MR_URL, "merged")]
        r.gitlab.mr_rows[MR] = {"state": "merged"}
        s = self.observe(r, a)
        self.assertEqual(s.state, "ready")
        self.assertIn("is merged", s.wake_text)

    def test_target_without_mr_still_waits_for_its_closing(self):
        # positive control: no merge request, no MR wake; closing the card wakes as before
        r, t, x = self.flow()
        a = self.declare(r, t, x)
        self.assertEqual(self.observe(r, a).state, "waiting")
        r.mesh.tasks[x["id"]]["status_id"] = CAT_TO_STATUS["done"]
        s = self.observe(r, a)
        self.assertEqual(s.state, "ready")
        self.assertIn(f"task #{x['id'][:8]} is done", s.wake_text)
        self.assertNotIn("is merged", s.wake_text)

    def test_redeclared_wait_on_the_same_merged_target_waits_for_closing(self):
        r, t, x = self.flow([link(MR_URL, "merged")], {MR: "merged"})
        a = self.declare(r, t, x)
        self.assertEqual(self.observe(r, a).state, "ready")
        a = self.declare(r, t, x, first=False)          # the owner needs the card closed after all
        self.assertEqual(self.observe(r, a).state, "waiting")    # told once: no wake loop
        r.mesh.links[x["id"]].append(link(MR2_URL, "merged"))  # a new merged MR is news again
        r.gitlab.mr_rows[MR2] = {"state": "merged"}
        s = self.observe(r, a)
        self.assertEqual(s.state, "ready")
        self.assertIn(MR2, s.wake_text)

    def test_an_open_second_mr_holds(self):
        r, t, x = self.flow([link(MR_URL, "merged"), link(MR2_URL, "open")], {MR: "merged", MR2: "opened"})
        a = self.declare(r, t, x)
        self.assertEqual(self.observe(r, a).state, "waiting")

    def test_gitlab_disagreeing_with_mesh_holds(self):
        r, t, x = self.flow([link(MR_URL, "merged")], {MR: "opened"})
        a = self.declare(r, t, x)
        self.assertEqual(self.observe(r, a).state, "waiting")

    def test_without_a_gitlab_observer_mesh_alone_never_releases_the_wait(self):
        # Mesh says merged, nothing can confirm it: the wait stands and no delivery mark is written;
        # the target's ordinary closing still ends it
        r, t, x = self.flow([link(MR_URL, "merged")], {MR: "merged"}, observer=False)
        a = self.declare(r, t, x)
        s = self.observe(r, a)
        self.assertEqual(s.state, "waiting", s.wake_text)
        self.assertNotIn("ci_observation", s.wait)
        self.assertIsNone(r.store.mark(self.agent.name, t["id"], wake.CARD_MR_MERGED + x["id"]))
        r.mesh.tasks[x["id"]]["status_id"] = CAT_TO_STATUS["done"]
        s = self.observe(r, a)
        self.assertEqual(s.state, "ready")
        self.assertIn(f"task #{x['id'][:8]} is done", s.wake_text)
        self.assertNotIn("is merged", s.wake_text)

    def test_closed_unmerged_mr_is_not_delivered_code(self):
        r, t, x = self.flow([link(MR_URL, "closed")], {MR: "closed"})
        a = self.declare(r, t, x)
        self.assertEqual(self.observe(r, a).state, "waiting")


class Observation(Bench):
    def test_typed_condition_and_question_waits_are_not_released_by_a_merge(self):
        mesh, gl = FakeMesh([task(2)]), FakeGitLab()
        target = task(2)
        mesh.links = {target["id"]: [link(MR_URL, "merged")]}
        gl.mr_rows[MR] = {"state": "merged"}
        self.assertTrue(wake.target_mr_merged(mesh, gl, target, {"kind": "card"}))
        for extra in ({"card_condition": "deployed"}, {"watch_comments_by": {"id": "agent-lead"}}):
            self.assertEqual(wake.target_mr_merged(mesh, gl, target, {"kind": "card", **extra}), "", extra)

    def test_only_a_gitlab_confirmation_releases(self):
        target = task(2)
        mesh = FakeMesh([target])
        mesh.links = {target["id"]: [link(MR_URL, "merged")]}
        gl = FakeGitLab()
        gl.mr_rows[MR] = {"state": "merged"}
        for name, observer in (("no observer", None), ("observer down", Down())):
            with self.subTest(name):
                wait = {"kind": "card"}
                self.assertEqual(wake.target_mr_merged(mesh, observer, target, wait), "")
                self.assertNotIn("ci_observation", wait)
        # a merge request this GitLab does not hold (another forge): nothing confirms it
        mesh.links = {target["id"]: [link("https://github.com/o/r/pull/7", "merged")]}
        wait = {"kind": "card"}
        self.assertEqual(wake.target_mr_merged(mesh, gl, target, wait), "")
        self.assertNotIn("ci_observation", wait)
        # positive control: the same Mesh state confirmed by GitLab releases it and records the mark
        mesh.links = {target["id"]: [link(MR_URL, "merged")]}
        wait = {"kind": "card"}
        self.assertIn("is merged", wake.target_mr_merged(mesh, gl, target, wait))
        self.assertEqual(wait["ci_observation"], {"key": wake.CARD_MR_MERGED + target["id"], "value": MR_URL})

    def test_unreadable_links_leave_the_wait(self):
        class Broken(FakeMesh):
            def vcs_links(self, tid):
                raise OSError("down")
        target = task(2)
        wait = {"kind": "card"}
        self.assertEqual(wake.target_mr_merged(Broken([target]), FakeGitLab(), target, wait), "")
        self.assertNotIn("ci_observation", wait)
