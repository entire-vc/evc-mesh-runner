# Kimi and ZCode harnesses

Two harnesses besides `claude` and `codex`. Both run headless (their print modes) whatever the
launch mode: `interactive.wrap` leaves an adapter without `interactive_argv` headless, and neither
provider bills its print mode differently from its terminal program.

| | `kimi` | `zcode` |
|---|---|---|
| program | Kimi Code CLI, npm `@moonshot-ai/kimi-code` (the Python `kimi-cli` is archived) | ZCode agent CLI (`zai-org/ZCode`, `apps/zcode-cli`) |
| turn | `kimi --output-format stream-json [--add-dir D] [--session=<id>] --prompt=<prompt>` | `zcode --output-format stream-json --mode yolo --cwd <cwd> [--resume <sess_…>] --prompt=<prompt>` |
| prompt | read from stdin by `/bin/sh`, passed as one argument (no stdin mode) | same |
| session id | `session.resume_hint` line written after the turn | `sessionId` on every event |
| end of turn | the resume hint (success) or `error: failed to run prompt: …` on stderr, exit 1 | `result` line (success) or `turn.failed` + `Error: …` on stderr, exit 1 |
| data home | `KIMI_CODE_HOME` (config, sessions, OAuth credentials) | `ZCODE_DATA_BASE_DIR` (provider data); CLI config, sessions and logs stay under `~/.zcode/cli` |
| instructions | `AGENTS.md` (no `@` imports; trusted workspace only) | `~/.zcode/AGENTS.md`, then the project `AGENTS.md` |
| MCP | project `.mcp.json` / `.kimi-code/mcp.json` (trusted workspace only; print mode has no trust prompt, so the routed env sets `KIMI_CODE_TRUST_WORKSPACE=1`), user `$KIMI_CODE_HOME/mcp.json` | user `~/.zcode/cli/config.json` (`mcp.servers`); project servers need an interactive trust review |
| read-only turn | not available: the turn is held (see below) | `--disallowed-tools` removes write and command tools |
| worktrees | `--add-dir` | no flag; yolo does not confine paths, worktrees are reached by path |

## Configuration

```toml
[[agents]]
name = "lane"
harness = "claude"

  [agents.alt.kimi]
  binary = "/path/to/kimi"
  args = ["--model", "<alias>"]

  [agents.alt.zcode]
  binary = "/path/to/wrapper"      # supplies the provider config (API key) for the CLI
```

The routing command selects the harness as for Codex (`MESH_RUNNER_HARNESS`); the environment it
returns carries the harness's home (`KIMI_CODE_HOME`, `ZCODE_DATA_BASE_DIR`).

## Errors the runner acts on

- Provider limit: Kimi `provider.rate_limit` or a quota message; Z.ai business codes 1302–1305,
  1308 (5-hour window), 1310 (weekly or monthly). The adapter names them so the turn error matches
  the runner's quota hint and `on_limit_command` runs.
- Unknown session: `Session "<id>" not found` / `Session not found: <id>` match the missing-session
  hint; the task starts a new session once.
- A rejected login or key sets `auth_failed`; the runner's authentication hold covers `claude` and
  `codex` only, so for these harnesses it is an ordinary turn error.

## Read-only metadata turns

A decision request that must run without write tools calls `with_read_only`. Kimi's print mode
cannot be combined with plan mode and its deny rules live in the account's `config.toml`, so the
adapter raises `NotImplementedError`; the runner then gives the start back (`aborted`, the task backs
off for the gate-hold period) instead of running it with write tools.
