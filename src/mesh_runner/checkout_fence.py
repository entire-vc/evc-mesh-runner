"""The checkout fence of a running turn: one file per task the lane holds.

Mesh accepts an optional precondition on the writes that publish a result (comment, artifact,
finish, move): the lock the caller holds (``X-Mesh-Checkout-Token``, or ``X-Mesh-Checkout-Session-Id``
with ``X-Mesh-Checkout-Generation``) and the ``input_revision`` the caller worked from
(``X-Mesh-Expected-Input-Revision``). With it a write commits only while that exact lock is live;
otherwise Mesh answers 409 and writes nothing (``checkout_precondition_failed``,
``input_revision_changed``).

The Runner holds the lock; the agent writes through its own Mesh MCP process, which the harness starts
and the Runner does not configure. The lock reaches that process through a file:

    <fence_dir>/<agent_id>/<task_id>.json        written by the Runner, read by the MCP on every write
    <fence_dir>/<agent_id>/<task_id>.refusals.jsonl   appended by the MCP when Mesh refused a fenced write

The Runner rewrites the task's file on every checkout and at the start of every turn (a resumed session
reads the new attempt's lock, never an earlier one: the MCP reads the file again on each write, it keeps
no copy) and removes it when the attempt lets the lock go. No file: no fence (a card the turn does not
hold is written as before). The file carries a secret-like token: 0600, written by rename.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DIR = Path.home() / ".mesh-runner" / "checkout-fence"
ENV_DIR = "MESH_CHECKOUT_FENCE_DIR"     # the same override for the Runner and the MCP (tests, odd homes)


def default_dir() -> Path:
    return Path(os.environ[ENV_DIR]).expanduser() if os.environ.get(ENV_DIR) else DEFAULT_DIR
VERSION = 1

TOKEN_HEADER = "X-Mesh-Checkout-Token"
SESSION_HEADER = "X-Mesh-Checkout-Session-Id"
GENERATION_HEADER = "X-Mesh-Checkout-Generation"
REVISION_HEADER = "X-Mesh-Expected-Input-Revision"

LOCK_LOST = "checkout_precondition_failed"
REVISION_CHANGED = "input_revision_changed"

def _time(value: str) -> dt.datetime | None:
    try:
        t = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def headers(checkout: dict | None, input_revision: int | None = None) -> dict[str, str]:
    """The fence headers of a held lock: the token when there is one, else session + generation."""
    co = checkout or {}
    out: dict[str, str] = {}
    if co.get("token"):
        out[TOKEN_HEADER] = str(co["token"])
    elif co.get("session_id") and co.get("generation") is not None:
        out[SESSION_HEADER] = str(co["session_id"])
        out[GENERATION_HEADER] = str(int(co["generation"]))
    if out and input_revision is not None:
        out[REVISION_HEADER] = str(int(input_revision))
    return out


def refusal_code(body) -> str:
    """The stable error code of a Mesh 409 body, or ""."""
    if not isinstance(body, dict):
        return ""
    for key in ("error_code", "code", "error"):
        value = body.get(key)
        if value in (LOCK_LOST, REVISION_CHANGED):
            return value
    return ""


@dataclass(frozen=True)
class Refusal:
    attempt_id: str
    code: str
    at: str
    tool: str
    reassigned: bool
    input_revision: int | None
    turn: int | None


class FenceDir:
    """The agent's fence directory. Every method is a no-op returning the neutral answer when the
    directory is disabled (``root is None``): that is the rollback (``[runner] checkout_fence = false``)."""

    def __init__(self, root: Path | None, agent_id: str):
        self.root = (Path(root) / agent_id) if root is not None and agent_id else None

    @property
    def enabled(self) -> bool:
        return self.root is not None

    def path(self, task_id: str) -> Path:
        if not _UUID.match(task_id or ""):
            raise ValueError("fence file needs a full task UUID")
        assert self.root is not None
        return self.root / f"{task_id}.json"

    def refusals_path(self, task_id: str) -> Path:
        return self.path(task_id).with_suffix(".refusals.jsonl")

    def publish(self, task_id: str, attempt_id: str, checkout: dict, input_revision: int | None,
                turn: int | None = None) -> bool:
        """Write the task's current lock for the MCP. False when there is nothing to publish. The MCP copies
        ``attempt_id`` and ``turn`` into every refusal it journals, so the Runner reads only its own turn's."""
        if not self.enabled or not headers(checkout):
            return False
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = {"version": VERSION, "task_id": task_id, "attempt_id": attempt_id,
                "checkout_token": str(checkout.get("token") or ""),
                "session_id": str(checkout.get("session_id") or ""),
                "generation": checkout.get("generation"),
                "input_revision": input_revision, "turn": turn}
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".fence-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                os.fchmod(fh.fileno(), 0o600)
                json.dump(data, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path(task_id))
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return True

    def read(self, task_id: str) -> dict | None:
        if not self.enabled:
            return None
        try:
            data = json.loads(self.path(task_id).read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def withdraw(self, task_id: str, attempt_id: str) -> None:
        """Remove the task's file if it is this attempt's (a later attempt's file stays)."""
        if not self.enabled:
            return
        data = self.read(task_id)
        if data is not None and data.get("attempt_id") != attempt_id:
            return
        self.path(task_id).unlink(missing_ok=True)

    def refusals(self, task_id: str, attempt_id: str, since: str = "", turn: int | None = None) -> list[Refusal]:
        """The MCP's refusals of this attempt's fenced writes, oldest first: of ``turn`` when given (a row
        without a turn then counts only by ``since``), at or after ``since``."""
        if not self.enabled:
            return []
        try:
            lines = self.refusals_path(task_id).read_text().splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or row.get("attempt_id") != attempt_id:
                continue
            if row.get("code") not in (LOCK_LOST, REVISION_CHANGED):
                continue
            at = str(row.get("at") or "")
            row_turn = row.get("turn") if type(row.get("turn")) is int else None
            if turn is not None and row_turn is not None and row_turn != turn:
                continue
            when, start = _time(at), _time(since) if since else None
            if row_turn is None and start is not None and (when is None or when < start):
                continue
            rev = row.get("input_revision")
            out.append(Refusal(attempt_id, row["code"], at, str(row.get("tool") or ""),
                               bool(row.get("reassigned")), int(rev) if type(rev) is int else None, row_turn))
        return out

    def sweep(self, active_attempts: dict[str, str]) -> list[str]:
        """Remove every file whose attempt is not the task's active attempt (``{task_id: attempt_id}``),
        and refusal journals of tasks without a file. Returns the task ids removed."""
        if not self.enabled or not self.root.is_dir():
            return []
        removed = []
        for p in self.root.glob("*.json"):
            task_id = p.stem
            try:
                data = json.loads(p.read_text())
            except (OSError, ValueError):
                data = None
            if not isinstance(data, dict) or active_attempts.get(task_id) != data.get("attempt_id"):
                p.unlink(missing_ok=True)
                removed.append(task_id)
        for p in self.root.glob("*.refusals.jsonl"):
            task_id = p.name.split(".")[0]
            if task_id not in active_attempts:
                p.unlink(missing_ok=True)
        return removed
