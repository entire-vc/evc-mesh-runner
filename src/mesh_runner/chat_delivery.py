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
import re
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


def _no_reply(why: str) -> dict:
    """A turn that ended without any text: the user would get an empty answer marked done."""
    msg = "the agent ended the turn without a reply" + (f" ({why})" if why else "")
    return {"type": "error", "code": "empty_reply", "retryable": False, "message": msg[:200]}


def _tool_status(cid, name) -> Iterator[dict]:
    """One tool call as a started/finished pair: a short label, never the input or the output."""
    label = ("Using " + str(name or "a tool"))[:LABEL_MAX]
    for status in ("started", "finished"):
        yield {"type": "tool_status", "tool_call_id": str(cid), "label": label, "status": status}


def translate(lines: Iterable[dict], harness: str) -> Iterator[dict]:
    """Driver output events → protocol events (no ``seq``). Always ends with final or error;
    a turn that delivered no text ends with an ``empty_reply`` error."""
    sid, ended, calls, texted = "", False, 0, False
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
                    texted = True
                    yield {"type": "text_delta", "text": b["text"]}
                elif b.get("type") == "tool_use":
                    calls += 1
                    yield from _tool_status(b.get("id") or f"t{calls}", b.get("name"))
        elif t == "item.completed" and (ev.get("item") or {}).get("type") == "tool_call":
            calls += 1
            yield from _tool_status(ev["item"].get("id") or f"t{calls}", ev["item"].get("name"))
        elif t == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
            text = ev["item"].get("text") or ""
            if text:
                texted = True
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
            elif not texted:
                yield _no_reply("")
            else:
                yield {"type": "final", "stop_reason": "end_turn"}
            return
        elif t == "turn.completed":
            u = ev.get("usage") or {}
            yield {"type": "usage", "tokens_in": int(u.get("input_tokens") or 0),
                   "tokens_out": int(u.get("output_tokens") or 0)}
            ended = True
            yield {"type": "final", "stop_reason": "end_turn"} if texted else _no_reply(str(ev.get("no_reply") or ""))
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
        t_plan = time.monotonic()
        try:
            argv, cwd, env, prompt, *rest = self.plan(turn)
        finally:                                          # a slow or failing plan is the one worth seeing
            logging.getLogger(__name__).info("chat turn %s plan=%.1f", str(turn.get("turn_id"))[:8],
                                             time.monotonic() - t_plan)
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

    # the driver's report lines that carry no session text: per-phase seconds, how the session was
    # started, why a turn ended without a reply. Other ``interactive:`` lines can quote the pane.
    _DRIVER_REPORT = re.compile(r"timing: |interactive: (reuse$|start$|restart: |codex ended the turn with no "
                                r"final message |\w+ exited during start-up, starting again )")

    @classmethod
    def _log_driver(cls, errlog: Path) -> None:
        """The driver's report lines go to the runner log; nothing else of its stderr, which may carry
        harness output or the screen."""
        try:
            for line in errlog.read_text(errors="replace").splitlines():
                if cls._DRIVER_REPORT.match(line):
                    log.info("chat turn %s", line[:300])
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
        state = {"interrupted": False}

        def interrupt() -> None:
            state["interrupted"] = True
            os.killpg(proc.pid, signal.SIGTERM)           # the driver presses Escape; the window stays
            deadline = time.monotonic() + self.term_wait
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(self.poll)
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)

        def follow() -> Iterator[dict]:
            """The driver's events as it writes them (it streams reply blocks during the turn)."""
            rest = b""
            with open(log, "rb") as fh:
                while True:
                    running = proc.poll() is None
                    if running and not state["interrupted"] and cancelled():
                        interrupt()
                        running = False
                    chunk = fh.read()
                    if chunk:
                        *lines, rest = (rest + chunk).split(b"\n")
                        for raw in lines:
                            try:
                                yield json.loads(raw)
                            except ValueError:
                                continue
                    elif not running:
                        break
                    else:
                        time.sleep(self.poll)
            if rest.strip():
                try:
                    yield json.loads(rest)
                except ValueError:
                    pass

        def finish() -> None:
            # the turn is reported over only once the driver has exited: until then it still owns
            # the window (its meta, busy marker), and the next turn of the chat must not take it
            while proc.poll() is None:
                if not state["interrupted"] and cancelled():
                    interrupt()
                time.sleep(self.poll)
            proc.wait()
            self._log_driver(errlog)

        events = follow()
        try:
            for ev in translate(events, harness):
                if ev["type"] in ("final", "error"):
                    if state["interrupted"]:
                        break
                    finish()
                    if ev["type"] == "error":        # Mesh keeps only the code: the reason stays in this log
                        # only the start-up line is quoted: other driver errors can carry the screen
                        msg = str(ev.get("message") or "")
                        logging.getLogger(__name__).warning(
                            "chat turn failed: %s: %s", ev.get("code"),
                            msg[:300] if re.match(r"\w+ exited during start-up \d+ times: ", msg) else "(text withheld: may carry the screen)")
                    yield ev
                    return
                yield ev
        finally:
            events.close()
        finish()
        yield {"type": "final", "stop_reason": "cancelled"}
