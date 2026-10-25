import json
import unittest
from pathlib import Path

from mesh_runner.adapters.claude import ClaudeAdapter
from mesh_runner.adapters.codex import CodexAdapter
from mesh_runner.workspace_scope import METADATA_ONLY, WORKSPACE_REQUIRED, metadata_only, metadata_receipt


class WorkspaceScope(unittest.TestCase):
    def test_only_explicit_typed_decision_opts_out_of_workspaces(self):
        self.assertTrue(metadata_only({"labels": ["decision-request", METADATA_ONLY]}))
        for labels in ([], ["decision-request"], [METADATA_ONLY],
                       [METADATA_ONLY, "alert"], [METADATA_ONLY, "coordination"],
                       [METADATA_ONLY, "decision-request", WORKSPACE_REQUIRED]):
            with self.subTest(labels=labels):
                self.assertFalse(metadata_only({"labels": labels}))

    def test_prose_and_malformed_labels_never_opt_in(self):
        for labels in (None, "decision-request runner:metadata-only", {}, [METADATA_ONLY, {}],
                       [METADATA_ONLY, "decision-request", None]):
            with self.subTest(labels=labels):
                self.assertFalse(metadata_only({"labels": labels, "title": "decision-request",
                                                "description": "runner:metadata-only"}))

    def test_conflicting_alert_types_never_opt_in_even_with_both_decision_labels(self):
        for label in ("alert", "auto-incident"):
            with self.subTest(label=label):
                self.assertFalse(metadata_only({"labels": ["decision-request", METADATA_ONLY, label]}))

    def test_review_is_always_normal_workspace_work(self):
        t = {"labels": [METADATA_ONLY, "decision-request"]}
        for mode in ("review", "unknown", None):
            with self.subTest(mode=mode):
                self.assertFalse(metadata_only(t, mode))

    def test_alert_diagnostics_need_their_own_unambiguous_opt_in(self):
        diagnostic = "runner:alert-diagnostics"
        for alert in (["alert"], ["auto-incident"], ["alert", "auto-incident"]):
            with self.subTest(alert=alert):
                self.assertTrue(metadata_only({"labels": [METADATA_ONLY, diagnostic, *alert]}))
        for labels in ([diagnostic, "alert"], [METADATA_ONLY, "alert"],
                       [METADATA_ONLY, diagnostic], [METADATA_ONLY, "runner:unknown", "alert"],
                       [METADATA_ONLY, diagnostic, "alert", "decision-request"],
                       [METADATA_ONLY, diagnostic, "decision-request"],
                       [METADATA_ONLY, diagnostic, "auto-incident", WORKSPACE_REQUIRED],
                       [METADATA_ONLY, diagnostic, "alert", None]):
            with self.subTest(labels=labels):
                self.assertFalse(metadata_only({"labels": labels}))
        for mode in ("review", "unknown", None):
            with self.subTest(mode=mode):
                self.assertFalse(metadata_only({"labels": [METADATA_ONLY, diagnostic, "alert"]}, mode))

    def test_workspace_required_transition_is_explicit_and_reversible(self):
        t = {"labels": [METADATA_ONLY, "decision-request"]}
        self.assertTrue(metadata_only(t))
        t["labels"].append(WORKSPACE_REQUIRED)
        self.assertFalse(metadata_only(t))
        t["labels"].remove(METADATA_ONLY)
        self.assertFalse(metadata_only(t))

    def test_restart_receipt_is_bound_to_the_exact_attempt(self):
        receipt = json.dumps({"attempt": "attempt-old", "scope": "metadata-only"})
        self.assertTrue(metadata_receipt(receipt, "attempt-old"))
        self.assertFalse(metadata_receipt(receipt, "attempt-new"))
        for value in (None, {}, [], "", "not json", "[]", '"metadata-only"',
                      json.dumps({"attempt": "attempt-old", "scope": "workspace"})):
            with self.subTest(value=value):
                self.assertFalse(metadata_receipt(value, "attempt-old"))

    def test_native_adapters_keep_identity_and_tools_without_git_directories(self):
        cwd = Path("/agent-profile")
        for adapter in (ClaudeAdapter("claude-custom", ["--model", "configured-model"]),
                        CodexAdapter("codex-custom", ["-c", 'model="configured-model"'], "agent-profile")):
            with self.subTest(adapter=adapter.name):
                start = adapter.start_argv("session", cwd, [])
                resume = adapter.resume_argv("session", cwd, [])
                self.assertEqual(start[0], adapter.binary)
                for argv in (start, resume):
                    self.assertNotIn("--add-dir", argv)
                    self.assertNotIn("--tools", argv)
                    self.assertNotIn("--restricted", argv)
                    for arg in adapter.extra_args:
                        self.assertIn(arg, argv)
                if adapter.name == "codex":
                    self.assertEqual(start[start.index("--profile") + 1], "agent-profile")
                    self.assertEqual(start[start.index("-C") + 1], str(cwd))
                self.assertIn("session", resume)
                admitted = adapter.resume_argv("session", cwd, [Path("/leased/app")])
                self.assertIn("--add-dir", admitted)
                self.assertIn("/leased/app", admitted)
                self.assertIn("session", admitted)
