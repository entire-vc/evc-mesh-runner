"""The contract agents see names the Mesh turn commands first, with the tool names, parameters and values the
Mesh MCP server exposes (evc-mesh-mcp internal/mcp/turn_outcomes.go); there is no marker fallback section."""
import re
import unittest

from mesh_runner import prompt as prompts
from mesh_runner import outcome as outcome_mod
from mesh_runner.outcome import CONTRACT, DECISION_WAIT_GUIDANCE, MERGE_WAIT_GUIDANCE, contract

from fakes import task

# copied from evc-mesh-mcp internal/mcp/turn_outcomes.go (registerTurnOutcomeTools)
TOOLS = {
    "wait_for": {"task_id", "on", "target_task_id", "target_condition", "criterion", "vcs_link_id", "provider", "project_path",
                 "mr_iid", "pipeline_id", "required_jobs", "until", "after_sha", "after_pipeline_id",
                 "after_status", "registration", "registration_ref", "until_at", "event_ref", "source", "why", "note"},
    "continue_next": {"task_id", "note"},
    "ask_agent": {"task_id", "to", "question"},
    "ask_human": {"task_id", "to", "question", "recommended_default", "deadline", "soft", "canonical_key",
                  "supersedes"},
    "finish_task": {"task_id", "result_kind", "evidence", "evidence_links", "to"},
    "hand_off": {"task_id", "to", "why"},
}
VALUES = {
    "on": {"card", "mr", "pipeline", "time", "merge", "registered"},
    "until": {"merged", "green", "changed", "finished"},
    "target_condition": {"task_done", "shipped", "deployed", "accepted", "artifact_published"},
    "provider": {"gitlab", "github"},
    "registration": {"ops-review", "gmail", "receipt", "lifecycle", "stall", "recovery", "queue", "native"},
    "result_kind": {"change", "defect", "research", "content", "ops"},
    "to": None,                                         # a name; finish_task's to is done|review
}


class TurnContract(unittest.TestCase):

    def command_lines(self, text):
        return [line.strip() for line in text.splitlines()
                if line.strip().split(" ", 1)[0] in TOOLS]

    def test_every_command_and_argument_is_one_the_mcp_server_exposes(self):
        for merge in (False, True):
            lines = self.command_lines(contract(merge))
            self.assertEqual({line.split(" ", 1)[0] for line in lines}, set(TOOLS))
            for line in lines:
                tool = line.split(" ", 1)[0]
                for key, value in re.findall(r"\b(\w+)=([^\s\]]+)", line.split("—")[0]):
                    self.assertIn(key, TOOLS[tool], line)
                    allowed = VALUES.get(key)
                    if allowed and not value.startswith("<"):
                        self.assertLessEqual(set(value.split("|")), allowed, line)

    def test_every_wait_the_old_contract_covered_has_its_command(self):
        text = contract(True)
        for form in ("wait_for on=card", "wait_for on=mr", "wait_for on=pipeline", "wait_for on=merge",
                     "wait_for on=time", "registration=queue", "ask_agent",
                     "ask_human", "continue_next", "finish_task", "hand_off", "after_sha=", "after_pipeline_id",
                     "after_status=opened", "required_jobs=", "provider=github", "event_ref=", "source=", "why="):
            self.assertIn(form, text)
        self.assertNotIn("on=merge", CONTRACT)              # only where the merge gate runs
        self.assertEqual(contract(True).replace(MERGE_WAIT_GUIDANCE, ""), CONTRACT)

    def test_there_is_no_marker_fallback_section(self):
        for merge in (False, True):
            text = contract(merge)
            for marker in ("⏳ WAIT", "↻ NEXT", "❓ Blocking", "Only if the Mesh command is unavailable",
                           "Marker:", "external=<stable-reference>", "until=merged after="):
                self.assertNotIn(marker, text)
            self.assertIn("Text is not an outcome", text)
        for name in ("parse_line", "MERGE_MARKER_GUIDANCE", "JOBS_EMPTY"):
            self.assertFalse(hasattr(outcome_mod, name), name)

    def test_retired_ops_review_wait_is_not_advertised(self):
        self.assertNotIn("ops-review", contract(True))

    def test_the_prompts_name_the_command_and_never_ask_for_a_line(self):
        t = task(1)
        for text in (prompts.no_outcome_prompt(t), prompts.no_outcome_prompt(t, "⏳ WAIT 2h", "relative", merge=True)):
            head = " ".join(text[:text.index("End every turn")].split())
            self.assertIn("call it, do not write it as a line", head)
            self.assertIn("Text is never read as an outcome", head)
            self.assertNotIn("marker", head)

    def test_a_named_result_wait_and_the_answered_refusal_are_offered(self):
        # a result another card produces in parts is waited on as that result, not as the card's
        # closure; an answered question is refused, not asked again
        for merge in (False, True):
            text = " ".join(contract(merge).split())
            self.assertIn("target_condition=shipped|deployed|accepted|artifact_published", text)
            self.assertIn("artifact_published criterion=<named result>", text)
            self.assertIn("not its closure", text)
        self.assertIn("already_answered", " ".join(DECISION_WAIT_GUIDANCE.split()))
        self.assertIn("a new question says what changed", " ".join(DECISION_WAIT_GUIDANCE.split()))
        self.assertTrue(CONTRACT.endswith(DECISION_WAIT_GUIDANCE))

    def test_it_is_no_longer_than_the_marker_contract_it_replaces(self):
        self.assertLessEqual(len(CONTRACT), 4513)            # 0.2.3's contract(False)
        self.assertLessEqual(len(contract(True)), 4749)

    def test_the_corrections_ask_for_the_command_first(self):
        t = task(1)
        for text in (prompts.no_outcome_prompt(t), prompts.no_outcome_prompt(t, "⏳ WAIT 2h", "relative")):
            self.assertIn("End the turn now with ONE Mesh turn command", text)
        asked = prompts.ask_not_raised_prompt(t, "owner")
        self.assertIn("ask_human to=owner", asked)
        self.assertIn("text is not an outcome", asked)
        self.assertIn("supersedes=", prompts.ask_not_raised_prompt(t, "owner", "answered on 10.11"))
        self.assertIn("one Mesh\nturn command", prompts.wake_prompt(t, "resumed"))


if __name__ == "__main__":
    unittest.main()
