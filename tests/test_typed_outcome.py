"""Turn outcomes recorded through Mesh's turn commands: built straight from Mesh's rows, read before any
marker line, deferred when Mesh cannot be read, and counted by source."""
import datetime as dt
import json
import unittest
from unittest import mock

from mesh_runner import outcome as outcome_mod
from mesh_runner.mesh import MeshError
from mesh_runner.outcome import Outcome, from_typed
from mesh_runner.runner import Runner

from fakes import AGENT_ID, FakeMesh, calls, task, write_replies
from test_runner import Bench

NOW = dt.datetime(2026, 11, 10, 12, 0, tzinfo=dt.timezone.utc)
CARD = "0f1e2d3c-0000-4000-8000-000000000001"
REQUEST = "6f1c2a90-1b2c-4d5e-8f90-0123456789ab"
COMMENT = "9a8b7c6d-1234-4abc-8def-0123456789ab"
SHA = "0123456789abcdef0123456789abcdef01234567"
HEX64 = "ab" * 32


def wait_row(note="", **wait):
    """A turn-outcomes row and the task-wait row it points at, as Mesh serves them."""
    row = {"id": "o-1", "kind": "wait", "wait_id": "w-1", "note": note}
    return row, dict({"id": "w-1", "note": note}, **wait)


class Mapping(unittest.TestCase):
    """Each kind Mesh has today gives the Outcome its marker equivalent parses to, field by field."""

    def same(self, typed: Outcome, line: str):
        self.assertNotEqual(typed.kind, "unparsed", typed.note)
        self.assertEqual(typed.line, line)              # the display spelling of the wait record
        self.assertTrue(typed.typed.get("id"))

    def test_continue_next(self):
        self.same(from_typed({"id": "o-1", "kind": "next", "note": "tests green,\n now the docs"}, None, NOW),
                  "↻ NEXT: tests green, now the docs")
        self.same(from_typed({"id": "o-1", "kind": "next", "note": ""}, None, NOW), "↻ NEXT")

    def test_card_wait_with_and_without_a_condition(self):
        self.same(from_typed(*wait_row("API first", kind="card", target_task_id=CARD), NOW),
                  "⏳ WAIT card:#0f1e2d3c: API first")
        self.same(from_typed(*wait_row(kind="card", target_task_id=CARD, target_condition="deployed"), NOW),
                  "⏳ WAIT card:#0f1e2d3c until=deployed")
        # task_done is Mesh's default condition: the plain card wait
        self.same(from_typed(*wait_row(kind="card", target_task_id=CARD, target_condition="task_done"), NOW),
                  "⏳ WAIT card:#0f1e2d3c")

    def test_mr_wait_until_states(self):
        base = dict(kind="mr", provider="gitlab", project_path="entire-vc/evc-mesh", mr_iid=1801)
        for until, spelled in (("merged", "merged"), ("green", "success"), ("changed", "change")):
            self.same(from_typed(*wait_row("review", **base, until=until), NOW),
                      f"⏳ WAIT mr:entire-vc/evc-mesh!1801 until={spelled}: review")
        github = dict(base, provider="github")
        self.same(from_typed(*wait_row(**github, until="merged"), NOW),
                  "⏳ WAIT mr:github:entire-vc/evc-mesh!1801 until=merged")
        # a GitHub PR has no head pipeline: refused
        refused = from_typed(*wait_row(**github, until="green"), NOW)
        self.assertEqual(refused.kind, "unparsed")
        self.assertIn("a GitHub pull request carries no head pipeline", refused.note)

    def test_pipeline_wait(self):
        base = dict(kind="pipeline", project_path="entire-vc/evc-mesh", pipeline_id=28905)
        self.same(from_typed(*wait_row(**base, until="finished"), NOW), "⏳ WAIT pipeline:entire-vc/evc-mesh#28905")
        self.same(from_typed(*wait_row(**base, until="green"), NOW),
                  "⏳ WAIT pipeline:entire-vc/evc-mesh#28905 until=success")
        self.same(from_typed(*wait_row("e2e", **base, until="finished", required_jobs=["test", "lint", "test"]), NOW),
                  "⏳ WAIT pipeline:entire-vc/evc-mesh#28905 until=change jobs=[test,lint]: e2e")
        self.same(from_typed(*wait_row(**base, until="green", required_jobs=["test"]), NOW),
                  "⏳ WAIT pipeline:entire-vc/evc-mesh#28905 until=success jobs=[test]")

    def test_time_wait_with_an_https_source(self):
        when = "2026-11-12T12:00:00Z"
        out = from_typed(*wait_row(kind="time", until_at=when, event_ref="store-review-window",
                                   source="https://example.com/schedule", why="review window opens"), NOW)
        self.same(out, f"⏳ WAIT {when}: external=store-review-window source=https://example.com/schedule "
                       "review window opens")

    def test_ask_agent_is_a_decision_wait_on_the_named_agent(self):
        out = from_typed(*wait_row("asked Steward", kind="decision", decision_request_id=REQUEST), NOW)
        self.same(out, "⏳ WAIT decision:@Steward: asked Steward")
        self.assertEqual(out.typed["decision_request_id"], REQUEST)

    def test_what_the_marker_grammar_cannot_spell_is_kept(self):
        # a Mesh receipt as the source, an event reference with spaces, nanosecond precision, an offset
        out = from_typed(*wait_row(kind="time", until_at="2026-11-12T15:00:00.123456789+03:00",
                                   event_ref="store review window", source=f"comment:{COMMENT}",
                                   why="the store publishes the window then"), NOW)
        self.assertEqual(out.kind, "time", out.note)
        self.assertEqual(out.until, dt.datetime(2026, 11, 12, 12, 0, 0, 123456, tzinfo=dt.timezone.utc))
        self.assertEqual(out.extra["external_time"], {
            "event": "store review window", "source": f"comment:{COMMENT}",
            "why": "the store publishes the window then", "at": "2026-11-12T15:00:00.123456+03:00"})
        self.assertEqual(out.note, "the store publishes the window then")

    def test_a_time_wait_refused_on_substance_names_what(self):
        base = dict(kind="time", until_at="2026-11-12T12:00:00Z", event_ref="window", why="the window opens")
        for wait, says in ((dict(base, source="http://example.com/x"), "is not an https URL or a Mesh receipt"),
                           (dict(base, source="https://example.com/x", event_ref="poll: queue"),
                            "a dependency or a poll is not a time event"),
                           (dict(base, source="https://example.com/x", until_at="2026-11-12T12:00:00"),
                            "is not a time with a timezone"),
                           (dict(base, source=""), "has no source")):
            out = from_typed(*wait_row(**wait), NOW)
            self.assertEqual(out.kind, "unparsed", wait)
            self.assertIn(says, out.note)
            self.assertIn("`wait on time`", out.note)

    def test_a_wait_kind_this_runner_does_not_have_is_a_precise_unparsed(self):
        for kind in ("smoke-signal", "vote"):
            out = from_typed(*wait_row(kind=kind), NOW)
            self.assertEqual(out.kind, "unparsed")
            self.assertIn(f"Mesh turn outcome `wait on {kind}` (outcome o-1, wait w-1)", out.note)
            self.assertIn(f"wait kind `{kind}` is not supported by this runner yet", out.note)
            self.assertEqual(out.line, f"Mesh turn outcome `wait on {kind}` (outcome o-1, wait w-1)")
        out = from_typed({"id": "o-2", "kind": "handoff", "note": "to QA"}, None, NOW)
        self.assertEqual(out.kind, "unparsed")
        self.assertIn("outcome kind `handoff`", out.note)
        out = from_typed({"id": "o-3", "kind": "wait", "wait_id": "w-3"}, None, NOW)
        self.assertIn("its wait record could not be found", out.note)

    def test_merge_wait(self):
        base = dict(kind="merge", provider="gitlab", project_path="entire-vc/evc-mesh", mr_iid=1801, after_sha=SHA)
        out = from_typed(*wait_row("SHIP on the head", **base), NOW, merge=True)
        self.same(out, f"⏳ WAIT merge:entire-vc/evc-mesh!1801 after={SHA}: SHIP on the head")
        self.assertEqual(out.extra, {"merge": {"sha": SHA}})
        # a runner without the merge gate refuses it exactly as it refuses the marker line
        off = from_typed(*wait_row(**base), NOW)
        self.assertEqual(off.kind, "unparsed")
        self.assertEqual(off.note, outcome_mod.MERGE_NOT_ENABLED)

    def test_registered_waits(self):
        for registration, ref, spelled in (("native", HEX64, HEX64),
                                           ("gmail", "gmr_0123456789ab", "gmr_0123456789ab"),
                                           ("receipt", "00000001", "#00000001"),
                                           ("lifecycle", "00000001", "#00000001"),
                                           ("stall", "00000001", "#00000001"),
                                           ("recovery", "00000001", "#00000001"),
                                           ("queue", "00000001", "#00000001")):
            with self.subTest(registration=registration):
                out = from_typed(*wait_row("armed", kind="registered", registration=registration,
                                           registration_ref=ref), NOW, "00000001")
                self.same(out, f"⏳ WAIT {registration}:{spelled}: armed")
                self.assertEqual((out.kind, out.target), (registration, ref))
                self.assertTrue(out.is_wait)

    def test_retired_ops_review_registration_is_refused(self):
        out = from_typed(*wait_row(kind="registered", registration="ops-review",
                                   registration_ref=HEX64), NOW, "00000001")
        self.assertEqual(out.kind, "unparsed")
        self.assertIn("unknown here", out.note)
        self.assertFalse(out.is_wait)

    def test_ask_human_is_the_blocking_line_mesh_posted(self):
        out = from_typed(*wait_row("asked @morgan", kind="human", addressee="morgan",
                                   question="Ship 0.2.4 tonight\n or wait for the canary?",
                                   human_gate_ref=f"comment:{COMMENT}"), NOW)
        self.same(out, "❓ Blocking @morgan: Ship 0.2.4 tonight or wait for the canary?")
        self.assertEqual((out.kind, out.handle), ("blocking", "morgan"))
        self.assertEqual(out.typed["human_gate_ref"], f"comment:{COMMENT}")

    def test_mr_baselines(self):
        base = dict(kind="mr", provider="gitlab", project_path="entire-vc/evc-mesh", mr_iid=1801)
        out = from_typed(*wait_row("the merge", **base, until="merged", after_sha=SHA, after_status="opened"), NOW)
        self.same(out, f"⏳ WAIT mr:entire-vc/evc-mesh!1801 until=merged after={SHA}:opened: the merge")
        self.assertEqual(out.extra, {"mr_state": {"predicate": "merged", "sha": SHA, "state": "opened"}})
        for until, spelled in (("green", "success"), ("changed", "change")):
            with self.subTest(until=until):
                out = from_typed(*wait_row(**base, until=until, after_sha=SHA, after_pipeline_id=28905,
                                           after_status="running", required_jobs=["e2e", "lint"]), NOW)
                self.same(out, f"⏳ WAIT mr:entire-vc/evc-mesh!1801 until={spelled} after={SHA}:28905:running "
                               "jobs=[e2e,lint]")
                self.assertEqual(out.extra["ci_change"]["jobs"], ["e2e", "lint"])
                # jobs without a baseline: the baseline of the moment, read from those jobs
                self.same(from_typed(*wait_row(**base, until=until, required_jobs=["e2e"]), NOW),
                          f"⏳ WAIT mr:entire-vc/evc-mesh!1801 until={spelled} jobs=[e2e]")

    def test_pipeline_baselines_and_github_runs(self):
        base = dict(kind="pipeline", project_path="entire-vc/evc-mesh", pipeline_id=28905)
        self.same(from_typed(*wait_row(**base, until="changed"), NOW),
                  "⏳ WAIT pipeline:entire-vc/evc-mesh#28905 until=change")
        for until, spelled in (("green", "success"), ("changed", "change")):
            with self.subTest(until=until):
                self.same(from_typed(*wait_row("retry", **base, until=until, after_sha=SHA, after_pipeline_id=28905,
                                               after_status="failed"), NOW),
                          f"⏳ WAIT pipeline:entire-vc/evc-mesh#28905 until={spelled} after={SHA}:28905:failed: retry")
        github = dict(kind="pipeline", provider="github", project_path="entire-vc/evc-site", pipeline_id=9912345678)
        self.same(from_typed(*wait_row(**github, until="finished"), NOW),
                  "⏳ WAIT pipeline:github:entire-vc/evc-site#9912345678")
        self.same(from_typed(*wait_row(**github, until="changed", after_sha=SHA, after_pipeline_id=9912345678,
                                       after_status="cancelled"), NOW),
                  f"⏳ WAIT pipeline:github:entire-vc/evc-site#9912345678 until=change after={SHA}:9912345678:cancelled")

    def test_a_field_combination_the_runner_cannot_act_on_names_the_field(self):
        mr = dict(kind="mr", provider="gitlab", project_path="g/p", mr_iid=3)
        pipe = dict(kind="pipeline", project_path="g/p", pipeline_id=4)
        merge = dict(kind="merge", project_path="g/p", mr_iid=3, after_sha=SHA)
        for wait, says in (
                (dict(mr, until="merged", after_sha=SHA, after_status="running"), "after_status `opened`"),
                (dict(mr, until="green", after_sha=SHA[:12], after_pipeline_id=7, after_status="running"),
                 f"after_sha `{SHA[:12]}`"),
                (dict(mr, until="green", after_sha=SHA, after_status="running"), "after_pipeline_id `missing`"),
                (dict(mr, until="green", after_sha=SHA, after_pipeline_id=7, after_status="exploded"),
                 "after_status `exploded`"),
                (dict(mr, until="merged", required_jobs=["e2e"]), "required_jobs are read only with until"),
                (dict(pipe, until="green", after_sha=SHA, after_pipeline_id=5, after_status="running"),
                 "after_pipeline_id `5` is not the awaited pipeline_id `4`"),
                (dict(pipe, until="finished", after_sha=SHA), "until `finished` takes no baseline"),
                (dict(pipe, provider="bitbucket"), "provider `bitbucket`"),
                (dict(merge, provider="github"), "a merge wait is GitLab only"),
                (dict(merge, project_path="flat"), "project_path `flat`"),
                (dict(merge, after_sha="abc"), "after_sha `abc`"),
                (dict(merge, until="merged"), "takes project_path, mr_iid and after_sha only, not until"),
                (dict(kind="registered", registration="carrier-pigeon", registration_ref="x"),
                 "registration `carrier-pigeon`"),
                (dict(kind="registered", registration="gmail", registration_ref="gmr_xyz"),
                 "registration_ref `gmr_xyz` is not a gmail reference"),
                (dict(kind="registered", registration="receipt"), "registration_ref `missing`"),
                (dict(kind="human", addressee="two words", question="q", human_gate_ref="comment:x"),
                 "addressee `two words`"),
                (dict(kind="human", addressee="morgan", question=" ", human_gate_ref="comment:x"), "no question"),
                (dict(kind="human", addressee="morgan", question="q"), "no human_gate_ref")):
            with self.subTest(wait=wait):
                out = from_typed(*wait_row(**wait), NOW, merge=True)
                self.assertEqual(out.kind, "unparsed", out)
                self.assertIn(says, out.note)
                self.assertIn(f"Mesh turn outcome `wait on {wait['kind']}` (outcome o-1, wait w-1)", out.note)

    def test_malformed_fields_are_named(self):
        for wait, says in ((dict(kind="card", target_task_id="not-a-uuid"), "target_task_id `not-a-uuid`"),
                           (dict(kind="card", target_task_id=CARD, target_condition="reviewed"),
                            "target_condition `reviewed`"),
                           (dict(kind="mr", project_path="g/p", until="merged"), "mr_iid `missing`"),
                           (dict(kind="mr", project_path="g/p", mr_iid=3, until="closed"), "MR until `closed`"),
                           (dict(kind="pipeline", project_path="g/p", pipeline_id=4, until="merged"),
                            "pipeline until `merged`"),
                           (dict(kind="decision", note="asked Steward"), "no decision_request_id")):
            out = from_typed(*wait_row(**wait), NOW)
            self.assertEqual(out.kind, "unparsed", wait)
            self.assertIn(says, out.note)

    def test_a_new_wait_kind_is_one_table_entry(self):
        def gmail(row, wait, now, task8):
            line = f"⏳ WAIT gmail:{wait['registration']}: {wait['note']}"
            return Outcome("gmail", wait["registration"], note=wait["note"], line=line)
        with mock.patch.dict(outcome_mod.TYPED_WAITS, {"gmail": gmail}):
            out = from_typed(*wait_row("inbox", kind="gmail", registration="gmr_0123456789ab"), NOW)
        self.same(out, "⏳ WAIT gmail:gmr_0123456789ab: inbox")
        self.assertFalse(hasattr(outcome_mod, "typed_line"))          # nothing turns a row into text any more


class TypedTurn(Bench):
    """A turn that ends with a Mesh turn command (helpers only, no tests of its own)."""

    def typed(self, mesh, t, kind, wait=None, ago=0, note=""):
        at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=5 - ago)).isoformat()
        row = {"id": "o-1", "task_id": t["id"], "agent_id": AGENT_ID, "kind": kind, "note": note, "created_at": at}
        if wait:
            row["wait_id"] = wait["id"] = "w-1"
            mesh.waits = {t["id"]: wait}
        mesh.outcomes = {t["id"]: [row]}

    def run_turn(self, mesh, t, reply="Recorded the outcome through Mesh."):
        write_replies(self.replies, {t["id"][:8]: reply})
        r = self.runner(mesh)
        r.tick()
        return r

    @staticmethod
    def events(r, a, kind):
        return [d for _, k, d in r.store.events(a.id) if k == kind]


class Settle(TypedTurn):
    """The agent ended its turn with a Mesh turn command."""

    def test_a_typed_card_wait_parks_the_attempt_like_a_marker_would(self):
        t, other = task(1), task(2)
        mesh = FakeMesh([t, other], max_concurrent=1)
        r = self.run_turn(mesh, t)
        self.typed(mesh, t, "wait", {"kind": "card", "target_task_id": other["id"], "note": "needs its API"})
        a = self.finish_turn(r)
        self.assertEqual(a.state, "waiting")
        self.assertEqual((a.wait["kind"], a.wait["target"], a.wait["note"]), ("card", other["id"][:8], "needs its API"))
        self.assertIn(("typed_outcome", "wait:o-1"), [(k, d) for _, k, d in r.store.events(a.id)])
        self.assertFalse(any("without a readable outcome" in c["body"] for c in mesh.comment_log.get(t["id"], [])))
        self.assertEqual(len(calls(self.calls)), 1)                  # no correction turn was spent
        self.assertEqual(mesh.typed_reads, 2)                        # the outcome row and its wait
        self.assertEqual([json.loads(d) for d in self.events(r, a, "outcome_source")],
                         [{"source": "typed", "kind": "card", "harness": "claude"}])

    def test_a_typed_next_continues_in_the_same_session(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.run_turn(mesh, t)
        self.typed(mesh, t, "next")
        a = self.finish_turn(r)
        self.assertEqual(a.state, "ready")
        self.assertIn("you asked to continue (continue_next)", a.wake_text)
        self.assertEqual(mesh.typed_reads, 1)

    def test_a_marker_comment_does_not_compete_with_the_typed_outcome(self):
        t, other = task(1), task(2)
        mesh = FakeMesh([t, other], max_concurrent=1)
        r = self.run_turn(mesh, t)
        self.typed(mesh, t, "wait", {"kind": "card", "target_task_id": other["id"]})
        mesh.add_comment(t["id"], "Done for now.\n↻ NEXT: more tests",
                         at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=3))
        a = self.finish_turn(r)
        self.assertEqual((a.state, a.wait["kind"], a.wait["target"]), ("waiting", "card", other["id"][:8]))
        self.assertEqual(self.events(r, a, "outcome_disagree"), [])
        self.assertEqual(json.loads(self.events(r, a, "outcome_source")[0]),
                         {"source": "typed", "kind": "card", "harness": "claude"})

    def test_a_marker_in_the_final_message_changes_nothing_beside_the_typed_outcome(self):
        t, other = task(1), task(2)
        mesh = FakeMesh([t, other], max_concurrent=1)
        r = self.run_turn(mesh, t, reply=f"Parked.\n⏳ WAIT card:#{other['id'][:8]}: needs its API")
        self.typed(mesh, t, "wait", {"kind": "card", "target_task_id": other["id"], "note": "needs its API"})
        a = self.finish_turn(r)
        self.assertEqual(a.state, "waiting")
        self.assertEqual(self.events(r, a, "outcome_disagree"), [])
        self.assertEqual(json.loads(self.events(r, a, "outcome_source")[0]),
                         {"source": "typed", "kind": "card", "harness": "claude"})

    def test_a_typed_time_wait_with_a_receipt_source_settles(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.run_turn(mesh, t)
        when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2, microseconds=250000)
        self.typed(mesh, t, "wait", {"kind": "time", "until_at": when.isoformat().replace("+00:00", "Z"),
                                     "event_ref": "store review window", "source": f"comment:{COMMENT}",
                                     "why": "the store opens the review window then"})
        a = self.finish_turn(r)
        self.assertEqual((a.state, a.wait["kind"]), ("waiting", "time"), a.wake_text)
        self.assertEqual(a.wait["external_time"]["source"], f"comment:{COMMENT}")
        self.assertEqual(a.wait["external_time"]["event"], "store review window")
        self.assertEqual(dt.datetime.fromisoformat(a.wait["until"]), when)

    def ask_agent(self, mesh, t, to="lead", question="ship v15 or hold it?"):
        """What Mesh's ask_agent leaves behind: the decision wait and its comment that wakes nobody."""
        self.typed(mesh, t, "wait", {"kind": "decision", "decision_request_id": REQUEST, "note": f"asked {to}"},
                   note=f"asked {to}")
        mesh.comment_log.setdefault(t["id"], []).append({
            "id": COMMENT, "author_id": AGENT_ID, "author_name": "builder", "author_type": "agent",
            "body": f"❔ **Question to {to}** · request `{REQUEST}`\n\n{question}",
            "metadata": {"wakes": False, "command": "ask_agent", "decision_request_id": REQUEST},
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat()})

    def test_ask_agent_files_the_request_card_that_starts_the_addressee(self):
        """Mesh records an ask_agent question and delivers it to nobody (its comment has wakes=false,
        no queue lists open requests). The request card is what the addressee starts on, so the runner
        files it for a typed question as it does for a marker one, with the question's own words; it
        posts and routes nothing else."""
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.mesh_effects = True
        r = self.run_turn(mesh, t)
        self.ask_agent(mesh, t)
        with mock.patch.object(r, "_route_ask") as routed:
            a = self.finish_turn(r)
        self.assertEqual((a.state, a.no_outcome), ("waiting", 0), a.wake_text)
        self.assertEqual((a.wait["kind"], a.wait["handle"], a.wait["decision_request_id"]),
                         ("decision", "lead", REQUEST))
        routed.assert_not_called()
        self.assertEqual(len(mesh.created), 1)
        card = mesh.created[0]
        self.assertEqual(card["assignee_id"], "agent-lead")
        self.assertIn("ship v15 or hold it?", card["title"])
        self.assertIn("ship v15 or hold it?", card["description"])
        self.assertEqual(a.wait["request"]["id"], card["id"])
        self.assertTrue(a.wait["ask_filed"])
        self.assertEqual(len(calls(self.calls)), 1)

    def test_an_ask_agent_wait_parked_without_a_request_card_gets_one_on_the_next_pass(self):
        """A wait parked by the runner that left the asking to Mesh (fx set, no request card): the
        addressee was never started. The next pass over the wait files the card, once."""
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.mesh_effects = True
        r = self.run_turn(mesh, t)
        self.ask_agent(mesh, t)
        with mock.patch.object(r.effects, "_file_decision"):
            a = self.finish_turn(r)
        wait = {k: v for k, v in a.wait.items() if k not in ("ask_filed", "question", "asked_note")}
        wait.update(note="asked lead", line="⏳ WAIT decision:@lead: asked lead")
        self.assertTrue(r.store.update(a, expect="waiting", wait=wait))
        self.assertEqual(mesh.created, [])
        r.tick()
        r.tick()
        a = r.store.get(a.id)
        self.assertEqual(len(mesh.created), 1)
        self.assertEqual(a.wait["request"]["id"], mesh.created[0]["id"])
        self.assertIn("ship v15 or hold it?", mesh.created[0]["description"])

    def test_an_ask_agent_question_that_cannot_be_read_is_retried_not_filed_blind(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.mesh_effects = True
        r = self.run_turn(mesh, t)
        self.ask_agent(mesh, t)
        asked = mesh.comment_log.pop(t["id"])
        a = self.finish_turn(r)
        self.assertEqual((a.state, mesh.created, a.wait.get("fx")), ("waiting", [], False))
        mesh.comment_log[t["id"]] = asked
        r.tick()
        self.assertEqual(len(mesh.created), 1)

    def test_ask_human_waits_on_the_gate_mesh_armed_and_raises_nothing(self):
        """Mesh posted the Blocking comment and armed the gate: the runner neither posts nor routes the
        question again (no `_route_ask`, no comment, no correction), and parks on the gate."""
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.run_turn(mesh, t, reply="Asked through Mesh.")
        mesh.tasks[t["id"]]["human_gate"] = True                 # armed with the question
        self.typed(mesh, t, "wait", {"kind": "human", "addressee": "morgan", "note": "asked @morgan",
                                     "question": "ship tonight or wait for the canary?",
                                     "human_gate_ref": f"comment:{COMMENT}"}, note="asked @morgan")
        posted = len(mesh.comment_log.get(t["id"], []))
        with mock.patch.object(r, "_route_ask", wraps=r._route_ask) as routed:
            a = self.finish_turn(r)
        self.assertEqual((a.state, a.no_outcome), ("waiting", 0), a.wake_text)
        self.assertEqual(len(mesh.comment_log.get(t["id"], [])), posted)   # nothing posted
        self.assertEqual(routed.call_count, 0)
        self.assertEqual(a.wait["human_gate_ref"], f"comment:{COMMENT}")
        self.assertEqual(json.loads(self.events(r, a, "outcome_source")[0])["kind"], "blocking")
        # the gate held is no event, the gate lifted wakes
        r._wake_budgeted(a)
        self.assertEqual(r.store.get(a.id).state, "waiting")
        mesh.tasks[t["id"]]["human_gate"] = False
        r._wake_budgeted(r.store.get(a.id))
        woke = r.store.get(a.id)
        self.assertEqual(woke.state, "ready")
        self.assertIn("the human gate was lifted", woke.wake_text)

    def test_a_typed_merge_wait_parks_on_the_runners_merge_gate(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.run_turn(mesh, t)
        sha = "0123456789abcdef0123456789abcdef01234567"
        self.typed(mesh, t, "wait", {"kind": "merge", "provider": "gitlab", "project_path": "g/p", "mr_iid": 7,
                                     "after_sha": sha, "note": "SHIP"})
        with mock.patch.object(type(r), "merge_enabled", new_callable=mock.PropertyMock, return_value=True):
            a = self.finish_turn(r)
        self.assertEqual((a.state, a.wait["kind"], a.wait["target"]), ("waiting", "merge", "g/p!7"), a.wake_text)
        self.assertEqual(a.wait["merge"], {"sha": sha, "runs": 0})

    def test_a_typed_merge_wait_on_a_runner_without_the_gate_is_sent_back(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.run_turn(mesh, t)
        self.typed(mesh, t, "wait", {"kind": "merge", "provider": "gitlab", "project_path": "g/p", "mr_iid": 7,
                                     "after_sha": "0123456789abcdef0123456789abcdef01234567"})
        a = self.finish_turn(r)
        self.assertEqual((a.state, a.no_outcome), ("ready", 1))
        self.assertIn("`wait_for on=merge` is not enabled on this runner", a.wake_text)

    def test_an_unsupported_wait_kind_asks_precisely(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.run_turn(mesh, t)
        self.typed(mesh, t, "wait", {"kind": "smoke-signal"})
        a = self.finish_turn(r)
        self.assertEqual((a.state, a.no_outcome), ("ready", 1))
        self.assertIn("wait kind `smoke-signal` is not supported by this runner yet", a.wake_text)
        self.assertEqual(json.loads(self.events(r, a, "outcome_source")[0])["kind"], "unparsed")

    def test_an_outcome_from_before_this_turn_or_from_another_agent_is_not_this_turns(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.run_turn(mesh, t)
        self.typed(mesh, t, "next", ago=3600)                        # recorded an hour before the turn began
        a = self.finish_turn(r)
        self.assertEqual(a.state, "ready")
        self.assertIn("did not end with", a.wake_text.lower() + " did not end with")   # the ordinary no-outcome ask
        self.assertEqual(a.no_outcome, 1)
        mesh.outcomes[t["id"]][0].update(agent_id="someone-else", created_at=(
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30)).isoformat())
        self.assertIsNone(r._typed_outcome(a, None))

    def test_a_wait_that_is_no_longer_the_active_one_is_not_stood_on(self):
        t, other = task(1), task(2)
        mesh = FakeMesh([t, other], max_concurrent=1)
        r = self.run_turn(mesh, t)
        self.typed(mesh, t, "wait", {"kind": "card", "target_task_id": other["id"]})
        mesh.waits[t["id"]] = {"id": "w-2", "kind": "card", "target_task_id": other["id"]}   # superseded
        a = self.finish_turn(r)
        self.assertEqual((a.state, a.no_outcome), ("ready", 1))
        self.assertIn("its wait is no longer the active one", a.wake_text)

    def deferred_then_settled(self, error):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.run_turn(mesh, t, reply="Parked.\n↻ NEXT: the marker alone must not settle this")
        self.typed(mesh, t, "next")
        mesh.typed_error = error
        a = self.finish_turn(r)
        self.assertEqual((a.state, a.no_outcome), ("running", 0))          # left as it was
        r.settle_finished()
        r.settle_finished()
        self.assertEqual(r.store.get(a.id).state, "running")
        self.assertEqual(self.events(r, a, "outcome_read_deferred"), ["turn 1"])   # once per turn
        self.assertEqual(self.events(r, a, "outcome_source"), [])
        mesh.typed_error = None                                        # Mesh answers again
        r.settle_finished()
        a = r.store.get(a.id)
        self.assertEqual((a.state, a.no_outcome), ("ready", 0))
        self.assertEqual(json.loads(self.events(r, a, "outcome_source")[0])["source"], "typed")
        self.assertEqual(len(calls(self.calls)), 1)                    # no correction turn was spent

    def test_a_mesh_error_defers_the_settle_instead_of_asking_again(self):
        self.deferred_then_settled(MeshError(503, "unavailable"))

    def test_a_transport_error_defers_the_settle_too(self):
        self.deferred_then_settled(TimeoutError("timed out"))

    def test_a_wait_read_error_defers_too(self):
        t, other = task(1), task(2)
        mesh = FakeMesh([t, other], max_concurrent=1)
        r = self.run_turn(mesh, t)
        self.typed(mesh, t, "wait", {"kind": "card", "target_task_id": other["id"]})
        mesh.wait_error = MeshError(502, "bad gateway")
        a = self.finish_turn(r)
        self.assertEqual(a.state, "running")
        self.assertEqual(len(self.events(r, a, "outcome_read_deferred")), 1)

    def test_a_mesh_without_the_typed_read_ignores_the_marker(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.run_turn(mesh, t, reply="Step done.\n↻ NEXT: the docs")
        mesh.typed_error = MeshError(404, "not found")
        a = self.finish_turn(r)
        self.assertEqual((a.state, a.no_outcome), ("ready", 1))       # the marker settles nothing
        self.assertEqual(json.loads(self.events(r, a, "outcome_source")[0]),
                         {"source": "none", "kind": "none", "harness": "claude"})

    def test_outcome_sources_are_counted_once_per_turn_and_exported(self):
        from mesh_runner.runner import OutcomeRead
        t, t2, t3 = task(1), task(2), task(3)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.run_turn(mesh, t)
        self.typed(mesh, t, "next")
        a = self.finish_turn(r)
        self.assertEqual(r.outcome_sources(), {"typed": {"claude": 1}})
        # a settle that runs again for the same turn is not counted again
        r._count_outcome(a, OutcomeRead(Outcome("next"), "", "typed"), Outcome("next"))
        self.assertEqual(r.outcome_sources(), {"typed": {"claude": 1}})
        codex = r.store.create(self.agent.name, t2["id"], "codex")
        r._count_outcome(codex, OutcomeRead(None, ""), None)
        claude = r.store.create(self.agent.name, t3["id"], "claude")
        r._count_outcome(claude, OutcomeRead(None, ""), None)
        self.assertEqual(json.loads(self.events(r, codex, "outcome_source")[0]),
                         {"source": "none", "kind": "none", "harness": "codex"})
        totals = {"typed": {"claude": 1}, "none": {"codex": 1, "claude": 1}}
        self.assertEqual(r.outcome_sources(), totals)
        outer = Runner(self.cfg, lock=False)
        outer.agents = [r, r]                                          # summed over the agents
        outer.write_status([])
        status = json.loads((self.cfg.state_dir / "status.json").read_text())
        self.assertEqual(status["outcome_sources"],
                         {s: {h: 2 * n for h, n in by.items()} for s, by in totals.items()})


if __name__ == "__main__":
    unittest.main()
