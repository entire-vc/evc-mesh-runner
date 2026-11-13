"""The contract agents see names the Mesh turn commands first, with the tool names, parameters and values the
Mesh MCP server exposes (evc-mesh-mcp internal/mcp/turn_outcomes.go), and keeps the marker lines as the fallback."""
import re
import unittest

from mesh_runner import prompt as prompts
from mesh_runner.outcome import CONTRACT, DECISION_WAIT_GUIDANCE, MERGE_MARKER_GUIDANCE, MERGE_WAIT_GUIDANCE, contract, parse_line

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
        self.assertEqual(contract(True).replace(MERGE_WAIT_GUIDANCE, "").replace(MERGE_MARKER_GUIDANCE, ""),
                         CONTRACT)

    def test_the_command_comes_first_and_the_marker_is_the_fallback(self):
        first_command = CONTRACT.index("wait_for on=card")
        fallback = CONTRACT.index("Only if the Mesh command is unavailable or fails")
        self.assertLess(first_command, fallback)
        self.assertNotIn("⏳ WAIT", CONTRACT[:fallback])
        for line in ("⏳ WAIT card:#<8hex>", "⏳ WAIT decision:@<agent>: <question>", "↻ NEXT: <note>",
                     "❓ Blocking @<person>: <question>", "until=merged after=<full-head-sha>:opened",
                     "external=<stable-reference> source=<https-url|card:#8hex>"):
            self.assertIn(line, CONTRACT[fallback:])

    def test_retired_ops_review_wait_is_not_advertised_or_accepted(self):
        self.assertNotIn("ops-review", contract(True))
        for ref in ("a" * 64, "bad", ""):
            out = parse_line("⏳ WAIT ops-review:" + ref, None)
            self.assertEqual(out.kind, "unparsed")
            self.assertIn("finish_task to=review", out.note)

    def test_a_marker_is_offered_only_after_a_refused_command(self):
        # Lanes wrote the marker in place of a command they never called ("habit"); the contract
        # must not present it as an equal path: no marker beside a command, the fallback names the refusal
        for merge in (False, True):
            text = contract(merge)
            fallback = text.index("Only if the Mesh command is unavailable or fails")
            self.assertNotIn("Marker:", text)
            self.assertNotIn("WAIT merge:", text[:fallback])
            tail = " ".join(text[fallback:].split())
            self.assertIn("a marker line never replaces a call", tail)
            self.assertIn("quoting the command and its error", tail)
        t = task(1)
        for text in (prompts.no_outcome_prompt(t), prompts.no_outcome_prompt(t, "⏳ WAIT 2h", "relative", merge=True)):
            head = " ".join(text[:text.index("End every turn")].split())
            self.assertIn("call it, do not write it as a line", head)
            self.assertIn("read only after Mesh refused that command", head)

    def test_the_marker_grammar_is_unchanged(self):
        self.assertEqual(parse_line("↻ NEXT: the docs", None).kind, "next")
        self.assertEqual(parse_line("❓ Blocking @morgan: ship?", None).kind, "blocking")

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
            self.assertLess(text.index("Mesh turn command"), text.index("marker"))
        asked = prompts.ask_not_raised_prompt(t, "owner")
        self.assertLess(asked.index("ask_human to=owner"), asked.index("❓ Blocking @owner:"))
        self.assertIn("supersedes=", prompts.ask_not_raised_prompt(t, "owner", "answered on 10.11"))
        self.assertIn("one Mesh\nturn command", prompts.wake_prompt(t, "resumed"))


if __name__ == "__main__":
    unittest.main()
