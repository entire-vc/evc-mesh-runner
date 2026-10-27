import unittest

from mesh_runner import prompt as prompts

TID = "0123456789abcdef0123456789abcdef0123"


def card(*labels):
    return {"id": TID, "title": "t", "priority": "high", "labels": list(labels)}


def start(task):
    return prompts.start_prompt(task, "Lane", ["/w"], [])


class DeliveryByKind(unittest.TestCase):
    def test_research_is_closed_by_a_report_with_sources(self):
        text = start(card("kind:research"))
        self.assertIn("Delivery (kind:research)", text)
        self.assertIn("a report with its sources and a conclusion", text)
        self.assertIn("No MR, CI or deploy is invented", text)
        self.assertIn("(to review when its acceptor is someone else)", text)

    def test_content_and_ops_name_their_own_evidence(self):
        self.assertIn("a link to the artifact and the criteria it meets", start(card("kind:content")))
        ops = start(card("kind:ops"))
        self.assertIn("command output or a GET showing the new state", ops)

    def test_change_defect_monitor_and_no_label_keep_the_plain_prompt(self):
        plain = start(card())
        for labels in (("kind:change",), ("kind:defect",), ("kind:monitor",), ("model:x", "decision-request")):
            self.assertEqual(start(card(*labels)), plain)
        self.assertNotIn("Delivery (", plain)

    def test_growth_is_small_and_only_with_the_label(self):
        grown = len(start(card("kind:ops"))) - len(start(card()))
        self.assertTrue(0 < grown <= 600, grown)

    def test_the_wake_and_closing_prompts_carry_it_too(self):
        task = card("kind:research")
        self.assertIn("Delivery (kind:research)", prompts.wake_prompt(task, "a comment"))
        self.assertIn("Delivery (kind:research)", prompts.no_outcome_prompt(task))
        self.assertNotIn("Delivery (", prompts.wake_prompt(card(), "a comment"))
        self.assertNotIn("Delivery (", prompts.no_outcome_prompt(card("kind:change")))

    def test_review_prompt_is_not_changed(self):
        self.assertNotIn("Delivery (", prompts.review_prompt(card("kind:research"), "Lane", ["/w"], []))


if __name__ == "__main__":
    unittest.main()
