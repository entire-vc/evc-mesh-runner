"""Opt-in reviewer merge-only handoff: dry-run, one card, default flow untouched, gate fails closed."""
import io
import json
from unittest import mock

from fakes import FakeGitLab, FakeMesh, task
from test_merge_wait import MergeBench, SHA, TARGET, mr_row

from mesh_runner import reviewer_merge as rm

PROJECT, IID = "g/app", "56"
OTHER = "b" * 40
SELECTION = {"lane": "Reviewer-Codex", "reason": "cross-family", "username": "reviewer-codex",
             "author_family": "claude"}


def source(**extra):
    return task(1, cat="in_progress", assignee_name="Linus", **extra)


def mr(**over):
    row = mr_row(labels=["verified-by:codex-review"])
    row.update(over)
    return row


def select(_argv, *, project_id, author):
    return dict(SELECTION)


def ask(mesh, gl, apply=False, sel=select, sha=SHA, ids=None):
    s = next(iter(mesh.tasks.values()))
    return rm.request(mesh=mesh, gitlab=gl, source_id=s["id"], target=TARGET, sha=sha, selector_argv=["x"],
                      agent_ids={"Reviewer-Codex": "agent-rc"} if ids is None else ids, apply=apply, selector=sel)


def world(src=None, row=None):
    mesh = FakeMesh([src or source()])
    gl = FakeGitLab()
    gl.mr_rows[TARGET] = row or mr()
    return mesh, gl


class Request(MergeBench):
    def test_dry_run_reads_only(self):
        mesh, gl = world()
        before = json.dumps(mesh.tasks, sort_keys=True)
        out = ask(mesh, gl)
        self.assertTrue(out["ready"])
        self.assertEqual((out["mode"], out["created"], out["card"]), ("dry-run", False, None))
        self.assertEqual(out["request"]["reviewer_username"], "reviewer-codex")
        self.assertEqual(mesh.created, [])
        self.assertEqual(json.dumps(mesh.tasks, sort_keys=True), before)

    def test_apply_creates_one_card_for_the_reviewer_and_leaves_the_product_card(self):
        mesh, gl = world()
        s = next(iter(mesh.tasks.values()))
        before = dict(s)
        out = ask(mesh, gl, apply=True)
        self.assertTrue(out["created"])
        card = mesh.tasks[out["card"]]
        self.assertEqual(card["assignee_id"], "agent-rc")
        self.assertIn(rm.LABEL, card["labels"])
        self.assertIn(f"⏳ WAIT merge:{TARGET} after={SHA}", card["description"])
        self.assertEqual(rm.parse_request(card["description"])["sha"], SHA)
        self.assertEqual(mesh.tasks[s["id"]], before)           # assignee, status, labels: untouched

    def test_second_apply_and_restart_make_no_duplicate(self):
        mesh, gl = world()
        first = ask(mesh, gl, apply=True)
        again = ask(mesh, gl, apply=True)
        self.assertFalse(again["created"])
        self.assertEqual((again["card"], len(mesh.created)), (first["card"], 1))

    def test_retry_repairs_a_card_left_unassigned(self):
        mesh, gl = world()
        first = ask(mesh, gl, apply=True)
        mesh.tasks[first["card"]]["assignee_id"] = None         # the assign call of the first apply never landed
        again = ask(mesh, gl, apply=True)
        self.assertEqual((again["card"], again.get("repaired"), len(mesh.created)), (first["card"], True, 1))
        self.assertEqual(mesh.tasks[first["card"]]["assignee_id"], "agent-rc")

    def test_concurrent_applies_make_one_card(self):
        import tempfile
        import threading
        import time
        mesh, gl = world()
        lock = tempfile.mkdtemp(dir=self.root)
        create = mesh.create_task

        def slow_create(pid, fields):
            time.sleep(0.2)                                     # widens the find-then-create window
            return create(pid, fields)
        mesh.create_task = slow_create
        s = next(iter(mesh.tasks.values()))
        outs = []

        def go():
            outs.append(rm.request(mesh=mesh, gitlab=gl, source_id=s["id"], target=TARGET, sha=SHA,
                                   selector_argv=["x"], agent_ids={"Reviewer-Codex": "agent-rc"}, apply=True,
                                   selector=select, lock_dir=lock))
        ts = [threading.Thread(target=go) for _ in range(3)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual((len(mesh.created), sorted(o["created"] for o in outs)), (1, [False, False, True]))

    def test_a_cross_host_twin_is_reported(self):
        mesh, gl = world()
        find = mesh.find_tasks
        calls = []

        def racing_find(pid, key):
            got = find(pid, key)
            if calls:                                           # after our create: a twin from another host appears
                twin = dict(mesh.created[0], id="twin-card")
                got = got + [twin]
            calls.append(1)
            return got
        mesh.find_tasks = racing_find
        out = ask(mesh, gl, apply=True)
        self.assertEqual((out["created"], out["duplicates"]), (True, ["twin-card"]))

    def test_retry_reports_a_preexisting_twin(self):
        mesh, gl = world()
        first = ask(mesh, gl, apply=True)
        twin = dict(mesh.tasks[first["card"]], id="twin-card")
        mesh.tasks["twin-card"] = twin
        again = ask(mesh, gl, apply=True)
        self.assertFalse(again["created"])
        self.assertEqual(len(again["duplicates"]), 1)

    def test_dry_run_is_not_ready_for_a_lane_this_runner_does_not_know(self):
        mesh, gl = world()
        out = ask(mesh, gl, ids={"someone-else": "x"})
        self.assertFalse(out["ready"])
        self.assertIn("reviewer-agent-id-unknown", out["refusals"])

    def test_router_username_that_the_lane_config_contradicts_is_not_ready(self):
        mesh, gl = world()
        s = next(iter(mesh.tasks.values()))
        out = rm.request(mesh=mesh, gitlab=gl, source_id=s["id"], target=TARGET, sha=SHA, selector_argv=["x"],
                         agent_ids={"Reviewer-Codex": "agent-rc"}, usernames={"Reviewer-Codex": "other-user"},
                         apply=True, selector=select)
        self.assertFalse(out["ready"])
        self.assertIn("reviewer-username-unknown", out["refusals"])
        self.assertEqual(mesh.created, [])

    def test_configured_username_fills_a_missing_one(self):
        mesh, gl = world()
        s = next(iter(mesh.tasks.values()))
        out = rm.request(mesh=mesh, gitlab=gl, source_id=s["id"], target=TARGET, sha=SHA, selector_argv=["x"],
                         agent_ids={"Reviewer-Codex": "agent-rc"}, usernames={"reviewer-codex": "rc-user"},
                         selector=lambda *a, **k: {**SELECTION, "username": ""})
        self.assertEqual(out["request"]["reviewer_username"], "rc-user")

    def test_retry_never_moves_a_card_to_a_different_reviewer(self):
        mesh, gl = world()
        first = ask(mesh, gl, apply=True)
        sel2 = lambda *a, **k: {**SELECTION, "lane": "Reviewer-Claude", "username": "reviewer-claude"}   # noqa: E731
        again = ask(mesh, gl, apply=True, sel=sel2, ids={"Reviewer-Claude": "agent-rcl"})
        self.assertFalse(again["ready"])
        self.assertTrue(again["refusals"][0].startswith("reviewer-changed:Reviewer-Codex->Reviewer-Claude"))
        self.assertEqual(mesh.tasks[first["card"]]["assignee_id"], "agent-rc")      # untouched
        self.assertEqual(len(mesh.created), 1)

    def test_new_head_is_a_new_key(self):
        a = rm.request_key("t", PROJECT, IID, SHA)
        self.assertNotEqual(a, rm.request_key("t", PROJECT, IID, OTHER))
        self.assertEqual(a, rm.request_key("t", PROJECT, IID, SHA))

    def refused(self, why, **kw):
        mesh, gl = world(kw.pop("src", None), kw.pop("row", None))
        out = ask(mesh, gl, apply=True, **kw)
        self.assertFalse(out["ready"])
        self.assertTrue(any(r.startswith(why) for r in out["refusals"]), out["refusals"])
        self.assertEqual(mesh.created, [])                       # fail closed: nothing was made

    def test_head_drift(self):
        self.refused("head-drift", row=mr(sha=OTHER))

    def test_short_sha(self):
        self.refused("expected-sha-not-full", sha=SHA[:12])

    def test_hold_label(self):
        self.refused("hold-label", row=mr(labels=["verified-by:codex-review", "hold"]))

    def test_no_ship_label(self):
        self.refused("no-ship-label", row=mr(labels=[]))

    def test_red_ci(self):
        self.refused("ci:failed", row=mr(head_pipeline={"id": 1, "sha": SHA, "status": "failed"}))

    def test_merged_mr(self):
        self.refused("mr-not-open", row=mr(state="merged"))

    def test_explicit_verify_label_is_not_bypassed(self):
        self.refused("explicit-verify", src=source(labels=["verify:garfield"]))

    def test_human_gate(self):
        self.refused("human-gate", src=source(human_gate=True))

    def test_unknown_pools_close_the_handoff(self):
        self.refused("no-reviewer:unknown: pools", sel=lambda *a, **k: {"lane": None, "reason": "unknown: pools"})

    def test_malformed_router_answer_fails_closed(self):
        for bad in ({"lane": ["x"], "reason": "cross-family", "username": "u"},
                    {"lane": "l", "reason": ["cross-family"], "username": "u"},
                    {"lane": "l", "reason": "cross-family", "username": 7},
                    {"lane": 5, "reason": "cross-family", "username": "u"}):
            with self.subTest(bad=bad):
                self.refused("", sel=lambda *a, _b=bad, **k: _b)

    def test_author_is_never_the_reviewer(self):
        self.refused("reviewer-is-author", sel=lambda *a, **k: {**SELECTION, "lane": "linus"})

    def test_missing_selector_refuses(self):
        mesh, gl = world()
        s = next(iter(mesh.tasks.values()))
        with self.assertRaises(rm.Refused):
            rm.request(mesh=mesh, gitlab=gl, source_id=s["id"], target=TARGET, sha=SHA, selector_argv=[],
                       agent_ids={})

    def test_reviewer_without_agent_id_creates_nothing(self):
        mesh, gl = world()
        out = ask(mesh, gl, apply=True, ids={})
        self.assertFalse(out["ready"])
        self.assertEqual(mesh.created, [])


class Identity(MergeBench):
    REQ = {"reviewer_username": "reviewer-codex"}

    def opener(self, user):
        return lambda req, timeout: io.BytesIO(json.dumps({"username": user}).encode())

    def test_match(self):
        rm.check_identity({"GITLAB_TOKEN": "t"}, self.REQ, "https://git.test", opener=self.opener("Reviewer-Codex"))

    def test_wrong_user_missing_token_and_unreadable(self):
        with self.assertRaisesRegex(rm.Refused, "identity-mismatch"):
            rm.check_identity({"GITLAB_TOKEN": "t"}, self.REQ, "https://git.test", opener=self.opener("linus"))
        with self.assertRaisesRegex(rm.Refused, "identity-missing"):
            rm.check_identity({}, self.REQ, "https://git.test", opener=self.opener("x"))

        def boom(req, timeout):
            raise OSError("down")
        with self.assertRaisesRegex(rm.Refused, "identity-unreadable"):
            rm.check_identity({"GITLAB_TOKEN": "t"}, self.REQ, "https://git.test", opener=boom)

    def test_secret_never_in_the_refusal(self):
        try:
            rm.check_identity({"GITLAB_TOKEN": "s3cr3t-token"}, self.REQ, "https://git.test", opener=self.opener("linus"))
        except rm.Refused as e:
            self.assertNotIn("s3cr3t", str(e))


class Gate(MergeBench):
    """The runner side: merge-only cards run the gate only on the named head, under the named lane."""

    def merge_only(self, lane=None, sha=SHA, label=True, block=True):
        r, a, t, gl = self.waiting()
        req = {"key": "rm-" + "1" * 12, "source_task": "00000001-0000-4000-8000-000000000001", "project": PROJECT, "iid": IID, "sha": sha,
               "reviewer_lane": lane or self.agent.name, "reviewer_username": "reviewer-codex"}
        r.mesh.tasks[t["id"]]["labels"] = [rm.LABEL] if label else []
        r.mesh.tasks[t["id"]]["description"] = (f"<!-- reviewer-merge:v1 {json.dumps(req)} -->" if block else "x")
        r.cfg.gitlab_url = "https://git.test"
        r.agent.gitlab_username = "reviewer-codex"
        return r, a

    def test_ordinary_card_is_unchanged(self):
        r, a = self.merge_only(label=False, block=False)
        self.check(r, a)
        self.assertEqual(len(self.runs()), 1)

    def test_matching_identity_runs_the_gate(self):
        r, a = self.merge_only()
        with mock.patch.object(rm, "check_identity") as ci:
            self.check(r, a)
        ci.assert_called_once()
        self.assertEqual(len(self.runs()), 1)

    def test_wrong_identity_does_not_run_the_gate(self):
        r, a = self.merge_only()
        with mock.patch.object(rm, "check_identity", side_effect=rm.Refused("identity-mismatch:linus!=x")):
            a = self.check(r, a)
        self.assertEqual(self.runs(), [])
        self.assertIn("identity check failed", a.wake_text)

    def not_run(self, **kw):
        r, a = self.merge_only(**kw)
        with mock.patch.object(rm, "check_identity"):
            a = self.check(r, a)
        self.assertEqual(self.runs(), [])
        self.assertIn("not merging", a.wake_text)

    def test_other_lane_does_not_run(self):
        self.not_run(lane="somebody-else")

    def test_head_mismatch_does_not_run(self):
        self.not_run(sha=OTHER)

    def test_corrupt_block_does_not_run(self):
        self.not_run(block=False)

    def test_unreadable_card_defers_an_ordinary_wait_instead_of_refusing_it(self):
        r, a = self.merge_only(label=False, block=False)
        m = a.wait["merge"]
        r.mesh.broken_tasks.add(a.task_id)
        self.assertIsNone(r._merge_only_refusal(a, a.wait, m, {}))        # deferred, not a refusal text
        r.mesh.broken_tasks.clear()
        self.assertEqual(r._merge_only_refusal(a, a.wait, m, {}), "")     # ordinary card: runs
        self.assertNotIn("task_errors", m)

    def test_card_that_stays_unreadable_wakes_the_agent(self):
        r, a = self.merge_only(label=False, block=False)
        m = a.wait["merge"]
        r.mesh.broken_tasks.add(a.task_id)
        got = [r._merge_only_refusal(a, a.wait, m, {}) for _ in range(5)]
        self.assertIn("not merging", got[-1] or "")

    def test_malformed_card_answer_never_counts_as_an_ordinary_card(self):
        r, a = self.merge_only()
        m = a.wait["merge"]
        for bad in (None, {}, {"id": "someone-else"}, []):
            m.pop("task_errors", None)
            with self.subTest(bad=bad), mock.patch.object(r.mesh, "task", return_value=bad):
                self.assertIsNone(r._merge_only_refusal(a, a.wait, m, {}))

    def test_malformed_request_fields_are_not_a_request(self):
        good = {"key": "rm-" + "0" * 12, "source_task": "00000001-0000-4000-8000-000000000001", "project": PROJECT,
                "iid": "56", "sha": SHA, "reviewer_lane": "l", "reviewer_username": "u"}
        block = lambda d: f"<!-- reviewer-merge:v1 {json.dumps(d)} -->"      # noqa: E731
        self.assertIsNotNone(rm.parse_request(block(good)))
        for field, bad in (("sha", 12345), ("sha", "abc"), ("iid", 56), ("iid", "0"), ("key", "x"),
                           ("source_task", "s" * 8), ("project", "nogroup"), ("reviewer_lane", 7)):
            with self.subTest(field=field, bad=bad):
                self.assertIsNone(rm.parse_request(block({**good, field: bad})))

    def test_block_without_the_label_does_not_run(self):
        self.not_run(label=False, block=True)

    def test_card_copy_of_the_username_is_not_trusted(self):
        r, a = self.merge_only()
        r.agent.gitlab_username = "someone-else"                    # the lane's config differs from the card
        with mock.patch.object(rm, "check_identity") as ci:
            a = self.check(r, a)
        ci.assert_not_called()
        self.assertEqual(self.runs(), [])
        self.assertIn("configured one", a.wake_text)

    def test_identity_is_checked_against_the_configured_username(self):
        r, a = self.merge_only()
        with mock.patch.object(rm, "check_identity") as ci:
            self.check(r, a)
        self.assertEqual(ci.call_args.args[1]["reviewer_username"], "reviewer-codex")

    def test_partial_card_answer_is_unreadable_not_ordinary(self):
        r, a = self.merge_only()
        m = a.wait["merge"]
        for bad in ({"id": a.task_id}, {"id": a.task_id, "labels": []}, {"id": a.task_id, "description": ""},
                    {"id": a.task_id, "labels": "x", "description": ""}):
            m.pop("task_errors", None)
            with self.subTest(bad=bad), mock.patch.object(r.mesh, "task", return_value=bad):
                self.assertIsNone(r._merge_only_refusal(a, a.wait, m, {}))
