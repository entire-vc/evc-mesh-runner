"""A Codex card turn's Mesh MCP child must see the runner carrier.

Codex starts a stdio MCP server with an allowlisted environment, so the runner names the
carrier in ``mcp_servers.<name>.env_vars`` for every Mesh MCP server of the profile Codex loads.
"""
import json
import tempfile
import unittest
from pathlib import Path

from mesh_runner import interactive
from mesh_runner.adapters.claude import ClaudeAdapter
from mesh_runner.adapters.codex import RUNNER_MCP_ENV, CodexAdapter

CARRIER = {"MESH_RUNNER_TASK": "00000001-0000-4000-8000-000000000001", "MESH_RUNNER_LANE": "carrier-lane",
           "MESH_RUNNER_CHECKOUT_GENERATION": "7713", "MESH_RUNNER_CHECKOUT_REQUEST": "carrier-req-1",
           "MESH_RUNNER_CHECKOUT_SESSION": "00000002-0000-4000-8000-000000000002"}


def overrides(argv: list[str]) -> dict[str, list[str]]:
    out = {}
    for i, x in enumerate(argv[:-1]):
        if x == "-c" and argv[i + 1].startswith("mcp_servers.") and ".env_vars=" in argv[i + 1]:
            key, value = argv[i + 1].split("=", 1)
            out[key[len("mcp_servers."):-len(".env_vars")]] = json.loads(value)
    return out


class CodexRunnerEnvTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.env = {"CODEX_HOME": str(self.home), **CARRIER}

    def write(self, name: str, text: str) -> None:
        (self.home / name).write_text(text)

    def test_profile_mesh_servers_get_the_carrier_by_name(self):
        self.write("builder.config.toml", """
model = "m"
[mcp_servers.evc-mesh]
command = "/opt/bin/mesh-mcp-agent"
args = ["builder"]
[mcp_servers.evc-mesh.tools.move_task]
approval_mode = "approve"
[mcp_servers.mesh-other]
command = "/opt/bin/mesh-mcp-agent"
args = ["builder", "other"]
env_vars = ["KEEP_ME"]
[mcp_servers.node_repl]
command = "node"
""")
        # config.toml of the same home is not the file Codex reads for this profile
        self.write("config.toml", '[mcp_servers.mesh]\ncommand = "/x/mesh-mcp-agent"\n')
        a = CodexAdapter(profile="builder")
        argv = a.with_runner_env(a.start_argv("", Path("/cwd"), []), self.env)
        self.assertEqual(overrides(argv), {"evc-mesh": list(RUNNER_MCP_ENV),
                                           "mesh-other": ["KEEP_ME", *RUNNER_MCP_ENV]})
        # root options: before the exec subcommand
        self.assertLess(max(i for i, x in enumerate(argv) if x == "-c"), argv.index("exec"))
        # names only: no value of the carrier reaches argv
        self.assertFalse(any(v in x for v in CARRIER.values() for x in argv))

    def test_names_come_from_the_launch_home(self):
        """The routed CODEX_HOME of the turn, not the runner's own: another home names it ``mesh``."""
        self.write("alt.config.toml", '[mcp_servers.mesh]\ncommand = "mesh-mcp-agent"\n')
        a = CodexAdapter(profile="alt")
        self.assertEqual(list(overrides(a.with_runner_env(a.start_argv("", Path("/c"), []), self.env))),
                         ["mesh"])
        other = Path(tempfile.mkdtemp())
        argv = a.start_argv("", Path("/c"), [])
        self.assertEqual(a.with_runner_env(argv, {**self.env, "CODEX_HOME": str(other)}), argv)

    def test_no_profile_reads_config_toml(self):
        self.write("config.toml", '[mcp_servers.evc-mesh]\ncommand = "/b/mesh-mcp"\n')
        a = CodexAdapter()
        self.assertEqual(list(overrides(a.with_runner_env(a.start_argv("", Path("/c"), []), self.env))),
                         ["evc-mesh"])

    def test_unchanged_without_carrier_or_readable_config(self):
        self.write("p.config.toml", '[mcp_servers.evc-mesh]\ncommand = "mesh-mcp-agent"\n')
        a = CodexAdapter(profile="p")
        argv = a.resume_argv("t-1", Path("/c"), [])
        # a cardless turn (answer desk, chat): nothing to forward
        self.assertEqual(a.with_runner_env(argv, {"CODEX_HOME": str(self.home)}), argv)
        # malformed config: the launch goes on as before, never a config Codex would refuse
        self.write("p.config.toml", "[mcp_servers.evc-mesh\ncommand = ")
        self.assertEqual(a.with_runner_env(argv, self.env), argv)
        # a non-bare server name is not addressable as a dotted -c key
        self.write("p.config.toml", '[mcp_servers."evc mesh"]\ncommand = "mesh-mcp-agent"\n')
        self.assertEqual(a.with_runner_env(argv, self.env), argv)

    def test_interactive_codex_puts_overrides_in_the_native_tail(self):
        self.write("p.config.toml", '[mcp_servers.evc-mesh]\ncommand = "mesh-mcp-agent"\n')
        wrapped = interactive.InteractiveAdapter(CodexAdapter(profile="p"), "builder", self.home / "state")
        argv = wrapped.with_runner_env(wrapped.resume_argv("t-1", Path("/c"), []), self.env)
        head, tail = argv[:argv.index("--") + 1], argv[argv.index("--") + 1:]
        self.assertEqual(overrides(head), {})
        self.assertEqual(overrides(tail), {"evc-mesh": list(RUNNER_MCP_ENV)})
        self.assertEqual(interactive.native_tail(argv), tail)

    def test_claude_mcp_inherits_env_argv_unchanged(self):
        a = ClaudeAdapter()
        argv = a.start_argv("s", Path("/c"), [])
        self.assertEqual(a.with_runner_env(argv, self.env), argv)


if __name__ == "__main__":
    unittest.main()
