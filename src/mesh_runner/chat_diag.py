"""Durable, secret-free diagnosis of a chat turn that failed before the native process started.

The turn's terminal event keeps only an error code, and the driver's own logs are removed with the turn,
so the cause of a ``delivery_failed`` used to be lost. ``record`` writes one JSON line per failure to
the runner log and to ``diagnostics.jsonl`` beside the driver log directory (which the turn cleanup never
touches). A line holds only: time, turn id prefix, stage, exception class name, a ``reason`` from a fixed
allowlist, and (for our own package) source basename / function / line. Never the exception text, the
traceback, locals, environment, argv, prompt, credentials, HTTP bodies or a full path.
"""
from __future__ import annotations

import json
import logging
import os
import time
import traceback
from pathlib import Path

log = logging.getLogger("mesh_runner")

STAGES = frozenset({"plan", "prepare", "spawn", "produce"})
# the only helper reasons a line may carry
REASONS = frozenset({
    "env_command_failed", "env_command_timeout", "env_file_unreadable",
    "barrier_refused", "chat_refused", "workspace_error", "os_error", "unclassified",
})
# class name -> reason; names, not imports, so this module stays dependency-free
_BY_CLASS = {
    "TimeoutExpired": "env_command_timeout",
    "ChatRefused": "chat_refused",
    "GuardRefused": "barrier_refused", "MaintenanceHeld": "barrier_refused",
    "AdmissionClosed": "barrier_refused", "AdmissionTimeout": "barrier_refused",
    "AdmissionCancelled": "barrier_refused", "AdmissionError": "barrier_refused",
    "WorkspaceError": "workspace_error", "BranchInUse": "workspace_error", "InventoryError": "workspace_error",
}
_MAX_BYTES = 64 * 1024
_KEEP_LINES = 200
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))


class EnvDenied(RuntimeError):
    """The lane's environment could not be read (env file / env command). ``diag_reason`` is a fixed token."""

    def __init__(self, message: str, diag_reason: str):
        super().__init__(message)
        self.diag_reason = diag_reason


def reason_of(exc: BaseException) -> str:
    r = getattr(exc, "diag_reason", "")
    if r in REASONS:
        return r
    for cls in type(exc).__mro__:
        if cls.__name__ in _BY_CLASS:
            return _BY_CLASS[cls.__name__]
    return "os_error" if isinstance(exc, OSError) else "unclassified"


def where(exc: BaseException) -> dict:
    """basename / function / line of the innermost frame in this package; nothing when there is none."""
    hit = None
    for fs in traceback.extract_tb(exc.__traceback__):
        if os.path.dirname(os.path.abspath(fs.filename)) == _PKG_DIR:
            hit = fs
    if hit is None:
        return {}
    return {"file": os.path.basename(hit.filename), "func": hit.name[:60], "line": hit.lineno}


def record(stage: str, exc: BaseException, turn_id: str, directory: Path | None) -> dict:
    """Write the diagnosis once per exception; returns the entry (also when writing failed)."""
    if getattr(exc, "_mesh_diag_done", False):
        return {}
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "turn": str(turn_id or "")[:8],
             "stage": stage if stage in STAGES else "produce", "exc": type(exc).__name__[:80],
             "reason": reason_of(exc), **where(exc)}
    try:
        exc._mesh_diag_done = True
    except Exception:
        pass
    line = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    log.warning("chat turn diagnosis: %s", line)
    if directory is not None:
        try:
            _append(Path(directory) / "diagnostics.jsonl", line)
        except OSError:
            pass
    return entry


def _append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as fh:
        fh.write(line + "\n")
    if path.stat().st_size > _MAX_BYTES:
        keep = path.read_text(errors="replace").splitlines()[-_KEEP_LINES:]
        tmp = path.with_suffix(".tmp")
        tmp.write_text("\n".join(keep) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
