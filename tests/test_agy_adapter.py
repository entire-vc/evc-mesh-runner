"""Google Antigravity CLI (``agy``) adapter: protocol on recorded agy 1.5.14 output, admission, Mesh MCP
workspace file, and the runner's reading of an agy turn's prompt delivery.

The streams below were recorded on the Mini (agy 1.5.14 through the Google egress wrapper, 2026-11-22);
the conversation id is synthetic and the ``init.tools`` list is shortened. The quota envelope is synthetic: a real account limit cannot be
produced on demand, its wording follows the CLI's own classifier (``RESOURCE_EXHAUSTED|quota|429|
exhausted your capacity|rate.?limit``, read from the binary). The egress line is the wrapper's
refusal format (exit 69).
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from mesh_runner import auth, interactive, turn_usage
from mesh_runner.adapters.agy import AgyAdapter, LANE_PERMISSIONS, READ_ONLY_PERMISSIONS, write_workspace_mcp
from mesh_runner.adapters.base import Launch, LaunchProbeDenied
from mesh_runner.runner import EGRESS_HINT, NO_SESSION_HINT, PROVIDER_OUTAGE_HINT, QUOTA_HINT

CID = "00000000-0000-4000-8000-0000000000a1"
INIT = {"event": "init", "conversation_id": CID, "init": {
    "model": "gemini-3.8-flash-low", "cwd": "/w", "tools": ["call_mcp_tool", "run_command"],
    "permission_mode": "request-review"}}


def step(index, **kw):
    return {"event": "step_update", "step_update": {"conversation_id": CID, "step_index": index, **kw}}


# fresh conversation: "Reply exactly OK"
FRESH = [INIT,
         step(0, state="DONE", step_type="user_input"),
         step(1, state="ACTIVE", step_type="agent_response", text_delta="OK"),
         step(1, state="DONE", step_type="agent_response", text_delta="\n", duration_seconds=2.539119,
              usage={"input_tokens": 9500, "output_tokens": 1, "thinking_tokens": 0, "cache_read_tokens": 0,
                     "total_tokens": 9501}),
         {"event": "result", "result": {"conversation_id": CID, "status": "SUCCESS", "response": "OK\n",
                                        "duration_seconds": 2.5667, "num_turns": 1,
                                        "usage": {"input_tokens": 9500, "output_tokens": 1, "thinking_tokens": 0,
                                                  "cache_read_tokens": 0, "total_tokens": 9501}}}]
# the same conversation resumed with --conversation: result.usage counts the whole conversation
RESUMED = [INIT,
           step(2, state="DONE", step_type="user_input"),
           step(3, state="DONE", step_type="system_message", duration_seconds=0.000298),
           step(4, state="ACTIVE", step_type="agent_response", text_delta="OK"),
           step(4, state="DONE", step_type="agent_response", text_delta="\n", duration_seconds=1.962796,
                usage={"input_tokens": 2258, "output_tokens": 1, "thinking_tokens": 0, "cache_read_tokens": 7457,
                       "total_tokens": 2259}),
           {"event": "result", "result": {"conversation_id": CID, "status": "SUCCESS", "response": "OK\n",
                                          "duration_seconds": 23.27, "num_turns": 2,
                                          "usage": {"input_tokens": 11758, "output_tokens": 2, "thinking_tokens": 0,
                                                    "cache_read_tokens": 7457, "total_tokens": 11760}}}]
# a Mesh MCP call, then the answer
TOOL = [INIT,
        step(0, state="DONE", step_type="user_input"),
        step(4, state="ACTIVE", step_type="tool", tool_name="call_mcp_tool", tool_info={
            "name": "call_mcp_tool", "parameters": {"Arguments": {}, "ServerName": "evc-mesh", "ToolName": "get_task"}}),
        step(4, state="DONE", step_type="tool", tool_name="call_mcp_tool", duration_seconds=0.14, tool_info={
            "name": "call_mcp_tool", "parameters": {}, "output": "in_progress"}),
        {"event": "result", "result": {"conversation_id": CID, "status": "SUCCESS", "response": "in_progress\n",
                                       "num_turns": 1, "usage": {}}}]
UNKNOWN_MODEL = [{"event": "result", "result": {
    "conversation_id": "", "status": "ERROR", "response": "",
    "error": 'invalid model selection (--model "no-such-model" --effort ""): model no-such-model is not recognized',
    "duration_seconds": 0, "num_turns": 0, "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    "finished_reason": "error"}}]
# headless mode cannot ask: the MCP call was soft-denied, the run "succeeded" with nothing done
DENIED = [INIT, step(0, state="DONE", step_type="user_input"),
          {"event": "result", "result": {"conversation_id": CID, "status": "SUCCESS", "response": "",
                                         "num_turns": 1, "denied_actions": [{"action": "mcp",
                                                                             "display_name": "McpTool"}]}}]
QUOTA = [INIT, step(0, state="DONE", step_type="user_input"),
         {"event": "result", "result": {"conversation_id": CID, "status": "ERROR", "response": "",
                                        "error": "RESOURCE_EXHAUSTED: You have exhausted your capacity on this model.",
                                        "num_turns": 1}}]
AUTH_STDERR = "Error: authentication required. Run 'agy' to log in.\n"
# the login refused after the conversation was opened (synthetic: the same CLI error as a typed result)
AUTH_IN_SESSION = [INIT, {"event": "result", "result": {
    "conversation_id": CID, "status": "ERROR", "response": "",
    "error": "authentication required. Run 'agy' to log in."}}]
EGRESS_STDERR = "Google egress: proxy-unavailable; launch refused (egress route required).\n"
NOT_FOUND_STDERR = 'warning: conversation "00000000-1111-2222-3333-444444444444" not found\n'


def write(lines, stderr: str = "", requested: str | None = None) -> Path:
    d = Path(tempfile.mkdtemp())
    log = d / "turn.jsonl"
    log.write_bytes(b"".join(json.dumps(x).encode() + b"\n" for x in lines))
    if stderr:
        log.with_suffix(".stderr").write_text(stderr)
    if requested is not None:
        log.with_suffix(".agy").write_text(json.dumps({"conversation": requested}))
    return log


class Argv(unittest.TestCase):
    def setUp(self):
        self.ad = AgyAdapter("/x/agy", ["--model", "gemini-pro-orchestrator"])

    def test_start_reads_the_prompt_from_stdin_into_one_print_argument(self):
        argv = self.ad.start_argv("", Path("/p"), [Path("/w1"), Path("/w2")])
        self.assertEqual(argv[:4], ["/bin/sh", "-c", 'prompt=$(cat); exec "$0" "$@" "--print=$prompt"', "/x/agy"])
        self.assertIn("stream-json", argv)
        self.assertEqual(argv[argv.index("--settings") + 1], json.dumps({"permissions": LANE_PERMISSIONS},
                                                                         separators=(",", ":")))
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"], ["/w1", "/w2"])
        self.assertNotIn("--conversation", argv)
        self.assertEqual(argv[-2:], ["--model", "gemini-pro-orchestrator"])

    def test_resume_names_the_conversation_never_the_latest(self):
        argv = self.ad.resume_argv(CID, Path("/p"), [])
        self.assertEqual(argv[argv.index("--conversation") + 1], CID)
        self.assertNotIn("--continue", argv)
        self.assertNotIn("-c", argv[3:])

    def test_the_shell_passes_a_dash_prompt_and_newlines_intact(self):
        shell = AgyAdapter(sys.executable).start_argv("", Path("/p"), [])[:3]
        prompt = "--model evil\nline two"
        out = subprocess.run(shell + [sys.executable, "-c", "import sys; sys.stdout.write(sys.argv[-1])"],
                             input=prompt, capture_output=True, text=True).stdout
        self.assertEqual(out, "--print=" + prompt)

    def test_model_and_effort_overrides_replace_the_configured_ones(self):
        argv = self.ad.with_effort(self.ad.with_model(self.ad.start_argv("", Path("/p"), []), "gemini-4-carbon-high"),
                                   "high")
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--model"], ["gemini-4-carbon-high"])
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--effort"], ["high"])
        self.assertEqual(self.ad.with_effort(argv, "turbo"), argv)          # agy has no such effort

    def test_read_only_denies_commands_and_writes_in_the_session_rules(self):
        argv = self.ad.with_read_only(self.ad.start_argv("", Path("/p"), []))
        settings = [json.loads(argv[i + 1]) for i, a in enumerate(argv) if a == "--settings"]
        self.assertEqual(settings, [{"permissions": READ_ONLY_PERMISSIONS}])
        self.assertIn("command(*)", READ_ONLY_PERMISSIONS["deny"])
        self.assertIn("write_file(*)", READ_ONLY_PERMISSIONS["deny"])
        with self.assertRaises(NotImplementedError):
            self.ad.with_slot_confined(argv)

    def test_a_configured_settings_file_is_not_overridden(self):
        argv = AgyAdapter("agy", ["--settings", "/etc/agy.json"]).start_argv("", Path("/p"), [])
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--settings"], ["/etc/agy.json"])

    def test_headless_in_every_launch_mode(self):
        self.assertFalse(hasattr(self.ad, "interactive_argv"))


class Parse(unittest.TestCase):
    ad = AgyAdapter()

    def test_success_has_conversation_text_model_and_this_turns_usage(self):
        r = self.ad.parse(write(FRESH, requested=""))
        self.assertTrue(r.finished)
        self.assertFalse(r.is_error)
        self.assertEqual((r.session_id, r.final_text, r.model), (CID, "OK\n", "gemini-3.8-flash-low"))
        self.assertEqual(r.usage, {"input_tokens": 9500, "output_tokens": 1, "cache_read_input_tokens": 0,
                                   "cache_creation_input_tokens": 0, "thinking_tokens": 0})

    def test_resume_by_conversation_id_counts_only_its_own_steps(self):
        r = self.ad.parse(write(RESUMED, requested=CID))
        self.assertFalse(r.is_error)
        self.assertEqual(r.session_id, CID)
        # result.usage is 11758 input for the whole conversation; this turn used 2258
        self.assertEqual(r.usage["input_tokens"], 2258)
        self.assertEqual(r.usage["cache_read_input_tokens"], 7457)

    def test_implicit_cache_only_reports_no_cache_write(self):
        # Gemini caches implicitly only (no explicit cache, no write price): a known read means write 0
        r = self.ad.parse(write(RESUMED, requested=CID))
        self.assertEqual(turn_usage.cache_fields(r.usage), ({"cache_read": 7457, "cache_write": 0}, []))

    def test_unknown_cache_read_keeps_cache_unknown(self):
        r = self.ad.parse(write(TOOL, requested=""))
        self.assertNotIn("cache_creation_input_tokens", r.usage)
        self.assertEqual(turn_usage.cache_fields(r.usage), ({}, ["cache_read", "cache_write"]))

    def test_resume_that_agy_silently_forked_is_a_missing_session(self):
        forked = [dict(e) for e in FRESH]
        log = write(forked, NOT_FOUND_STDERR, requested="00000000-1111-2222-3333-444444444444")
        r = self.ad.parse(log)
        self.assertTrue(r.is_error)
        self.assertEqual(r.session_id, "")
        self.assertRegex(r.error, NO_SESSION_HINT)
        # also without the warning line: the returned id is not the requested one
        r = self.ad.parse(write(FRESH, requested="00000000-1111-2222-3333-444444444444"))
        self.assertTrue(r.is_error)
        self.assertRegex(r.error, NO_SESSION_HINT)

    def test_tool_turn_worked(self):
        r = self.ad.parse(write(TOOL))
        self.assertEqual(r.final_text, "in_progress\n")
        self.assertFalse(r.is_error)

    def test_error_status_is_an_error_even_on_exit_zero(self):
        r = self.ad.parse(write(UNKNOWN_MODEL, "error: invalid model selection\n"))
        self.assertTrue(r.finished and r.is_error)
        self.assertIn("invalid model selection", r.error)
        self.assertFalse(r.auth_failed)
        self.assertNotRegex(r.error, QUOTA_HINT)

    def test_canceled_status_is_an_error(self):
        stream = FRESH[:-1] + [{"event": "result", "result": {"conversation_id": CID, "status": "INTERRUPTED",
                                                              "response": ""}}]
        r = self.ad.parse(write(stream))
        self.assertTrue(r.is_error)
        self.assertIn("INTERRUPTED", r.error)

    def test_soft_denied_permission_is_named_not_hidden(self):
        r = self.ad.parse(write(DENIED, "jetski: no output produced — a tool required the \"mcp\" permission\n"))
        self.assertFalse(r.is_error)
        self.assertEqual(r.error_details, "headless permission denied: mcp")

    def test_quota_is_a_limit_in_the_runners_words(self):
        r = self.ad.parse(write(QUOTA))
        self.assertTrue(r.is_error)
        self.assertRegex(r.error, QUOTA_HINT)
        self.assertTrue(r.error.startswith("quota: "))
        self.assertFalse(r.auth_failed)
        self.assertNotRegex(r.error, EGRESS_HINT)

    def test_authentication_required_is_an_auth_failure(self):
        r = self.ad.parse(write([], AUTH_STDERR))
        self.assertTrue(r.is_error and r.auth_failed and r.auth_only)
        self.assertNotRegex(r.error, QUOTA_HINT)

    def test_exit_69_egress_refusal_is_egress_not_quota_or_auth(self):
        r = self.ad.parse(write([], EGRESS_STDERR))
        self.assertTrue(r.finished and r.is_error)
        self.assertEqual(r.session_id, "")
        self.assertRegex(r.error, EGRESS_HINT)
        self.assertNotRegex(r.error, QUOTA_HINT)
        self.assertNotRegex(r.error, PROVIDER_OUTAGE_HINT)
        self.assertFalse(r.auth_failed)

    def test_cut_off_turn_is_not_finished(self):
        r = self.ad.parse(write(FRESH[:3]))
        self.assertFalse(r.finished)
        self.assertEqual(r.session_id, CID)


class SessionKnown(unittest.TestCase):
    def test_reads_the_conversation_store_of_the_launch_home(self):
        home = Path(tempfile.mkdtemp())
        ad = AgyAdapter()
        env = {"HOME": str(home)}
        self.assertTrue(ad.session_known(CID, Path("/p"), env))          # no store: cannot tell
        store = home / ".gemini" / "antigravity-cli" / "conversations"
        store.mkdir(parents=True)
        self.assertFalse(ad.session_known(CID, Path("/p"), env))
        (store / f"{CID}.db").write_text("")
        self.assertTrue(ad.session_known(CID, Path("/p"), env))
        self.assertFalse(ad.session_known("../x", Path("/p"), env))


class Launching(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.profile = self.root / "profile"
        self.profile.mkdir()
        self.mesh = {"command": "/u/bin/mcp-wrap", "args": ["/u/bin/mesh-mcp", "--transport", "stdio"],
                     "env": {"MESH_API_URL": "https://mesh.example"}}
        (self.profile / ".mcp.json").write_text(json.dumps({"mcpServers": {
            "evc-mesh": self.mesh, "playwright": {"command": "npx", "args": ["@playwright/mcp"]}}}))
        self.env = {"HOME": str(self.root / "home"), "PATH": os.environ.get("PATH", "")}
        self.native = self.root / "native-ran"

    def adapter(self, *commands):
        fake = self.root / "agy"
        fake.write_text(f"#!/bin/sh\necho \"$@\" > {self.native}\n")
        fake.chmod(0o755)
        return AgyAdapter(str(fake), admission_commands=list(commands))

    def spawn(self, ad, argv=None):
        log = self.root / "logs" / "t.jsonl"
        argv = argv or ad.start_argv("", self.profile, [])
        proc = ad.spawn(Launch(argv, self.profile, self.env, "hello"), log)
        proc.wait(timeout=10)
        return log

    def test_workspace_mcp_keeps_only_the_mesh_server_private(self):
        self.spawn(self.adapter(["true"]))
        target = self.profile / ".agents" / "mcp_config.json"
        self.assertEqual(json.loads(target.read_text()), {"mcpServers": {"evc-mesh": self.mesh}})
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.native.exists())

    def test_admission_refusal_starts_nothing(self):
        guard = self.root / "guard"
        with self.assertRaises(LaunchProbeDenied) as caught:
            self.spawn(self.adapter(["true"], [sys.executable, "-c",
                                               "import sys; open(sys.argv[1], 'w').write(sys.argv[2]); sys.exit(3)",
                                               str(guard), "{gemini_dir}"]))
        self.assertEqual(caught.exception.reason, "agy-admission-2-refused:3")
        self.assertEqual(guard.read_text(), str(self.root / "home" / ".gemini"))   # the launch's own home
        self.assertFalse(self.native.exists())

    def test_without_admission_commands_nothing_starts(self):
        with self.assertRaises(LaunchProbeDenied) as caught:
            self.spawn(self.adapter())
        self.assertEqual(caught.exception.reason, "agy-admission-unconfigured")
        self.assertFalse(self.native.exists())

    def test_only_the_mesh_executable_counts_never_an_argument_naming_it(self):
        foreign = {"command": "python3", "args": ["unrelated_server.py", "mesh-mcp"]}
        wrapped_foreign = {"command": "/u/bin/mcp-wrap", "args": ["/u/bin/other", "/u/bin/mesh-mcp"]}
        direct = {"command": "/u/bin/mesh-mcp-agent", "args": ["stdio"]}
        (self.profile / ".mcp.json").write_text(json.dumps({"mcpServers": {
            "evc-mesh": self.mesh, "foreign": foreign, "wrapped": wrapped_foreign, "agent": direct}}))
        self.assertTrue(write_workspace_mcp(self.profile))
        target = json.loads((self.profile / ".agents" / "mcp_config.json").read_text())
        self.assertEqual(sorted(target["mcpServers"]), ["agent", "evc-mesh"])

    def test_an_existing_workspace_file_is_filtered_too(self):
        (self.profile / ".mcp.json").write_text(json.dumps({"mcpServers": {}}))
        target = self.profile / ".agents" / "mcp_config.json"
        target.parent.mkdir()
        target.write_text(json.dumps({"mcpServers": {"evc-mesh": self.mesh,
                                                     "foreign": {"command": "/u/bin/other-mcp"}}}))
        target.chmod(0o644)
        self.assertTrue(write_workspace_mcp(self.profile))
        self.assertEqual(json.loads(target.read_text()), {"mcpServers": {"evc-mesh": self.mesh}})
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        target.chmod(0o644)                                           # same servers, readable by others
        self.assertTrue(write_workspace_mcp(self.profile))
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        target.write_text(json.dumps({"mcpServers": {"foreign": {"command": "/u/bin/other-mcp"}}}))
        self.assertFalse(write_workspace_mcp(self.profile))           # no Mesh server left: nothing starts

    def test_without_a_mesh_server_nothing_starts(self):
        (self.profile / ".mcp.json").write_text(json.dumps({"mcpServers": {"x": {"command": "npx"}}}))
        with self.assertRaises(LaunchProbeDenied) as caught:
            self.spawn(self.adapter(["true"]))
        self.assertEqual(caught.exception.reason, "agy-mesh-mcp-missing")
        self.assertFalse(self.native.exists())

    def test_spawn_records_the_requested_conversation(self):
        ad = self.adapter(["true"])
        log = self.spawn(ad, ad.resume_argv(CID, self.profile, []))
        self.assertEqual(json.loads(log.with_suffix(".agy").read_text()), {"conversation": CID})
        self.assertIn(f"--conversation {CID} --print=hello", self.native.read_text())

    # -- per-lane allowlist of other MCP servers (mcp_allow) ------------------------------
    PLAYWRIGHT = {"command": "/u/bin/mcp-wrap",
                  "args": ["/u/.local/bin/uv", "run", "--with", "mcp>=1.0,<2.0", "--with", "playwright==1.61.0",
                           "--python", "/opt/homebrew/bin/python3.12", "--", "python3",
                           "/u/ops/scripts/playwright-mcp-server.py"],
                  "env": {"BROWSER_DEFAULT_PROFILE": "lane", "MCP_WRAP_NAME": "playwright"},
                  "cwd": "/u/lane", "type": "stdio", "timeout": 30}

    def write_mcp(self, servers):
        (self.profile / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))

    def workspace(self):
        return json.loads((self.profile / ".agents" / "mcp_config.json").read_text())["mcpServers"]

    def test_an_allowed_server_is_carried_by_its_exact_name_with_its_launch_fields_only(self):
        self.write_mcp({"evc-mesh": self.mesh, "playwright": self.PLAYWRIGHT,
                        "github": {"command": "npx", "args": ["@github/mcp"]},
                        "playwright-other": {"command": "/u/bin/mcp-wrap", "args": ["/u/bin/x"]}})
        ad = AgyAdapter(str(self.root / "agy"), admission_commands=[["true"]], mcp_allow=["playwright"])
        self.spawn(ad)
        servers = self.workspace()
        self.assertEqual(sorted(servers), ["evc-mesh", "playwright"])           # no pattern, no neighbour
        self.assertEqual(servers["playwright"], {k: self.PLAYWRIGHT[k] for k in ("command", "args", "env", "cwd")})
        self.assertEqual(servers["evc-mesh"], self.mesh)
        target = self.profile / ".agents" / "mcp_config.json"
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_without_an_allowlist_the_named_server_is_still_dropped(self):
        self.write_mcp({"evc-mesh": self.mesh, "playwright": self.PLAYWRIGHT})
        self.assertTrue(write_workspace_mcp(self.profile))
        self.assertEqual(sorted(self.workspace()), ["evc-mesh"])
        self.assertTrue(write_workspace_mcp(self.profile, []))
        self.assertEqual(sorted(self.workspace()), ["evc-mesh"])

    def test_disabled_and_remote_allowed_servers_are_not_carried(self):
        self.write_mcp({"evc-mesh": self.mesh, "playwright": {**self.PLAYWRIGHT, "disabled": True},
                        "remote": {"url": "https://mcp.example/mcp"},
                        "both": {"command": "/u/bin/x", "url": "https://mcp.example/mcp"}})
        self.assertTrue(write_workspace_mcp(self.profile, ["playwright", "remote", "both"]))
        self.assertEqual(sorted(self.workspace()), ["evc-mesh"])

    def test_an_allowed_server_alone_is_no_mesh_server(self):
        self.write_mcp({"playwright": self.PLAYWRIGHT})
        self.assertFalse(write_workspace_mcp(self.profile, ["playwright"]))
        self.assertFalse((self.profile / ".agents" / "mcp_config.json").exists())
        ad = AgyAdapter(str(self.root / "agy"), admission_commands=[["true"]], mcp_allow=["playwright"])
        with self.assertRaises(LaunchProbeDenied) as caught:
            self.spawn(ad)
        self.assertEqual(caught.exception.reason, "agy-mesh-mcp-missing")

    def test_an_allowed_server_comes_from_the_lane_file_never_from_the_old_workspace_file(self):
        target = self.profile / ".agents" / "mcp_config.json"
        target.parent.mkdir()
        target.write_text(json.dumps({"mcpServers": {"evc-mesh": self.mesh, "playwright": self.PLAYWRIGHT,
                                                     "foreign": {"command": "/u/bin/other-mcp"}}}))
        self.write_mcp({"evc-mesh": self.mesh})                    # the lane file no longer lists it
        self.assertTrue(write_workspace_mcp(self.profile, ["playwright", "foreign"]))
        self.assertEqual(sorted(self.workspace()), ["evc-mesh"])
        target.write_text(json.dumps({"mcpServers": {"evc-mesh": self.mesh, "playwright": self.PLAYWRIGHT}}))
        self.write_mcp({})                                         # no Mesh in the lane file: old Mesh kept
        self.assertTrue(write_workspace_mcp(self.profile, ["playwright"]))
        self.assertEqual(sorted(self.workspace()), ["evc-mesh"])
        self.write_mcp({"evc-mesh": self.mesh, "playwright": self.PLAYWRIGHT})
        self.assertTrue(write_workspace_mcp(self.profile, ["playwright"]))
        self.assertTrue(write_workspace_mcp(self.profile, []))     # allowlist removed: dropped again
        self.assertEqual(sorted(self.workspace()), ["evc-mesh"])

    def test_an_allowed_name_cannot_replace_a_mesh_server(self):
        self.write_mcp({"evc-mesh": self.mesh})
        self.assertTrue(write_workspace_mcp(self.profile, ["evc-mesh"]))
        self.assertEqual(self.workspace(), {"evc-mesh": self.mesh})
        # the lane file renames another server to the Mesh name: the kept Mesh server wins
        self.write_mcp({"evc-mesh": {"command": "/u/bin/other-mcp"}})
        self.assertTrue(write_workspace_mcp(self.profile, ["evc-mesh"]))
        self.assertEqual(self.workspace(), {"evc-mesh": self.mesh})

    def test_an_allowlist_takes_exact_names_only(self):
        for bad in (["*"], ["play*"], [""], ["a/b"], ["x)"], [1]):
            with self.assertRaises(ValueError):
                AgyAdapter("agy", mcp_allow=bad)

    def test_read_only_denies_the_tools_of_allowed_servers(self):
        ad = AgyAdapter("agy", mcp_allow=["playwright", "figma"])
        argv = ad.with_read_only(ad.start_argv("", Path("/p"), []))
        settings, = [json.loads(argv[i + 1]) for i, a in enumerate(argv[:-1]) if a == "--settings"]
        rules = settings["permissions"]
        self.assertEqual(rules["allow"], READ_ONLY_PERMISSIONS["allow"])
        self.assertEqual(rules["deny"], READ_ONLY_PERMISSIONS["deny"] + ["mcp(figma/*)", "mcp(playwright/*)"])
        lane = ad.start_argv("", Path("/p"), [])                    # an ordinary turn may use them
        self.assertEqual([json.loads(lane[i + 1]) for i, a in enumerate(lane[:-1]) if a == "--settings"],
                         [{"permissions": LANE_PERMISSIONS}])

    def test_a_read_only_turn_never_denies_the_mesh_server_named_in_the_allowlist(self):
        self.write_mcp({"evc-mesh": self.mesh, "playwright": self.PLAYWRIGHT})
        ad = self.adapter(["true"])
        ad.mcp_allow = ["evc-mesh", "playwright"]
        self.spawn(ad, ad.with_read_only(ad.start_argv("", self.profile, [])))
        ran = self.native.read_text()
        settings = json.loads(ran[ran.index("{"):ran.rindex("}") + 1])
        self.assertNotIn("mcp(evc-mesh/*)", settings["permissions"]["deny"])     # Mesh stays usable
        self.assertIn("mcp(playwright/*)", settings["permissions"]["deny"])
        self.assertIn("command(*)", settings["permissions"]["deny"])
        self.assertEqual(sorted(self.workspace()), ["evc-mesh", "playwright"])

    def test_unchanged_workspace_file_is_not_rewritten(self):
        self.assertTrue(write_workspace_mcp(self.profile))
        target = self.profile / ".agents" / "mcp_config.json"
        before = target.stat().st_ino
        self.assertTrue(write_workspace_mcp(self.profile))
        self.assertEqual(target.stat().st_ino, before)


if __name__ == "__main__":
    unittest.main()


from fakes import FakeMesh, task  # noqa: E402  (runner fixtures, after the protocol tests)
from mesh_runner.adapters.base import proc_stamp  # noqa: E402
from test_runner import Bench  # noqa: E402


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    stamp = proc_stamp(p.pid)
    p.wait(timeout=10)
    return p.pid, stamp


class RunnerReadsAgyDelivery(Bench):
    """An agy alt-harness turn on a claude lane whose process group is gone (journal ``native-exited``): how the
    Runner reads the delivery of its exact pending prompt from the agy stream."""

    PROMPT = "EXACT PENDING REQUEST: answer the card."

    def setUp(self):
        super().setUp()
        self.agent.alt = {"agy": {"admission_commands": [["true"]]}}
        self.t = task(1, cat="in_progress")
        self.mesh = FakeMesh([self.t])
        self.r = self.runner(self.mesh)
        self.r.me = self.mesh.me()
        self.r.alt_adapters = {"agy": AgyAdapter(binary=str(self.root / "no-agy"), admission_commands=[["true"]])}
        self.directory = interactive.agent_dir(self.cfg.state_dir, self.agent.name)
        self.directory.mkdir(parents=True, exist_ok=True)

    def turn(self, stream, stderr="", session="", requested=None):
        a = self.r.store.create(self.agent.name, self.t["id"], "agy", self.t["title"])
        workspaces = [str(self.r.pool.lease(a.id, "app"))]
        log = self.root / f"{a.id}.turn2.agy.jsonl"
        log.write_text("".join(json.dumps(e) + "\n" for e in stream))
        log.with_suffix(".stderr").write_text(stderr)
        log.with_suffix(".agy").write_text(json.dumps({"conversation": session if requested is None else requested}))
        self.intent = {"version": 1, "token": uuid.uuid4().hex, "turn": 2, "log_path": str(log),
                       "sha256": hashlib.sha256(self.PROMPT.encode()).hexdigest(), "prompt": self.PROMPT,
                       "kind": "headless", "harness": "agy", "session_id": session, "resume": bool(session),
                       "workspaces": workspaces, "source_read_at": "pending-source",
                       "prior": {"turns": 1, "session_id": session, "log_path": "", "turn_started_at": "",
                                 "source_read_at": "previous-source"}}
        interactive.write_delivery(self.directory, a.id, {"version": 1, "token": self.intent["token"],
                                                          "sha256": self.intent["sha256"], "phase": "native-exited"})
        pid, stamp = dead_pid()
        self.assertTrue(self.r.store.update(a, state="running", pid=pid, stamp=stamp, turns=2, session_id=session,
                                            log_path=str(log), workspaces=workspaces, wake_text="pending wake",
                                            wait={"interactive_prompt_intent": self.intent}))
        self.a = self.r.store.get(a.id)

    def reconcile(self):
        return self.r._reconcile_interactive_prompt(self.r.store.get(self.a.id))

    def details(self, kind):
        return [d for _, k, d in self.r.store.events(self.a.id) if k == kind]

    def assert_acknowledged(self):
        self.assertTrue(self.reconcile())
        a = self.r.store.get(self.a.id)
        self.assertNotIn("interactive_prompt_intent", a.wait)
        self.assertEqual(self.details("prompt_delivery_unknown"), [])
        self.assertIn("mesh_runner.prompt_ack", Path(self.intent["log_path"]).read_text())

    def test_finished_fresh_conversation_acknowledges_the_prompt(self):
        self.turn(FRESH)
        self.assert_acknowledged()

    def test_resume_by_conversation_id_acknowledges_the_prompt(self):
        self.turn(RESUMED, session=CID)
        self.assert_acknowledged()

    def test_egress_refusal_keeps_the_exact_prompt_unsent_without_an_alert(self):
        self.turn([], EGRESS_STDERR)
        self.assertFalse(self.reconcile())
        a = self.r.store.get(self.a.id)
        self.assertEqual(a.state, "ready")
        self.assertEqual(self.details("prompt_delivery_unknown"), [])
        self.assertEqual(self.details("prompt_unsent_reconciled"), [self.intent["token"]])
        self.assertEqual(a.wait["interactive_prompt_intent"], dict(self.intent, retryable=True))
        self.assertFalse(self.r._quota_admission_required(a))             # not an account limit

    def test_other_failure_without_a_conversation_stays_unknown(self):
        # control for the egress case: a refusal that is not the egress wrapper's is no proof the prompt was unsent
        self.turn([], "Error: something else broke\n")
        self.assertFalse(self.reconcile())
        self.assertEqual(self.r.store.get(self.a.id).state, "unknown")
        self.assertEqual(self.details("prompt_delivery_unknown"), ["headless-native-session-mismatch"])

    def test_auth_refusal_before_a_conversation_holds_the_login_and_keeps_the_prompt(self):
        profile = auth.agy_profile_id(self.r._fresh_env())
        self.turn([], AUTH_STDERR)
        self.r.store.mark(self.agent.name, self.t["id"], "auth_launch",
                          json.dumps({"attempt": self.a.id, "turn": 2, "profile": profile}))
        self.assertFalse(self.reconcile())
        a = self.r.store.get(self.a.id)
        self.assertEqual(a.state, "ready")
        self.assertEqual(self.details("prompt_delivery_unknown"), [])
        self.assertEqual(self.details("prompt_unsent_reconciled"), [self.intent["token"]])
        self.assertEqual(self.details("auth_failed"), ["2"])
        # the unsent turn is undone once (turns back to prior), never also excused as an auth-only turn:
        # the product work already spent keeps its budget
        self.assertEqual(a.turns, self.intent["prior"]["turns"])
        self.assertEqual(self.r.store.auth_turns(a.id), 0)
        self.assertEqual(a.turns - self.r.store.auth_turns(a.id), 1)
        self.cfg.max_turns = 1                                            # the budget boundary still holds
        self.assertGreaterEqual(a.turns - self.r.store.auth_turns(a.id), self.cfg.max_turns)
        self.assertTrue(auth.held(self.r.store, profile))
        self.assertFalse(self.r._quota_admission_required(a))

    def test_silently_forked_resume_is_not_acknowledged(self):
        self.turn(FRESH, NOT_FOUND_STDERR, session="00000000-1111-2222-3333-444444444444")
        self.assertFalse(self.reconcile())
        self.assertEqual(self.r.store.get(self.a.id).state, "unknown")


class EgressNeverPausesOtherLanes(Bench):
    def test_egress_outage_is_not_counted_as_a_provider_outage_lane(self):
        import datetime as dt
        r = self.runner(FakeMesh([task(1, cat="in_progress")]))
        a = r.store.create("lane-a", "t1", "agy", "x")
        r.store.event(a.id, "provider_outage", "1")
        r.store.event(a.id, "egress_outage", "1")
        b = r.store.create("lane-b", "t2", "claude", "y")
        r.store.event(b.id, "provider_outage", "3")
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)
        self.assertEqual(r.store.outage_lanes(since), 1)


class AgyAuthHold(Bench):
    def test_auth_refusal_in_a_conversation_holds_the_login_without_spending_a_turn(self):
        t = task(42)
        mesh = FakeMesh([t], max_concurrent=1)
        self.agent.alt = {"agy": {"admission_commands": [["true"]]}}
        r = self.runner(mesh)
        r.refresh_me()
        r.alt_adapters = {"agy": AgyAdapter(admission_commands=[["true"]])}
        a = r.store.create(self.agent.name, t["id"], "agy")
        profile = auth.agy_profile_id(r._fresh_env())
        self.assertEqual(r._auth_profile_id("agy", r._fresh_env()), profile)
        path = write(AUTH_IN_SESSION)
        r.store.mark(self.agent.name, t["id"], "auth_launch",
                     json.dumps({"attempt": a.id, "turn": 1, "profile": profile}))
        r.store.update(a, state="running", turns=1, log_path=str(path))
        r.settle(r.store.get(a.id))
        held = r.store.get(a.id)
        self.assertEqual(held.state, "ready")
        self.assertEqual(r.store.auth_turns(a.id), 1)
        self.assertTrue(auth.held(r.store, profile))
        self.assertEqual(mesh.comment_log, {})
        self.assertEqual([d for _, k, d in r.store.events(a.id) if k == "auth_failed"], ["1"])

    def test_login_fingerprint_is_the_gemini_home(self):
        a = auth.agy_profile_id({"HOME": "/h1"})
        self.assertEqual(a, auth.agy_profile_id({"HOME": "/h1", "OTHER": "v"}))
        self.assertNotEqual(a, auth.agy_profile_id({"HOME": "/h2"}))
        self.assertNotEqual(a, auth.codex_profile_id({"HOME": "/h1"}))
