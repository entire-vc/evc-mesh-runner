"""Mesh's start_after contract (docs/api/typed-outcomes.md, "start_after").

An agent sets a date only with ``start_after_evidence`` (event_ref, source, why) and takes it back
only through ``POST /tasks/:id/start-after/compare-clear``, which names the date, version, origin
and receipt it read. A PATCH with ``start_after: null`` from an agent is a 422; a date without
evidence is a 422. The runner never produces either, and a start_after that will not clear never
sends a requeued task up to its lead.
"""
import datetime as dt
import json

from test_runner import Bench
from test_unschedule_retry import RealisticMesh, conflict, mesh_time
from fakes import AGENT_ID, FakeMesh, task
from mesh_runner.effects import EVIDENCE_MAX, GENERIC_RETRY_WORDS, UNSCHEDULE_MARK, _event_ref, same_instant
from mesh_runner.mesh import MeshClient, MeshError
from mesh_runner.store import iso, now


def patches_of(mesh, op):
    return [(tid, body) for o, tid, body in mesh.start_after_calls if o == op]


def clear_attempts(mesh):
    """Every compare-clear a RealisticMesh was asked for, refused or not."""
    return [body["compare_clear"] for _, body in mesh.patches if "compare_clear" in body]


class ContractBench(Bench):
    def setUp(self):
        super().setUp()
        self.cfg.requeue_on_cap = True

    def waiting(self, wait, mesh=None, **extra):
        t = task(1, cat="in_progress", **extra)
        mesh = mesh or RealisticMesh([t])
        r = self.runner(mesh)
        r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        r.effects.on_wait(a, t, wait, r.me)
        r.store.update(a, state="waiting", wait=wait)
        return r, mesh, t, r.store.get(a.id)

    def pending(self, r, t):
        mark = r.store.mark(self.agent.name, t["id"], UNSCHEDULE_MARK)
        return json.loads(mark[0])["scheduled"] if mark and mark[0] else None


def time_wait(**extra):
    until = now() + dt.timedelta(hours=3)
    return {"kind": "time", "until": iso(until), "since": iso(), "started": iso(),
            "line": f"⏳ WAIT {until.strftime('%Y-%m-%dT%H:%MZ')}: measurement window closes",
            "note": "measurement window closes", **extra}


EVENT_WAITS = {
    "mr": lambda: {"kind": "mr", "target": "g/app!7", "since": iso(), "started": iso()},
    "pipeline": lambda: {"kind": "pipeline", "target": "g/app#99", "since": iso(), "started": iso()},
    "merge": lambda: {"kind": "merge", "target": "g/app!7", "since": iso(), "started": iso()},
    "card": lambda: {"kind": "card", "target": "abcdef01", "since": iso(), "started": iso()},
    "decision": lambda: {"kind": "decision", "handle": "howard", "since": iso(), "started": iso(),
                         "note": "which option?"},
    "blocking": lambda: {"kind": "blocking", "handle": "pavel", "since": iso(), "started": iso(),
                         "note": "which option?"},
    "queue": lambda: {"kind": "queue", "since": iso(), "started": iso()},
}


class EventWaitsWriteNoHold(ContractBench):
    """Owner rule of 28.10: MR, CI, card, decision and blocking waits follow their event with no
    timer. A start_after is for a time wait with a known moment."""

    def test_no_event_wait_writes_a_start_after_on_wait_renew_or_adopt(self):
        for kind, make in EVENT_WAITS.items():
            with self.subTest(kind):
                self.setUp()
                wait = make()
                r, mesh, t, a = self.waiting(wait)
                r.effects.renew(a, wait)
                bare = make()                                   # a wait begun without effects
                self.assertTrue(r.effects.adopt(a, mesh.tasks[t["id"]], bare, r.me))
                r.effects.renew(a, bare)
                self.assertEqual(mesh.start_after_calls, [])
                self.assertIsNone(mesh.tasks[t["id"]].get("start_after"))
                self.assertNotIn("scheduled", wait)
                self.assertNotIn("scheduled", bare)
                self.tmp.cleanup()

    def stale_hold(self, kind="merge"):
        """A card an older runner left a rolling horizon on, with the wait that wrote it."""
        t = task(1, cat="in_progress")
        mesh = RealisticMesh([t])
        horizon = mesh_time(iso(now() + dt.timedelta(hours=20)))
        mesh.tasks[t["id"]].update(start_after=horizon, start_after_origin="agent_evidence",
                                   start_after_receipt_id="rcpt-old", version=4)
        wait = EVENT_WAITS[kind]()
        wait["scheduled"] = horizon
        r = self.runner(mesh)
        r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        r.store.update(a, state="waiting", wait=wait)
        return r, mesh, t, r.store.get(a.id), wait

    def test_a_hold_an_older_runner_wrote_is_compare_cleared_when_the_wait_is_renewed(self):
        r, mesh, t, a, wait = self.stale_hold()
        r.effects.renew(a, wait)
        clears = patches_of(mesh, "compare-clear")
        self.assertEqual(len(clears), 1)
        self.assertEqual((clears[0][1]["expected_version"], clears[0][1]["expected_origin"],
                          clears[0][1]["schedule_receipt_id"]), (4, "agent_evidence", "rcpt-old"))
        self.assertIsNone(mesh.tasks[t["id"]]["start_after"])
        self.assertNotIn("scheduled", wait)
        self.assertEqual([b for _, b in patches_of(mesh, "patch")], [])

    def test_a_hold_found_on_adopt_is_compare_cleared(self):
        r, mesh, t, a, wait = self.stale_hold("decision")
        self.assertTrue(r.effects.adopt(a, mesh.tasks[t["id"]], wait, r.me))
        self.assertEqual(len(patches_of(mesh, "compare-clear")), 1)
        self.assertIsNone(mesh.tasks[t["id"]]["start_after"])
        self.assertNotIn("scheduled", wait)

    def test_a_date_that_is_not_the_runners_is_left_alone(self):
        r, mesh, t, a, wait = self.stale_hold()
        mesh.tasks[t["id"]].update(start_after=mesh_time(iso(now() + dt.timedelta(days=5))),
                                   start_after_origin="human_explicit")
        r.effects.renew(a, wait)
        self.assertEqual(patches_of(mesh, "compare-clear"), [])
        self.assertIsNotNone(mesh.tasks[t["id"]]["start_after"])

    def test_a_hold_that_will_not_clear_is_kept_for_the_tick(self):
        r, mesh, t, a, wait = self.stale_hold()
        mesh.patch_errors = [MeshError(503, "down")]
        r.effects.renew(a, wait)
        self.assertIsNotNone(self.pending(r, t))
        r.effects._unschedule_due.clear()
        r.effects.retry_unschedules()
        self.assertIsNone(mesh.tasks[t["id"]]["start_after"])


class ScheduleCarriesEvidence(ContractBench):
    def test_a_time_wait_schedules_with_evidence_the_server_accepts(self):
        wait = time_wait()
        r, mesh, t, a = self.waiting(wait)
        sent = patches_of(mesh, "patch")
        self.assertEqual(len(sent), 1)
        ev = sent[0][1]["start_after_evidence"]
        self.assertEqual(set(ev), {"event_ref", "source", "why"})
        self.assertEqual(ev["source"], f"{mesh.base}/tasks/{t['id']}")      # no URL in the line: the card
        self.assertEqual(ev["why"], wait["line"])
        self.assertRegex(ev["event_ref"], r"^time-00000001-\d{8}t\d{4}z$")
        self.assertTrue(all(0 < len(v) <= EVIDENCE_MAX for v in ev.values()))
        self.assertFalse(set(ev["event_ref"].split("-")) & GENERIC_RETRY_WORDS)
        # the strict model of the server took it: this is a stored agent_evidence date
        self.assertIn("scheduled", wait)
        got = mesh.tasks[t["id"]]
        self.assertEqual(got["start_after_origin"], "agent_evidence")
        self.assertTrue(got["start_after_receipt_id"])
        self.assertTrue(same_instant(got["start_after"], wait["scheduled"]))

    def test_the_first_https_url_of_the_wait_line_is_the_source(self):
        wait = time_wait(line="⏳ WAIT 2026-11-02T09:00Z: window opens, see https://ci.example/schedules/7). "
                              "Mirror: https://other.example/x")
        r, mesh, t, a = self.waiting(wait)
        ev = patches_of(mesh, "patch")[0][1]["start_after_evidence"]
        self.assertEqual(ev["source"], "https://ci.example/schedules/7")
        self.assertEqual(ev["why"], wait["line"])
        self.assertIn("scheduled", wait)

    def test_a_cited_url_makes_the_write_possible_without_an_https_mesh_url(self):
        mesh = RealisticMesh([task(1, cat="in_progress")])
        mesh.base = "http://mesh.local/api/v1"
        wait = time_wait(line="⏳ WAIT 2026-11-02T09:00Z: export at https://ci.example/s/1")
        r, mesh, t, a = self.waiting(wait, mesh)
        self.assertEqual(patches_of(mesh, "patch")[0][1]["start_after_evidence"]["source"], "https://ci.example/s/1")

    def test_a_short_line_is_padded_with_the_reason(self):
        wait = time_wait(line="⏳ WAIT 2h", note="export finishes")
        r, mesh, t, a = self.waiting(wait)
        why = patches_of(mesh, "patch")[0][1]["start_after_evidence"]["why"]
        self.assertGreaterEqual(len(why), 10)
        self.assertIn("export finishes", why)

    def test_a_short_line_without_a_reason_still_reaches_ten_characters(self):
        r, mesh, t, a = self.waiting(time_wait(line="⏳ WAIT 2h", note=""))
        self.assertGreaterEqual(len(patches_of(mesh, "patch")[0][1]["start_after_evidence"]["why"]), 10)

    def test_a_timer_word_in_a_reference_gets_a_digest_name(self):
        ref = _event_ref("mr", "g/retry-service!7")
        self.assertRegex(ref, r"^mr-[0-9a-f]{12}$")
        self.assertEqual(ref, _event_ref("mr", "g/retry-service!7"))             # stable
        self.assertEqual(_event_ref("mr", "g/app!7"), "mr-g-app-7")
        self.assertLessEqual(len(_event_ref("mr", "x" * 2000)), EVIDENCE_MAX)

    def test_long_values_stay_inside_the_servers_limit(self):
        wait = time_wait(line="⏳ WAIT 2026-11-02T09:00Z: " + "x" * 2000)
        r, mesh, t, a = self.waiting(wait)
        ev = patches_of(mesh, "patch")[0][1]["start_after_evidence"]
        self.assertTrue(all(len(v) <= EVIDENCE_MAX for v in ev.values()))
        self.assertIn("scheduled", wait)

    def test_a_refused_schedule_is_logged_once_and_not_repeated(self):
        mesh = RealisticMesh([task(1, cat="in_progress")])
        mesh.patch_errors = [MeshError(422, "start_after from an agent needs start_after_evidence",
                                       {"error_code": "missing_evidence"})]
        wait = time_wait()
        with self.assertLogs("mesh_runner", "WARNING") as logs:
            r, mesh, t, a = self.waiting(wait, mesh)
        self.assertEqual(len([m for m in logs.output if "schedule" in m]), 1)
        self.assertEqual(wait["schedule_skipped"], "missing_evidence")
        self.assertNotIn("scheduled", wait)
        before = len(mesh.start_after_calls)
        r.effects.renew(a, wait)
        self.assertEqual(len(mesh.start_after_calls), before)       # nothing is sent every tick

    def test_a_mesh_url_that_is_not_https_and_no_cited_url_writes_nothing_and_says_so_once(self):
        mesh = RealisticMesh([task(1, cat="in_progress")])
        mesh.base = "http://mesh.local/api/v1"
        wait = time_wait()
        with self.assertLogs("mesh_runner", "INFO") as logs:
            r, mesh, t, a = self.waiting(wait, mesh)
            r.effects.on_wait(a, mesh.tasks[t["id"]], wait, r.me)
        self.assertEqual(mesh.start_after_calls, [])
        self.assertEqual(len([m for m in logs.output if "https" in m]), 1)
        self.assertNotIn("scheduled", wait)

    def test_the_old_bare_patch_is_never_sent(self):
        # {"start_after": when} alone is exactly what the server answers with 422
        r, mesh, t, a = self.waiting(time_wait())
        for _, body in patches_of(mesh, "patch"):
            self.assertIn("start_after_evidence", body)


class ClearIsCompareClear(ContractBench):
    def test_the_clear_names_the_date_version_origin_and_receipt_it_read(self):
        r, mesh, t, a = self.waiting(time_wait())
        stored = dict(mesh.tasks[t["id"]])
        wait = dict(a.wait)
        r.effects.on_end(a, wait, "woken")
        clears = patches_of(mesh, "compare-clear")
        self.assertEqual(len(clears), 1)
        tid, body = clears[0]
        self.assertEqual(tid, t["id"])
        self.assertEqual(body["expected_start_after"], stored["start_after"])
        self.assertEqual(body["expected_version"], stored["version"])
        self.assertEqual(body["expected_origin"], "agent_evidence")
        self.assertEqual(body["schedule_receipt_id"], stored["start_after_receipt_id"])
        self.assertTrue(0 < len(body["idempotency_key"]) <= 200)
        self.assertTrue(body["reason"])
        self.assertIsNone(mesh.tasks[t["id"]]["start_after"])
        self.assertIsNone(mesh.tasks[t["id"]]["start_after_origin"])
        self.assertNotIn("scheduled", wait)
        # a null PATCH is the 422 of the old contract: never sent
        self.assertEqual([b for _, b in patches_of(mesh, "patch") if b["start_after"] is None], [])

    def test_the_client_posts_the_documented_body_to_the_documented_path(self):
        seen = []

        class Resp:
            def __init__(self, data): self.data = data
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return self.data

        def opener(req, timeout):
            seen.append((req.method, req.full_url, json.loads(req.data)))
            return Resp(b'{"replayed": false, "version": 43}')
        client = MeshClient("https://mesh.example", "k", opener=opener)
        client.compare_clear_start_after(
            "tid", expected_start_after="2026-11-01T06:00:00Z", expected_version=42,
            expected_origin="agent_evidence", schedule_receipt_id="rid", idempotency_key="key", reason="done")
        client.compare_clear_start_after("tid", expected_start_after="2026-11-01T06:00:00Z", expected_version=42,
                                         expected_origin="legacy_unknown", idempotency_key="key")
        self.assertEqual(seen[0], ("POST", "https://mesh.example/api/v1/tasks/tid/start-after/compare-clear", {
            "expected_start_after": "2026-11-01T06:00:00Z", "expected_version": 42, "idempotency_key": "key",
            "expected_origin": "agent_evidence", "schedule_receipt_id": "rid", "reason": "done"}))
        self.assertNotIn("schedule_receipt_id", seen[1][2])        # a legacy_unknown date has no receipt

    def test_a_mismatch_is_read_again_and_retried_once_with_the_new_version(self):
        r, mesh, t, a = self.waiting(time_wait())
        real = mesh.compare_clear_start_after

        def someone_edits_the_card_first(tid, **kw):
            mesh.compare_clear_start_after = real
            mesh.tasks[tid]["version"] += 1                       # any other write bumps the version
            return real(tid, **kw)                                # the server answers a real 409
        mesh.compare_clear_start_after = someone_edits_the_card_first
        with self.assertLogs("mesh_runner", "INFO") as logs:
            r.effects.on_end(a, dict(a.wait), "woken")
        clears = patches_of(mesh, "compare-clear")
        self.assertEqual(len(clears), 2)
        self.assertEqual(clears[1][1]["expected_version"], clears[0][1]["expected_version"] + 1)
        self.assertNotEqual(clears[0][1]["idempotency_key"], clears[1][1]["idempotency_key"])
        self.assertIsNone(mesh.tasks[t["id"]]["start_after"])
        self.assertTrue(any("cleared after 1 conflict" in m for m in logs.output))

    def test_a_second_mismatch_is_logged_and_kept_for_the_tick_not_retried_in_a_loop(self):
        r, mesh, t, a = self.waiting(time_wait())
        mesh.patch_errors = [conflict(), conflict(), conflict()]
        with self.assertLogs("mesh_runner", "WARNING") as logs:
            r.effects.on_end(a, dict(a.wait), "woken")
        self.assertEqual(len(clear_attempts(mesh)), 2)                   # one retry, not three
        self.assertTrue(any("unschedule" in m and "retried on later ticks" in m for m in logs.output))
        self.assertIsNotNone(self.pending(r, t))
        self.assertIsNotNone(mesh.tasks[t["id"]]["start_after"])

    def test_a_date_a_person_set_is_left_alone(self):
        t = task(1, cat="todo")
        mesh = RealisticMesh([t])
        at = mesh_time(iso(now() + dt.timedelta(days=2)))
        mesh.tasks[t["id"]].update(start_after=at, start_after_origin="human_explicit", start_after_receipt_id="r-h")
        r = self.runner(mesh)
        r.refresh_me()
        with self.assertLogs("mesh_runner", "INFO") as logs:
            outcome = r.effects.clear_start_after(t["id"], at)
        self.assertEqual(outcome, "gone")
        self.assertEqual(mesh.tasks[t["id"]]["start_after"], at)
        self.assertIsNone(self.pending(r, t))                            # no retry loop on a person's date
        self.assertTrue(any("set by a person" in m for m in logs.output))

    def test_the_same_read_repeats_the_same_idempotency_key(self):
        r, mesh, t, a = self.waiting(time_wait())
        mesh.patch_errors = [MeshError(0, "TimeoutError: timed out")]    # the answer is lost; nothing changed
        r.effects.on_end(a, dict(a.wait), "woken")
        r.effects._unschedule_due.clear()
        r.effects.retry_unschedules()
        clears = clear_attempts(mesh)
        self.assertEqual(len(clears), 2)
        self.assertEqual(clears[0]["idempotency_key"], clears[1]["idempotency_key"])
        self.assertIsNone(mesh.tasks[t["id"]]["start_after"])

    def test_a_date_of_unknown_origin_is_cleared_without_a_receipt(self):
        t = task(1, cat="todo")
        mesh = RealisticMesh([t])
        at = mesh_time(iso(now() + dt.timedelta(days=2)))
        mesh.tasks[t["id"]].update(start_after=at, start_after_origin="legacy_unknown")
        r = self.runner(mesh)
        r.refresh_me()
        self.assertEqual(r.effects.clear_start_after(t["id"], at), "cleared")
        body = patches_of(mesh, "compare-clear")[0][1]
        self.assertEqual(body["expected_origin"], "legacy_unknown")
        self.assertIsNone(body["schedule_receipt_id"])

    def test_a_mesh_without_provenance_still_gets_the_plain_clear(self):
        # no start_after_origin on the card: a Mesh from before the contract, which has no endpoint
        t = task(1, cat="todo", start_after=iso(now() + dt.timedelta(days=2)))
        mesh = FakeMesh([t])
        r = self.runner(mesh)
        r.refresh_me()
        self.assertEqual(r.effects.clear_start_after(t["id"], t["start_after"]), "cleared")
        self.assertEqual([o for o, _, _ in mesh.start_after_calls], ["patch"])
        self.assertIsNone(mesh.tasks[t["id"]]["start_after"])


class RequeueStaysInPlace(ContractBench):
    """The no-outcome requeue (and the caps) must not fall back to the hand-up because of start_after."""

    def requeue(self, mesh, t):
        self.agent.lead = "lead"
        r = self.runner(mesh)
        r.refresh_me()
        ok = r._requeue_on_event(dict(mesh.tasks[t["id"]]), "no outcome", "NO OUTCOME", "no readable outcome",
                                 None, keep_lane=True)
        return r, ok

    def card(self, **extra):
        at = mesh_time(iso(now() + dt.timedelta(days=1)))
        t = task(1, cat="in_progress", start_after=at, start_after_origin="agent_evidence",
                 start_after_receipt_id="rcpt-1", **extra)
        return t, RealisticMesh([t])

    def test_the_start_after_is_cleared_by_compare_clear_and_the_task_requeues(self):
        t, mesh = self.card()
        r, ok = self.requeue(mesh, t)
        self.assertTrue(ok)
        got = mesh.tasks[t["id"]]
        self.assertEqual((got["assignee_id"], mesh.category(got), got["start_after"]), (AGENT_ID, "todo", None))
        self.assertEqual(len(patches_of(mesh, "compare-clear")), 1)
        self.assertEqual([b for _, b in patches_of(mesh, "patch") if b["start_after"] is None], [])   # no 422 path

    def test_a_card_that_keeps_changing_is_requeued_anyway_and_the_clear_is_retried(self):
        t, mesh = self.card()
        mesh.patch_errors = [conflict(), conflict()]
        r, ok = self.requeue(mesh, t)
        self.assertTrue(ok)                                               # not the hand-up path
        got = mesh.tasks[t["id"]]
        self.assertEqual((got["assignee_id"], mesh.category(got)), (AGENT_ID, "todo"))
        self.assertTrue(r._cap_wait(t["id"]))
        self.assertIsNotNone(self.pending(r, t))                          # kept for the next tick
        r.effects._unschedule_due.clear()
        r.effects.retry_unschedules()
        self.assertIsNone(mesh.tasks[t["id"]]["start_after"])

    def test_a_date_a_person_set_stays_and_the_task_requeues_in_place(self):
        t, mesh = self.card()
        mesh.tasks[t["id"]]["start_after_origin"] = "human_explicit"
        r, ok = self.requeue(mesh, t)
        self.assertTrue(ok)
        got = mesh.tasks[t["id"]]
        self.assertEqual((got["assignee_id"], mesh.category(got)), (AGENT_ID, "todo"))
        self.assertIsNotNone(got["start_after"])
        self.assertIsNone(self.pending(r, t))

    def test_no_outcome_turns_end_in_a_requeue_not_a_hand_up_when_the_clear_is_refused(self):
        t, mesh = self.card()
        mesh.patch_errors = [MeshError(503, "down")] * 4
        self.cfg.max_no_outcome = 0
        self.cfg.no_outcome_route_after = 99
        r = self.runner(mesh)
        r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        r.store.update(a, state="running", mode="work")
        r._no_outcome(r.store.get(a.id), dict(mesh.tasks[t["id"]]), None)
        got = mesh.tasks[t["id"]]
        self.assertEqual(got["assignee_id"], AGENT_ID)                    # never handed up
        self.assertEqual(mesh.category(got), "todo")
        self.assertTrue(r._cap_wait(t["id"]))
        self.assertIn("cap: requeued (no outcome)", r.store.get(a.id).note)


if __name__ == "__main__":
    import unittest
    unittest.main()
