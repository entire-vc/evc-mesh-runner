"""The outcome module reads typed Mesh records only: no marker parser is left, and each typed turn command
builds the Outcome kind the runner settles on."""
import unittest

from mesh_runner import outcome as outcome_mod

from fakes import (ask_agent, ask_human, mesh_turn, next_turn, typed, wait_card, wait_merge, wait_mr, wait_pipeline)


class NoMarkerParser(unittest.TestCase):
    def test_the_marker_parser_is_gone(self):
        for name in ("parse_line", "parse_outcome", "declaration_lines"):
            self.assertFalse(hasattr(outcome_mod, name), name)


class TypedRowsBuildOutcomes(unittest.TestCase):
    def test_continue_next(self):
        o = typed(next_turn("tests next"))
        self.assertEqual((o.kind, o.note), ("next", "tests next"))

    def test_card_wait_carries_its_condition(self):
        o = typed(wait_card("abcdef12-0000-4000-8000-000000000000", condition="deployed"))
        self.assertEqual((o.kind, o.target), ("card", "abcdef12"))
        self.assertEqual(o.extra["card_condition"], "deployed")
        self.assertNotIn("card_condition", typed(wait_card("abcdef12")).extra)

    def test_card_cannot_wait_for_itself(self):
        self.assertEqual(typed(wait_card("abcdef12"), task8="abcdef12").kind, "unparsed")

    def test_pipeline_with_jobs(self):
        o = typed(wait_pipeline("grp/app", 123, jobs=("lint", "test")))
        self.assertEqual((o.kind, o.target, o.jobs), ("pipeline", "grp/app#123", ("lint", "test")))

    def test_mr_wait(self):
        o = typed(wait_mr("grp/sub/app", 42, until="merged"))
        self.assertEqual((o.kind, o.target), ("mr", "grp/sub/app!42"))

    def test_merge_wait_needs_the_merge_gate(self):
        sha = "0123456789abcdef0123456789abcdef01234567"
        self.assertEqual(typed(wait_merge("grp/app", 7, sha), merge=True).kind, "merge")
        self.assertEqual(typed(wait_merge("grp/app", 7, sha)).kind, "unparsed")

    def test_ask_human_is_a_blocking_question_to_the_addressee(self):
        o = typed(ask_human("owner", "pick a plan?"))
        self.assertEqual((o.kind, o.handle), ("blocking", "owner"))

    def test_ask_agent_is_a_decision_wait(self):
        o = typed(ask_agent("lead", "ship?"))
        self.assertEqual((o.kind, o.handle), ("decision", "lead"))

    def test_a_marker_in_the_prose_is_not_an_outcome(self):
        # the turn's text is not read: only the command's rows are
        o = typed(mesh_turn("next", text="⏳ WAIT card:#abcdef12", note="go on"))
        self.assertEqual(o.kind, "next")


if __name__ == "__main__":
    unittest.main()
