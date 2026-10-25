"""The chat session token file: written by the controller (never the model), 0600, atomic rename,
renewed at ttl-60s while the turn streams, removed when the turn ends. The token value is
never logged and never leaves this file."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from .chat_protocol import ChatClient, ChatError

RENEW_MARGIN = 60.0


def write_token(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(token)
    os.replace(tmp, path)


class TokenKeeper:
    def __init__(self, client: ChatClient, turn: dict, path: Path, epoch: int, ttl: float = 300.0,
                 wait=None):
        self.client, self.turn, self.path, self.epoch, self.ttl = client, turn, Path(path), epoch, ttl
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.error = ""
        self._lock = threading.Lock()                # write vs. final removal: a late renewal never revives the file
        self._wait = wait or self.stop_event.wait

    def issue(self) -> float:
        res = self.client.session_token(self.turn["turn_id"], self.turn["claim_token"], self.epoch)
        with self._lock:
            if self.stop_event.is_set():             # stop() returned while this response was in flight
                return RENEW_MARGIN
            write_token(self.path, res["token"])
        self.epoch = int(res.get("execution_epoch") or self.epoch)
        return max(5.0, self.ttl_of(res) - RENEW_MARGIN)

    def ttl_of(self, res: dict) -> float:
        return float(res.get("ttl_seconds") or self.ttl)

    def start(self) -> None:
        try:
            delay = self.issue()
        except ChatError as e:
            self.error = e.error_code            # the turn runs without MCP rights; Mesh refused a token
            return
        except (KeyError, OSError, ValueError) as e:
            self.error = type(e).__name__
            self.remove()
            return

        def loop(delay=delay):
            while not self._wait(delay):
                try:
                    delay = self.issue()
                except ChatError as e:
                    self.error = e.error_code
                    self.remove()                # a refused renewal ends the rights, at once
                    return
                except Exception as e:           # any other failure also ends the rights: no stale file
                    self.error = type(e).__name__
                    self.remove()
                    return
        self.thread = threading.Thread(target=loop, daemon=True, name="chat-token")
        self.thread.start()

    def remove(self) -> None:
        with self._lock:
            self._unlink()

    def _unlink(self) -> None:
        for p in (self.path, self.path.with_name(self.path.name + ".tmp")):
            p.unlink(missing_ok=True)

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
        self.remove()                                # a renewal still in flight sees stop_event under the lock
