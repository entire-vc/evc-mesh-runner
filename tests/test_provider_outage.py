"""A provider outage is retried with backoff; it is not a failed turn, a start or a hand-off."""
import time
import unittest

from fakes import FakeMesh, task
from test_runner import Loop


class ProviderOutage(Loop):
    def outage(self, r, t, **kw):
        return self.error_envelope(r, t, "authentication_failed", **kw)

    def test_failed_turns_past_the_cap_do_not_give_up(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        a = self.outage(r, t, failures=self.cfg.max_failures)
        self.assertEqual((a.state, a.no_outcome), ("ready", self.cfg.max_failures))
        self.assertEqual(mesh.comment_log, {})
        self.assertGreater(r._resume_hold[a.id], time.monotonic())

    def test_backoff_doubles_and_does_not_spend_the_turn_budget(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.max_turns = 2
        r = self.runner(mesh)
        a = self.outage(r, t)
        delays = []
        for turn in range(2, 6):
            r.store.update(a, state="running", turns=turn, log_path=a.log_path)
            r.settle(r.store.get(a.id))
            delays.append(r._resume_hold[a.id] - time.monotonic())
        self.assertEqual(r.store.get(a.id).state, "ready")
        self.assertEqual(r.store.auth_turns(a.id), 5)
        self.assertTrue(all(b > x for x, b in zip(delays, delays[1:])))
        self.assertEqual(mesh.comment_log, {})

    def test_gives_up_only_after_a_continuous_outage(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.provider_outage_giveup_minutes = 60
        r = self.runner(mesh)
        a = self.outage(r, t)
        r.store.mark(self.agent.name, t["id"], "outage_since", "2000-01-01T00:00:00+00:00")
        r.store.update(a, state="running", turns=2, log_path=a.log_path)
        r.settle(r.store.get(a.id))
        self.assertEqual(r.store.get(a.id).state, "failed")
        self.assertIn("provider has failed", " ".join(c["body"] for c in mesh.comment_log[t["id"]]))

    def test_a_good_turn_ends_the_outage(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        a = self.outage(r, t)
        path = self.root / f"{a.id}.jsonl"
        path.write_text('{"type": "result", "is_error": true, "result": "some other failure"}\n')
        r.store.update(a, state="running", turns=2, log_path=str(path))
        r.settle(r.store.get(a.id))
        self.assertFalse(r.store.mark(self.agent.name, t["id"], "outage_since")[0])

    def test_a_successful_turn_ends_the_outage(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        a = self.outage(r, t)
        self.assertTrue(r.store.mark(self.agent.name, t["id"], "outage_since")[0])
        path = self.root / f"{a.id}-ok.jsonl"
        path.write_text('{"type": "result", "is_error": false, "result": "done a piece"}\n')
        r.store.update(a, state="running", turns=2, log_path=str(path))
        r.settle(r.store.get(a.id))
        self.assertFalse(r.store.mark(self.agent.name, t["id"], "outage_since")[0])

    def test_an_outage_on_the_last_turn_is_not_a_turn_cap_give_up(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.max_turns = 1
        r = self.runner(mesh)
        a = self.outage(r, t)
        self.assertEqual(r.store.get(a.id).state, "ready")
        self.assertEqual(mesh.comment_log, {})

    def test_outage_turns_are_not_a_start(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        a = self.outage(r, t)
        self.assertEqual(r.store.starts_today(self.agent.name, t["id"]), 0)

    def test_a_shorter_backoff_does_not_shorten_the_shared_pause(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        for lane in ("A", "B", "C"):
            a = r.store.create(lane, f"task-{lane}", "claude")
            r.store.event(a.id, "provider_outage", "1")
        late = "2999-01-01T00:00:00+00:00"
        r.store.mark("_runtime", "_provider_outage", "pause_until", late)
        self.outage(r, t)
        self.assertEqual(r.store.mark("_runtime", "_provider_outage", "pause_until")[0], late)

    def test_a_stderr_only_bootstrap_outage_on_the_last_turn_is_retried(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        self.cfg.max_turns = 1
        r = self.runner(mesh)
        r.refresh_me()
        a = r.store.create(self.agent.name, t["id"], "claude")
        path = self.root / f"{a.id}-boot.jsonl"
        path.write_text("")
        path.with_suffix(".stderr").write_text("account/read failed during TUI bootstrap\n")
        r.store.update(a, state="running", turns=1, log_path=str(path))
        r.settle(r.store.get(a.id))
        self.assertEqual(r.store.get(a.id).state, "ready")
        self.assertEqual(mesh.comment_log, {})

    def test_several_lanes_failing_pause_every_start(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        self.assertFalse(r._outage_paused())
        for lane in ("A", "B", "C"):
            a = r.store.create(lane, f"task-{lane}", "claude")
            r.store.event(a.id, "provider_outage", "1")
        self.outage(r, t)
        self.assertTrue(r._outage_paused())
        self.assertEqual(r.start_new(1), 1)


if __name__ == "__main__":
    unittest.main()
