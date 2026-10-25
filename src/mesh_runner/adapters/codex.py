"""Codex adapter: one non-interactive thread per task, resumed by thread id.

    codex [--profile P] -C <cwd> [--add-dir D] exec --json -             (prompt on stdin)
    codex [--profile P] -C <cwd> [--add-dir D] exec resume <thread> --json -

Profile, working directory and extra writable directories are root options:
``codex exec resume`` does not accept them itself. The thread id arrives in the
first event (``thread.started``); the runner stores it as the session id.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .base import Adapter, TurnResult, read_events

_MODEL_LINE = re.compile(r'^\s*model\s*=\s*"([^"]+)"', re.M)


class CodexAdapter(Adapter):
    name = "codex"

    def __init__(self, binary: str = "codex", extra_args: list[str] | None = None, profile: str = ""):
        super().__init__(binary, extra_args)
        self.profile = profile

    def _root(self, cwd: Path, add_dirs: list[Path]) -> list[str]:
        argv = [self.binary]
        if self.profile:
            argv += ["--profile", self.profile]
        argv += ["-C", str(cwd)]
        for d in add_dirs:
            argv += ["--add-dir", str(d)]
        return argv

    # The session cwd is the agent's profile, which need not be a git repository
    # (code comes in through --add-dir worktrees); without this Codex refuses with
    # "Not inside a trusted directory" before reading the prompt.
    EXEC_OPTS = ("--json", "--skip-git-repo-check")

    def start_argv(self, session_id: str, cwd: Path, add_dirs: list[Path]) -> list[str]:
        return [*self._root(cwd, add_dirs), "exec", *self.EXEC_OPTS, *self.extra_args, "-"]

    def resume_argv(self, session_id: str, cwd: Path, add_dirs: list[Path]) -> list[str]:
        return [*self._root(cwd, add_dirs), "exec", "resume", session_id, *self.EXEC_OPTS, *self.extra_args, "-"]

    def interactive_argv(self, session_id: str, cwd: Path, add_dirs: list[Path], resume: bool) -> list[str]:
        """The interactive TUI (no exec): ``codex [root options] [resume <thread>]``."""
        argv = self._root(cwd, add_dirs)
        if resume and session_id:
            argv += ["resume", session_id]
        return argv + self.extra_args

    def with_read_only(self, argv: list[str]) -> list[str]:
        """Codex's own read-only sandbox; a root option, so it goes before any subcommand."""
        out, j = [], 0
        while j < len(argv):                   # drop a configured sandbox, then name ours
            if argv[j] in ("--sandbox", "-s"):
                j += 2
                continue
            if argv[j].startswith("--sandbox="):
                j += 1
                continue
            if argv[j] in ("--full-auto", "--dangerously-bypass-approvals-and-sandbox"):
                j += 1
                continue
            out.append(argv[j])
            j += 1
        # headless: before ``exec``; the interactive TUI has no subcommand (or ``resume``)
        i = next((k for k, a in enumerate(out) if a in ("exec", "resume")), len(out))
        return out[:i] + ["--sandbox", "read-only"] + out[i:]

    def profile_model(self) -> str:
        """The model the profile pins (``~/.codex/<profile>.config.toml``): Codex does
        not name it in its event stream, and spend reports need it."""
        if not self.profile:
            return ""
        home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        for name in (f"{self.profile}.config.toml", "config.toml"):
            try:
                text = (home / name).read_text(errors="replace")
            except OSError:
                continue
            m = _MODEL_LINE.search(text.split("\n[", 1)[0])     # top level only, not [profiles.*]
            if m:
                return m.group(1)
        return ""

    def with_effort(self, argv: list[str], effort: str) -> list[str]:
        return [argv[0], "-c", f'model_reasoning_effort="{effort}"', *argv[1:]]

    def parse(self, log_path: Path) -> TurnResult:
        r = TurnResult()
        r.model = self.profile_model()
        for ev in read_events(log_path):
            t = ev.get("type", "")
            if t == "thread.started" and ev.get("thread_id"):
                r.session_id = ev["thread_id"]
            elif t == "item.completed":
                item = ev.get("item") or {}
                if item.get("type") in ("agent_message", "assistant_message") and item.get("text"):
                    r.final_text = item["text"]
            elif t == "turn.completed":
                r.finished = True
                r.usage = ev.get("usage") or {}
                r.is_error, r.error = False, ""     # a transient error earlier in the turn did not end it
            elif t in ("turn.failed", "error"):
                r.finished = True
                r.is_error = True
                err = ev.get("error") or {}
                r.error = (err.get("message") if isinstance(err, dict) else str(err)) or ev.get("message", "") or t
        return r
