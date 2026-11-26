"""Blocking human-gate waits ignore agent/bot comments; held ready attempts publish stopped."""
import datetime as dt
import json
import unittest
from unittest.mock import patch

from mesh_runner.effects import Effects, runtime_state, skip_state
from mesh_runner.outcome import Outcome
from mesh_runner.store import Attempt
from mesh_runner.wake import check as wake_check
from mesh_runner.workspace_scope import RECEIPT_KEY
from fakes import FakeMesh, task, wait_time, write_replies
from test_runner import Bench

SINCE = "2026-10-18T00:00:00+00:00"
NOW = dt.datetime(2026, 10, 18, 0, 5, tzinfo=dt.timezone.utc)


def blocking_wait(**extra):
    return {"kind": "blocking", "handle": "operator", "since": SINCE, "started": SINCE,
            "target": "", "until": "", **extra}


def comment(author="other-agent", body="note", kind="agent", minute=1, **extra):
    c = {"id": f"c{minute}", "author_id": author, "author_name": author, "body": body,
         "created_at": f"2026-10-18T00:0{minute}:00+00:00", **extra}
    if kind is not None:
        c["author_type"] = kind
    return c


class BlockingHumanGateWake(unittest.TestCase):
    def test_agent_and_bot_comments_do_not_wake_blocking_wait_under_human_gate(self):
        gated = {"human_gate": True}
        cases = [
            comment("notifier", "🔔 Immediate alert sent to operator in Telegram", kind="agent"),
            comment("other-agent", "@builder please check this card", kind="agent"),
            comment("other-agent", "Answer: proceed now", kind="agent"),
            comment("coordinator", "FYI status update", kind="agent"),
            comment("alert-bot", "automated reminder", kind="bot"),
            comment("unknown-actor", "untyped automated note", kind=None),
        ]
        for mode in ("all", "addressed"):
            for c in cases:
                with self.subTest((mode, c["author_name"], c.get("author_type"), c["body"][:20])):
                    w = blocking_wait()
                    out = wake_check(None, None, None, gated, w, "me", [c], NOW,
                                     comment_wake=mode, agent_name="builder",
                                     comment_wake_always_from=("coordinator",))
                    self.assertEqual(out, "")
                    self.assertFalse(w.get("met"))

    def test_human_comment_still_wakes_blocking_wait_under_human_gate(self):
        gated = {"human_gate": True}
        w = blocking_wait()
        out = wake_check(None, None, None, gated, w, "me",
                         [comment("operator", "approved, go ahead", kind="user")], NOW,
                         comment_wake="addressed", agent_name="builder")
        self.assertIn("operator commented: approved, go ahead", out)
        self.assertTrue(w.get("met"))

    def test_agent_alert_after_human_comment_does_not_mask_human_answer(self):
        gated = {"human_gate": True}
        w = blocking_wait()
        comments = [
            comment("operator", "yes, ship it", kind="user", minute=1),
            comment("notifier", "🔔 Immediate alert sent", kind="agent", minute=2),
        ]
        out = wake_check(None, None, None, gated, w, "me", comments, NOW,
                         comment_wake="all", agent_name="builder")
        self.assertIn("operator commented: yes, ship it", out)
        self.assertTrue(w.get("met"))

    def test_lifted_human_gate_ends_blocking_wait(self):
        w = blocking_wait()
        out = wake_check(None, None, None, {"human_gate": False}, w, "me", [], NOW)
        self.assertEqual(out, "the human gate was lifted")
        self.assertTrue(w.get("met"))


class RuntimeStateResumeHold(unittest.TestCase):
    def test_runtime_state_for_held_ready_attempt_reports_stopped(self):
        a = Attempt(id="att-1", agent="builder", task_id="t-1", harness="claude", state="ready")
        self.assertEqual(runtime_state(a, "builder"), {"kind": "queued", "lane": "builder"})
        for reason in ("waits for a human decision", "waiting for a human decision",
                       "gate: waits for a human decision"):
            with self.subTest(reason=reason):
                self.assertEqual(
                    runtime_state(a, "builder", hold_reason=reason),
                    {"kind": "stopped", "reason_code": "human_gate", "reason": "waits for a human decision"},
                )
        self.assertEqual(
            skip_state("gate: waits for a human decision", "builder"),
            {"kind": "stopped", "reason_code": "human_gate", "reason": "waits for a human decision"},
        )
        self.assertEqual(
            runtime_state(a, "builder", hold_reason="runtime capacity unavailable"),
            {"kind": "stopped", "reason": "runtime capacity unavailable"},
        )
        self.assertEqual(
            runtime_state(a, "builder", hold_reason="paused"),
            {"kind": "stopped", "reason_code": "pause", "reason": "operator pause"},
        )

    def test_effects_publish_uses_resume_holds(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        fx = Effects(mesh, None, "builder")
        a = Attempt(id="att-1", agent="builder", task_id=t["id"], harness="claude", state="ready")
        fx.publish([a], holds={a.id: "waits for a human decision"})
        self.assertEqual(
            mesh.runtime[t["id"]],
            {"kind": "stopped", "reason_code": "human_gate", "reason": "waits for a human decision"},
        )


class RunnerBlockingAndResumeHold(Bench):
    def test_agent_comment_on_gated_blocking_wait_keeps_waiting_without_cancelling_mesh_wait(self):
        t = task(1, human_gate=True)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)).isoformat()
        wait = {"kind": "blocking", "handle": "operator", "since": since, "started": since,
                "mesh_wait_id": "wait-human-1"}
        a = r.store.create("builder", t["id"], "claude", title=t["title"])
        r.store.update(a, state="waiting", wait=wait)
        c1 = mesh.add_comment(t["id"], "🔔 Immediate alert sent to operator in Telegram", author="agent-notifier",
                              at=dt.datetime.now(dt.timezone.utc))
        c1["author_type"] = "agent"
        c2 = mesh.add_comment(t["id"], "@builder status?", author="agent-peer",
                              at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1))
        c2["author_type"] = "agent"

        resolved = []
        mesh.resolve_wait = lambda *args, **kw: resolved.append((args, kw))

        r.check_wakes()
        after = r.store.get(a.id)
        self.assertEqual(after.state, "waiting")
        self.assertEqual(resolved, [])

        # Human answer wakes the blocking wait
        c3 = mesh.add_comment(t["id"], "approved", author="operator",
                              at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=2))
        c3["author_type"] = "user"
        r._last_wake_check.clear()
        r.check_wakes()
        self.assertEqual(r.store.get(a.id).state, "ready")

    def test_review_event_ignores_agent_and_bot_comments_under_human_gate(self):
        t = task(1, human_gate=True)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)
        declared = Outcome("blocking", handle="operator", note="approve?")
        agent_comments = [
            {"id": "c1", "author_id": "agent-notifier", "author_name": "notifier", "author_type": "agent",
             "body": "🔔 Immediate alert sent", "created_at": (since + dt.timedelta(minutes=1)).isoformat()},
            {"id": "c2", "author_id": "agent-peer", "author_name": "peer", "author_type": "agent",
             "body": "@builder ping", "created_at": (since + dt.timedelta(minutes=2)).isoformat()},
            {"id": "c3", "author_id": "bot-1", "author_name": "alert-bot", "author_type": "bot",
             "body": "reminder", "created_at": (since + dt.timedelta(minutes=3)).isoformat()},
            {"id": "c3b", "author_id": "unknown-1", "author_name": "unknown",
             "body": "untyped note", "created_at": (since + dt.timedelta(minutes=3, seconds=30)).isoformat()},
        ]
        self.assertEqual(r._review_event(t, since, declared, agent_comments), "")

        human_comment = {"id": "c4", "author_id": "user-1", "author_name": "operator", "author_type": "user",
                         "body": "approved", "created_at": (since + dt.timedelta(minutes=4)).isoformat()}
        self.assertIn("operator commented: approved",
                      r._review_event(t, since, declared, agent_comments + [human_comment]))
        # Agent comment after the human comment does not hide the human comment
        trailing_agent = {"id": "c5", "author_id": "agent-notifier", "author_name": "notifier", "author_type": "agent",
                          "body": "noted", "created_at": (since + dt.timedelta(minutes=5)).isoformat()}
        self.assertIn("operator commented: approved",
                      r._review_event(t, since, declared, agent_comments + [human_comment, trailing_agent]))

        # Lifting the gate after gate_seen wakes even without a human comment
        t_lifted = dict(t, human_gate=False)
        self.assertEqual(r._review_event(t_lifted, since, declared, []),
                         "the human gate on your question was lifted")

    def test_tick_publishes_stopped_human_gate_for_ready_attempt_held_by_start_gate(self):
        t = task(1, human_gate=True)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        a = r.store.create("builder", t["id"], "claude", title=t["title"])
        r.store.update(a, state="ready")
        with patch.object(r, "_gate", return_value=("waits for a human decision", {})):
            r.tick()
            self.assertEqual(r.store.get(a.id).state, "ready")
            self.assertEqual(
                mesh.runtime[t["id"]],
                {"kind": "stopped", "reason_code": "human_gate", "reason": "waits for a human decision"},
            )
            # Second tick while _resume_hold suppresses _resume_one still keeps stopped/human_gate
            r.tick()
            self.assertEqual(
                mesh.runtime[t["id"]],
                {"kind": "stopped", "reason_code": "human_gate", "reason": "waits for a human decision"},
            )

    def test_tick_publishes_stopped_human_gate_for_ready_attempt_held_by_policy_and_recovers(self):
        t = task(1, human_gate=True)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: wait_time(why="external event")})
        r = self.runner(mesh)
        r.refresh_me()
        a = r.store.create("builder", t["id"], "claude", title=t["title"])
        r.store.update(a, state="ready")
        r.store.mark("builder", t["id"], RECEIPT_KEY, json.dumps({"attempt": a.id, "scope": "workspace"}))
        r.tick()
        self.assertEqual(r.store.get(a.id).state, "ready")
        self.assertEqual(
            mesh.runtime[t["id"]],
            {"kind": "stopped", "reason_code": "human_gate", "reason": "waits for a human decision"},
        )
        # Lifting human_gate and clearing backoff resumes normally and clears stopped state
        mesh.tasks[t["id"]]["human_gate"] = False
        r._resume_hold.clear()
        r.tick()
        self.assertIn(r.store.get(a.id).state, ("running", "waiting"))
        self.assertNotEqual(mesh.runtime[t["id"]].get("reason_code"), "human_gate")
        self.settle_all(r)

    def test_tick_publishes_stopped_for_non_human_resume_hold(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        a = r.store.create("builder", t["id"], "claude", title=t["title"])
        r.store.update(a, state="ready")
        with patch.object(r, "_gate", return_value=("runtime capacity unavailable", {})):
            r.tick()
            self.assertEqual(r.store.get(a.id).state, "ready")
            self.assertEqual(
                mesh.runtime[t["id"]],
                {"kind": "stopped", "reason": "runtime capacity unavailable"},
            )
            # Once the hold deadline expires with no active hold, stale reason is cleared
            r._resume_hold[a.id] = 0
            self.assertEqual(r._resume_holds([r.store.get(a.id)]), {})
            self.assertNotIn(a.id, r._resume_hold_reason)


if __name__ == "__main__":
    unittest.main()
