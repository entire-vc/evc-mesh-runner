import unittest

from mesh_runner import prompt as prompts

TID = "0123456789abcdef0123456789abcdef0123"
TASK = {"id": TID, "title": "t", "priority": "high", "labels": []}


def c(i, body, who="Foreman", at=None):
    return {"id": f"c{i:03d}", "author_name": who, "created_at": at or f"2026-11-04T10:{i % 60:02d}:00Z", "body": body}


class DeltaContext(unittest.TestCase):
    HARNESSES = ("claude", "codex", "glm")

    def test_initial_and_review_are_bounded_for_every_harness(self):
        comments = [c(i, "note %d " % i + "x" * 900) for i in range(30)]
        for h in self.HARNESSES:
            for fn in (prompts.start_prompt, prompts.review_prompt):
                text = fn(TASK, "Lane", ["/w"], comments, harness=h)
                base = len(fn(TASK, "Lane", ["/w"], [], harness=h))
                self.assertIn(f"running on the {h} harness", text)
                self.assertNotIn("all comments", text)
                self.assertIn("not the whole thread", text)
                self.assertIn("+24 older comments not shown", text)
                self.assertIn("list_comments(task_id, full=true", text)
                self.assertLess(len(text), base + 6 * 800)

    def test_ids_and_times_are_present(self):
        text = prompts.start_prompt(TASK, "Lane", ["/w"], [c(1, "hello")])
        self.assertIn("c001 2026-11-04T10:01:00Z Foreman: hello", text)

    def test_old_decision_outside_tail_is_pinned(self):
        comments = [c(0, "SHIP on HEAD abc; do not touch the parser")] + [c(i, "chatter") for i in range(1, 12)]
        text = prompts.start_prompt(TASK, "Lane", ["/w"], comments)
        self.assertIn("Older decisions/gates outside the tail", text)
        self.assertIn("c000", text)
        self.assertIn("do not touch the parser", text)
        self.assertIn("+5 older comments not shown", text)

    def test_long_blocking_comment_is_marked_truncated_and_keeps_its_end(self):
        body = "analysis " * 400 + "\n❓ Blocking @morgan: ship?"
        text = prompts.new_comments_block([c(1, body)], size=1500)
        self.assertIn("TRUNCATED", text)
        self.assertIn("fetch it before relying on it", text)
        self.assertIn("❓ Blocking @morgan: ship?", text)
        self.assertIn("c001", text)

    def test_short_comment_is_not_marked(self):
        self.assertNotIn("TRUNCATED", prompts.new_comments_block([c(1, "ok")]))

    def test_new_comments_are_not_repeated_in_review(self):
        comments = [c(i, f"body-{i}") for i in range(4)]
        news = comments[2:]
        text = prompts.review_prompt(TASK, "L", ["/w"], comments, news=prompts.new_comments_block(news),
                                     news_comments=news)
        self.assertEqual(text.count("body-3"), 1)
        self.assertEqual(text.count("body-2"), 1)
        self.assertIn("body-1", text)

    def test_older_omitted_comments_in_resume_delta_are_counted(self):
        text = prompts.new_comments_block([c(i, "n") for i in range(12)], limit=8)
        self.assertIn("4 older omitted", text)
        self.assertIn("c011", text)
        self.assertNotIn("c003", text)

    def test_wake_prompt_does_not_demand_full_history(self):
        text = prompts.wake_prompt(TASK, "a comment")
        self.assertNotIn("Re-read the task's new comments", text)
        self.assertIn("concrete gap", text)

    def test_no_comments(self):
        self.assertIn("- (none)", prompts.start_prompt(TASK, "L", ["/w"], []))

    def test_fetch_hint_does_not_offer_the_cursor_as_an_exact_comment_lookup(self):
        text = prompts.start_prompt(TASK, "L", ["/w"], [c(i, "note") for i in range(12)])
        self.assertIn("full=true, limit=N, page=P", text)
        self.assertIn("returns only newer comments, excluding the cursor comment", text)
        self.assertIn("use a page to fetch its body", text)


if __name__ == "__main__":
    unittest.main()
