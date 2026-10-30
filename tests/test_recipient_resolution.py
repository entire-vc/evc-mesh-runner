"""Recipient validation uses controlled rosters; no real notifications."""
import io
import json
import types
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fakes import FakeMesh, task, calls, write_replies
from test_runner import Bench
from mesh_runner import diagnostic_get
from mesh_runner.mesh import MeshClient, MeshError
from mesh_runner.outcome import Outcome


class RecipientResolution(Bench):
    def setup_route(self, handle="unknown"):
        self.source = task(1)
        self.mesh = FakeMesh([self.source])
        self.r = self.runner(self.mesh)
        self.r.refresh_me()
        self.a = self.r.store.create(self.agent.name, self.source["id"], "claude")
        self.handle = handle

    def route(self):
        self.wait = {"kind": "decision", "handle": self.handle, "note": "pick one?"}
        return self.r._route_ask(self.a, self.source,
            Outcome(kind="decision", handle=self.handle, note="pick one?"), self.wait,
            "2026-10-21T00:00:00Z")

    def assert_no_ask(self):
        self.assertFalse(self.mesh.created)
        self.assertFalse(any("❓ Blocking" in c["body"] for c in self.mesh.comments(self.source["id"])))

    def test_unknown_and_subagent_type_get_correction_without_human_ask(self):
        for handle, rows in (("code-reviewer", []), ("mini", []),
                ("review", [{"id": "x", "kind": "subagent", "slug": "review"}])):
            with self.subTest(handle=handle):
                self.setup_route(handle)
                self.mesh.recipients = rows
                self.assertIn("recipient", self.route())
                self.assert_no_ask()
                self.tmp.cleanup()
                self.setUp()

    def test_collision_is_not_arbitrarily_routed(self):
        self.setup_route("lead")
        self.mesh.recipients = [
            {"id": "agent-lead", "kind": "agent", "slug": "lead", "display_name": "lead"},
            {"id": "person", "kind": "user", "slug": "lead", "display_name": "person"}]
        self.assertIn("ambiguous", self.route())
        self.assert_no_ask()

    def test_agent_service_user_with_same_slug_routes_to_the_agent(self):
        self.setup_route("example")
        self.mesh.agents.append({"id": "agent-example", "name": "example"})
        self.mesh.recipients = [
            {"id": "agent-example", "kind": "agent", "slug": "example", "display_name": "Example"},
            {"id": "user-example", "kind": "user", "slug": "example", "display_name": "Example (QA Mesh)",
             "is_service_user": True, "agent_id": "agent-example"}]
        self.assertEqual(self.route(), "")
        self.assertEqual(self.wait["kind"], "decision")
        self.assertFalse(self.wait.get("person_ask"))
        self.assert_no_ask()

    def test_real_person_with_same_slug_stays_ambiguous_even_when_name_is_close(self):
        self.setup_route("example")
        self.mesh.recipients = [
            {"id": "agent-example", "kind": "agent", "slug": "example", "display_name": "Example"},
            {"id": "user-example", "kind": "user", "slug": "example", "display_name": "Examplees Smith"}]
        self.assertIn("ambiguous", self.route())
        self.assert_no_ask()

    def test_parenthesised_real_person_stays_ambiguous(self):
        for name in ("Example (Smith)", "Example(qa)", "Example (QA)", "Example (QA\tMesh)", "Example (QA Team\tRelay)", "Example (QA Mesh)\n", "Example (QA )", "Example (QA Mesh) Jr"):
            with self.subTest(name=name):
                self.setup_route("example")
                self.mesh.recipients = [
                    {"id": "agent-example", "kind": "agent", "slug": "example", "display_name": "Example"},
                    {"id": "user-example", "kind": "user", "slug": "example", "display_name": name}]
                self.assertIn("ambiguous", self.route())
                self.tmp.cleanup()
                self.setUp()

    def test_real_person_named_like_a_service_user_stays_ambiguous(self):
        for extra in ({"is_service_user": False, "agent_id": None}, {},
                      {"is_service_user": True, "agent_id": "agent-other"}):
            with self.subTest(extra=extra):
                self.setup_route("example")
                self.mesh.recipients = [
                    {"id": "agent-example", "kind": "agent", "slug": "example", "display_name": "Example"},
                    {"id": "user-example", "kind": "user", "slug": "example", "display_name": "Example (QA Mesh)", **extra}]
                self.assertIn("ambiguous", self.route())
                self.assert_no_ask()
                self.tmp.cleanup()
                self.setUp()

    def test_service_user_plus_real_person_stays_ambiguous(self):
        self.setup_route("example")
        self.mesh.recipients = [
            {"id": "agent-example", "kind": "agent", "slug": "example", "display_name": "Example"},
            {"id": "user-example", "kind": "user", "slug": "example", "display_name": "example (qa mesh)",
             "is_service_user": True, "agent_id": "agent-example"},
            {"id": "user-h2", "kind": "user", "slug": "example", "display_name": "Example (Smith)"}]
        self.assertIn("ambiguous", self.route())

    def test_two_service_shaped_users_stay_ambiguous(self):
        self.setup_route("example")
        self.mesh.recipients = [
            {"id": "agent-example", "kind": "agent", "slug": "example", "display_name": "Example"},
            {"id": "user-a", "kind": "user", "slug": "example", "display_name": "Example (QA Mesh)",
             "is_service_user": True, "agent_id": "agent-example"},
            {"id": "user-b", "kind": "user", "slug": "example", "display_name": "Example (QA Team Relay)",
             "is_service_user": True, "agent_id": "agent-example"}]
        self.assertIn("ambiguous", self.route())
        self.assert_no_ask()

    def test_real_human_gate_is_preserved_even_with_unknown_recipient(self):
        self.setup_route()
        self.mesh.tasks[self.source["id"]]["human_gate"] = True
        with patch.object(self.mesh, "workspace_recipients", side_effect=MeshError(503, "down")):
            self.assertEqual(self.route(), "")
        self.assertEqual(self.wait["kind"], "blocking")
        self.assert_no_ask()

    def test_failed_roster_is_not_unknown_or_a_human(self):
        self.setup_route("owner")
        with patch.object(self.mesh, "workspace_recipients", side_effect=MeshError(503, "down")):
            self.assertIn("unavailable", self.route())
            self.route()
        self.assert_no_ask()
        self.assertEqual(self.r.store.get(self.a.id).state, "failed")

    def test_repeated_invalid_wait_stops_after_one_correction(self):
        t = task(1)
        mesh = FakeMesh([t])
        write_replies(self.replies, {t["id"][:8]: "⏳ WAIT decision:@code-reviewer: pick one?"})
        r = self.runner(mesh)
        r.tick()
        self.settle_all(r)
        for _ in range(4):
            r.tick()
        self.assertEqual(len(calls(self.calls)), 2)
        self.assertEqual(r.store.by_state("builder", ("failed",))[0].state, "failed")
        self.assertFalse(any("❓ Blocking" in c["body"] for c in mesh.comments(t["id"])))

    def test_adapter_is_bounded_and_rejects_invalid_or_truncated_roster(self):
        client = MeshClient("http://mesh", "fixture")
        with patch.object(client, "bounded_get", return_value=[]) as get:
            self.assertEqual(client.workspace_recipients("ws", "a+b"), [])
        path = get.call_args.args[0]
        self.assertEqual(parse_qs(urlsplit(path).query), {"q": ["a+b"], "limit": ["50"]})
        self.assertLessEqual(get.call_args.kwargs["timeout"], 5)
        for bad in ({"items": []}, [None], [{}] * 50,
                    [{"id": [], "kind": "user", "slug": "owner"}]):
            with self.subTest(bad=type(bad)), patch.object(client, "bounded_get", return_value=bad):
                with self.assertRaises(MeshError):
                    client.workspace_recipients("ws", "owner")


class RecipientRosterPathAllowed(unittest.TestCase):
    """The roster GET goes through the diagnostic worker, whose path allowlist must admit it."""

    def test_worker_serves_the_exact_path_workspace_recipients_builds(self):
        paths = []
        client = MeshClient("http://mesh.invalid", "k")
        client.bounded_get = lambda path, timeout: paths.append(path) or []
        client.workspace_recipients("11111111-1111-4111-8111-111111111111", "Lead")
        for hostile in ("/workspaces/x/mentionables?q=a", "/agents/me", "/tasks/../agents",
                        "/workspaces/" + "-" * 36 + "/mentionables?q=a&limit=50",
                        "/workspaces/11111111-1111-4111-8111-111111111111/mentionables?q=%zz&limit=50"):
            with self.subTest(path=hostile), \
                    patch.object(MeshClient, "_bounded_get_request", return_value=[]) as fetched:
                self.assertEqual(self.worker(hostile)["status"], 0)
                fetched.assert_not_called()
        with patch.object(MeshClient, "_bounded_get_request", return_value=[{"id": "1"}]):
            self.assertEqual(self.worker(paths[0]), {"status": 200, "body": [{"id": "1"}]})

    @staticmethod
    def worker(path):
        stdin = io.BytesIO(json.dumps({"base": "http://mesh.invalid", "key": "k", "basic": "",
                                       "path": path, "timeout": 1}).encode())
        out = io.BytesIO()
        with patch("sys.stdin", types.SimpleNamespace(buffer=stdin)), \
                patch("sys.stdout", types.SimpleNamespace(buffer=out)):
            diagnostic_get.main()
        return json.loads(out.getvalue())
