"""Kimi Code CLI adapter (npm ``@moonshot-ai/kimi-code``, binary ``kimi``): one print-mode session
per task, resumed by id.

    kimi --output-format stream-json [--add-dir D] ... --prompt=<prompt>
    kimi --output-format stream-json [--add-dir D] ... --session=<id> --prompt=<prompt>

Print mode takes the prompt only as an argument, so the prompt the runner writes to stdin is read by
a POSIX shell and handed over in one ``--prompt=`` argument (the ``=`` form keeps a prompt that
starts with a dash a value). Print mode never asks for approval (the CLI's "auto" policy); static
deny rules of the account's ``config.toml`` still apply.

The adapter has no ``interactive_argv``: ``interactive.wrap`` keeps it headless in every launch mode.
Kimi's print mode draws on the same membership quota as its TUI, so a headless turn costs the
same and needs no terminal.

Stream (one JSON object per stdout line):
    {"role": "meta", "type": "system.version", "version": "2.6.1"}
    {"role": "assistant", "content": "...", "tool_calls": [...]}
    {"role": "tool", "tool_call_id": "...", "content": "..."}
    {"role": "meta", "type": "turn.step.retrying", ...}
    {"role": "meta", "type": "session.resume_hint", "session_id": "<id>", ...}   # after a turn
    {"type": "error", "code": "...", "message": "..."}                             # session busy
A failed turn prints ``error: failed to run prompt: <code>: <message>`` on stderr and exits 1; the
adapter reads that line from the turn's ``.stderr`` file next to the log.

Usage. Print mode writes no usage to the stream; Kimi keeps it in the session's journal
``$KIMI_CODE_HOME/sessions/<workspace>/<session id>/agents/main/wire.jsonl``:
    {"type": "turn.prompt", "turnId": 1, ...}
    {"type": "usage.record", "usage": {"inputOther": 905, "output": 768, "inputCacheRead": 55296,
                                       "inputCacheCreation": 0}, "usageScope": "turn", ...}   # per step
    {"type": "turn.ended", "turnId": 1, "reason": "completed", ...}
One invocation runs one turn. Before it starts, ``spawn`` notes next to the turn log (``.wire``) which
Kimi home it runs in, which session it resumes, how long that journal already is, its file identity
and the hash of the bytes before that point; ``parse`` sums the ``usage.record`` events of the first
turn the journal gained after that point, and reads nothing from a journal replaced or rewritten
since. So every runner turn reads its own Kimi turn, and a session's usage is never counted twice.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from .base import Adapter, Launch, TurnResult, read_events

# The shell reads the whole prompt, then execs kimi with it as the last argument.
_SHELL = 'prompt=$(cat); exec "$0" "$@" "--prompt=$prompt"'
# a Kimi session id as the CLI writes it (session_<uuid>): letters, digits, "-", "_" only
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
FAILED_RE = re.compile(r"^error: failed to run prompt: (.*)$", re.M)
# Provider codes and words that mean the account's quota or rate window, not the task. The runner
# moves the lane to another account on "quota"/"rate limit"; these are named so it always matches.
_LIMIT_RE = re.compile(r"provider\.rate_limit|rate[ _-]?limit|quota|usage limit|too many requests|"
                       r"\b429\b|membership|insufficient[ _]balance", re.I)
_AUTH_RE = re.compile(r"provider\.auth_error|no model configured|/login|unauthori[sz]ed|\b401\b", re.I)
# Kimi's usage keys -> the keys the completion line and the spend report read. ``inputOther`` is the
# input that was neither read from nor written to the cache, as Anthropic's ``input_tokens``.
_USAGE_KEYS = (("inputOther", "input_tokens"), ("output", "output_tokens"),
               ("inputCacheRead", "cache_read_input_tokens"), ("inputCacheCreation", "cache_creation_input_tokens"))


def _typed(detail: str) -> str:
    """A failure in the runner's words: a provider limit says so."""
    if _LIMIT_RE.search(detail) and not re.search(r"quota|rate.?limit", detail, re.I):
        return "quota: " + detail
    return detail


def _journal(home: Path, session_id: str) -> Path | None:
    """The main agent's journal of ``session_id`` in Kimi home ``home``; None unless exactly one.
    The id must have the CLI's own shape and is joined as a path, never used as a glob pattern."""
    sessions = home / "sessions"
    if not _SESSION_ID_RE.fullmatch(session_id or ""):
        return None
    try:
        found = [w for ws in sessions.iterdir() if ws.is_dir() and not ws.name.startswith(".")
                 for w in (ws / session_id / "agents" / "main" / "wire.jsonl",) if w.is_file()]
    except OSError:
        return None
    return found[0] if len(found) == 1 else None


_PREFIX = 4096          # bytes before the mark whose hash says the journal is still the one measured


def _identity(fh, offset: int) -> dict:
    """What tells this journal from a replacement: its file identity and the hash of the bytes
    just before ``offset`` (a rewrite in place keeps the inode, a replacement by rename does not)."""
    st = os.fstat(fh.fileno())
    fh.seek(max(0, offset - _PREFIX))
    return {"dev": st.st_dev, "ino": st.st_ino,
            "prefix": hashlib.sha256(fh.read(min(offset, _PREFIX))).hexdigest()}


def _turn_usage(journal: Path, mark: dict) -> dict:
    """Summed usage of the first turn that starts at or after the mark's byte offset of the journal,
    {} when the journal is no longer the one the mark measured."""
    offset = mark["offset"]
    started, turn, total = False, None, {}
    try:
        with open(journal, "rb") as fh:
            if fh.seek(0, 2) < offset:
                return {}                         # the journal was replaced: the mark means nothing
            if offset and any(_identity(fh, offset).get(k) != mark.get(k) for k in ("dev", "ino", "prefix")):
                return {}                         # replaced or rewritten since the mark, whatever its size
            fh.seek(offset)
            for raw in fh:                        # line by line: a long session's journal is large
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                kind = ev.get("type") if isinstance(ev, dict) else None
                if kind == "turn.prompt":
                    if started:
                        break                     # the next invocation's turn
                    started, turn = True, ev.get("turnId")
                elif not started:
                    continue
                elif kind == "turn.ended" and ev.get("turnId") == turn:
                    break
                elif kind == "usage.record" and isinstance(ev.get("usage"), dict):
                    for src, dst in _USAGE_KEYS:
                        v = ev["usage"].get(src)
                        if type(v) is int:
                            total[dst] = total.get(dst, 0) + v
    except OSError:
        return {}
    return total


class KimiAdapter(Adapter):
    name = "kimi"

    def __init__(self, binary: str = "kimi", extra_args: list[str] | None = None, shell: str = "/bin/sh"):
        super().__init__(binary, extra_args)
        self.shell = shell

    def _argv(self, add_dirs: list[Path], session: str = "") -> list[str]:
        argv = [self.shell, "-c", _SHELL, self.binary, "--output-format", "stream-json"]
        for d in add_dirs:
            argv += ["--add-dir", str(d)]
        if session:
            argv.append(f"--session={session}")
        return argv + self.extra_args

    def start_argv(self, session_id: str, cwd: Path, add_dirs: list[Path]) -> list[str]:
        return self._argv(add_dirs)

    def resume_argv(self, session_id: str, cwd: Path, add_dirs: list[Path]) -> list[str]:
        return self._argv(add_dirs, session_id)

    @staticmethod
    def _home(env: dict) -> Path:
        return Path(env.get("KIMI_CODE_HOME") or Path(env.get("HOME") or Path.home()) / ".kimi-code")

    def session_known(self, session_id: str, cwd: Path, env: dict) -> bool:
        """False only when the Kimi home of ``env`` keeps sessions and none is this one
        (``sessions/<workspace>/<session id>/``); a home without a sessions directory says True."""
        sessions = self._home(env) / "sessions"
        if not session_id or not sessions.is_dir() or "/" in session_id or session_id.startswith("."):
            return True
        return any(p.is_dir() for p in sessions.glob(f"*/{session_id}"))

    def with_model(self, argv: list[str], model: str) -> list[str]:
        out, i = [], 0
        while i < len(argv):                   # drop every configured model, then name ours
            if argv[i] in ("--model", "-m"):
                i += 2
                continue
            if not argv[i].startswith("--model="):
                out.append(argv[i])
            i += 1
        return out + ["--model", model]

    def with_read_only(self, argv: list[str]) -> list[str]:
        """Not available: print mode cannot be combined with ``--plan``, and Kimi's deny rules live
        only in the account's ``config.toml`` (one per data home, which also holds the session), so
        one turn cannot be confined without moving its session."""
        raise NotImplementedError("print mode has no per-turn read-only switch")

    def with_slot_confined(self, argv: list[str]) -> list[str]:
        raise NotImplementedError("no sandbox: writes cannot be confined to the slot")

    def spawn(self, launch: Launch, log_path: Path):
        """Notes where this turn's session journal stands before the turn starts (see the module
        docstring); a mark that cannot be written leaves the usage unknown and never fails a launch."""
        home = self._home(launch.env)
        session = next((a[len("--session="):] for a in launch.argv if a.startswith("--session=")), "")
        journal = _journal(home, session)
        mark = {"home": str(home), "session": session, "offset": 0}
        try:
            if journal:
                with open(journal, "rb") as fh:
                    mark["offset"] = fh.seek(0, 2)
                    mark.update(_identity(fh, mark["offset"]))
        except OSError:
            mark = {}                             # unmeasured: this turn's usage stays unknown
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.with_suffix(".wire").write_text(json.dumps(mark))
        except OSError:
            pass
        return super().spawn(launch, log_path)

    @staticmethod
    def _usage(log_path: Path, session_id: str) -> dict:
        try:
            mark = json.loads(log_path.with_suffix(".wire").read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(mark, dict):
            return {}
        home, resumed, offset = Path(str(mark.get("home") or "")), str(mark.get("session") or ""), mark.get("offset")
        session = session_id or resumed
        if not home.is_absolute() or type(offset) is not int or offset < 0 or not session:
            return {}
        if resumed and session != resumed:
            return {}                             # the mark measured another session's journal
        journal = _journal(home, session)
        return _turn_usage(journal, mark) if journal else {}

    @staticmethod
    def failure_sources(log_path: Path) -> list[str]:
        """Every terminal failure the turn names, typed as ``parse`` types its one: the stream's ``error``
        events (``code: message``) and each ``error: failed to run prompt:`` line on stderr. ``parse`` reports
        only one of them (the last stderr line wins), so a verdict about the cause must hold for all of them,
        or the evidence contradicts itself."""
        out = [f"{ev.get('code') or 'error'}: {ev.get('message') or ''}".strip()
               for ev in read_events(log_path) if ev.get("role") is None and ev.get("type") == "error"]
        try:
            stderr = log_path.with_suffix(".stderr").read_text(errors="replace")
        except OSError:
            stderr = ""
        return [_typed(x) for x in out + [line.strip() for line in FAILED_RE.findall(stderr)]]

    def parse(self, log_path: Path) -> TurnResult:
        r = TurnResult()
        worked = False
        for ev in read_events(log_path):
            role, kind = ev.get("role"), ev.get("type")
            if role == "assistant":
                worked = True
                text = ev.get("content")
                if isinstance(text, str) and text.strip():
                    r.final_text = text
            elif role == "tool":
                worked = True
            elif role == "meta" and kind == "session.resume_hint" and ev.get("session_id"):
                r.session_id = str(ev["session_id"])
                r.finished = True              # written only after the turn completed
            elif role is None and kind == "error":
                r.finished = r.is_error = True
                r.error_details = f"{ev.get('code') or 'error'}: {ev.get('message') or ''}".strip()
        try:
            stderr = log_path.with_suffix(".stderr").read_text(errors="replace")
        except OSError:
            stderr = ""
        failed = FAILED_RE.findall(stderr)
        if failed:
            r.finished = r.is_error = True
            r.error_details = failed[-1].strip()
        if worked or (r.finished and not r.is_error):
            # only a turn that ran: an invocation refused before its prompt wrote no turn, and the
            # first turn after its mark would be the next invocation's
            r.usage = self._usage(log_path, r.session_id) or r.usage
        if r.is_error:
            detail = _typed(r.error_details or "turn failed")
            r.error_details = detail
            r.error = detail[:300]
            r.auth_failed = bool(_AUTH_RE.search(detail)) and not _LIMIT_RE.search(detail)
            r.auth_only = r.auth_failed and not worked
        return r
