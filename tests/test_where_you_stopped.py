"""A fresh session on a card this lane already worked on opens with a "where you stopped" block: why the
session is new, the lane's own last comment in full, the last declared outcome, linked merge requests and
the working copies' git status. A card the lane never worked on gets no block."""
import datetime as dt
import subprocess
import tempfile
import unittest
from pathlib import Path

from mesh_runner import prompt as prompts

from fakes import AGENT_ID, FakeMesh, calls, task, write_replies, next_turn
from test_runner import Bench

BLOCK = "[runner] Where you stopped."


class WhereYouStopped(Bench):

    def earlier_attempt(self, r, t, note, turns=3, line="⏳ WAIT card:#0000beef: needs the API"):
        prev = r.store.create(self.agent.name, t["id"], "claude")
        r.store.update(prev, state="completed", note=note)
        r.store.update(prev, turns=turns, wait={"kind": "card", "line": line} if line else {})
        return prev

    def first_prompt(self, mesh, t):
        write_replies(self.replies, {t["id"][:8]: next_turn("more", text="Step done.")})
        r = self.runner(mesh)
        r.tick()
        self.finish_turn(r)
        return r, calls(self.calls)[0]["prompt"]

    def test_a_card_the_lane_never_worked_on_has_no_block(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        mesh.add_comment(t["id"], "Please build it.", author="morgan")
        _, prompt = self.first_prompt(mesh, t)
        self.assertNotIn(BLOCK, prompt)
        self.assertIn("[runner] Task #00000001 — task 1\nMesh task id: ", prompt)

    def test_an_earlier_attempt_that_ran_turns_opens_the_new_session_with_the_block(self):
        t = task(1, vcs_link_count=1)
        mesh = FakeMesh([t], max_concurrent=1)
        mesh.links = {t["id"]: [{"link_type": "pr", "url": "https://gitlab.example/g/p/-/merge_requests/7",
                                 "status": "open", "pipeline_status": "failed"},
                                {"link_type": "branch", "url": "https://gitlab.example/g/p/-/tree/x"}]}
        at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
        closing = "Implemented the parser; tests pass locally.\n" + "detail " * 700 + "\n⏳ WAIT card:#0000beef"
        mesh.add_comment(t["id"], closing, at=at)
        mesh.add_comment(t["id"], "[runner] ✅ completed in ~3m", at=at + dt.timedelta(minutes=1))
        mesh.add_comment(t["id"], "Looks good, one more case.", author="morgan", at=at + dt.timedelta(minutes=5))
        r = self.runner(mesh)
        self.earlier_attempt(r, t, "gave up: stopped after 30 turns on this task")
        r.store.db.close()
        _, prompt = self.first_prompt(mesh, t)
        head, block = prompt.split(BLOCK, 1)
        self.assertTrue(head.rstrip().endswith("priority medium"), head[-200:])     # right after the header
        self.assertIn("this session is new because your previous attempt stopped at the turn cap (30 turns)", block)
        body = closing.strip()
        self.assertIn(body[:4000] + "\n[… cut at 4000 of " + str(len(body)) + " characters", block)
        self.assertNotIn("completed in ~3m", block.split("Your last declared outcome")[0])
        self.assertIn("Your last declared outcome: ⏳ WAIT card:#0000beef: needs the API", block)
        self.assertIn("Linked merge requests:\n  - https://gitlab.example/g/p/-/merge_requests/7 (open, pipeline "
                      "failed)", block)
        self.assertNotIn("tree/x", block)

    def test_a_closed_and_reopened_card_says_so(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        prev = self.earlier_attempt(r, t, "task left the agent's working statuses", line="")
        r.store.mark(self.agent.name, t["id"], r.OWNERSHIP_LOSS, __import__("json").dumps(
            {"attempt": prev.id, "session_id": "s-1", "harness": "claude", "cwd": str(self.agent.profile_dir),
             "workspaces": [], "observed_assignee": AGENT_ID, "observed_status": "done"}))
        r.store.db.close()
        _, prompt = self.first_prompt(mesh, t)
        self.assertIn("because the card was closed (done or cancelled) and reopened since your previous session",
                      prompt)
        self.assertNotIn("Your last declared outcome", prompt)            # none recorded, none in a comment

    def test_a_dropped_session_of_this_attempt_names_its_cause(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        a = r.store.create(self.agent.name, t["id"], "claude")
        r.store.update(a, state="ready", wait={"line": "↻ NEXT: the docs"})
        r.store.update(a, turns=2)
        r.store.event(a.id, "harness_switch", "claude->codex")
        text = r._first_prompt(mesh.tasks[t["id"]], "work", [], attempt_id=a.id)
        self.assertIn("because this lane now runs on another harness (claude->codex)", text)
        self.assertIn("Your last declared outcome: ↻ NEXT: the docs", text)
        r.store.event(a.id, "session_restart", "path_changed")       # the latest cause wins
        self.assertIn("because your working copies moved to other paths",
                      r._first_prompt(mesh.tasks[t["id"]], "work", [], attempt_id=a.id))
        self.assertNotIn(BLOCK, r._first_prompt(mesh.tasks[t["id"]], "review", [], attempt_id=a.id))

    def test_an_earlier_attempt_without_a_turn_is_not_work_on_the_card(self):
        t = task(1)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        self.earlier_attempt(r, t, "held: gate", turns=0)
        a = r.store.create(self.agent.name, t["id"], "claude")
        self.assertNotIn(BLOCK, r._first_prompt(mesh.tasks[t["id"]], "work", [], attempt_id=a.id))


class GitStatus(unittest.TestCase):

    def test_the_status_of_each_working_copy_is_summed_up_in_twenty_lines(self):
        from mesh_runner.runner import AgentRunner
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            for n in range(25):
                (repo / f"f{n:02}.txt").write_text("x")
            clean = Path(d) / "clean"
            clean.mkdir()
            subprocess.run(["git", "init", "-q", str(clean)], check=True)
            statuses = [(str(p), AgentRunner._git_status(str(p))) for p in (repo, clean, Path(d) / "missing")]
        block = prompts.where_you_stopped_block("a reason", None, "", [], statuses)
        self.assertIn(f"Working copy {repo} (git status --short):\n    ?? f00.txt", block)
        self.assertIn("    ?? f19.txt\n    (+5 more)", block)
        self.assertNotIn("f20.txt", block)
        self.assertIn(f"Working copy {clean}: clean (git status --short)", block)
        self.assertIn("missing: git status unavailable", block)


if __name__ == "__main__":
    unittest.main()
