"""MR waits that cannot complete on their own tell the owner once per head."""
import unittest

from fakes import FakeGitLab
from mesh_runner.wake import ci_change_event, typed_event

SHA = "a" * 40
SHA2 = "b" * 40
TARGET = "g/app!56"


def mr(status, sha=SHA, pid=10, state="opened"):
    return {"sha": sha, "state": state, "head_pipeline": {"id": pid, "sha": sha, "status": status}}


class SuccessWaitOnMR(unittest.TestCase):
    def contract(self, baseline="running"):
        return {"sha": SHA, "pipeline": 10, "status": baseline, "predicate": "success"}

    def test_manual_and_failed_heads_notify_once(self):
        for status in ("manual", "failed", "canceled", "cancelled", "skipped"):
            for baseline in ("running", status):
                with self.subTest(status=status, baseline=baseline):
                    gl = FakeGitLab()
                    gl.mr_rows[TARGET] = mr(status)
                    observation = {}
                    event = ci_change_event(gl, "mr", TARGET, self.contract(baseline),
                                            observation=observation, allow_action_required=True)
                    self.assertIn("action required", event)
                    self.assertIn("success predicate is not met", event)
                    self.assertTrue(observation["action_required"])
                    again = ci_change_event(gl, "mr", TARGET, self.contract(baseline),
                                            delivered=lambda key: observation["value"],
                                            allow_action_required=True)
                    self.assertEqual(again, "")
                    # Observers that do not opt into owner action keep the old contract.
                    self.assertEqual(ci_change_event(gl, "mr", TARGET, self.contract(baseline)), "")

    def test_a_new_failed_head_notifies_again(self):
        gl = FakeGitLab()
        gl.mr_rows[TARGET] = mr("failed")
        first = {}
        ci_change_event(gl, "mr", TARGET, self.contract(), observation=first, allow_action_required=True)
        gl.mr_rows[TARGET] = mr("failed", sha=SHA2, pid=11)
        second = {}
        event = ci_change_event(gl, "mr", TARGET, self.contract(), delivered=lambda key: first["value"],
                                observation=second, allow_action_required=True)
        self.assertIn(SHA2[:8], event)
        self.assertNotEqual(first["key"], second["key"])

    def test_running_and_success_heads_keep_existing_behaviour(self):
        gl = FakeGitLab()
        for status in ("running", "pending", "created"):
            gl.mr_rows[TARGET] = mr(status)
            observation = {}
            self.assertEqual(ci_change_event(gl, "mr", TARGET, self.contract(), observation=observation,
                                             allow_action_required=True), "", status)
            self.assertEqual(observation, {}, status)
        gl.mr_rows[TARGET] = mr("success")
        observation = {}
        event = ci_change_event(gl, "mr", TARGET, self.contract(), observation=observation,
                                allow_action_required=True)
        self.assertIn("changed CI", event)
        self.assertNotIn("action_required", observation)


class MergedWaitOnFailedHead(unittest.TestCase):
    def wait(self):
        return {"kind": "mr", "target": TARGET, "mr_state": {"predicate": "merged", "sha": SHA, "state": "opened"}}

    def test_failed_head_notifies_once_manual_does_not(self):
        for status in ("failed", "canceled", "cancelled"):
            with self.subTest(status=status):
                gl = FakeGitLab()
                gl.mr_rows[TARGET] = mr(status)
                observation = {}
                event = typed_event(gl, "mr", TARGET, self.wait(), "", observation, allow_action_required=True)
                self.assertIn("will not merge as is", event)
                self.assertTrue(observation["action_required"])
                self.assertEqual(typed_event(gl, "mr", TARGET, self.wait(),
                                             lambda key: observation["value"], {},
                                             allow_action_required=True), "")
                self.assertEqual(typed_event(gl, "mr", TARGET, self.wait(), "", {}), "")
        for status in ("manual", "running", "success"):
            with self.subTest(status=status):
                gl = FakeGitLab()
                gl.mr_rows[TARGET] = mr(status)
                self.assertEqual(typed_event(gl, "mr", TARGET, self.wait(), "", {},
                                             allow_action_required=True), "")

    def test_merged_and_closed_mrs_still_wake_as_before(self):
        for state, field, word in (("merged", "merged_at", "merged"), ("closed", "closed_at", "closed without merge")):
            with self.subTest(state=state):
                gl = FakeGitLab()
                row = mr("failed", state=state)
                row[field] = "2026-10-21T09:00:00Z"
                gl.mr_rows[TARGET] = row
                observation = {}
                event = typed_event(gl, "mr", TARGET, self.wait(), "", observation, allow_action_required=True)
                self.assertIn(word, event)
                self.assertNotIn("action required", event)
                self.assertNotIn("action_required", observation)


if __name__ == "__main__":
    unittest.main()
