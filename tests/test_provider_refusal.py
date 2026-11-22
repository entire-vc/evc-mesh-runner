"""A provider's refusal of a Claude turn's content is not a transient harness error.

18.11 04:29-04:31Z: four interactive Claude turns of one card ended on the assistant message
``stop_reason: refusal`` (``stop_details.category: reasoning_extraction``) and the driver's
``error_during_execution / invalid_request`` result. The runner resumed the same session four times
(each refused the same way, 0 output tokens), then handed the card up as
``harness error [sha256:…; details in local turn log]``: the lead could neither see the cause nor
unblock it. The journal below is that turn's recorded shape, its prompt events left out."""
import json
import sys
import unittest
from pathlib import Path

from fakes import FakeMesh, FakeProfileAdapter, calls, task, wait_time, write_replies
from mesh_runner.adapters.claude import ClaudeAdapter, refusal_category
from mesh_runner.runner import PROVIDER_REFUSAL_ROUTE
from test_runner import Bench

JOURNAL = Path(__file__).parent / "fixtures" / "claude_interactive_refusal.jsonl"
PROVIDER_TEXT = ("Terms of Service", "safeguards flagged", "invalid_request", "anthropic.com")


class ParseRefusal(unittest.TestCase):
    def test_recorded_refusal_is_typed_with_its_category(self):
        r = ClaudeAdapter().parse(JOURNAL)
        self.assertTrue(r.is_error)
        self.assertEqual(r.provider_refusal, "reasoning_extraction")

    def test_only_the_typed_stop_reason_counts(self):
        self.assertEqual(refusal_category({"stop_reason": "end_turn", "stop_details": {"category": "cyber"}}), "")
        self.assertEqual(refusal_category({"stop_reason": "refusal"}), "unspecified")
        for odd in ("This request was blocked as it seems", "A" * 3, "x" * 80, 7, None):
            with self.subTest(category=odd):
                self.assertEqual(refusal_category({"stop_reason": "refusal", "stop_details": {"category": odd}}),
                                 "unspecified")

    def test_a_refusal_the_turn_got_past_is_none(self, ):
        lines = JOURNAL.read_text().splitlines()
        later = {"type": "assistant", "session_id": "SESSION_ID", "message": {
            "id": "m2", "role": "assistant", "stop_reason": "end_turn", "usage": {"output_tokens": 3},
            "content": [{"type": "text", "text": "done"}]}}
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "turn.jsonl"
            path.write_text("\n".join(lines[:2] + [json.dumps(later)] + lines[2:]) + "\n")
            self.assertEqual(ClaudeAdapter().parse(path).provider_refusal, "")


class RefusalSettles(Bench):
    def events(self, kind):
        path = self.cfg.state_dir / "events.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [row for row in rows if row["kind"] == kind]

    def bodies(self, mesh, t):
        return " ".join(c["body"] for c in mesh.comment_log.get(t["id"], []))

    def test_refusal_without_another_harness_goes_up_at_once_naming_the_category(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: f"JOURNAL:{JOURNAL}",
                                     t["id"][:8] + ":resume": f"JOURNAL:{JOURNAL}"})
        r = self.runner(mesh)
        for _ in range(8):
            r.tick()
            self.settle_all(r)
        # the same session is refused again: one turn, not max_failures + 1 resumes of it
        self.assertEqual(len(calls(self.calls)), 1)
        self.assertEqual(self.states(r)[t["id"][:8]], "failed")
        bodies = self.bodies(mesh, t)
        self.assertIn("provider refusal: reasoning_extraction", bodies)
        self.assertNotIn("harness error [sha256:", bodies)
        for text in PROVIDER_TEXT:
            self.assertNotIn(text, bodies)
        [row] = self.events("provider_refusal")
        self.assertEqual({k: row[k] for k in ("agent", "task", "harness", "category", "route", "action")},
                         {"agent": "builder", "task": t["id"], "harness": "claude",
                          "category": "reasoning_extraction", "route": "", "action": "hand_up"})

    def test_refusal_moves_only_this_card_to_the_lanes_codex_in_a_new_session(self):
        t, other = task(1), task(2)
        mesh = FakeMesh([t, other], max_concurrent=2)
        seen = self.root / "env-command.log"
        self.agent.env_command = [sys.executable, "-c", (
            "import os; open(%r, 'a').write(os.environ.get('MESH_RUNNER_TASK', '')[:8] + '='"
            " + os.environ.get('MESH_RUNNER_TASK_HARNESS', '-') + '\\n'); print('X=1')") % str(seen)]
        write_replies(self.replies, {
            t["id"][:8]: f"JOURNAL:{JOURNAL}",
            t["id"][:8] + ":resume": f"JOURNAL:{JOURNAL}",
            t["id"][:8] + ":codex": wait_time(why="soak window"),
            other["id"][:8]: wait_time(why="soak window"),
        })
        r = self.runner(mesh)
        r.alt_adapters = {"codex": FakeProfileAdapter(r.adapter.script, "base", native="codex")}
        for _ in range(6):
            r.tick()
            self.settle_all(r)
        mine = [c for c in calls(self.calls) if c["task"] == t["id"][:8]]
        self.assertEqual(len(mine), 2, mine)                              # refused once, then on Codex
        self.assertEqual((mine[1]["resume"], mine[1].get("profile")), (False, "base"))
        self.assertNotEqual(mine[1]["sid"], mine[0]["sid"])              # a new session, not the refused one
        self.assertEqual(self.states(r)[t["id"][:8]], "waiting")
        [a] = [x for x in r.store.by_state(self.agent.name, ("waiting",)) if x.task_id == t["id"]]
        self.assertEqual(a.harness, "codex")
        self.assertEqual(r.store.mark(self.agent.name, t["id"], PROVIDER_REFUSAL_ROUTE)[0], "codex")
        # the lane's other card keeps the lane's route: Claude, no per-card harness for its env command
        [b] = [x for x in r.store.by_state(self.agent.name, ("waiting",)) if x.task_id == other["id"]]
        self.assertEqual(b.harness, "claude")
        theirs = [c for c in calls(self.calls) if c["task"] == other["id"][:8]]
        self.assertEqual([c.get("profile") for c in theirs], [""])
        log = seen.read_text().splitlines()
        self.assertIn(f"{t['id'][:8]}=codex", log)
        self.assertNotIn(f"{other['id'][:8]}=codex", log)
        self.assertTrue(all(line.endswith("=-") for line in log if line.startswith(other["id"][:8])), log)
        [row] = self.events("provider_refusal")
        self.assertEqual({k: row[k] for k in ("harness", "category", "route", "action")},
                         {"harness": "claude", "category": "reasoning_extraction", "route": "codex",
                          "action": "route"})
        self.assertNotIn("RUNNER GAVE UP", self.bodies(mesh, t))

    def test_a_card_moved_once_is_not_moved_back_on_another_refusal(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        r.alt_adapters = {"codex": FakeProfileAdapter(r.adapter.script, "base", native="codex")}
        a = r.store.create(self.agent.name, t["id"], "claude")
        r.store.mark(self.agent.name, t["id"], PROVIDER_REFUSAL_ROUTE, "codex")
        r.store.update(a, state="running", turns=1, log_path=str(JOURNAL), session_id="refused-session")
        r.settle(r.store.get(a.id))
        self.assertEqual(r.store.get(a.id).state, "failed")
        bodies = self.bodies(mesh, t)
        self.assertIn("provider refusal: reasoning_extraction on claude", bodies)
        self.assertIn("already moved once", bodies)

    def test_a_refusal_on_the_last_turn_of_the_budget_still_moves_the_card_or_names_it(self):
        # the refused turn spent the whole budget (max_turns=1): the move still gets its turn on Codex;
        # without Codex the card goes up naming the refusal, not "stopped after 1 turns"
        self.cfg.max_turns = 1
        for alt in (True, False):
            with self.subTest(alt=alt):
                t = task(20 + alt)
                mesh = FakeMesh([t], max_concurrent=1)
                write_replies(self.replies, {t["id"][:8]: f"JOURNAL:{JOURNAL}",
                                             t["id"][:8] + ":resume": f"JOURNAL:{JOURNAL}",
                                             t["id"][:8] + ":codex": wait_time(why="soak window")})
                r = self.runner(mesh)
                if alt:
                    r.alt_adapters = {"codex": FakeProfileAdapter(r.adapter.script, "base", native="codex")}
                for _ in range(6):
                    r.tick()
                    self.settle_all(r)
                mine = [c for c in calls(self.calls) if c["task"] == t["id"][:8]]
                bodies = self.bodies(mesh, t)
                self.assertNotIn("stopped after", bodies)
                [row] = [e for e in self.events("provider_refusal") if e["task"] == t["id"]]
                if alt:
                    self.assertEqual([(c["resume"], c.get("profile") or "") for c in mine], [(False, ""), (False, "base")])
                    self.assertEqual(self.states(r)[t["id"][:8]], "waiting")
                    self.assertEqual((row["route"], row["action"]), ("codex", "route"))
                else:
                    self.assertEqual(len(mine), 1)
                    self.assertEqual(self.states(r)[t["id"][:8]], "failed")
                    self.assertIn("provider refusal: reasoning_extraction", bodies)
                    self.assertEqual((row["route"], row["action"]), ("", "hand_up"))

    def test_a_review_refused_on_its_last_turn_is_classified_before_the_cap(self):
        # a review is capped at settle, before its outcome is read: the refusal is read first
        self.cfg.max_turns = 2
        for alt in (True, False):
            with self.subTest(alt=alt):
                t = task(30 + alt, cat="review")
                mesh = FakeMesh([t], max_concurrent=1)
                r = self.runner(mesh)
                r.refresh_me()
                if alt:
                    r.alt_adapters = {"codex": FakeProfileAdapter(r.adapter.script, "base", native="codex")}
                a = r.store.create(self.agent.name, t["id"], "claude", mode="review")
                r.store.update(a, state="running", turns=2, log_path=str(JOURNAL), session_id="refused-session")
                r.settle(r.store.get(a.id))
                a = r.store.get(a.id)
                bodies = self.bodies(mesh, t)
                self.assertNotIn("stopped after", bodies)
                [row] = [e for e in self.events("provider_refusal") if e["task"] == t["id"]]
                self.assertEqual(row["category"], "reasoning_extraction")
                if alt:
                    self.assertEqual((a.state, row["route"]), ("ready", "codex"))
                    self.assertEqual(r.store.mark(self.agent.name, t["id"], PROVIDER_REFUSAL_ROUTE)[0], "codex")
                    # the refused turn is outside the budget, so the resume on Codex is not the turn cap
                    self.assertLess(a.turns - r.store.auth_turns(a.id), self.cfg.max_turns)
                else:
                    self.assertEqual((a.state, row["route"]), ("failed", ""))
                    self.assertIn("provider refusal: reasoning_extraction on claude", bodies)

    def test_refusal_ending_a_turn_without_an_error_envelope_is_not_a_missed_outcome(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        rows = [json.loads(line) for line in JOURNAL.read_text().splitlines()]
        for row in rows:
            if row["type"] == "result":
                row.update(subtype="success", is_error=False, result="")
        path = self.root / "turn.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        a = r.store.create(self.agent.name, t["id"], "claude")
        r.store.update(a, state="running", turns=1, log_path=str(path), session_id="refused-session")
        r.settle(r.store.get(a.id))
        a = r.store.get(a.id)
        self.assertEqual((a.state, a.no_outcome), ("failed", 0))
        self.assertIn("provider refusal: reasoning_extraction", self.bodies(mesh, t))


if __name__ == "__main__":
    unittest.main()
