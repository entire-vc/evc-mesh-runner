"""The start prompt carries the description, and a new attempt on a card the lane
already worked on is logged as a restart with how the previous attempt ended."""
import json
import unittest

from fakes import FakeMesh, ask_agent, calls, task, write_replies
from mesh_runner import prompt as prompts
from test_runner import Loop


class DescriptionBlock(unittest.TestCase):
    def test_the_description_is_in_the_prompt_and_long_ones_are_cut_with_a_marker(self):
        short = prompts.description_block(task(1, description="Do the thing.\nAC: `make test` green"))
        self.assertIn("Do the thing.\nAC: `make test` green", short)
        self.assertNotIn("cut at", short)
        long = prompts.description_block(task(1, description="x" * (prompts.DESCRIPTION_MAX + 50)))
        self.assertIn(f"cut at {prompts.DESCRIPTION_MAX} of {prompts.DESCRIPTION_MAX + 50} characters", long)
        self.assertLess(len(long), prompts.DESCRIPTION_MAX + 300)

    def test_empty_description_and_open_dependencies_are_named(self):
        self.assertIn("Description: (empty)", prompts.description_block(task(1)))
        text = prompts.description_block(task(1, open_dependency_count=2))
        self.assertIn("Open dependencies: 2", text)
        self.assertNotIn("Open dependencies", prompts.description_block(task(1, open_dependency_count=0)))

    def test_start_and_review_prompts_carry_it_instead_of_asking_to_fetch_it(self):
        t = task(1, description="UNIQUE-DESCRIPTION-BODY")
        for text in (prompts.start_prompt(t, "builder", ["/w"], []),
                     prompts.review_prompt(t, "reviewer", ["/w"], [])):
            self.assertIn("UNIQUE-DESCRIPTION-BODY", text)
            self.assertNotIn("Read the task description and dependencies with your Mesh tools", text)


class DescriptionReachesTheSession(Loop):
    def test_a_listing_row_without_description_still_gives_the_session_the_card_text(self):
        t = task(1, description="FULL-CARD-TEXT", open_dependency_count=1)
        mesh = FakeMesh([t], max_concurrent=1)
        listed = dict(t, description="")
        write_replies(self.replies, {t["id"][:8]: ask_agent("lead", "ready?")})
        r = self.runner(mesh)
        r.refresh_me()
        prompt = r._first_prompt(listed, "work", [])
        self.assertIn("FULL-CARD-TEXT", prompt)
        self.assertIn("Open dependencies: 1", prompt)
        r.tick()
        self.settle_all(r)
        self.assertIn("FULL-CARD-TEXT", calls(self.calls)[0]["prompt"])


class NewAttemptReason(Loop):
    def test_a_new_attempt_after_a_finished_one_names_the_previous_end(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: ask_agent("lead", "ready?")})
        r = self.runner(mesh)
        r.refresh_me()
        old = r.store.create(self.agent.name, t["id"], "claude")
        r.store.update(old, state="completed", turns=3, note="handed to review")
        with self.assertLogs("mesh_runner", level="INFO") as logs:
            r.tick()
            self.settle_all(r)
        self.assertEqual([c["resume"] for c in calls(self.calls)], [False])
        a = [x for x in r.store.on_task(t["id"]) if x.id != old.id][0]
        launch = [json.loads(d) for _, k, d in r.store.events(a.id) if k == "launch"][0]
        self.assertEqual((launch["restart_reason"], launch["previous_attempt"], launch["previous_state"],
                          launch["previous_note"]), ("new_attempt", old.id, "completed", "handed to review"))
        self.assertTrue(any("reason=new_attempt after completed: handed to review" in m for m in logs.output))

    def test_the_first_attempt_on_a_card_stays_a_first_start(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: ask_agent("lead", "ready?")})
        r = self.runner(mesh)
        with self.assertLogs("mesh_runner", level="INFO") as logs:
            r.tick()
            self.settle_all(r)
        a = r.store.on_task(t["id"])[0]
        launch = [json.loads(d) for _, k, d in r.store.events(a.id) if k == "launch"][0]
        self.assertEqual(launch["restart_reason"], "first_start")
        self.assertNotIn("previous_attempt", launch)
        self.assertTrue(any("reason=first_start" in m for m in logs.output))


if __name__ == "__main__":
    unittest.main()


class CorrectionAfterCompaction(Loop):
    def _corrected(self, compacted: bool, cat: str = "todo", mode: str = "work") -> str:
        t = task(1, cat=cat)
        mesh = FakeMesh([t], max_concurrent=1)
        write_replies(self.replies, {t["id"][:8]: "all done, nothing else"})
        r = self.runner(mesh)
        r.tick()
        a = r.store.by_state(self.agent.name, ("running",))[0]
        self.assertEqual(a.mode, mode)
        if compacted:
            r.store.mark(self.agent.name, a.task_id, "compactions", f"{a.id}:1")
        a = self.finish_turn(r)
        self.assertEqual((a.state, a.no_outcome), ("ready", 1))
        return " ".join(a.wake_text.split())

    def test_a_session_that_kept_its_context_gets_the_pointer(self):
        text = self._corrected(False)
        self.assertIn("outcome contract at the end of this session's first prompt", text)
        self.assertNotIn("target_condition=", text)

    def test_a_compacted_session_gets_the_full_contract_again(self):
        # K7: after a compaction the first prompt may be gone from the session
        text = self._corrected(True)
        self.assertNotIn("outcome contract at the end of this session's first prompt", text)
        self.assertIn("target_condition=", text)

    def test_a_review_session_gets_the_full_contract(self):
        # a review prompt carries the review protocol, not the outcome contract: no pointer to it
        text = self._corrected(False, cat="review", mode="review")
        self.assertNotIn("outcome contract at the end of this session's first prompt", text)
        self.assertIn("target_condition=", text)
