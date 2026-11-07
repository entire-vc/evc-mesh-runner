"""A Codex 401 / refresh refusal is an auth hold, never burned turns or a give-up."""
import json
import tempfile
import unittest
from pathlib import Path

from mesh_runner import auth
from mesh_runner.adapters.codex import CodexAdapter
from fakes import FakeMesh, task
from test_runner import Bench

UNAUTH = "workspace routing discovery unauthorized (401)"
REFRESH = ("Your access token could not be refreshed because your refresh token was already used. "
           "Please log out and sign in again.")


def log(root, *events):
    path = Path(root) / "turn.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return path


def failed(message, kind="turn.failed"):
    return {"type": kind, "error": {"message": message}} if kind == "turn.failed" else {"type": kind, "message": message}


class Parse(unittest.TestCase):
    def parse(self, *events):
        with tempfile.TemporaryDirectory() as root:
            return CodexAdapter().parse(log(root, *events))

    def test_typed_auth_refusals_set_auth_failed(self):
        for message in (UNAUTH, REFRESH, "refresh_token_reused",
                        "failed to refresh token: 401 Unauthorized from https://auth.openai.com/oauth/token"):
            for kind in ("turn.failed", "error"):
                with self.subTest(message=message, kind=kind):
                    r = self.parse({"type": "thread.started", "thread_id": "t1"}, failed(message, kind))
                    self.assertTrue(r.auth_failed and r.auth_only and r.is_error)

    def test_outage_and_prose_are_not_auth(self):
        for message in ("workspace routing discovery failed", "workspace routing discovery timed out",
                        "I saw 401 unauthorized in the logs", "stream disconnected"):
            with self.subTest(message=message):
                self.assertFalse(self.parse(failed(message)).auth_failed)
        prose = {"type": "item.completed", "item": {"type": "agent_message", "text": UNAUTH}}
        r = self.parse(prose, {"type": "turn.completed", "usage": {}})
        self.assertFalse(r.auth_failed)
        r = self.parse(prose, failed("boom"))
        self.assertFalse(r.auth_failed)

    def test_work_before_the_refusal_is_not_auth_only(self):
        r = self.parse({"type": "item.completed", "item": {"type": "command_execution"}}, failed(UNAUTH))
        self.assertTrue(r.auth_failed)
        self.assertFalse(r.auth_only)

    def test_a_later_completed_turn_clears_a_transient_refusal(self):
        r = self.parse(failed(UNAUTH, "error"), {"type": "turn.completed", "usage": {}})
        self.assertFalse(r.auth_failed or r.is_error)

    def test_interactive_wrapper_parses_the_same_way(self):
        from mesh_runner import interactive
        with tempfile.TemporaryDirectory() as root:
            wrapped = interactive.wrap(CodexAdapter(), "a", "interactive", Path(root), "tmux", 1)
            self.assertTrue(wrapped.parse(log(root, failed(UNAUTH))).auth_failed)

    def test_profile_fingerprint_separates_homes_and_profiles(self):
        a = auth.codex_profile_id({"CODEX_HOME": "/h1"}, "p")
        self.assertEqual(a, auth.codex_profile_id({"CODEX_HOME": "/h1", "OPENAI_X": "v"}, "p"))
        self.assertNotEqual(a, auth.codex_profile_id({"CODEX_HOME": "/h2"}, "p"))
        self.assertNotEqual(a, auth.codex_profile_id({"CODEX_HOME": "/h1"}, "q"))


class Hold(Bench):
    def test_codex_401_holds_the_profile_after_one_turn_without_giving_up(self):
        t = task(41)
        mesh = FakeMesh([t], max_concurrent=1)
        r = self.runner(mesh)
        r.refresh_me()
        r.alt_adapters = {"codex": CodexAdapter(profile="base")}
        a = r.store.create(self.agent.name, t["id"], "codex")
        profile = auth.codex_profile_id(r._fresh_env(), "base")
        path = log(self.root, {"type": "thread.started", "thread_id": "t1"}, failed(UNAUTH))
        r.store.mark(self.agent.name, t["id"], "auth_launch",
                     json.dumps({"attempt": a.id, "turn": 1, "profile": profile}))
        r.store.update(a, state="running", turns=1, log_path=str(path))
        r.settle(r.store.get(a.id))
        held = r.store.get(a.id)
        self.assertEqual(held.state, "ready")
        self.assertEqual(r.store.auth_turns(a.id), 1)
        self.assertTrue(auth.held(r.store, profile))
        self.assertEqual(mesh.comment_log, {})


if __name__ == "__main__":
    unittest.main()
