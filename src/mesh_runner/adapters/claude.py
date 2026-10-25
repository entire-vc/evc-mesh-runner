"""Claude Code adapter: one headless session per task, resumed by id.

    claude -p --session-id <uuid> --output-format stream-json --verbose ...
    claude -p --resume <uuid>     --output-format stream-json --verbose ...

The prompt is written to stdin. The agent's rules load from the session cwd
(CLAUDE.md, .claude/, .mcp.json), exactly as in an interactive session, and
the task's worktrees are added with --add-dir.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .base import Adapter, TurnResult, read_events
from ..auth import is_auth_banner


class ClaudeAdapter(Adapter):
    name = "claude"

    def __init__(self, binary: str = "claude", extra_args: list[str] | None = None):
        super().__init__(binary, extra_args)

    @property
    def session_id_known_upfront(self) -> bool:
        return True

    def _common(self, add_dirs: list[Path]) -> list[str]:
        argv = ["-p", "--output-format", "stream-json", "--verbose"]
        for d in add_dirs:
            argv += ["--add-dir", str(d)]
        return argv + self.extra_args

    def start_argv(self, session_id: str, cwd: Path, add_dirs: list[Path]) -> list[str]:
        return [self.binary, "--session-id", session_id, *self._common(add_dirs)]

    def resume_argv(self, session_id: str, cwd: Path, add_dirs: list[Path]) -> list[str]:
        return [self.binary, "--resume", session_id, *self._common(add_dirs)]

    def interactive_argv(self, session_id: str, cwd: Path, add_dirs: list[Path], resume: bool) -> list[str]:
        """The interactive program (no -p): the same session id, worktrees and flags."""
        argv = [self.binary, "--resume" if resume else "--session-id", session_id]
        for d in add_dirs:
            argv += ["--add-dir", str(d)]
        return argv + self.extra_args

    def prepare_resume(self, session_id: str, cwd: Path, env: dict) -> str:
        """Claude keeps sessions per config directory (CLAUDE_CONFIG_DIR). When the
        agent now runs under another account than the one that last ran the session
        (a quota switch, a pin), --resume finds nothing, or an older copy. Bring the
        newest transcript on this host into the current config directory, keeping its
        project folder."""
        home = Path(env.get("HOME") or Path.home())
        current = Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude")
        name = f"{session_id}.jsonl"
        dirs = [current, home / ".claude"]
        if current.parent.is_dir() and current.parent != home:
            dirs += sorted(p for p in current.parent.iterdir() if p.is_dir())
        copies = []
        for d in dict.fromkeys(dirs):                    # ordered, no duplicates
            if (d / "projects").is_dir():
                copies += [(f.stat().st_mtime, d, f) for f in (d / "projects").glob(f"*/{name}")]
        if not copies:
            return ""
        _, src_dir, newest = max(copies, key=lambda c: c[0])
        if src_dir == current:
            return ""                                    # the current account has the latest turns
        dest = current / "projects" / newest.parent.name
        dest.mkdir(parents=True, exist_ok=True)
        tmp = dest / f".{name}.carry"
        shutil.copy2(newest, tmp)                        # never leave a half-written transcript
        tmp.replace(dest / name)
        side = newest.parent / session_id                # subagent transcripts, tool results
        if side.is_dir():
            shutil.copytree(side, dest / session_id, dirs_exist_ok=True)
        return f"session carried over from {src_dir}"

    def with_effort(self, argv: list[str], effort: str) -> list[str]:
        out, i = [], 0
        while i < len(argv):                   # drop a configured effort, then name ours
            if argv[i] == "--effort":
                i += 2
                continue
            if not argv[i].startswith("--effort="):
                out.append(argv[i])
            i += 1
        return out + ["--effort", effort]

    # Every tool that can change a file or run a command. Claude has no sandbox in
    # headless mode, so the denial list is the boundary; Mesh MCP tools stay available.
    READ_ONLY_DENIED = ("Edit", "MultiEdit", "Write", "NotebookEdit", "Bash")

    def with_read_only(self, argv: list[str]) -> list[str]:
        return argv + ["--disallowedTools", *self.READ_ONLY_DENIED]

    def with_model(self, argv: list[str], model: str) -> list[str]:
        out, i = [], 0
        while i < len(argv):                   # drop every configured model, then name ours
            if argv[i] == "--model":
                i += 2
                continue
            if not argv[i].startswith("--model="):
                out.append(argv[i])
            i += 1
        return out + ["--model", model]

    def parse(self, log_path: Path) -> TurnResult:
        r = TurnResult()
        last_text = ""
        had_work = False
        for ev in read_events(log_path):
            sid = ev.get("session_id")
            if sid:
                r.session_id = sid
            if ev.get("type") == "system" and ev.get("model"):
                r.model = str(ev["model"])
            if ev.get("type") == "system" and ev.get("subtype") == "compact_boundary":
                r.compactions += 1
            if ev.get("type") == "assistant":
                # The CLI can repeat its auth banner as an assistant text event
                # (zero tokens, no tools) before the identical terminal result.
                # Every other content block is conservatively treated as work.
                content = (ev.get("message") or {}).get("content") or []
                had_work = had_work or any(not isinstance(block, dict) or block.get("type") != "text"
                    or (bool(str(block.get("text") or "").strip()) and
                        not is_auth_banner(str(block.get("text") or ""))) for block in content)
                output = ((ev.get("message") or {}).get("usage") or {}).get("output_tokens", 0)
                had_work = had_work or (isinstance(output, (int, float)) and output > 0)
                for block in (ev.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                        last_text = block["text"]
            elif ev.get("type") == "user":
                had_work = had_work or bool((ev.get("message") or {}).get("content"))
            elif ev.get("type") == "result":
                r.finished = True
                r.is_error = bool(ev.get("is_error")) or ev.get("subtype", "success") != "success"
                r.final_text = ev.get("result") or last_text
                errs = ev.get("errors")
                errors = "; ".join(str(e) for e in errs) if isinstance(errs, list) else ""
                # ``success`` describes the terminal envelope, not whether the turn
                # succeeded: Claude can send it with is_error=true and the actual
                # failure in ``result``. Preserve that reason for classification and
                # the hand-off, instead of retrying/reporting an error named "success".
                subtype = str(ev.get("subtype") or "")
                reason = str(ev.get("result") or "")
                r.auth_failed = r.is_error and any(is_auth_banner(str(part))
                    for part in [reason, *(errs if isinstance(errs, list) else [])])
                output = (ev.get("usage") or {}).get("output_tokens", 0)
                r.auth_only = r.auth_failed and not had_work and not (isinstance(output, (int, float)) and output > 0)
                parts = [x for x in (reason, errors) if x]
                if subtype and subtype != "success":
                    parts.insert(0, subtype)
                r.error_details = ((": ".join(dict.fromkeys(parts)) or "result flagged is_error")
                                   if r.is_error else "")
                r.error = r.error_details[:300]
                r.cost_usd = ev.get("total_cost_usd")
                r.usage = ev.get("usage") or {}
        if not r.finished:
            r.final_text = last_text
        return r
