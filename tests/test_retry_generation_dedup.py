"""A retried pipeline keeps its id: its next terminal outcome is a new notice, a repeated poll is not."""
import datetime as dt
import hashlib
import unittest

from fakes import FakeGitLab, FakeMesh, task, write_replies
from mesh_runner.outcome import parse_line
from mesh_runner.store import iso
from mesh_runner.wake import ci_change_event
from test_runner import Bench

SHA = "a" * 40
TARGET = "g/app#5"
BASE = dt.datetime(2026, 10, 1, 12, 0, 0, tzinfo=dt.timezone.utc)


def stamp(minutes: int, base: dt.datetime = BASE) -> str:
    return (base + dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def row(status: str, finished: str | None = None) -> dict:
    out = {"id": 5, "sha": SHA, "status": status}
    if finished is not None:
        out["finished_at"] = finished
    return out


def legacy_key_and_value(status: str = "failed") -> tuple[str, str]:
    identity = f"{TARGET}:{SHA}:5:{status}"
    return "ci_action_required:" + hashlib.sha256(identity.encode()).hexdigest(), identity


class Stored(str):
    """A stored mark with the time it was stored, as the runner's reader returns it."""

    def __new__(cls, value: str, at: str):
        obj = super().__new__(cls, value)
        obj.at = at
        return obj


class Marks:
    """Delivery marks as the runner keeps them: written only when a notice is delivered."""

    def __init__(self):
        self.rows: dict[str, tuple[str, str]] = {}

    def reader(self, key: str):
        found = self.rows.get(key)
        return Stored(found[0], found[1]) if found else ""


class RetryGenerationDedup(unittest.TestCase):
    def setUp(self):
        self.gl = FakeGitLab()
        self.marks = Marks()
        self.contract = {"sha": SHA, "pipeline": 5, "status": "running", "predicate": "success"}
        self.clock = BASE

    def poll(self, status: str, finished: str | None = None) -> str:
        self.gl.pipeline_rows[TARGET] = row(status, finished)
        observation: dict = {}
        event = ci_change_event(self.gl, "pipeline", TARGET, self.contract, self.marks.reader,
                                observation, allow_action_required=True)
        if event:       # the runner stores the mark when the notice is delivered
            self.marks.rows[observation["key"]] = (observation["value"], self.clock.isoformat())
        return event

    def test_first_failure_wakes_exactly_once(self):
        self.assertEqual(self.poll("running"), "")
        first = self.poll("failed", stamp(1))
        self.assertIn("action required", first)
        self.assertIn("failed", first)
        self.assertEqual(self.poll("failed", stamp(1)), "")

    def test_repeated_polls_of_the_unchanged_failure_do_not_wake(self):
        self.assertIn("action required", self.poll("failed", stamp(1)))
        for _ in range(5):
            self.assertEqual(self.poll("failed", stamp(1)), "")
        self.assertEqual(len(self.marks.rows), 1)

    def test_failure_after_a_retry_of_the_same_pipeline_wakes_once_more(self):
        self.assertIn("action required", self.poll("failed", stamp(1)))     # job A failed
        self.clock = BASE + dt.timedelta(minutes=2)
        self.assertEqual(self.poll("running"), "")                          # job A retried
        self.assertEqual(self.poll("running"), "")
        again = self.poll("failed", stamp(10))                              # job B failed
        self.assertIn("action required", again)
        self.assertIn("failed", again)
        self.assertEqual(self.poll("failed", stamp(10)), "")                # and only once
        self.assertEqual(self.poll("failed", stamp(10)), "")

    def test_retry_that_settles_between_two_polls_is_still_seen(self):
        self.assertIn("action required", self.poll("failed", stamp(1)))
        self.clock = BASE + dt.timedelta(minutes=2)
        # no observation inside the in-progress phase at all
        self.assertIn("action required", self.poll("failed", stamp(10)))

    def test_success_after_a_retry_wakes_by_the_success_rules(self):
        self.assertIn("action required", self.poll("failed", stamp(1)))
        self.assertEqual(self.poll("running"), "")
        done = self.poll("success", stamp(10))
        self.assertIn("changed CI", done)
        self.assertIn("success", done)
        self.assertNotIn("action required", done)
        self.assertEqual(self.poll("success", stamp(10)), "")

    def test_pre_generation_mark_does_not_wake_again_for_an_unchanged_failure(self):
        key, value = legacy_key_and_value()
        self.marks.rows[key] = (value, (BASE + dt.timedelta(minutes=5)).isoformat())   # stored after it settled
        self.clock = BASE + dt.timedelta(minutes=6)
        for _ in range(3):
            self.assertEqual(self.poll("failed", stamp(1)), "")
        self.assertEqual(len(self.marks.rows), 1)
        # a later outcome of the same pipeline still wakes: the old mark covers only what it saw
        self.assertEqual(self.poll("running"), "")
        self.assertIn("action required", self.poll("failed", stamp(30)))
        self.assertEqual(self.poll("failed", stamp(30)), "")

    def test_pre_generation_mark_with_an_unreadable_time_covers_the_unchanged_failure(self):
        key, value = legacy_key_and_value()
        self.marks.reader = lambda k: value if k == key else ""      # a plain string, no time
        self.assertEqual(self.poll("failed", stamp(1)), "")

    def test_provider_row_without_finished_at_keys_as_before(self):
        self.assertIn("action required", self.poll("failed"))
        key, value = legacy_key_and_value()
        self.assertEqual(self.marks.rows[key][0], value)
        self.assertEqual(self.poll("failed"), "")


class RunnerWake(Bench):
    """The same defect through the production wake/spawn/receipt path."""

    def test_second_failure_of_a_retried_pipeline_wakes_the_waiting_attempt(self):
        t = task(1, cat="in_progress")
        gl = FakeGitLab()
        failed_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
        gl.pipeline_rows[TARGET] = row("failed", stamp(0, failed_at))
        r = self.runner(FakeMesh([t]), gl)
        r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        marker = f"⏳ WAIT pipeline:{TARGET} until=success after={SHA}:5:failed"
        outcome = parse_line(marker, dt.datetime.now(dt.timezone.utc))
        wait = {"kind": outcome.kind, "target": outcome.target, **outcome.extra,
                "since": iso(), "started": iso()}
        r.store.update(a, state="waiting", wait=wait, session_id="existing-session")

        r._wake_one(a)
        self.assertEqual(a.state, "ready")
        self.assertIn("action required", a.wake_text)
        write_replies(self.replies, {t["id"][:8]: marker})
        r._resume_one(a)                                   # the real spawn stores the delivery mark
        pipeline = gl.pipeline_rows[TARGET]
        status, pipeline["status"] = pipeline["status"], "running"      # the owner retries job A
        settled = self.finish_turn(r)
        pipeline["status"] = status
        self.assertEqual(settled.state, "waiting")

        r._wake_one(settled)                               # same failure, polled again
        self.assertEqual(settled.state, "waiting")
        pipeline["status"] = "running"
        r._wake_one(settled)
        self.assertEqual(settled.state, "waiting")
        pipeline.update(status="failed", finished_at=stamp(10, dt.datetime.now(dt.timezone.utc)))   # job B failed
        r._wake_one(settled)
        self.assertEqual(settled.state, "ready")
        self.assertIn("action required", settled.wake_text)


if __name__ == "__main__":
    unittest.main()
