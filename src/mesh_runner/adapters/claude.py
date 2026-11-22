"""Claude Code adapter: one headless session per task, resumed by id.

    claude -p --session-id <uuid> --output-format stream-json --verbose ...
    claude -p --resume <uuid>     --output-format stream-json --verbose ...

The prompt is written to stdin. The agent's rules load from the session cwd
(CLAUDE.md, .claude/, .mcp.json), exactly as in an interactive session, and
the task's worktrees are added with --add-dir.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from .base import Adapter, TurnResult, read_events
from ..auth import is_auth_banner, is_verification_required


RATE_LIMIT_STATUSES = ("allowed", "allowed_warning", "rejected")
# ``stop_details.category`` of a provider refusal is an identifier (``reasoning_extraction``, ``cyber``):
# anything else (prose, a long or odd value) is not published and reads as ``unspecified``.
REFUSAL_CATEGORY = re.compile(r"[a-z][a-z0-9_.-]{0,47}")


def refusal_category(message: dict) -> str:
    """The category of the provider's typed refusal of one assistant message (``stop_reason: refusal``),
    ``unspecified`` without a well-formed one; '' when the message is no refusal. Only the typed fields
    are read: the explanation and the message text never are."""
    if not isinstance(message, dict) or message.get("stop_reason") != "refusal":
        return ""
    details = message.get("stop_details")
    category = details.get("category") if isinstance(details, dict) else None
    return category if isinstance(category, str) and REFUSAL_CATEGORY.fullmatch(category) else "unspecified"


def _epoch(value) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None


def _fraction(value) -> float | None:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 10
    return round(float(value), 4) if ok else None


def rate_limit_info(raw) -> dict:
    """The provider's account-window verdict from a ``rate_limit_event`` (``claude -p`` stream-json),
    reduced to numbers and enums: ``status``, ``resets_at`` (epoch), ``rate_limit_type``,
    ``is_using_overage`` and ``windows`` ({window: {utilization, resets_at}} from ``unifiedWindows``).
    An event without a known status is no evidence: {}."""
    if not isinstance(raw, dict) or raw.get("status") not in RATE_LIMIT_STATUSES:
        return {}
    kind = raw.get("rateLimitType")
    windows = {}
    for name, w in (raw.get("unifiedWindows") or {}).items() if isinstance(raw.get("unifiedWindows"), dict) else ():
        if isinstance(name, str) and len(name) <= 32 and isinstance(w, dict):
            windows[name] = {"utilization": _fraction(w.get("utilization")), "resets_at": _epoch(w.get("resetsAt"))}
    if isinstance(kind, str) and kind not in windows and _fraction(raw.get("utilization")) is not None:
        windows[kind[:32]] = {"utilization": _fraction(raw.get("utilization")), "resets_at": _epoch(raw.get("resetsAt"))}
    return {"status": raw["status"], "resets_at": _epoch(raw.get("resetsAt")),
            "rate_limit_type": kind[:32] if isinstance(kind, str) else "",
            "is_using_overage": raw.get("isUsingOverage") is True, "windows": windows}


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

    ACCOUNTS_DIR = ".claude-accounts"

    @classmethod
    def _config_dirs(cls, env: dict) -> tuple[Path, list[Path]]:
        """``(current config dir, every config dir on this host that may hold the session)``:
        the current one, the default ``~/.claude``, its siblings, and the fleet's account
        directories ``~/.claude-accounts/*``. A lane on the default directory (no
        CLAUDE_CONFIG_DIR) has no siblings of its own, so the account directories must be
        named: a session may exist only under the account the lane just left; resuming without
        carrying it would fail with "No conversation found"."""
        home = Path(env.get("HOME") or Path.home())
        current = Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude")
        dirs = [current, home / ".claude"]
        roots = [home / cls.ACCOUNTS_DIR]
        if current.parent != home:
            roots.append(current.parent)
        for root in roots:
            if root.is_dir():
                dirs += sorted(p for p in root.iterdir() if p.is_dir())
        return current, list(dict.fromkeys(dirs))        # ordered, no duplicates

    def session_known(self, session_id: str, cwd: Path, env: dict) -> bool:
        """The current config directory holds the session's transcript: only then can
        ``--resume`` find it. Call after ``prepare_resume``."""
        current, _ = self._config_dirs(env)
        return any((current / "projects").glob(f"*/{session_id}.jsonl"))

    def prepare_resume(self, session_id: str, cwd: Path, env: dict) -> str:
        """Claude keeps sessions per config directory (CLAUDE_CONFIG_DIR). When the
        agent now runs under another account than the one that last ran the session
        (a quota switch, a pin), --resume finds nothing, or an older copy. Bring the
        newest transcript on this host into the current config directory, keeping its
        project folder."""
        current, dirs = self._config_dirs(env)
        name = f"{session_id}.jsonl"
        copies = []
        for d in dirs:
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

    def with_slot_confined(self, argv: list[str]) -> list[str]:
        """Not available: this harness has no sandbox here and its file tools are not path-bounded
        in a session that skips permission prompts, so a write session cannot be confined to a directory."""
        raise NotImplementedError("no sandbox: writes cannot be confined to the slot")

    def without_local_memory(self, env: dict) -> dict:
        """Auto-memory off: with it on, "remember this" goes to a memory file (denied in a read-only
        chat, and shared by every chat of the agent) instead of the Mesh server's remember."""
        return {**env, "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}

    def with_instructions(self, argv: list[str], text: str) -> list[str]:
        return argv + ["--append-system-prompt", text]

    def with_mcp(self, argv: list[str], name: str, command: list[str], env: dict) -> list[str]:
        """Only this server: --strict-mcp-config drops the profile's .mcp.json (the agent's own Mesh
        MCP, with the agent key). It also ends the variadic --mcp-config before any later flag."""
        cfg = {"mcpServers": {name: {"command": command[0], "args": list(command[1:]), "env": dict(env)}}}
        return argv + ["--mcp-config", json.dumps(cfg), "--strict-mcp-config"]

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
        refusal = ""
        for ev in read_events(log_path):
            sid = ev.get("session_id")
            if sid:
                r.session_id = sid
            if ev.get("type") == "system" and ev.get("model"):
                r.model = str(ev["model"])
            if ev.get("type") == "system" and ev.get("subtype") == "init" and isinstance(ev.get("apiKeySource"), str):
                r.api_key_source = ev["apiKeySource"][:40]
            if ev.get("type") == "system" and ev.get("subtype") == "compact_boundary":
                r.compactions += 1
            if ev.get("type") == "rate_limit_event":
                info = rate_limit_info(ev.get("rate_limit_info"))
                if info:
                    r.rate_limit = info
                    r.rate_limit_rejected = r.rate_limit_rejected or info["status"] == "rejected"
            if ev.get("type") == "assistant":
                # the turn's last assistant message decides: a refusal the model got past is no refusal
                refusal = refusal_category(ev.get("message") or {})
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
                hook_auth = (ev.get("runner_error") == {
                    "source": "claude.StopFailure", "code": "authentication_failed"})
                r.auth_failed = r.is_error and (hook_auth or any(is_auth_banner(str(part))
                    for part in [reason, *(errs if isinstance(errs, list) else [])]))
                # the provider wants an account check no retry can satisfy: same profile hold as auth,
                # but only the exact terminal reason, never assistant text or a longer message
                r.verification_required = r.is_error and any(is_verification_required(str(part))
                    for part in [reason, *(errs if isinstance(errs, list) else [])])
                output = (ev.get("usage") or {}).get("output_tokens", 0)
                r.auth_only = (r.auth_failed or r.verification_required) and not had_work and not (isinstance(output, (int, float)) and output > 0)
                parts = [x for x in (reason, errors) if x]
                if subtype and subtype != "success":
                    parts.insert(0, subtype)
                r.error_details = ((": ".join(dict.fromkeys(parts)) or "result flagged is_error")
                                   if r.is_error else "")
                r.error = r.error_details[:300]
                r.cost_usd = ev.get("total_cost_usd")
                r.usage = ev.get("usage") or {}
                r.model_usage = ev["modelUsage"] if isinstance(ev.get("modelUsage"), dict) else {}
        if not r.finished:
            r.final_text = last_text
        r.provider_refusal = refusal
        return r
