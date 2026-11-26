"""A reviewer's SHIP on the head of the card's one open merge request: the runner declares the author's
``wait_for on=merge`` itself, the merge gate runs from that wait, and the author's session starts only on
the gate's answer. The same holds whether the card stays in review or the reviewer hands it back to the
author (todo). Anything else (the author's own verdict, no commit named, two open MRs, a draft, a refused
declaration, the merge gate off, someone else speaking after the verdict, a DO-NOT-SHIP) keeps the ordinary
path. FakeMesh and FakeGitLab are the external boundaries; Runner, Store and the merge gate command are real."""
import datetime as dt

from fakes import AGENT_ID, FakeGitLab, FakeMesh, calls, task, write_replies
from mesh_runner.mesh import MeshError
from test_merge_wait import MergeBench, OTHER, PROJECT, SHA, TARGET, mr_row

MR_URL = "https://git.example/g/app/-/merge_requests/56"
MR2_URL = "https://git.example/g/app/-/merge_requests/57"
TARGET2 = f"{PROJECT}!57"


def pr(url=MR_URL):
    return {"link_type": "pr", "url": url, "status": "open", "created_at": "2026-01-01T00:00:00Z"}


def soon(seconds=1):
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)


class ShipBench(MergeBench):
    def flow(self, links=None, rows=None):
        """The author's work turn ended in review; nobody has answered yet."""
        t = task(1)
        mesh, gl = FakeMesh([t], max_concurrent=1), FakeGitLab()
        mesh.links = {t["id"]: [pr()] if links is None else links}
        gl.mr_rows.update({TARGET: mr_row()} if rows is None else rows)
        r = self.runner(mesh, gl)
        write_replies(self.replies, {t["id"][:8]: "SLEEP:1:done"})
        r.tick()                                                   # the work turn
        mesh.add_comment(t["id"], "done; sent to review", author=AGENT_ID)
        mesh.set_category(t["id"], "review")
        self.settle_all(r)
        self.assertEqual(len(self.prompts()), 1)
        return r, t, mesh, gl

    def verdict(self, r, t, line=f"Verify: SHIP @{SHA}", by="agent-reviewer"):
        r.mesh.add_comment(t["id"], line, author=by, at=soon())
        r._hold_cache.clear()
        r.tick()

    def hand_back(self, r, t, line=f"Verify: SHIP @{SHA}", by="agent-reviewer", after=()):
        """The reviewer's verdict, then its hand_off: a note and the card back with the author in todo."""
        r.mesh.add_comment(t["id"], line, author=by, at=soon())
        r.mesh.add_comment(t["id"], "Handed over to the author: the head is reviewed.", author=by, at=soon(2))
        for n, (author, body) in enumerate(after):
            r.mesh.add_comment(t["id"], body, author=author, at=soon(3 + n))
        r.mesh.set_category(t["id"], "todo")
        r._hold_cache.clear()
        r.tick()

    def prompts(self):
        return [c["prompt"] for c in calls(self.calls)]

    def work_prompts(self):
        """The author's work session starts (a reprompt of the same session is not a start)."""
        return [p for p in self.prompts() if p.startswith("[runner] Task #") and "Review #" not in p]

    def review_prompts(self):
        return [p for p in self.prompts() if "Review #" in p]

    def parked(self, r):
        rows = r.store.by_state(self.agent.name, ("waiting", "ready"))
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def started(self, r):
        """The parked attempt after the gate run its declaration started in the same tick (joined)."""
        a = self.parked(r)
        job = r._merge_jobs.get(a.id)
        self.assertIsNotNone(job, "the declaring tick started no gate run")
        job["thread"].join(15)
        self.assertIsNotNone(job["result"])
        return r.store.get(a.id)

    def assert_fed(self, r, t):
        """The ordinary path: no declaration, the author's review session starts with the verdict."""
        self.settle_all(r)
        self.assertEqual(getattr(r.mesh, "declared", []), [])
        review = self.review_prompts()
        self.assertEqual(len(review), 1, self.prompts())
        self.assertIn("review verdict SHIP by", review[0])


class ShipMergeWait(ShipBench):
    def test_ship_on_the_head_declares_the_merge_wait_without_a_session(self):
        r, t, mesh, gl = self.flow()
        self.verdict(r, t)
        a = self.parked(r)
        self.assertEqual(len(mesh.declared), 1)
        tid, body, row = mesh.declared[0]
        self.assertEqual(tid, t["id"])
        self.assertEqual({k: body[k] for k in ("on", "project_path", "mr_iid", "after_sha", "session_id")},
                         {"on": "merge", "project_path": PROJECT, "mr_iid": 56, "after_sha": SHA,
                          "session_id": a.id})
        self.assertNotIn(t["id"], mesh.checkouts)                  # Mesh released it with the declaration
        self.assertEqual(a.checkout, {})
        self.assertEqual((a.mode, a.turns, a.session_id, a.pid), ("review", 0, "", 0))
        self.assertEqual((a.wait["kind"], a.wait["target"], a.wait["merge"]["sha"]), ("merge", TARGET, SHA))
        self.assertEqual((a.wait["mesh_wait_id"], a.wait["runner_declared"]), (row["id"], "ship"))
        self.assertEqual(self.review_prompts(), [])                # no author session was started
        self.assertEqual(r.store.mark(self.agent.name, t["id"], "review_fed")[0], a.id)
        self.assertNotIn(t["id"], r._review_why)
        self.started(r)
        r.tick()
        self.assertEqual(len(mesh.declared), 1)                    # one declaration per verdict

    def test_the_gate_runs_from_that_wait_and_the_author_starts_on_its_answer(self):
        r, t, mesh, gl = self.flow()
        self.say("merged ok\n", rc=0)
        self.verdict(r, t)
        a = self.started(r)
        self.assertEqual(self.runs(), [f"56 {PROJECT} {SHA}"])     # the gate ran with no author session
        self.assertEqual(self.review_prompts(), [])
        gl.mr_rows[TARGET] = mr_row("merged", merge_commit_sha="c" * 40)
        r._wake_budgeted(r.store.get(a.id))                         # the ordinary observer of a waiting attempt
        a = r.store.get(a.id)
        self.assertEqual(a.state, "ready")
        self.assertIn(f"MR !56 ({PROJECT}) merged at {'c' * 40}", a.wake_text)
        self.assertEqual([x[:3] for x in getattr(mesh, "resolved", [])], [(t["id"], "wait-1", "met")])
        self.settle_all(r)
        review = self.review_prompts()
        self.assertEqual(len(review), 1, self.prompts())           # the author's first session, on the answer
        self.assertIn(f"MR !56 ({PROJECT}) merged at {'c' * 40}", review[0])
        self.assertEqual(len(self.runs()), 1)

    def test_the_gate_starts_in_the_tick_of_the_verdict(self):
        # 17.11 live: the declaration and the first gate run were one tick apart (20 s of 77 s SHIP -> gate)
        r, t, mesh, gl = self.flow()
        self.say("queued\n", rc=0)
        self.verdict(r, t)
        a = self.started(r)
        self.assertEqual(self.runs(), [f"56 {PROJECT} {SHA}"])     # no wake check was needed to start it
        self.assertEqual((a.state, a.wait["merge"]["runs"]), ("waiting", 1))
        self.assertIn("first_run", a.wait["merge"])
        self.assertEqual(self.review_prompts(), [])

    def test_a_closed_mr_wakes_the_author_with_that_answer(self):
        r, t, mesh, gl = self.flow()
        self.say("not merged\n", rc=1)
        self.verdict(r, t)
        self.started(r)                                            # the gate ran once in the verdict tick
        gl.mr_rows[TARGET] = mr_row("closed")
        a = self.check(r, self.parked(r))
        self.assertEqual(a.state, "ready")
        self.assertIn(f"MR {TARGET} was closed without merge", a.wake_text)
        self.assertEqual(self.runs(), [f"56 {PROJECT} {SHA}"])     # and not again on the closed MR
        self.settle_all(r)
        review = self.review_prompts()
        self.assertEqual(len(review), 1, self.prompts())
        self.assertIn("was closed without merge", review[0])

    def test_the_authors_own_verdict_keeps_the_ordinary_feed(self):
        r, t, mesh, gl = self.flow()
        self.verdict(r, t, by=AGENT_ID)
        self.assert_fed(r, t)

    def test_a_verdict_naming_no_commit_keeps_the_ordinary_feed(self):
        r, t, mesh, gl = self.flow()
        self.verdict(r, t, line="Verify: SHIP")
        self.assert_fed(r, t)

    def test_two_open_merge_requests_keep_the_ordinary_feed(self):
        r, t, mesh, gl = self.flow(links=[pr(), pr(MR2_URL)], rows={TARGET: mr_row(), TARGET2: mr_row(sha=OTHER)})
        self.verdict(r, t)
        self.assert_fed(r, t)

    def test_a_draft_keeps_the_ordinary_feed(self):
        r, t, mesh, gl = self.flow(rows={TARGET: mr_row(draft=True)})
        self.verdict(r, t)
        self.assert_fed(r, t)

    def test_merge_gate_off_keeps_the_ordinary_feed(self):
        self.cfg.merge_command = ()
        r, t, mesh, gl = self.flow()
        self.verdict(r, t)
        self.assert_fed(r, t)

    def test_a_refused_declaration_aborts_once_and_feeds_the_author(self):
        r, t, mesh, gl = self.flow()
        mesh.wait_for_error = MeshError(409, "not the checkout holder")
        self.verdict(r, t)
        self.settle_all(r)
        aborted = r.store.by_state(self.agent.name, ("aborted",))
        self.assertEqual(len(aborted), 1)
        self.assertIn("runner merge wait refused", aborted[0].note)
        self.assertNotIn(t["id"], mesh.checkouts)
        review = self.review_prompts()
        self.assertEqual(len(review), 1, self.prompts())
        self.assertIn("review verdict SHIP by agent-reviewer", review[0])

    def test_a_dry_run_declares_nothing(self):
        r, t, mesh, gl = self.flow()
        mesh.add_comment(t["id"], f"Verify: SHIP @{SHA}", author="agent-reviewer", at=soon())
        r._hold_cache.clear()
        self.assertEqual(r.hold_reason(dict(mesh.tasks[t["id"]], _mode="review"), "review", log_change=False), "")
        self.assertEqual(getattr(mesh, "declared", []), [])
        self.assertEqual(r.store.by_state(self.agent.name, ("waiting", "ready", "starting")), [])


class HandedBack(ShipBench):
    """The reviewer hands the card back (todo) after SHIP: the work start would open the author's session
    only to declare the merge wait. The runner declares it instead."""

    def test_ship_then_todo_declares_the_merge_wait_without_a_session(self):
        r, t, mesh, gl = self.flow()
        self.hand_back(r, t)
        a = self.parked(r)
        self.assertEqual(len(mesh.declared), 1)
        tid, body, row = mesh.declared[0]
        self.assertEqual({k: body[k] for k in ("on", "project_path", "mr_iid", "after_sha", "session_id")},
                         {"on": "merge", "project_path": PROJECT, "mr_iid": 56, "after_sha": SHA,
                          "session_id": a.id})
        self.assertNotIn(t["id"], mesh.checkouts)
        self.assertEqual((a.mode, a.state, a.turns, a.session_id, a.pid), ("work", "waiting", 0, "", 0))
        self.assertEqual((a.wait["kind"], a.wait["target"], a.wait["runner_declared"]), ("merge", TARGET, "ship"))
        self.assertEqual(len(self.prompts()), 1)                    # only the author's original work turn
        self.assertIn("informed_start", [kind for _, kind, _ in r.store.events(a.id)])
        self.started(r)
        r.tick()
        self.assertEqual(len(mesh.declared), 1)                     # one declaration per verdict

    def test_the_gate_answer_starts_the_authors_work_session(self):
        r, t, mesh, gl = self.flow()
        self.say("merged ok\n", rc=0)
        self.hand_back(r, t)
        a = self.started(r)                                         # in the tick of the hand-back
        self.assertEqual(self.runs(), [f"56 {PROJECT} {SHA}"])      # the gate ran with no author session
        self.assertEqual(len(self.prompts()), 1)
        gl.mr_rows[TARGET] = mr_row("merged", merge_commit_sha="c" * 40)
        r._wake_budgeted(r.store.get(a.id))
        self.assertEqual(r.store.get(a.id).state, "ready")
        self.settle_all(r)
        work = self.work_prompts()
        self.assertEqual(len(work), 2, self.prompts())
        self.assertIn(f"MR !56 ({PROJECT}) merged at {'c' * 40}", work[-1])
        self.assertIn("the merge gate ran without you", work[-1])
        self.assertEqual(self.review_prompts(), [])
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(r.store.starts_today(self.agent.name, t["id"]), 1)   # the woken start is news
        r._hold_cache.clear()
        r.tick()
        self.settle_all(r)
        self.assertEqual(len(mesh.declared), 1)                     # never again for the same verdict

    def test_someone_else_after_the_verdict_starts_the_author(self):
        r, t, mesh, gl = self.flow()
        self.hand_back(r, t, after=[("agent-lead", "Hold the merge: fix the README first.")])
        self.settle_all(r)
        self.assertEqual(getattr(mesh, "declared", []), [])
        work = self.work_prompts()
        self.assertEqual(len(work), 2, self.prompts())
        self.assertIn("Hold the merge", work[-1])

    def test_do_not_ship_handed_back_starts_the_author_as_before(self):
        # positive control: the same hand-back without SHIP is the ordinary work start
        r, t, mesh, gl = self.flow()
        self.hand_back(r, t, line=f"Verify: DO-NOT-SHIP @{SHA}")
        self.settle_all(r)
        self.assertEqual(getattr(mesh, "declared", []), [])
        self.assertEqual(len(self.work_prompts()), 2, self.prompts())

    def test_the_authors_own_verdict_handed_back_starts_the_author(self):
        r, t, mesh, gl = self.flow()
        self.hand_back(r, t, by=AGENT_ID)
        self.settle_all(r)
        self.assertEqual(getattr(mesh, "declared", []), [])
        self.assertEqual(len(self.work_prompts()), 2, self.prompts())

    def test_a_refused_declaration_starts_the_author_once(self):
        r, t, mesh, gl = self.flow()
        mesh.wait_for_error = MeshError(409, "not the checkout holder")
        self.hand_back(r, t)
        self.settle_all(r)
        aborted = r.store.by_state(self.agent.name, ("aborted",))
        self.assertEqual(len(aborted), 1)
        self.assertIn("runner merge wait refused", aborted[0].note)
        self.assertEqual(len(self.work_prompts()), 2, self.prompts())


class SpokenAfter(ShipBench):
    def test_in_review_someone_else_after_the_verdict_keeps_the_feed(self):
        r, t, mesh, gl = self.flow()
        mesh.add_comment(t["id"], f"Verify: SHIP @{SHA}", author="agent-reviewer", at=soon())
        mesh.add_comment(t["id"], "Hold the merge: fix the README first.", author="agent-lead", at=soon(2))
        r._hold_cache.clear()
        r.tick()
        self.assert_fed(r, t)


class HandOffVerdictLine(ShipBench):
    """A verdict below a blank line: the hand-off note "Handed over to X." + blank line + "Verify: SHIP @sha"
    (17.11: 20 of 49 SHIP verdicts). ``^\\W*`` took the newline, the line read from the start of
    the match came out empty, no sha was found and the author's session started to declare the wait."""

    NOTE = f"Handed over to the author.\n\nVerify: SHIP @{SHA}\nMR: {MR_URL}"

    def test_the_line_is_read_from_the_verdict_word(self):
        import re
        from mesh_runner.config import DEFAULT_VERDICT_PATTERN
        from mesh_runner.runner import verdict_line
        m = list(re.compile(DEFAULT_VERDICT_PATTERN, re.I | re.M).finditer(self.NOTE))[-1]
        self.assertEqual(verdict_line(self.NOTE, m), (f"Verify: SHIP @{SHA}", f"Verify: SHIP @{SHA}"))
        plain = f"Verify: SHIP @{SHA}"                       # positive control: the one-line verdict
        m = next(re.compile(DEFAULT_VERDICT_PATTERN, re.I | re.M).finditer(plain))
        self.assertEqual(verdict_line(plain, m), (plain, plain))

    def test_the_verdict_inside_the_hand_off_note_declares_the_merge_wait(self):
        r, t, mesh, gl = self.flow()
        mesh.add_comment(t["id"], self.NOTE, author="agent-reviewer", at=soon())
        mesh.set_category(t["id"], "todo")
        r._hold_cache.clear()
        r.tick()
        a = self.parked(r)
        self.assertEqual(len(mesh.declared), 1)
        self.assertEqual((a.mode, a.wait["runner_declared"], a.wait["merge"]["sha"]), ("work", "ship", SHA))
        self.assertEqual(len(self.prompts()), 1)                   # only the author's original work turn

    def test_the_verdict_below_a_blank_line_in_review_declares_the_merge_wait(self):
        r, t, mesh, gl = self.flow()
        self.verdict(r, t, line=f"Reviewed the head.\n\nVerify: SHIP @{SHA}")
        self.parked(r)
        self.assertEqual(len(mesh.declared), 1)
        self.assertEqual(self.review_prompts(), [])

    def test_an_enumerated_line_below_a_blank_line_is_no_verdict(self):
        r, t, mesh, gl = self.flow()
        since = soon(-60)
        ask = {"body": f"Please check.\n\nVerify: SHIP / DO-NOT-SHIP @{SHA}", "author_id": "agent-reviewer",
               "created_at": soon().isoformat()}
        self.assertEqual(r._verdict_event([ask], since, lambda: [SHA]), "")
        ship = dict(ask, body=f"Checked.\n\nVerify: SHIP @{SHA}")  # positive control
        self.assertTrue(r._verdict_event([ship], since, lambda: [SHA]).startswith("review verdict SHIP by "))


class HandBackBetweenSteps(ShipBench):
    """A card handed back with SHIP gets its merge wait between the steps of the author's tick (``ShipDue``),
    not in the start phase of its next tick: a busy lane's tick took 135-274 s (17.11)."""

    def handed(self):
        r, t, mesh, gl = self.flow()
        mesh.add_comment(t["id"], f"Verify: SHIP @{SHA}", author="agent-reviewer", at=soon())
        mesh.set_category(t["id"], "todo")
        r._hold_cache.clear()
        return r, t, mesh, gl

    def test_a_due_card_is_declared_by_the_pass_alone(self):
        r, t, mesh, gl = self.handed()
        r._ship_due_pass()                                         # positive control: nothing due, nothing done
        self.assertEqual(getattr(mesh, "declared", []), [])
        r.ship_due.add([self.agent.name], t["id"])
        r._ship_due_pass()                                         # no tick, no start phase
        a = self.started(r)
        self.assertEqual(len(mesh.declared), 1)
        self.assertEqual((a.mode, a.wait["runner_declared"]), ("work", "ship"))
        self.assertEqual(r.ship_due.due(self.agent.name), [])
        self.assertEqual(len(self.prompts()), 1)
        r.tick()                                                   # the start phase finds it parked
        self.assertEqual(len(mesh.declared), 1)
        self.assertEqual(len(self.prompts()), 1)

    def test_a_step_of_the_tick_takes_a_due_card_before_the_start_phase(self):
        r, t, mesh, gl = self.handed()
        r.ship_due.add([self.agent.name], t["id"])
        seen = []
        real = r.check_wakes

        def wakes():
            seen.append(len(getattr(mesh, "declared", [])))
            real()
        r.check_wakes = wakes
        r.tick()
        self.assertEqual(seen, [1])                                # declared before the wakes step began
        self.parked(r)

    def test_the_reviewers_turn_holding_the_card_keeps_it_due_until_its_release(self):
        r, t, mesh, gl = self.handed()
        rev = r.store.create("reviewer", t["id"], "codex", "task 1", mode="review")
        r.store.mark("reviewer", t["id"], "gate", '{"RES_ID": "r0"}')
        r.ship_due.add([self.agent.name], t["id"])
        r._ship_due_pass()
        self.assertEqual(getattr(mesh, "declared", []), [])
        self.assertEqual(r.ship_due.due(self.agent.name), [t["id"]])   # looked at again shortly
        r.store.mark("reviewer", t["id"], "gate", "")             # the reviewer's release ...
        r.store.update(rev, state="completed")
        r.ship_due.add([self.agent.name], t["id"])                # ... makes it due now (``_ship_handed_back``)
        r._ship_due_pass()
        self.parked(r)
        self.assertEqual(len(mesh.declared), 1)

    def test_a_card_not_handed_back_or_without_ship_is_dropped(self):
        r, t, mesh, gl = self.flow()                               # still in review, no verdict
        r.ship_due.add([self.agent.name], t["id"])
        r._ship_due_pass()
        self.assertEqual(getattr(mesh, "declared", []), [])
        self.assertEqual(r.ship_due.due(self.agent.name), [])
        r.ship_due.add([self.agent.name], "not-this-lanes-card")
        r._ship_due_pass()
        self.assertEqual(r.ship_due.due(self.agent.name), [])

    def test_a_review_turn_end_makes_the_card_due_for_its_author_and_wakes_it(self):
        from mesh_runner.config import AgentConfig
        from mesh_runner.runner import AgentRunner, ShipDue
        from mesh_runner.store import Store
        r, t, mesh, gl = self.handed()
        poked = []
        r.ship_due = ShipDue(poked.append)
        cfg = AgentConfig(name="reviewer", key="k2", harness="claude", profile_dir=self.agent.profile_dir,
                          repos=self.agent.repos, env=self.agent.env)
        rev = AgentRunner(self.cfg, cfg, Store(self.root / "state" / "r.sqlite"), mesh=FakeMesh([]),
                          adapter=self.adapter)
        self._runners.append(rev)
        rev.ship_due, rev.lanes, r.lanes = r.ship_due, [r, rev], [r, rev]
        a = rev.store.create("reviewer", t["id"], "claude", "task 1", mode="review")
        rev._release_gate(a, "launch-probe-denied")                 # not a turn end: nobody is told
        self.assertEqual((r.ship_due.due(self.agent.name), poked), ([], []))
        rev._release_gate(a, "turn-end")
        self.assertEqual((r.ship_due.due(self.agent.name), poked), ([t["id"]], [[self.agent.name]]))
        r._ship_due_pass()
        self.parked(r)
        self.assertEqual(len(mesh.declared), 1)

    def test_an_unconfirmed_release_tells_the_author_only_once_confirmed(self):
        import sys
        from mesh_runner.config import AgentConfig
        from mesh_runner.runner import AgentRunner, ShipDue
        from mesh_runner.store import Store
        r, t, mesh, gl = self.handed()
        r.ship_due = ShipDue()
        # mock: external boundary — the operator's end hook answers without contacting a live writer service.
        answer = self.root / "release-confirmation"
        answer.write_text("RELEASE_CONFIRMED=0\n")
        cfg = AgentConfig(name="reviewer", key="k2", harness="claude", profile_dir=self.agent.profile_dir,
                          repos=self.agent.repos, env=self.agent.env,
                          turn_end_command=[sys.executable, "-c",
                                            "import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text())",
                                            str(answer)])
        rev = AgentRunner(self.cfg, cfg, Store(self.root / "state" / "r.sqlite"), mesh=FakeMesh([]),
                          adapter=self.adapter)
        self._runners.append(rev)
        rev.ship_due, rev.lanes = r.ship_due, [r, rev]
        a = rev.store.create("reviewer", t["id"], "claude", "task 1", mode="review")
        rev.store.mark("reviewer", t["id"], "gate", '{"RES_ID": "r1", "RES_ADMITTED": "1"}')
        rev._release_gate(a, "turn-end")
        self.assertEqual(r.ship_due.due(self.agent.name), [])     # the writer slot is still taken
        pending = rev._pending_releases(t["id"])
        self.assertEqual(len(pending), 1)
        pending[0]["next_at"] = soon(-1).isoformat()
        rev._save_pending(t["id"], pending)
        answer.write_text("RELEASE_CONFIRMED=1\n")
        rev.retry_gate_releases()
        self.assertEqual(r.ship_due.due(self.agent.name), [t["id"]])
        self.assertEqual(rev._pending_releases(t["id"]), [])

    def test_a_hand_back_event_makes_the_card_due_for_the_lane_it_names(self):
        from types import SimpleNamespace as NS
        from mesh_runner.runner import Runner, ShipDue, TaskOwners
        run = Runner.__new__(Runner)
        run._wake_gen, run._task_owner = {}, TaskOwners()
        run.agents = [NS(agent=NS(name=n), agent_id=i) for n, i in (("a", "id-a"), ("b", "id-b"))]
        run.ship_due = ShipDue(run.wake)
        run.wake_event("task.assigned", {"task_id": "t1", "assignee_id": "id-a", "author_id": "id-b"})
        run.wake_event("comment.created", {"task_id": "t2", "assignee_id": "id-a"})   # a comment is no hand-back
        self.assertEqual((run.ship_due.due("a"), run.ship_due.due("b")), (["t1"], []))
