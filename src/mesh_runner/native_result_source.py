"""Read native-result frames from an enrolled, trusted Codex rollout file.

Only harness response_item / assistant / output_text is a producer emission;
user/tool/quoted event_msg records cannot emit. This validates source provenance
within the trusted local filesystem, not across an arbitrary upload. The caller
must enroll the original native machine's real session root. A copied file or
an agent's Mesh author name alone is not such enrollment.

No source contents beyond validated frames leave this adapter. It never writes
the source, changes a Runner attempt or publishes Mesh comments. scan() returns
the next byte cursor; the caller persists it ONLY AFTER ledger.publish commits.
If it crashes before saving cursor, replay is an exact duplicate, not loss.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import errno

from .native_result_ledger import Refused, binding, event, uuid

MARKER = "NATIVE_RESULT_V1 "
ACK_MARKER = "NATIVE_ACK_V1 "
MAX_LINE = 16 * 1024 * 1024
MAX_SCAN = 32 * 1024 * 1024


def _open(root, source):
    root, source = Path(root), Path(source)
    # Walk from / with openat + O_NOFOLLOW, retaining directory descriptors.
    # A renamed intermediate directory cannot redirect a subsequent open.
    if not root.is_absolute() or not source.is_absolute():
        raise Refused("absolute enrolled root and source required")
    try:
        parts = source.relative_to(root).parts
    except ValueError:
        raise Refused("source outside enrolled root") from None
    if not parts or any(p in (".", "..") for p in parts):
        raise Refused("invalid source path")
    if ".." in root.parts:
        raise Refused("invalid enrolled root")
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in (*root.parts[1:], *parts[:-1]):
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=directory)
            os.close(directory)
            directory = following
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise Refused("source symlink or invalid directory") from None
        raise
    finally:
        os.close(directory)
    stream = os.fdopen(fd, "rb")
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        stream.close()
        raise Refused("owned regular native source required")
    return stream, info


def _metadata(stream, expected_producer):
    raw = stream.readline(MAX_LINE + 1)
    if len(raw) > MAX_LINE or not raw.endswith(b"\n"):
        raise Refused("complete native metadata required")
    try:
        row = json.loads(raw)
    except ValueError:
        raise Refused("invalid native metadata") from None
    if not isinstance(row, dict):
        raise Refused("invalid native metadata")
    payload = row.get("payload") or {}
    if (row.get("type") != "session_meta" or not isinstance(payload, dict)
            or payload.get("id") != uuid(expected_producer)):
        raise Refused("original native producer mismatch")
    return hashlib.sha256(raw).hexdigest(), stream.tell()


def enroll(root, source, expected_binding, *, marker=MARKER, at_end=False):
    """Readback-only source enrollment; does not imply receiver readiness."""
    contract = binding(expected_binding)
    if marker not in (MARKER, ACK_MARKER):
        raise Refused("unsupported native marker")
    stream, info = _open(root, source)
    with stream:
        metadata_sha, offset = _metadata(stream, contract["producer"])
        if at_end:
            # A resume fence excludes every old ACK. Do not skip an incomplete
            # record which could become a new frame after the receiver starts.
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) != b"\n":
                raise Refused("resume source must end at a complete record")
            offset = stream.tell()
    registration = {"root": str(root), "source": str(source), "device": info.st_dev,
            "inode": info.st_ino, "metadata_sha256": metadata_sha,
            "binding": contract, "cursor": offset}
    if marker != MARKER:
        registration["marker"] = marker
    return registration


def _frame(raw, source_binding, marker=MARKER):
    try:
        row = json.loads(raw)
    except ValueError:
        raise Refused("malformed native record") from None
    if not isinstance(row, dict) or row.get("type") != "response_item":
        return None
    payload = row.get("payload") or {}
    if (not isinstance(payload, dict) or payload.get("type") != "message"
            or payload.get("role") != "assistant"):
        return None
    parts = payload.get("content")
    if not isinstance(parts, list):
        return None
    texts = [p.get("text", "") for p in parts
             if isinstance(p, dict) and p.get("type") == "output_text"]
    marked = [t for t in texts if isinstance(t, str) and t.startswith(marker)]
    if not marked:
        return None
    # Whole-message frame only: embedded snippets and prose are not events.
    if len(parts) != 1 or len(marked) != 1:
        raise Refused("ambiguous native frame")
    try:
        value = event(json.loads(marked[0][len(marker):]))
    except (ValueError, TypeError):
        raise Refused("invalid native frame") from None
    if value["binding"] != source_binding:
        raise Refused("native frame binding mismatch")
    return value


def scan(registration):
    """One bounded read. No timers/retries. Last incomplete line stays unread.

    Any bad frame fails the entire batch, including earlier valid frames. The
    caller must retain the old cursor on refusal and deliver UNKNOWN with the
    adapter owner/evidence through the registered failure path, never readiness.
    """
    contract = binding(registration["binding"])
    marker = registration.get("marker", MARKER)
    if marker not in (MARKER, ACK_MARKER):
        raise Refused("unsupported native marker")
    cursor = registration["cursor"]
    if type(cursor) is not int or cursor < 0:
        raise Refused("invalid source cursor")
    stream, info = _open(registration["root"], registration["source"])
    with stream:
        if (info.st_dev, info.st_ino) != (registration["device"], registration["inode"]):
            raise Refused("source replaced")
        metadata_sha, start = _metadata(stream, contract["producer"])
        if metadata_sha != registration["metadata_sha256"]:
            raise Refused("native metadata changed")
        if not start <= cursor <= info.st_size:
            raise Refused("source cursor lost or source truncated")
        stream.seek(cursor)
        result, consumed = [], 0
        while consumed < MAX_SCAN:
            begin = stream.tell()
            raw = stream.readline(MAX_LINE + 1)
            if len(raw) > MAX_LINE:
                raise Refused("native record exceeds bound")
            if not raw or not raw.endswith(b"\n"):
                stream.seek(begin)
                break
            consumed += len(raw)
            value = _frame(raw, contract, marker)
            if value is not None:
                result.append(value)
        return {"cursor": stream.tell(), "frames": result,
                "producer": contract["producer"], "task": contract["task"]}
