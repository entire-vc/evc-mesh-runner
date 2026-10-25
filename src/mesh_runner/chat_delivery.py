"""Delivers a chat turn into an interactive session through ``tui_driver`` and turns what the
driver wrote (the headless adapters' event format) into protocol events.

One driver process per turn; the session lives on in its tmux window, keyed by the chat
(``chat-<thread8>``), so the next message reuses it warm. Stop terminates the driver, which
presses Escape in the session and leaves the session alive. Tool activity leaves only as a
short label (never input or output).
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterable, Iterator

from .interactive import CHAT_PREFIX

log = logging.getLogger(__name__)
LABEL_MAX = 120


def window_key(thread_id: str) -> str:
    """tmux window / state key of a chat; the ``chat-`` prefix keeps it apart from attempt ids."""
    return CHAT_PREFIX + "".join(c for c in thread_id if c.isalnum())[:12]


def translate(lines: Iterable[dict], harness: str) -> Iterator[dict]:
    """Driver output events → protocol events (no ``seq``). Always ends with final or error."""
    sid, ended, calls = "", False, 0
    for ev in lines:
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init" and ev.get("session_id"):
            sid = ev["session_id"]
            yield {"type": "session", "session_id": sid, "harness": harness, "resumed": False}
        elif t == "thread.started":
            sid = ev.get("thread_id") or sid
            yield {"type": "session", "session_id": sid, "harness": harness}
        elif t == "assistant":
            for b in (ev.get("message") or {}).get("content") or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text"):
                    yield {"type": "text_delta", "text": b["text"]}
                elif b.get("type") == "tool_use":
                    calls += 1
                    cid = str(b.get("id") or f"t{calls}")
                    label = ("Using " + str(b.get("name") or "a tool"))[:LABEL_MAX]
                    yield {"type": "tool_status", "tool_call_id": cid, "label": label, "status": "started"}
                    yield {"type": "tool_status", "tool_call_id": cid, "label": label, "status": "finished"}
        elif t == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
            text = ev["item"].get("text") or ""
            if text:
                yield {"type": "text_delta", "text": text}
        elif t == "result":
            u = ev.get("usage") or {}
            tin = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                                   "cache_creation_input_tokens"))
            yield {"type": "usage", "tokens_in": tin, "tokens_out": int(u.get("output_tokens") or 0)}
            ended = True
            if ev.get("is_error"):
                yield {"type": "error", "code": "harness_error", "retryable": False,
                       "message": str(ev.get("result") or "turn failed")[:200]}
            else:
                yield {"type": "final", "stop_reason": "end_turn"}
            return
        elif t == "turn.completed":
            u = ev.get("usage") or {}
            yield {"type": "usage", "tokens_in": int(u.get("input_tokens") or 0),
                   "tokens_out": int(u.get("output_tokens") or 0)}
            ended = True
            yield {"type": "final", "stop_reason": "end_turn"}
            return
        elif t == "turn.failed":
            ended = True
            yield {"type": "error", "code": "harness_error", "retryable": False,
                   "message": str((ev.get("error") or {}).get("message") or "turn failed")[:200]}
            return
    if not ended:
        yield {"type": "error", "code": "no_result", "retryable": False,
               "message": "the session ended the turn without a result"}


def read_log(path: Path) -> list[dict]:
    out = []
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                try:
                    out.append(json.loads(raw))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


class DriverDelivery:
    """``plan(turn)`` returns ``(argv, cwd, env, prompt)`` for the driver (the service builds it
    from the agent's adapter, routing environment and the chat's session id)."""

    def __init__(self, harness: str, plan: Callable[[dict], tuple[list[str], Path, dict, str]],
                 log_dir: Path, poll: float = 0.2, term_wait: float = 20.0):
        self.harness, self.plan, self.log_dir = harness, plan, Path(log_dir)
        self.poll, self.term_wait = poll, term_wait

    def run(self, turn: dict, cancelled: Callable[[], bool]) -> Iterator[dict]:
        argv, cwd, env, prompt, *rest = self.plan(turn)
        harness = rest[0] if rest else self.harness       # a plan may name the harness it routed to
        # one directory per chat: deleting the chat removes whatever a crash left in it
        logs = self.log_dir / window_key(str(turn.get("thread_id") or "none"))
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(dir=logs, prefix=turn["turn_id"][:8] + ".", suffix=".jsonl")
        log = Path(name)
        errlog = log.with_suffix(".stderr")
        try:
            yield from self._drive(argv, cwd, env, prompt, cancelled, fd, log, errlog, harness)
        finally:                                        # the answer text must not outlive the turn on disk
            for f in (log, errlog):
                try:
                    f.unlink()
                except OSError:
                    pass
            try:
                logs.rmdir()
            except OSError:
                pass

    @staticmethod
    def _log_timing(errlog: Path) -> None:
        """The driver's ``timing:`` line (seconds per phase) goes to the runner log; nothing else of its stderr."""
        try:
            for line in errlog.read_text(errors="replace").splitlines():
                if line.startswith("timing: "):
                    log.info("chat turn %s", line[:200])
        except OSError:
            pass

    def _drive(self, argv, cwd, env, prompt, cancelled, fd, log, errlog, harness) -> Iterator[dict]:
        with os.fdopen(fd, "ab") as out, open(os.open(errlog, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "ab") as err:
            proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.PIPE, stdout=out, stderr=err,
                                    start_new_session=True)
        try:
            proc.stdin.write(prompt.encode())
            proc.stdin.close()
        except BrokenPipeError:
            pass
        interrupted = False
        while proc.poll() is None:
            if cancelled() and not interrupted:
                interrupted = True
                os.killpg(proc.pid, signal.SIGTERM)       # the driver presses Escape; the window stays
                deadline = time.monotonic() + self.term_wait
                while proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(self.poll)
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                break
            time.sleep(self.poll)
        proc.wait()
        self._log_timing(errlog)
        events = read_log(log)
        if interrupted:
            for ev in translate(events, harness):
                if ev["type"] in ("final", "error"):
                    break
                yield ev
            yield {"type": "final", "stop_reason": "cancelled"}
            return
        yield from translate(events, harness)
