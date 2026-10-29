"""The rolling start_after an event wait sets is always taken back when the wait ends.

Mesh answers start_after normalised to UTC (``...Z``, its own sub-second precision),
not as the runner wrote it, refuses a concurrent write with 409, and may commit a
write whose answer was lost. None of that may leave the task held by a schedule
the runner owned, and none of it may clear a schedule someone else set.
"""
import datetime as dt
import json

from test_runner import Bench
from fakes import AGENT_ID, FakeMesh, task
from mesh_runner.effects import UNSCHEDULE_MARK, own_schedule, same_instant
from mesh_runner.mesh import MeshError
from mesh_runner.selection import parse_time
from mesh_runner.store import iso, now


def mesh_time(value):
    """start_after the way Mesh serialises it: UTC, ``Z``, trailing zeros trimmed."""
    t = parse_time(value)
    if t is None:
        return None
    text = t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f").rstrip("0").rstrip(".")
    return text + "Z"


class RealisticMesh(FakeMesh):
    """FakeMesh with Mesh's start_after contract: dates answered in UTC, an agent's date needs
    evidence and is cleared only by compare-clear, and a write can be refused on demand."""

    def __init__(self, tasks):
        super().__init__(tasks)
        self.strict_start_after = True
        self.patch_errors = []        # MeshError per start_after write, None = let it through
        self.apply_failed = False     # a failed write still commits (answer lost)
        self.patches = []             # every start_after write: the PATCH and the compare-clear

    def _injected(self, record, apply):
        self.patches.append(record)
        err = self.patch_errors.pop(0) if self.patch_errors else None
        if err is not None:
            if self.apply_failed:
                apply()
            raise err

    def patch_task(self, tid, fields):
        fields = dict(fields)
        if "start_after" in fields:
            fields["start_after"] = mesh_time(fields["start_after"])
        self._injected((tid, dict(fields)), lambda: super(RealisticMesh, self).patch_task(tid, fields))
        return super().patch_task(tid, fields)

    def compare_clear_start_after(self, tid, **kw):
        self._injected((tid, {"compare_clear": dict(kw)}),
                       lambda: super(RealisticMesh, self).compare_clear_start_after(tid, **kw))
        return super().compare_clear_start_after(tid, **kw)


def time_wait(hours=20, **extra):
    """A time wait: the only kind that holds the card on the server."""
    until = now() + dt.timedelta(hours=hours)
    return {"kind": "time", "until": iso(until), "since": iso(), "started": iso(),
            "line": f"⏳ WAIT {until.strftime('%Y-%m-%dT%H:%MZ')}: soak window", "note": "soak window", **extra}


def conflict():
    return MeshError(409, "Task was modified concurrently — re-read the task and retry")


class UnscheduleRetry(Bench):
    def setup_wait(self, wait=None):
        t = task(1, cat="in_progress")
        mesh = RealisticMesh([t])
        r = self.runner(mesh)
        r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        wait = wait or time_wait()
        r.effects.on_wait(a, t, wait, r.me)
        r.store.update(a, state="waiting", wait=wait)
        return r, mesh, t, r.store.get(a.id)

    def start_after(self, mesh, t):
        return mesh.tasks[t["id"]].get("start_after")

    def pending(self, r, t):
        """The instants a pending clear is kept for, None when nothing is pending."""
        mark = r.store.mark(self.agent.name, t["id"], UNSCHEDULE_MARK)
        return json.loads(mark[0])["scheduled"] if mark and mark[0] else None

    # ---------------------------------------------------------------- comparison
    def test_own_schedule_covers_a_schedule_whose_answer_was_lost(self):
        # queue waits use this to tell our horizon from an external start gate
        at = "2026-10-24T11:42:53.132+00:00"
        self.assertTrue(own_schedule("2026-10-24T11:42:53.132Z", {"scheduled": at}))
        self.assertTrue(own_schedule("2026-10-24T11:42:53.132Z", {"scheduled_maybe": at}))
        self.assertFalse(own_schedule("2026-10-24T11:42:54Z", {"scheduled_maybe": at}))
        self.assertFalse(own_schedule("2026-10-24T11:42:53.132Z", {}))
        self.assertFalse(own_schedule(None, {"scheduled": at}))

    def test_same_instant_is_tolerant_to_zone_spelling_and_sub_second(self):
        self.assertTrue(same_instant("2026-10-24T11:42:53.132Z", "2026-10-24T11:42:53.132+00:00"))
        self.assertTrue(same_instant("2026-10-24T14:42:53.132+03:00", "2026-10-24T11:42:53.132Z"))
        # a schedule someone else set a moment apart is not ours
        self.assertFalse(same_instant("2026-10-24T11:42:53.132Z", "2026-10-24T11:42:53.133Z"))
        self.assertFalse(same_instant("2026-10-24T11:42:53Z", "2026-10-24T11:42:20Z"))
        self.assertTrue(same_instant("2026-10-24T11:42:53.132592Z", "2026-10-24T11:42:53.132+00:00"))
        self.assertFalse(same_instant("2026-10-24T11:44:00Z", "2026-10-24T11:42:00Z"))
        self.assertFalse(same_instant(None, "2026-10-24T11:42:00Z"))
        self.assertFalse(same_instant("not-a-time", "not-a-time"))

    # ---------------------------------------------------------------- path 2
    def test_task_left_while_waiting_clears_a_schedule_written_with_an_offset(self):
        """A time wait schedules the agent's own ``until`` (here +03:00). Mesh keeps the
        instant but answers it in UTC: comparing the first 16 characters of the two
        strings took our own schedule for someone else's and left the new assignee
        held without a word."""
        until = (now() + dt.timedelta(hours=5)).astimezone(dt.timezone(dt.timedelta(hours=3)))
        wait = {"kind": "time", "until": until.isoformat(timespec="milliseconds"),
                "since": iso(), "started": iso()}
        r, mesh, t, a = self.setup_wait(wait)
        self.assertTrue(self.start_after(mesh, t).endswith("Z"))
        mesh.tasks[t["id"]]["assignee_id"] = "agent-lead"        # handed to another lane
        r._wake_one(a)
        self.assertEqual(r.store.get(a.id).state, "completed")
        self.assertIsNone(self.start_after(mesh, t))
        self.assertIsNone(self.pending(r, t))

    def test_task_left_while_waiting_clears_the_time_hold(self):
        r, mesh, t, a = self.setup_wait()
        self.assertTrue(same_instant(self.start_after(mesh, t), a.wait["scheduled"]))
        mesh.tasks[t["id"]]["assignee_id"] = "agent-lead"
        r._wake_one(a)
        self.assertIsNone(self.start_after(mesh, t))

    # ---------------------------------------------------------------- path 1
    def test_a_409_is_retried_after_a_fresh_read(self):
        r, mesh, t, a = self.setup_wait()
        mesh.patch_errors = [conflict()]
        wait = dict(a.wait)
        with self.assertLogs("mesh_runner", "INFO"):
            r.effects.on_end(a, wait, "woken", close_request=False)
        self.assertIsNone(self.start_after(mesh, t))
        self.assertNotIn("scheduled", wait)
        self.assertIsNone(self.pending(r, t))

    def test_persistent_failure_is_remembered_and_cleared_on_a_later_tick(self):
        r, mesh, t, a = self.setup_wait()
        scheduled = a.wait["scheduled"]
        mesh.patch_errors = [conflict(), conflict(), conflict()]
        wait = dict(a.wait)
        with self.assertLogs("mesh_runner", "WARNING") as logs:
            r.effects.on_end(a, wait, "woken", close_request=False)
        self.assertEqual(len([m for m in logs.output if "unschedule" in m]), 1)
        self.assertIsNotNone(self.start_after(mesh, t))
        kept = self.pending(r, t)
        self.assertTrue(kept and any(same_instant(v, scheduled) for v in kept))
        mesh.patch_errors = [MeshError(0, "TimeoutError: timed out")]
        before = len(mesh.patches)
        r.effects.retry_unschedules()                 # not due yet: nothing is sent
        self.assertEqual(len(mesh.patches), before)
        r.effects._unschedule_due.clear()             # skip the retry backoff
        with self.assertNoLogs("mesh_runner", "WARNING"):
            r.effects.retry_unschedules()             # still failing: kept, not logged again
        self.assertEqual(len(mesh.patches), before + 1)
        self.assertIsNotNone(self.pending(r, t))
        r.effects._unschedule_due.clear()             # skip the retry backoff
        with self.assertLogs("mesh_runner", "INFO") as logs:
            r.effects.retry_unschedules()
        self.assertTrue(any("cleared" in m for m in logs.output))
        self.assertIsNone(self.start_after(mesh, t))
        self.assertIsNone(self.pending(r, t))

    def test_a_timeout_on_unschedule_is_retried_later(self):
        r, mesh, t, a = self.setup_wait()
        mesh.patch_errors = [MeshError(0, "TimeoutError: timed out")]
        r.effects.on_end(a, dict(a.wait), "woken", close_request=False)
        self.assertIsNotNone(self.pending(r, t))
        r.effects._unschedule_due.clear()
        r.effects.retry_unschedules()
        self.assertIsNone(self.start_after(mesh, t))
        self.assertIsNone(self.pending(r, t))

    def test_pending_is_dropped_when_someone_changed_start_after(self):
        r, mesh, t, a = self.setup_wait()
        mesh.patch_errors = [MeshError(503, "unavailable")]
        r.effects.on_end(a, dict(a.wait), "woken", close_request=False)
        self.assertIsNotNone(self.pending(r, t))
        theirs = mesh_time(iso(now() + dt.timedelta(days=3)))
        mesh.tasks[t["id"]]["start_after"] = theirs
        before = len(mesh.patches)
        r.effects._unschedule_due.clear()
        r.effects.retry_unschedules()
        self.assertEqual(self.start_after(mesh, t), theirs)
        self.assertEqual(len(mesh.patches), before)
        self.assertIsNone(self.pending(r, t))

    def test_pending_is_dropped_when_the_lane_schedules_the_task_again(self):
        r, mesh, t, a = self.setup_wait()
        mesh.patch_errors = [MeshError(503, "unavailable")]
        r.effects.on_end(a, dict(a.wait), "woken", close_request=False)
        self.assertIsNotNone(self.pending(r, t))
        again = time_wait(hours=30)
        r.effects.on_wait(a, mesh.tasks[t["id"]], again, r.me)
        self.assertIsNone(self.pending(r, t))
        self.assertTrue(same_instant(self.start_after(mesh, t), again["scheduled"]))

    # ---------------------------------------------------------------- not ours
    def test_someone_elses_schedule_is_left_alone(self):
        r, mesh, t, a = self.setup_wait()
        theirs = mesh_time(iso(now() + dt.timedelta(days=2)))
        mesh.tasks[t["id"]]["start_after"] = theirs
        before = len(mesh.patches)
        wait = dict(a.wait)
        r.effects.on_end(a, wait, "woken", close_request=False)
        self.assertEqual(self.start_after(mesh, t), theirs)
        self.assertEqual(len(mesh.patches), before)
        self.assertNotIn("scheduled", wait)
        self.assertIsNone(self.pending(r, t))

    def test_a_409_retry_stops_when_the_fresh_read_shows_another_schedule(self):
        r, mesh, t, a = self.setup_wait()
        theirs = mesh_time(iso(now() + dt.timedelta(days=2)))

        def conflicting(tid, **kw):
            del mesh.compare_clear_start_after
            mesh.tasks[tid]["start_after"] = theirs     # the concurrent write we lost to
            raise conflict()
        mesh.compare_clear_start_after = conflicting
        r.effects.on_end(a, dict(a.wait), "woken", close_request=False)
        self.assertEqual(self.start_after(mesh, t), theirs)
        self.assertIsNone(self.pending(r, t))

    # ---------------------------------------------------------------- lost answers on schedule
    def test_a_schedule_whose_answer_was_lost_is_still_taken_back(self):
        t = task(1, cat="in_progress")
        mesh = RealisticMesh([t])
        r = self.runner(mesh)
        r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        mesh.apply_failed = True
        mesh.patch_errors = [MeshError(0, "TimeoutError: timed out")]
        wait = time_wait()
        r.effects.on_wait(a, t, wait, r.me)
        self.assertIsNotNone(self.start_after(mesh, t))   # committed although the answer was lost
        self.assertNotIn("scheduled", wait)
        self.assertTrue(r.effects.pending_cleanup(wait))
        mesh.apply_failed = False
        r.effects.on_end(a, wait, "woken", close_request=False)
        self.assertIsNone(self.start_after(mesh, t))

    def test_a_refused_schedule_is_not_taken_for_ours(self):
        r, mesh, t, a = self.setup_wait()
        mesh.patch_errors = [conflict()]                   # 409: nothing was written
        wait = time_wait(hours=30)
        r.effects.on_wait(a, mesh.tasks[t["id"]], wait, r.me)
        self.assertNotIn("scheduled_maybe", wait)

    # ---------------------------------------------------------------- orphan reconcile
    def orphan(self, *, open_owner=False, foreign=False, closed_days_ago=0):
        t = task(1, cat="todo")
        mesh = RealisticMesh([t])
        r = self.runner(mesh)
        r.refresh_me()
        scheduled = iso(now() + dt.timedelta(hours=20))
        mesh.tasks[t["id"]].update(
            start_after=mesh_time(iso(now() + dt.timedelta(days=2)) if foreign else scheduled),
            start_after_origin="agent_evidence", start_after_receipt_id="rcpt-1")
        old = r.store.create("other-lane", t["id"], "claude")
        r.store.update(old, state="waiting" if open_owner else "completed",
                       wait={"kind": "merge", "target": "g/app!7", "scheduled": scheduled},
                       note="" if open_owner else "task left the agent while waiting")
        if closed_days_ago:
            r.store.db.execute("UPDATE attempts SET updated_at=? WHERE id=?",
                               (iso(now() - dt.timedelta(days=closed_days_ago)), old.id))
            r.store.db.commit()
        return r, mesh, t

    def test_orphan_schedule_of_a_closed_attempt_is_cleared_by_the_holder(self):
        r, mesh, t = self.orphan()
        self.assertEqual(r.hold_reason(dict(mesh.tasks[t["id"]]), "work"), "")
        self.assertIsNone(self.start_after(mesh, t))

    def test_orphan_schedule_of_an_attempt_closed_days_ago_is_still_cleared(self):
        # a time wait schedules days ahead: the attempt's age does not hide its schedule
        r, mesh, t = self.orphan(closed_days_ago=5)
        self.assertEqual(r.hold_reason(dict(mesh.tasks[t["id"]]), "work"), "")
        self.assertIsNone(self.start_after(mesh, t))

    def test_orphan_reconcile_leaves_an_open_attempts_schedule(self):
        r, mesh, t = self.orphan(open_owner=True)
        self.assertTrue(r.hold_reason(dict(mesh.tasks[t["id"]]), "work").startswith("start_after"))
        self.assertIsNotNone(self.start_after(mesh, t))

    def test_orphan_reconcile_leaves_a_foreign_schedule(self):
        r, mesh, t = self.orphan(foreign=True)
        self.assertTrue(r.hold_reason(dict(mesh.tasks[t["id"]]), "work").startswith("start_after"))
        self.assertIsNotNone(self.start_after(mesh, t))

    def test_dry_run_never_reconciles(self):
        r, mesh, t = self.orphan()
        before = len(mesh.patches)
        self.assertTrue(r.hold_reason(dict(mesh.tasks[t["id"]]), "work", log_change=False)
                        .startswith("start_after"))
        self.assertEqual(len(mesh.patches), before)


if __name__ == "__main__":
    import unittest
    unittest.main()
