"""A release that fails on the wire keeps the lease token and is retried; only a definitive answer forgets it."""
import datetime as dt
import uuid

from mesh_runner.mesh import Checkout, MeshError
from mesh_runner.runner import RELEASE_PENDING, RELEASE_RETRY_SECONDS
from mesh_runner.store import iso, now
from fakes import FakeMesh, task
from test_runner import Bench

TIMEOUT = MeshError(0, "URLError: <urlopen error timed out>")


class LeaseMesh(FakeMesh):
    """Mesh's checkout endpoints: one live lease per task, DELETE needs the lease's own token
    (403 otherwise), POST on a live lease is a 409."""

    def __init__(self, tasks):
        super().__init__(tasks)
        self.lease: dict[str, dict] = {}
        self.release_errors: list[MeshError] = []     # raised before the DELETE reaches the lease
        self.release_calls: list[str] = []            # tokens as sent

    def checkout(self, tid, session_id, request_id, ttl_minutes=120):
        if tid in self.lease:
            raise MeshError(409, "Task is already checked out")
        self.lease[tid] = {"token": str(uuid.uuid4()), "session": session_id}
        return Checkout(tid, self.lease[tid]["token"], 1, "", session_id, request_id)

    def release(self, co):
        self.release_calls.append(co.token)
        if self.release_errors:
            err = self.release_errors[0]
            if err.status in (0, 408) or err.status >= 500:
                self.release_errors.pop(0)
                raise err
            self.release_errors.pop(0)
            return                                    # a definitive 403/404/409 is swallowed by MeshClient.release
        lease = self.lease.get(co.task_id)
        if lease and lease["token"] == co.token:
            del self.lease[co.task_id]


class ReleasePending(Bench):
    def setUp(self):
        super().setUp()
        self.t = task(1)
        self.tid = self.t["id"]
        self.mesh = LeaseMesh([self.t])
        self.r = self.runner(self.mesh)
        self.a = self.r.store.create("builder", self.tid, "claude", "t")
        self.assertTrue(self.r._acquire(self.a))
        self.a = self.r.store.get(self.a.id)
        self.token = self.a.checkout["token"]
        self.session = self.a.checkout["session_id"]

    def pending(self):
        return self.r._read_pending(self.tid)

    def due(self, **kw):
        p = dict(self.pending(), **kw)
        self.r._write_pending(self.tid, p)

    def test_transport_error_keeps_the_token(self):
        self.mesh.release_errors = [TIMEOUT]
        self.r._release_checkout(self.a)
        self.assertEqual(self.r.store.get(self.a.id).checkout, {})
        p = self.pending()
        self.assertEqual(p["token"], self.token)
        self.assertEqual(p["session_id"], self.session)
        self.assertEqual(p["generation"], 1)
        self.assertIn(self.tid, self.mesh.lease)

    def test_server_error_is_not_definitive_either(self):
        self.mesh.release_errors = [MeshError(503, "unavailable")]
        self.r._release_checkout(self.a)
        self.assertIsNotNone(self.pending())

    def test_success_leaves_no_record(self):
        self.r._release_checkout(self.a)
        self.assertIsNone(self.pending())
        self.assertNotIn(self.tid, self.mesh.lease)

    def test_retry_waits_for_its_backoff_then_releases(self):
        self.mesh.release_errors = [TIMEOUT]
        self.r._release_checkout(self.a)
        self.r.retry_releases()                       # not due yet: no request
        self.assertEqual(len(self.mesh.release_calls), 1)
        self.due(next_at=iso(now() - dt.timedelta(seconds=1)))
        with self.assertLogs("mesh_runner", "INFO") as cm:
            self.r.retry_releases()
        self.assertEqual(self.mesh.release_calls, [self.token, self.token])
        self.assertIsNone(self.pending())
        self.assertNotIn(self.tid, self.mesh.lease)
        self.assertEqual(sum("retried: ok" in m for m in cm.output), 1)

    def test_a_failing_retry_backs_off_and_logs_once(self):
        self.mesh.release_errors = [TIMEOUT, TIMEOUT, TIMEOUT]
        self.r._release_checkout(self.a)
        # the reason is the one already logged at the first failure, so the retries stay silent
        with self.assertNoLogs("mesh_runner", "WARNING"):
            for _ in range(2):
                self.due(next_at=iso(now() - dt.timedelta(seconds=1)))
                self.r.retry_releases()
        p = self.pending()
        self.assertEqual(p["tries"], 2)
        wait = (dt.datetime.fromisoformat(p["next_at"]) - now()).total_seconds()
        self.assertTrue(RELEASE_RETRY_SECONDS[2] - 5 < wait <= RELEASE_RETRY_SECONDS[2])

    def test_a_changed_reason_is_logged_once(self):
        self.mesh.release_errors = [TIMEOUT, MeshError(502, "bad gateway")]
        self.r._release_checkout(self.a)
        self.due(next_at=iso(now() - dt.timedelta(seconds=1)))
        with self.assertLogs("mesh_runner", "WARNING") as cm:
            self.r.retry_releases()
        self.assertEqual(sum("still failing" in m and "bad gateway" in m for m in cm.output), 1)

    def test_retrying_is_bounded_by_the_lease_expiry(self):
        self.mesh.release_errors = [TIMEOUT] * 50
        self.r._release_checkout(self.a)
        self.due(deadline=iso(now() - dt.timedelta(seconds=1)), next_at=iso(now() - dt.timedelta(seconds=1)))
        with self.assertLogs("mesh_runner", "WARNING") as cm:
            self.r.retry_releases()
        self.assertIsNone(self.pending())
        self.assertTrue(any("giving up" in m for m in cm.output))
        calls = len(self.mesh.release_calls)
        self.r.retry_releases()
        self.assertEqual(len(self.mesh.release_calls), calls)      # nothing left to resend

    def test_many_ticks_do_not_hammer_a_dead_mesh(self):
        self.mesh.release_errors = [TIMEOUT] * 500
        self.r._release_checkout(self.a)
        for _ in range(100):
            self.r.retry_releases()
        self.assertEqual(len(self.mesh.release_calls), 1)

    def test_agent_ticks_drive_the_retry_on_its_backoff(self):
        self.mesh.release_errors = [TIMEOUT] * 500
        self.r._release_checkout(self.a)
        for _ in range(100):
            self.r.tick()
        self.assertEqual(len(self.mesh.release_calls), 1)       # 100 ticks inside the first 30 s: no resend
        self.due(next_at=iso(now() - dt.timedelta(seconds=1)))
        self.mesh.release_errors = []
        self.r.tick()                                           # the tick itself retries and clears
        self.assertEqual(self.mesh.release_calls, [self.token, self.token])
        self.assertIsNone(self.pending())
        self.assertNotIn(self.tid, self.mesh.lease)

    def test_a_definitive_answer_clears_the_record(self):
        for status in (403, 404, 409):
            with self.subTest(status=status):
                self.mesh.lease.setdefault(self.tid, {"token": self.token, "session": "s"})
                self.r._write_pending(self.tid, {"token": self.token, "tries": 1, "attempt": self.a.id,
                                                 "next_at": iso(now() - dt.timedelta(seconds=1)),
                                                 "deadline": iso(now() + dt.timedelta(hours=1))})
                self.mesh.release_errors = [MeshError(status, "gone")]
                self.r.retry_releases()
                self.assertIsNone(self.pending())

    def test_checkout_409_releases_the_kept_token_and_retries_in_the_same_tick(self):
        self.mesh.release_errors = [TIMEOUT]
        self.r._release_checkout(self.a)
        self.r.store.update(self.a, state="completed")
        a2 = self.r.store.create("builder", self.tid, "claude", "t2")
        self.assertTrue(self.r._acquire(a2))                    # POST 409 -> DELETE with the kept token -> POST ok
        self.assertEqual(self.mesh.release_calls, [self.token, self.token])
        self.assertIsNone(self.pending())
        self.assertNotEqual(self.r.store.get(a2.id).checkout["token"], self.token)

    def test_checkout_409_with_a_release_that_keeps_failing_gives_up_after_one_try(self):
        self.mesh.release_errors = [TIMEOUT] * 10
        self.r._release_checkout(self.a)
        self.r.store.update(self.a, state="completed")
        a2 = self.r.store.create("builder", self.tid, "claude", "t2")
        self.assertFalse(self.r._acquire(a2))
        self.assertEqual(len(self.mesh.release_calls), 2)       # the original + exactly one forced retry
        self.assertIsNotNone(self.pending())                    # still kept for the tick retries

    def test_a_foreign_409_without_a_record_does_not_touch_release(self):
        self.mesh.lease[self.tid] = {"token": "foreign", "session": "other"}
        self.r.store.update(self.a, state="completed", checkout={})
        a2 = self.r.store.create("builder", self.tid, "claude", "t2")
        self.assertFalse(self.r._acquire(a2))
        self.assertEqual(self.mesh.release_calls, [])

    def test_pending_release_survives_a_restart(self):
        self.mesh.release_errors = [TIMEOUT]
        self.r._release_checkout(self.a)
        r2 = self.runner(self.mesh)
        self.assertEqual(r2._read_pending(self.tid)["token"], self.token)
