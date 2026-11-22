"""Merge order (``merge-after:`` on the card, or the author's own wait on another merge request): the merge
gate does not start while a predecessor is unmerged, whoever declared the merge wait.

The replay: a frontend card says it merges after the API card, whose merge request does not exist yet. A
reviewer's SHIP on the frontend head is handed back; the runner declared the merge wait and ran the gate
in the same tick, and the frontend merged before the API. FakeMesh and FakeGitLab are the external
boundaries; Runner, Store and the merge gate command are real."""
import unittest

from fakes import AGENT_ID, FakeGitLab, FakeMesh, task, write_replies
from mesh_runner import merge_order
from mesh_runner.mesh import MeshError
from mesh_runner.store import iso
from test_merge_wait import PROJECT, SHA, TARGET, mr_row
from test_ship_merge_wait import ShipBench, pr, soon

API = "g/api!9"
API_URL = "https://git.example/g/api/-/merge_requests/9"
PRED = task(2, cat="in_progress", assignee_id="agent-backend")      # the predecessor card (stub id 00000002)
ORDER = "Starts at once on the contract.\n- merge-after: `#00000002`\n"


class Refs(unittest.TestCase):
    def test_reads_only_the_merge_after_line(self):
        self.assertEqual(merge_order.refs("merge-after: #00000002"), ["card:00000002"])
        self.assertEqual(merge_order.refs(ORDER), ["card:00000002"])
        self.assertEqual(merge_order.refs("* `merge-after: g/api!9, #0000beef`"), ["mr:g/api!9", "card:0000beef"])
        self.assertEqual(merge_order.refs(f"Merge-After: {PRED['id']}"), [f"card:{PRED['id']}"])
        self.assertEqual(merge_order.refs("merge-after: #00000002\nmerge-after: #00000002"), ["card:00000002"])

    def test_prose_holds_nothing(self):
        for text in ("", None, "Merges after the API card (#00000002).", "see merge-after: below"):
            with self.subTest(text=text):
                self.assertEqual(merge_order.refs(text), [])

    def test_a_line_that_names_nothing_readable_is_kept_as_bad(self):
        for text, bad in (("merge-after: the API", "bad:the API"), ("merge-after: #0000", "bad:#0000"),
                          ("merge-after: !2011", "bad:!2011"),
                          ("merge-after: https://github.example/a/b/pull/3", "bad:https://github.example/a/b/pull/3")):
            with self.subTest(text=text):
                self.assertEqual(merge_order.refs(text), [bad])
        self.assertEqual(merge_order.refs("merge-after: g/api!9, !2011"), ["mr:g/api!9", "bad:g/api!9, !2011"])

    def test_an_empty_line_is_kept_as_bad(self):
        for text in ("merge-after:", "- merge-after:\nnext line", "merge-after:   ", "`merge-after:`"):
            with self.subTest(text=text):
                self.assertEqual(merge_order.refs(text), ["bad:"])

    def test_a_merge_request_url(self):
        self.assertEqual(merge_order.refs(f"- merge-after: {API_URL}"), [f"url:{API_URL}"])

    def test_description_left_out_by_mesh(self):
        self.assertEqual(merge_order.description({"id": "x", "has_description": False}), "")
        self.assertEqual(merge_order.description({"id": "x", "description": ORDER}), ORDER)
        for partial in ({"id": "x", "has_description": True}, {"id": "x"}, {"id": "x", "description": None}):
            with self.subTest(partial=partial), self.assertRaises(ValueError):   # not sent: unreadable
                merge_order.description(partial)

    def test_carried_references_come_first(self):
        self.assertEqual(merge_order.refs("merge-after: #00000002", ["mr:g/api!9", "junk"]),
                         ["mr:g/api!9", "card:00000002"])


class OrderBench(ShipBench):
    def flow(self, description=ORDER, pred=PRED, pred_links=(), rows=None):
        """The author's card with a merge order; its work turn ended in review."""
        t = task(1, description=description)
        mesh, gl = FakeMesh([t, dict(pred)], max_concurrent=1), FakeGitLab()
        mesh.links = {t["id"]: [pr()], pred["id"]: list(pred_links)}
        gl.mr_rows.update({TARGET: mr_row(), **(rows or {})})
        r = self.runner(mesh, gl)
        write_replies(self.replies, {t["id"][:8]: "SLEEP:1:done"})
        r.tick()
        mesh.add_comment(t["id"], "done; sent to review", author=AGENT_ID)
        mesh.set_category(t["id"], "review")
        self.settle_all(r)
        self.assertEqual(len(self.prompts()), 1)
        return r, t, mesh, gl

    def joined(self, r, a):
        """The parked attempt, after any gate run its declaration started has finished."""
        job = r._merge_jobs.get(a.id)
        if job:
            job["thread"].join(15)
        return r.store.get(a.id)

    def held(self, r, a):
        a = self.check(r, r.store.get(a.id))
        self.assertEqual(a.state, "waiting", a.wake_text)
        self.assertEqual(self.runs(), [])
        return a

    def merge_pred(self, mesh, gl):
        mesh.links[PRED["id"]] = [dict(pr(API_URL), status="merged")]
        gl.mr_rows[API] = mr_row("merged", sha="c" * 40)


class Replay(OrderBench):
    def test_ship_handed_back_before_the_predecessor_merged_does_not_start_the_gate(self):
        r, t, mesh, gl = self.flow()
        self.say("merged ok\n", rc=0)
        self.hand_back(r, t)
        a = self.joined(r, self.parked(r))
        self.assertEqual(len(mesh.declared), 1)                    # the wait is declared ...
        self.assertEqual(a.wait["kind"], "merge")
        self.assertEqual(self.runs(), [])                          # ... the gate does not start
        note = mesh.declared[0][1]["note"]
        self.assertIn("#00000002 has no merge request yet", note)
        self.assertNotIn("runs now", note)
        a = self.held(r, a)                                        # and not on the next wake checks
        mesh.links[PRED["id"]] = [pr(API_URL)]                     # its MR opens: still held
        gl.mr_rows[API] = mr_row(sha="c" * 40)
        a = self.held(r, a)
        self.assertIn("#00000002 is not merged yet", a.wait["merge"]["order"])
        self.assertEqual(len(self.prompts()), 1)                   # no author session meanwhile
        self.merge_pred(mesh, gl)                                  # the API merged: the gate runs
        a = self.check(r, a)
        self.assertEqual(self.runs(), [f"56 {PROJECT} {SHA}"])
        self.assertNotIn("order", a.wait["merge"])

    def test_positive_control_without_the_line_the_gate_runs_in_the_verdict_tick(self):
        r, t, mesh, gl = self.flow(description="Starts at once on the contract.")
        self.say("queued\n", rc=0)
        self.hand_back(r, t)
        self.started(r)
        self.assertEqual(self.runs(), [f"56 {PROJECT} {SHA}"])
        self.assertIn("runs now", mesh.declared[0][1]["note"])

    def test_ship_in_review_is_held_the_same_way(self):
        r, t, mesh, gl = self.flow()
        self.verdict(r, t)
        a = self.joined(r, self.parked(r))
        self.assertEqual((a.mode, len(mesh.declared), self.runs()), ("review", 1, []))
        a = self.held(r, a)

    def test_an_order_broken_at_the_verdict_is_declared_and_wakes_the_author(self):
        r, t, mesh, gl = self.flow(pred=dict(PRED, status_category="cancelled"))
        self.hand_back(r, t)
        a = self.joined(r, self.parked(r))
        self.assertIn("the merge order cannot be met (#00000002 was cancelled", mesh.declared[0][1]["note"])
        a = self.check(r, a)
        self.assertEqual((a.state, self.runs()), ("ready", []))
        self.assertIn("merge order: #00000002 was cancelled before its code was merged", a.wake_text)

    def test_an_order_met_at_the_verdict_runs_the_gate_at_once(self):
        r, t, mesh, gl = self.flow(pred_links=[dict(pr(API_URL), status="merged")],
                                   rows={API: mr_row("merged", sha="c" * 40)})
        self.say("queued\n", rc=0)
        self.hand_back(r, t)
        self.started(r)
        self.assertEqual(self.runs(), [f"56 {PROJECT} {SHA}"])


class Predecessor(OrderBench):
    def wait_on(self, description=ORDER, pred=PRED, pred_links=(), rows=None):
        """The author's own ``wait_for on=merge`` on its card (no SHIP path)."""
        t = task(1, description=description)
        mesh, gl = FakeMesh([t, dict(pred)]), FakeGitLab()
        mesh.links = {t["id"]: [pr()], pred["id"]: list(pred_links)}
        gl.mr_rows.update({TARGET: mr_row(), **(rows or {})})
        r = self.runner(mesh, gl)
        r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        r.store.update(a, state="waiting", wait={"kind": "merge", "target": TARGET,
                                                 "merge": {"sha": SHA, "runs": 0}, "since": iso(), "started": iso()})
        return r, a, t, mesh, gl

    def test_the_authors_own_merge_wait_is_held_too(self):
        r, a, t, mesh, gl = self.wait_on()
        a = self.held(r, a)
        self.merge_pred(mesh, gl)
        self.check(r, a)
        self.assertEqual(self.runs(), [f"56 {PROJECT} {SHA}"])

    def test_merged_and_closed_requests_meet_it_an_open_one_does_not(self):
        r, a, t, mesh, gl = self.wait_on(
            pred_links=[dict(pr(API_URL), status="merged"),
                        dict(pr("https://git.example/g/api/-/merge_requests/10"), status="open")],
            rows={API: mr_row("merged"), "g/api!10": mr_row()})
        a = self.held(r, a)
        gl.mr_rows["g/api!10"] = mr_row("closed")                  # GitLab decides, not the Mesh link state
        self.check(r, a)
        self.assertEqual(len(self.runs()), 1)

    def test_mesh_saying_merged_while_gitlab_says_open_holds(self):
        r, a, t, mesh, gl = self.wait_on(pred_links=[dict(pr(API_URL), status="merged")], rows={API: mr_row()})
        a = self.held(r, a)

    def test_a_done_predecessor_meets_it(self):
        r, a, t, mesh, gl = self.wait_on(pred=dict(PRED, status_id="s-done"))
        self.check(r, a)
        self.assertEqual(len(self.runs()), 1)

    def test_a_cancelled_predecessor_wakes_the_author_and_merges_nothing(self):
        r, a, t, mesh, gl = self.wait_on(pred=dict(PRED, status_category="cancelled"))
        a = self.check(r, a)
        self.assertEqual(a.state, "ready")
        self.assertIn("merge order", a.wake_text)
        self.assertIn("#00000002 was cancelled before its code was merged", a.wake_text)
        self.assertEqual(self.runs(), [])

    def test_a_predecessor_mesh_does_not_find_holds_then_wakes(self):
        r, a, t, mesh, gl = self.wait_on(description="merge-after: #0000beef")   # no such card
        a = self.held(r, a)
        self.assertIn("#0000beef is not found (Mesh 404)", a.wait["merge"]["order"])
        a = self.held(r, a)
        a = self.check(r, a)                                       # the third check: the author is told
        self.assertEqual((a.state, self.runs()), ("ready", []))
        self.assertIn("merge order: #0000beef is not found (Mesh 404)", a.wake_text)

    def test_an_unreadable_predecessor_holds(self):
        r, a, t, mesh, gl = self.wait_on(description=f"merge-after: {PRED['id']}")
        mesh.broken_tasks.add(PRED["id"])                          # Mesh 500: transient, never assumed met
        for _ in range(4):
            a = self.held(r, a)
        self.assertIn("#00000002 cannot be read (Mesh 500)", a.wait["merge"]["order"])
        mesh.broken_tasks.discard(PRED["id"])
        self.merge_pred(mesh, gl)
        self.check(r, a)
        self.assertEqual(len(self.runs()), 1)

    def test_a_line_naming_nothing_wakes_the_author(self):
        r, a, t, mesh, gl = self.wait_on(description="- merge-after: !2011")
        a = self.check(r, a)
        self.assertEqual((a.state, self.runs()), ("ready", []))
        self.assertIn("the line `merge-after: !2011` names no card", a.wake_text)

    def test_an_empty_line_wakes_the_author(self):
        """Review of 8a743ff: an exact empty `merge-after:` was not read at all and the gate ran."""
        r, a, t, mesh, gl = self.wait_on(description="Front.\nmerge-after:\n")
        a = self.check(r, a)
        self.assertEqual((a.state, self.runs()), ("ready", []))
        self.assertIn("the line `merge-after: ` names no card", a.wake_text)

    def test_a_link_this_gitlab_does_not_hold_is_not_taken_on_mesh_word(self):
        r, a, t, mesh, gl = self.wait_on(pred_links=[dict(pr("https://github.example/a/api/pull/3"), status="merged")])
        a = self.held(r, a)
        self.assertIn("not on this GitLab", a.wait["merge"]["order"])

    def test_a_merge_request_url_in_the_line(self):
        r, a, t, mesh, gl = self.wait_on(description=f"merge-after: {API_URL}", rows={API: mr_row()})
        a = self.held(r, a)
        gl.mr_rows[API] = mr_row("merged", sha="c" * 40)
        self.check(r, a)
        self.assertEqual(len(self.runs()), 1)

    def test_a_card_without_a_description_runs_the_gate(self):
        r, a, t, mesh, gl = self.wait_on()
        del mesh.tasks[t["id"]]["description"]                     # Mesh leaves an empty one out
        mesh.tasks[t["id"]]["has_description"] = False
        self.check(r, a)
        self.assertEqual(len(self.runs()), 1)

    def test_a_description_not_sent_holds(self):
        r, a, t, mesh, gl = self.wait_on()
        del mesh.tasks[t["id"]]["description"]
        mesh.tasks[t["id"]]["has_description"] = True              # it has one, it was not sent
        a = self.held(r, a)
        a = self.held(r, a)
        a = self.check(r, a)                                       # still not sent: the author is told
        self.assertEqual((a.state, self.runs()), ("ready", []))
        self.assertIn("cannot read the card", a.wake_text)

    def test_the_authors_card_unreadable_holds_then_wakes(self):
        r, a, t, mesh, gl = self.wait_on()
        mesh.broken_tasks.add(t["id"])
        m = {"sha": SHA, "runs": 0}
        self.assertIsNone(r._merge_order_hold(a, m))               # transient: the wait stands
        self.assertIsNone(r._merge_order_hold(a, m))
        text = r._merge_order_hold(a, m)                           # still unreadable: the author is told
        self.assertIn("merge order", text)
        self.assertIn("did not run", text)
        mesh.broken_tasks.discard(t["id"])
        m = {"sha": SHA, "runs": 0, "order_errors": 2}
        self.assertIsNone(r._merge_order_hold(a, m))               # readable again: held by the predecessor
        self.assertNotIn("order_errors", m)
        self.assertIn("#00000002 has no merge request yet", m["order"])

    def test_a_named_merge_request(self):
        r, a, t, mesh, gl = self.wait_on(description=f"merge-after: {API}", rows={API: mr_row()})
        a = self.held(r, a)
        gl.mr_rows[API] = mr_row("closed")
        a = self.check(r, a)
        self.assertEqual((a.state, self.runs()), ("ready", []))
        self.assertIn(f"{API} was closed without merge", a.wake_text)

    def test_a_line_added_while_waiting_holds_the_next_run(self):
        r, a, t, mesh, gl = self.wait_on(description="")
        self.say("\u23f3 WAIT pipeline:g/app#77 \u2014 behind it\n", rc=0)   # queued: the wait stands
        a = self.check(r, self.check(r, a))                        # the run, then its result read
        self.assertEqual((a.state, len(self.runs())), ("waiting", 1))
        mesh.tasks[t["id"]]["description"] = ORDER
        r.store.update(a, wait={**a.wait, "merge": {**a.wait["merge"], "last_run": "2000-01-01T00:00:00+00:00"}})
        a = self.check(r, r.store.get(a.id))                       # a re-run is due, the order holds it
        self.assertEqual((a.state, len(self.runs())), ("waiting", 1), a.wake_text)
        self.assertIn("#00000002 has no merge request yet", a.wait["merge"]["order"])


class AuthorsOwnMrWait(OrderBench):
    def test_the_authors_wait_on_another_mr_is_carried_into_the_merge_wait(self):
        r, t, mesh, gl = self.flow(description="", rows={API: mr_row(sha="c" * 40)})
        mesh.waits = {t["id"]: {"id": "wait-own", "task_id": t["id"], "agent_id": AGENT_ID, "kind": "mr",
                                "project_path": "g/api", "mr_iid": 9, "until": "merged",
                                "after_sha": "c" * 40, "after_status": "opened", "created_at": iso()}}
        self.say("merged ok\n", rc=0)
        self.verdict(r, t)
        a = self.joined(r, self.parked(r))
        self.assertEqual(a.wait["merge"].get("after"), [f"mr:{API}"])
        self.assertEqual(self.runs(), [])
        self.assertIn(f"{API} is not merged yet", mesh.declared[0][1]["note"])
        del mesh.waits[t["id"]]                                    # Mesh replaced it with the merge wait
        a = self.held(r, a)
        gl.mr_rows[API] = mr_row("merged", sha="c" * 40)
        self.check(r, a)
        self.assertEqual(self.runs(), [f"56 {PROJECT} {SHA}"])

    def own_wait(self, t, **extra):
        return {t["id"]: {"id": "wait-own", "task_id": t["id"], "agent_id": AGENT_ID, "kind": "mr",
                          "project_path": "g/api", "mr_iid": 9, "until": "merged", "created_at": iso(), **extra}}

    def carried(self, row=None, raises=False, **extra):
        """Review of 8a743ff: the author's `on=mr until=merged` without after_sha/after_status on a merge
        request GitLab does not answer for lost its predicate in the default baseline, so nothing was
        carried and the gate ran."""
        r, t, mesh, gl = self.flow(description="", rows={} if row is None else {API: row})
        if raises:
            real = gl.get_mr
            def get_mr(target):
                if target == API:
                    raise OSError("gitlab read failed")
                return real(target)
            gl.get_mr = get_mr
        mesh.waits = self.own_wait(t, **extra)
        self.say("merged ok\n", rc=0)
        self.verdict(r, t)
        a = self.joined(r, self.parked(r))
        self.assertEqual(a.wait["merge"].get("after"), [f"mr:{API}"])
        self.assertEqual(self.runs(), [])
        del mesh.waits[t["id"]]                                    # Mesh replaced it with the merge wait
        return r, a, mesh, gl

    def released(self, r, a, gl):
        a = self.held(r, a)
        gl.get_mr = lambda target: dict(gl.mr_rows.get(target, {}))
        gl.mr_rows[API] = mr_row("merged", sha="c" * 40)
        self.check(r, a)
        self.assertEqual(self.runs(), [f"56 {PROJECT} {SHA}"])

    def test_no_baseline_and_the_mr_read_raises(self):
        r, a, mesh, gl = self.carried(raises=True)
        self.released(r, a, gl)

    def test_no_baseline_and_an_empty_mr_row(self):
        r, a, mesh, gl = self.carried()
        self.released(r, a, gl)

    def test_no_baseline_and_the_mr_closed_meanwhile(self):
        r, a, mesh, gl = self.carried(row=mr_row("closed"))
        a = self.check(r, a)                                       # can no longer be met: the author is told
        self.assertEqual((a.state, self.runs()), ("ready", []))
        self.assertIn(f"{API} was closed without merge", a.wake_text)

    def test_a_partial_baseline_kept_by_mesh(self):
        r, a, mesh, gl = self.carried(row=mr_row(sha="c" * 40), after_sha="c" * 40)
        self.released(r, a, gl)

    def test_a_merge_wait_on_an_unnamed_mr_declares_nothing(self):
        r, t, mesh, gl = self.flow(description="")
        mesh.waits = self.own_wait(t, project_path="", mr_iid=0)
        self.say("merged ok\n", rc=0)
        self.verdict(r, t)
        self.settle_all(r)
        self.assertEqual((getattr(mesh, "declared", []), self.runs()), ([], []))

    def test_the_authors_wait_unreadable_declares_nothing(self):
        """The merge wait replaces the author's wait in Mesh: one never read is never replaced. The runner
        declares nothing and runs no gate; the verdict goes the ordinary way (the author's session reads it),
        as when Mesh refuses the declaration."""
        r, t, mesh, gl = self.flow(description="")
        mesh.waits = {t["id"]: {"id": "wait-own", "task_id": t["id"], "agent_id": AGENT_ID, "kind": "mr",
                                "project_path": "g/api", "mr_iid": 9, "until": "merged", "created_at": iso()}}
        mesh.wait_error = MeshError(500, "wait read failed")
        self.say("merged ok\n", rc=0)
        self.verdict(r, t)
        self.settle_all(r)
        self.assertEqual((getattr(mesh, "declared", []), self.runs()), ([], []))
        self.assertTrue(self.prompts()[1].startswith("[runner] Review #00000001"))   # the author reads it

    def test_a_wait_on_the_cards_own_mr_carries_nothing(self):
        r, t, mesh, gl = self.flow(description="")
        mesh.waits = {t["id"]: {"id": "wait-own", "task_id": t["id"], "agent_id": AGENT_ID, "kind": "mr",
                                "project_path": PROJECT, "mr_iid": 56, "until": "merged",
                                "after_sha": SHA, "after_status": "opened", "created_at": iso()}}
        self.say("queued\n", rc=0)
        self.verdict(r, t)
        a = self.started(r)
        self.assertNotIn("after", a.wait["merge"])
        self.assertEqual(len(self.runs()), 1)


if __name__ == "__main__":
    unittest.main()
