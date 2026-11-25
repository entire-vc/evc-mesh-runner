"""Two-step effort policy: field validation, triggers, choice, the record format and the config load."""
import logging
import tempfile
import unittest
from pathlib import Path

from mesh_runner import effort_policy as ep
from mesh_runner.config import load


class Fields(unittest.TestCase):
    def test_unset_fields_stay_empty_and_the_policy_is_off(self):
        self.assertEqual(ep.fields(None, None), ("", ""))
        self.assertEqual(ep.fields("", ""), ("", ""))
        self.assertFalse(ep.configured(*ep.fields(None, None)))

    def test_valid_levels_are_kept_lower_cased(self):
        self.assertEqual(ep.fields("Medium", "HIGH"), ("medium", "high"))
        self.assertEqual(ep.fields(None, "xhigh"), ("", "xhigh"))
        self.assertEqual(ep.fields("high", None), ("high", ""))

    def test_an_unknown_level_is_ignored_not_fatal(self):
        with self.assertLogs("mesh_runner", logging.WARNING):
            self.assertEqual(ep.fields("turbo", "high", "agent X"), ("", "high"))

    def test_escalation_never_keeps_or_lowers_the_level(self):
        for base, up in (("high", "high"), ("xhigh", "high")):
            with self.subTest(base=base, up=up), self.assertLogs("mesh_runner", logging.WARNING):
                self.assertEqual(ep.fields(base, up), (base, ""))


class Triggers(unittest.TestCase):
    def test_no_fact_no_trigger(self):
        self.assertEqual(ep.trigger(), "")
        self.assertEqual(ep.trigger(verdict="SHIP", ci_failures=1, labels=["kind:change", "ui"]), "")

    def test_each_trigger(self):
        self.assertEqual(ep.trigger(verdict="DO-NOT-SHIP"), "do_not_ship")
        self.assertEqual(ep.trigger(ci_failures=2), "ci_failed_again")
        self.assertEqual(ep.trigger(failed_attempts=1), "failed_attempt")
        for name in ep.ESCALATION_LABELS:
            with self.subTest(label=name):
                self.assertEqual(ep.trigger(labels=[name.upper()]), f"label:{name}")

    def test_a_label_that_only_contains_a_trigger_word_is_not_a_trigger(self):
        self.assertEqual(ep.trigger(labels=["security-review-done", "kind:incident", "non-urgent"]), "")

    def test_first_match_wins_in_a_fixed_order(self):
        self.assertEqual(ep.trigger(verdict="DO-NOT-SHIP", ci_failures=3, failed_attempts=1, labels=["urgent"]),
                         "do_not_ship")
        self.assertEqual(ep.trigger(ci_failures=2, failed_attempts=1, labels=["urgent"]), "ci_failed_again")
        self.assertEqual(ep.trigger(failed_attempts=1, labels=["urgent"]), "failed_attempt")


class Choose(unittest.TestCase):
    def test_base_without_trigger(self):
        self.assertEqual(ep.choose("medium", "high", ""), ep.Choice("medium", "medium", False, ""))

    def test_one_step_up_on_a_trigger(self):
        self.assertEqual(ep.choose("medium", "high", "do_not_ship"), ep.Choice("high", "medium", True, "do_not_ship"))

    def test_only_escalate_set_leaves_the_args_on_the_first_pass(self):
        self.assertEqual(ep.choose("", "xhigh", "").effort, "")
        self.assertEqual(ep.choose("", "xhigh", "label:security").effort, "xhigh")

    def test_only_default_set_never_escalates(self):
        self.assertEqual(ep.choose("high", "", "do_not_ship"), ep.Choice("high", "high", False, ""))

    def test_mark_round_trip_and_garbage(self):
        c = ep.choose("medium", "high", "label:urgent")
        self.assertEqual(ep.parse_mark(c.mark()), ("high", "label:urgent"))
        self.assertEqual(ep.parse_mark("|"), ("", ""))
        self.assertEqual(ep.parse_mark("turbo|x"), ("", ""))
        self.assertEqual(ep.parse_mark(""), ("", ""))

    def test_record_line_has_fixed_keys_and_reads_the_launched_level(self):
        up = ep.choose("medium", "high", "ci_failed_again")
        self.assertEqual(up.detail(3, "high"),
                         "turn=3 effort=high base=medium escalated=1 trigger=ci_failed_again source=policy")
        # a route that kept its own args never claims the policy's level or an escalation
        self.assertEqual(up.detail(3, "xhigh", "xhigh"),
                         "turn=3 effort=xhigh base=medium escalated=0 trigger=- source=args")
        self.assertEqual(up.detail(3, "low", "xhigh", label="low"),
                         "turn=3 effort=low base=medium escalated=0 trigger=- source=label")
        self.assertEqual(ep.choose("", "high", "").detail(1, "", ""),
                         "turn=1 effort=- base=- escalated=0 trigger=- source=args")
        self.assertEqual(ep.choose("", "high", "").detail(1, "xhigh", "xhigh"),
                         "turn=1 effort=xhigh base=xhigh escalated=0 trigger=- source=args")


class Facts(unittest.TestCase):
    def test_only_a_failure_of_the_work_counts(self):
        for note in ("gave up: the session ended without a readable outcome twice",
                     "gave up: 2 turns in a row ended without a readable outcome",
                     "gave up: stopped after 30 turns on this task", "no outcome declared"):
            with self.subTest(note=note):
                self.assertTrue(ep.work_failure(note))
        for note in ("gave up: stopped after 4 failed turns: provider limit or overload",
                     "gave up: stopped after 4 failed turns: harness error",
                     "checkout lost: reassigned", "turn budget: 0 NEXT and 10 waits",
                     "invalid recipient: unknown recipient", "", None):
            with self.subTest(note=note):
                self.assertFalse(ep.work_failure(note))

    def test_failed_pipelines_are_counted_once_each(self):
        sha = "c" * 40
        self.assertEqual(ep.failed_pipelines([f"{sha}:1:failed", f"{sha}:1:failed", f"{sha}:2:success"]), 1)
        self.assertEqual(ep.failed_pipelines([f"{sha}:1:failed", f"{sha}:2:failed"]), 2)
        self.assertEqual(ep.failed_pipelines(["garbage", "", f"{sha}:3:manual"]), 0)


class Config(unittest.TestCase):
    def agent(self, options=""):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = root / "key"
            key.write_text("fixture")
            key.chmod(0o600)
            path = root / "runner.toml"
            path.write_text(f'[runner]\nmesh_url="http://mesh.test"\nstate_dir="{tmp}"\n'
                            f'[[agents]]\nname="dev"\nkey_file="{key}"\nprofile_dir="{tmp}"\n'
                            'args=["--model", "opus", "--effort", "xhigh"]\n' + options)
            return load(path).agents[0]

    def test_absent_fields_leave_the_policy_off_and_the_args_as_they_were(self):
        a = self.agent()
        self.assertEqual((a.default_effort, a.escalate_effort), ("", ""))
        self.assertEqual(a.args, ["--model", "opus", "--effort", "xhigh"])

    def test_fields_are_read(self):
        a = self.agent('default_effort="high"\nescalate_effort="xhigh"\n')
        self.assertEqual((a.default_effort, a.escalate_effort), ("high", "xhigh"))

    def test_an_invalid_field_is_ignored_and_the_config_still_loads(self):
        with self.assertLogs("mesh_runner", logging.WARNING):
            a = self.agent('default_effort="ultra"\nescalate_effort="low"\n')
        self.assertEqual((a.default_effort, a.escalate_effort), ("", "low"))


if __name__ == "__main__":
    unittest.main()
