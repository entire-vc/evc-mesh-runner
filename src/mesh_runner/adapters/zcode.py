"""ZCode agent CLI adapter (``zai-org/ZCode``, binary ``zcode``): one headless session per task,
resumed by id.

    zcode --output-format stream-json --mode yolo --cwd <cwd> ... --prompt=<prompt>
    zcode --output-format stream-json --mode yolo --cwd <cwd> ... --resume <sess_…> --prompt=<prompt>

The prompt is taken only as an argument: a POSIX shell reads the runner's stdin and hands it over
as one ``--prompt=`` argument. ``--mode yolo`` skips permission prompts (there is no approval
client in a headless run); ZCode has no ``--add-dir``, and yolo does not confine tools to the
cwd, so the task's worktrees are reachable by path.

Like the Kimi adapter it has no ``interactive_argv``: a GLM Coding Plan key is the same quota
in the TUI and in a headless prompt, so ``interactive.wrap`` keeps it headless in every mode.

Stream (NDJSON on stdout): ``{"type": "turn.started" | "model.streaming" | "tool.updated" |
"session.updated" | "turn.completed" | "turn.failed" | …, "sessionId": "sess_…", "payload": {…}}``
and, after a completed turn, one ``{"type": "result", "sessionId", "response", "usage", …}`` line.
A failed turn has ``turn.failed`` (``payload.error``: code, message, attribution with the provider's
status and business code), no result line, ``Error: …`` on stderr and exit 1.
"""
from __future__ import annotations

import re
from pathlib import Path

from .base import Adapter, TurnResult, read_events

_SHELL = 'prompt=$(cat); exec "$0" "$@" "--prompt=$prompt"'
ERROR_RE = re.compile(r"^Error: (.*)$", re.M)
# Z.ai business codes of the plan's rate and quota windows (1308: 5-hour, 1310: weekly/monthly)
LIMIT_CODES = {"1302", "1303", "1304", "1305", "1308", "1310"}
# Tools that change files, run commands or start other agents: removed whole for a read-only turn.
READ_ONLY_DENIED = ("Bash", "Write", "Edit", "ApplyPatch", "Agent", "Workflow", "CreateSession",
                    "SendToSession", "ForkSession", "CronCreate", "CronUpdate", "CronDelete", "OffPeakCreate")


def _usage(raw: dict) -> dict:
    """ZCode reports camelCase usage (inputTokens, outputTokens, cacheReadTokens, ...); the
    completion line and cost tools read the Anthropic-style keys. Both are kept."""
    out = dict(raw)
    for src, dst in (("inputTokens", "input_tokens"), ("outputTokens", "output_tokens"),
                     ("cacheReadTokens", "cache_read_input_tokens"),
                     ("cacheWriteTokens", "cache_creation_input_tokens")):
        v = raw.get(src)
        if type(v) is int and dst not in out:
            out[dst] = v
    return out


class ZCodeAdapter(Adapter):
    name = "zcode"

    def __init__(self, binary: str = "zcode", extra_args: list[str] | None = None, shell: str = "/bin/sh"):
        super().__init__(binary, extra_args)
        self.shell = shell

    def _argv(self, cwd: Path, session: str = "") -> list[str]:
        argv = [self.shell, "-c", _SHELL, self.binary, "--output-format", "stream-json",
                "--mode", "yolo", "--cwd", str(cwd)]
        if session:
            argv += ["--resume", session]
        return argv + self.extra_args

    def start_argv(self, session_id: str, cwd: Path, add_dirs: list[Path]) -> list[str]:
        return self._argv(cwd)

    def resume_argv(self, session_id: str, cwd: Path, add_dirs: list[Path]) -> list[str]:
        return self._argv(cwd, session_id)

    def with_read_only(self, argv: list[str]) -> list[str]:
        """The write and command tools are removed for this run (``--disallowed-tools``): they are
        not registered, so the model cannot call them; Mesh MCP tools stay available."""
        out, i = [], 0
        while i < len(argv):                   # drop a configured denylist, then name ours
            if argv[i] in ("--disallowed-tools", "--disallowedTools"):
                i += 2
                continue
            out.append(argv[i])
            i += 1
        return out + ["--disallowed-tools", " ".join(READ_ONLY_DENIED)]

    def with_slot_confined(self, argv: list[str]) -> list[str]:
        raise NotImplementedError("no sandbox: writes cannot be confined to the slot")

    def parse(self, log_path: Path) -> TurnResult:
        r = TurnResult()
        worked = False
        provider = {}                          # the last failed model request: status, business code
        for ev in read_events(log_path):
            kind = ev.get("type", "")
            payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            if ev.get("sessionId"):
                r.session_id = str(ev["sessionId"])
            if kind == "tool.updated":
                worked = True
            elif kind == "session.updated" and payload.get("type") == "model_request_failed":
                provider = payload
            elif kind == "turn.completed":
                worked = True
                if isinstance(payload.get("response"), str) and payload["response"].strip():
                    r.final_text = payload["response"]
                r.usage = _usage(payload.get("usage")) if isinstance(payload.get("usage"), dict) else r.usage
            elif kind == "turn.failed":
                err = payload.get("error") if isinstance(payload.get("error"), dict) else {}
                attr = err.get("attribution") if isinstance(err.get("attribution"), dict) else {}
                r.finished = r.is_error = True
                r.error_details = self._failure(err, attr, provider)
                r.auth_failed = attr.get("reason") == "auth_failed" or attr.get("statusCode") in (401, 403)
            elif kind == "result":
                r.finished = True
                if isinstance(ev.get("response"), str):
                    r.final_text = ev["response"]
                r.usage = _usage(ev.get("usage")) if isinstance(ev.get("usage"), dict) else r.usage
        if not r.finished:
            try:
                stderr = log_path.with_suffix(".stderr").read_text(errors="replace")
            except OSError:
                stderr = ""
            failed = ERROR_RE.findall(stderr)
            if failed:                         # the process refused before a turn (unknown session, bad flag)
                r.finished = r.is_error = True
                r.error_details = failed[-1].strip()
        if r.is_error:
            r.error = r.error_details[:300] or "turn failed"
            r.auth_only = r.auth_failed and not worked
        return r

    @staticmethod
    def _failure(err: dict, attr: dict, provider: dict) -> str:
        code = str(attr.get("providerErrorCode") or provider.get("providerErrorCode") or err.get("code") or "")
        status = attr.get("statusCode") or provider.get("statusCode")
        message = str(err.get("message") or provider.get("providerErrorMessage") or provider.get("message") or "")
        reason = str(attr.get("reason") or provider.get("reason") or "")
        text = " ".join(x for x in (f"[{code}]" if code else "", f"HTTP {status}" if status else "",
                                    reason, message) if x)
        if code in LIMIT_CODES or status == 429 or "rate" in reason:
            text = "quota: rate limit / usage limit of the plan: " + text   # the runner switches the account
        return text or "turn failed"
