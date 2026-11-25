# Harness `agy` (Google Antigravity CLI)

`agy` runs headless (print mode) in every launch mode, like `kimi` and `zcode`.

| | |
|---|---|
| turn | `agy --output-format stream-json --settings <permissions> [--add-dir D] [--conversation <id>] … --print=<prompt>` |
| prompt | read from stdin by `/bin/sh`, passed as one `--print=` argument |
| session id | `conversation_id` of the `init` and `result` events |
| end of turn | the `result` event; `status` decides (`SUCCESS`, else an error), not the exit code |
| usage | the sum of this turn's `step_update` usages (`result.usage` counts the whole conversation) |
| instructions | `AGENTS.md` / `GEMINI.md` of the session cwd (the lane profile) |
| MCP | `<profile>/.agents/mcp_config.json`, written before each start from the profile's `.mcp.json`: servers whose executable is `mesh-mcp`/`mesh-mcp-agent` (directly or as the first argument of `mcp-wrap`), plus the stdio servers named in `mcp_allow`; mode 0600; an existing file is filtered the same way |
| read-only turn | session rules deny `command(*)`, `write_file(*)`, `download_url(*)` and `mcp(<name>/*)` of every `mcp_allow` server; reads and Mesh MCP stay allowed |
| slot-confined turn | not available |

## Configuration

```toml
[[agents]]
name = "lane"
harness = "agy"
binary = "/path/to/agy"                 # e.g. a wrapper that pins the egress route
args = ["--model", "gemini-pro-orchestrator"]
admission_commands = [
  ["python3", "/path/to/secret-guard-check.py", "--gemini-dir", "{gemini_dir}"],
  ["/path/to/egress-check"],
]

# or as a route of another lane
  [agents.alt.agy]
  binary = "/path/to/agy"
  args = ["--model", "gemini-4-carbon-high"]
  admission_commands = [["/path/to/egress-check"]]
```

`admission_commands` run before every start under the launch environment; `{gemini_dir}` is
`$HOME/.gemini` of that environment. Any exit other than 0, a timeout (30 s) or no commands at all
refuses the start before a process runs: the pending prompt is kept and the start is retried after
the gate hold. It is never read as a quota.

## Other MCP servers: `mcp_allow`

```toml
[[agents]]
harness = "agy"
mcp_allow = ["playwright"]      # exact names in the profile's .mcp.json; [] or absent = Mesh only

# or on a route
  [agents.alt.agy]
  mcp_allow = ["playwright"]
```

By default the workspace file holds the Mesh servers only. A name in `mcp_allow` carries that server
of the profile's `.mcp.json` into it with its `command`, `args`, `env` and `cwd` (nothing else). The
match is the server's name, not its executable: a `playwright` server run as
`mcp-wrap uv run … playwright-mcp-server.py` is matched by `"playwright"`. The `.mcp.json` of the
profile is the source of truth; `mcp_allow` only lets a name through.

- No pattern: a name is letters, digits and `_.-`; `*`, `/` or an empty name is a config error, so is
  `mcp_allow` on a lane whose harness is not agy (`[agents.alt.<other>]` included).
- Stdio only: a disabled server or one with `url`/`serverUrl` is not carried.
- An allowed server is read from `.mcp.json` only, never kept from an earlier workspace file; removing it
  from `mcp_allow` or `.mcp.json` drops it at the next start.
- It never replaces the Mesh server: without a Mesh server the start is still refused
  (`agy-mesh-mcp-missing`), and a server renamed to a Mesh server's name loses to the Mesh one.
- `~/.gemini/config/mcp_config.json` is shared by every agy lane (one `HOME`); a lane-specific server
  (a browser profile, a login) belongs in `mcp_allow`, not there.
- The workspace file is shared by every session of the lane, so a read-only turn cannot drop the
  servers from it; it denies `mcp(<name>/*)` for each of them in its own `--settings` instead.
  A listed name that is a Mesh server of the workspace (by its executable) is never denied: the
  start removes that rule again, so the Mesh server stays usable in every turn.

## Permissions

Print mode cannot ask. A lane turn passes `--settings` with
`allow = command(*), read_file(*), write_file(*), read_url(*), download_url(*), mcp(*)`; a
configured `--settings` in `args` replaces it. An action the rules do not grant is soft-denied by
agy (exit 0, `denied_actions` in the result); the adapter names it in the turn's error details.

## Errors the runner acts on

- Provider limit: `RESOURCE_EXHAUSTED`, `429`, `quota`, `exhausted your capacity`, `rate limit`
  (the CLI's own classifier) are reported as `quota: …`, so the on-limit routing runs.
- Authentication: `authentication required` sets `auth_failed`. The login of that launch (its Gemini home,
  `$HOME/.gemini`) is held like a Claude or Codex login: the turn does not count against the budget, the
  operator is told once, `on_auth_command` may route the lane, `mesh-runner clear-auth-hold` lifts it. A
  refusal before any conversation opened keeps the exact prompt unsent.
- Unknown conversation: agy warns `conversation "<id>" not found` and starts a new one. The
  runner checks `~/.gemini/antigravity-cli/conversations/<id>.db` before a resume
  (`session_known`), and a result whose conversation differs from the requested one is reported as
  `session not found`: the task starts a new session once.
- Egress refused: a wrapper line `Google egress: <reason>; launch refused …` with no stream (the
  wrapper's exit 69) is `egress_unavailable`. The prompt counts as unsent; the turn is retried
  with the provider-outage backoff, but it never pauses other lanes' starts.
- Cancel: SIGINT to the process group ends the turn with `status ERROR`, `error: interrupted`.
