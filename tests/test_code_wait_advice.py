"""The runner's advice for code another card delivers follows the fleet rule: its merge request
merging (``wait_for on=mr … until=merged``) or a stack on its branch; without a merge request yet,
``wait_for on=card target_condition=artifact_published`` for it, then the stack. A plain card wait
(no condition, ``shipped``, ``task_done``) is never advised for code: it holds the waiter until the
owner closes the card, long after the code merged, and lanes followed it word for word."""
import re
import unittest

from mesh_runner.outcome import open_mr_time_wait_guidance
from mesh_runner.runner import AgentRunner
from mesh_runner.wake import foreign_mr_next_step, foreign_mr_notice

SHA = "a" * 40


def card_waits(text: str) -> list[str]:
    """Every card wait the text advises, up to the end of its backticked command."""
    return re.findall(r"wait_for on=card[^`]*", text)


class CodeWaitAdvice(unittest.TestCase):
    def assert_code_advice(self, text: str, mr: str = "", needs_publish: bool = True):
        self.assertIn("on=mr", text)
        self.assertIn("until=merged", text)
        self.assertIn("stack", text)
        if mr:
            self.assertIn(mr, text)
        bare = [w for w in card_waits(text) if "target_condition=artifact_published" not in w]
        self.assertEqual(bare, [], text)
        self.assertNotIn("wait on that card", text)
        self.assertNotIn("on the card that fixes", text)
        if needs_publish:
            self.assertIn("target_condition=artifact_published", text)
        else:
            self.assertEqual(card_waits(text), [], text)

    # -- the shared phrase ----------------------------------------------------------------------
    def test_helpers(self):
        from mesh_runner.outcome import foreign_code_wait, mr_merged_wait
        self.assertEqual(mr_merged_wait("g/app!56"), "`wait_for on=mr project_path=g/app mr_iid=56 until=merged "
                                                     "after_sha=<full-head-sha> after_status=opened`")
        self.assertIn("provider=github project_path=o/r mr_iid=3", mr_merged_wait("github:o/r!3"))
        self.assertIn("project_path=<group>/<project> mr_iid=<iid>", mr_merged_wait(""))
        self.assert_code_advice(foreign_code_wait("1234abcd"))
        self.assertIn("target_task_id=1234abcd", foreign_code_wait("1234abcd"))
        self.assert_code_advice(foreign_code_wait("", "g/app!56"), "mr_iid=56", needs_publish=False)

    # -- 1. outcome.py: a red head of a merge request whose fix is not this card's -----------------
    def test_time_wait_guidance_on_a_red_head(self):
        text = open_mr_time_wait_guidance([{"target": "g/app!7", "sha": SHA, "pipeline_id": 41,
                                            "pipeline_status": "failed"}])
        self.assert_code_advice(text)

    # -- 2. wake.py: a task that only waits on another card's merge request ----------------------
    def test_foreign_mr_next_step_names_the_merge_request(self):
        for card in ("1234abcd", ""):
            with self.subTest(card=card):
                text = foreign_mr_next_step(card, "g/app!56")
                self.assert_code_advice(text, "project_path=g/app mr_iid=56", needs_publish=False)
                self.assertIn("do not push, merge or rebase its branch", text)
        notice = foreign_mr_notice("g/app!56", "head aaaaaaaa pipeline 9 failed", "1234abcd")
        self.assert_code_advice(notice, "project_path=g/app mr_iid=56", needs_publish=False)
        self.assertIn("#1234abcd", notice)
        github = foreign_mr_notice("github:o/r!3", "head aaaaaaaa failed", "1234abcd")
        self.assert_code_advice(github, "provider=github project_path=o/r mr_iid=3", needs_publish=False)

    # -- 3. runner.py: an until=change wait on a finished red head --------------------------------
    def test_dead_change_text_on_a_red_head(self):
        for kind, target in (("MR", "g/app!56"), ("pipeline", "g/app:900")):
            with self.subTest(kind=kind):
                self.assert_code_advice(AgentRunner._dead_change_text(kind, target, SHA, 900, "failed"))
        text = AgentRunner._foreign_change_text("MR g/app!56: pipeline 9 failed", "1234abcd", "g/app!56")
        self.assert_code_advice(text, "mr_iid=56", needs_publish=False)


if __name__ == "__main__":
    unittest.main()
