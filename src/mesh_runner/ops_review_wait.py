"""Read-only bridge from Board's durable ops-review intent to its source owner's WAIT.

This observes eligibility, never creates a reviewer, changes pins or admits a turn.
The private registry and the exact Mesh POST receipt are both required. No timer,
ordinary mention, quota timestamp or queue-request can substitute for that receipt.
"""
from __future__ import annotations

import hashlib
import json
import datetime as dt
import math
import os
import re
import stat
from pathlib import Path

HEX = re.compile(r"[0-9a-f]{64}")
MAX_BYTES = 4 * 1024 * 1024
WAIT_GUIDANCE = (
    "ops-review WAIT requires `⏳ WAIT ops-review:<64hex request-id>` on the registered "
    "source task, by its registered owner, with unchanged pins/context and an enabled "
    "private registry observer. It wakes once on Board's persisted exact ready receipt; "
    "it does not dispatch, bypass admission, or wait on an account reset."
)


def digest(value: object) -> str:
    # Same binding algorithm as Board's ops_review_pending.digest (including ensure_ascii).
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def delivered_key(identity: str) -> str:
    return "ops_review_delivered:" + identity


def source_context(task: dict) -> str:
    return digest({k: task.get(k) for k in ("id", "project_id", "assignee_id", "description", "labels")})


def read(path: Path, identity: str) -> dict:
    """Bounded, nonblocking, no-symlink read of the same-user private producer file.

    Board atomically replaces this file; a reader sees one complete generation.
    Never writes/migrates the registry or opens package/receipt paths from its data.
    """
    if not isinstance(identity, str) or not HEX.fullmatch(identity):
        raise ValueError("invalid request id")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > MAX_BYTES):
            raise ValueError("registry must be private, owned, regular and bounded")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("registry exceeds size bound")
        state = json.loads(raw)
    finally:
        os.close(fd)
    if (not isinstance(state, dict) or state.get("version") != 1
            or not isinstance(state.get("requests"), dict) or len(state["requests"]) > 128):
        raise ValueError("unsupported registry envelope")
    row = state["requests"].get(identity)
    if not isinstance(row, dict) or not isinstance(row.get("binding"), dict):
        raise ValueError("request not registered")
    if digest(row["binding"]) != identity:
        raise ValueError("request binding digest changed")
    return row


def registration(path: Path, identity: str, task: dict, owner_id: str) -> dict:
    row = read(path, identity)
    binding = row["binding"]
    contract = binding.get("contract")
    if not isinstance(contract, dict):
        raise ValueError("missing request contract")
    if contract.get("source_task_id") != task.get("id"):
        raise ValueError("request belongs to another source task")
    if contract.get("project_id") != task.get("project_id"):
        raise ValueError("request belongs to another project")
    if not owner_id or binding.get("owner_id") != owner_id or task.get("assignee_id") != owner_id:
        raise ValueError("request belongs to another owner")
    if binding.get("source_context") != source_context(task):
        raise ValueError("source context changed; reconcile existing request")
    if (task.get("human_gate") is not False or task.get("completion_signal")
            or task.get("checked_out_by") not in (None, "", owner_id)
            or task.get("checked_out_by_id") not in (None, "", owner_id)):
        raise ValueError("source hold/completion/foreign lease")
    if row.get("state") not in ("pending", "notifying", "notified"):
        raise ValueError("request no longer active; reconcile existing request")
    if any(not isinstance(contract.get(k), str) or not HEX.fullmatch(contract[k])
           for k in ("package_sha256", "manifest_sha256", "criteria_sha256")):
        raise ValueError("missing contract pins")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(binding.get("head_sha", ""))):
        raise ValueError("missing HEAD pin")
    return row


def receipt(identity: str, row: dict, comments: list[dict], notifier_id: str) -> dict | None:
    """Only a completed producer POST/reconciliation can yield an event.

    The intent hash was durably fenced before POST by Board. Require the exact
    comment ID + author + body hash, so explanations containing a marker cannot
    be mistaken for a receipt. Legacy notified rows without that hash stay held.
    """
    if (row.get("state") != "notified" or not notifier_id
            or row.get("notification_version", 1) not in (1, 2)):
        return None
    sha, comment_id = row.get("notification_sha256"), row.get("comment_id")
    publication = row.get("publication")
    if (not isinstance(sha, str) or not HEX.fullmatch(sha) or not comment_id
            or not isinstance(publication, dict) or not publication.get("tick_at")
            or not publication.get("controller_ref") or not publication.get("digest")
            or not row.get("reviewer") or not isinstance(row.get("registered_at"), (int, float))):
        return None
    try:
        tick = dt.datetime.fromisoformat(publication["tick_at"].replace("Z", "+00:00"))
        if (tick.tzinfo is None or not math.isfinite(row["registered_at"])
                or tick.timestamp() <= row["registered_at"]):
            return None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None
    intent = {"request_id": identity, "publication": publication, "reviewer": row["reviewer"],
              "reason": row.get("reason"), "notification_version": row.get("notification_version", 1)}
    if row.get("notification_binding_sha256") != digest(intent):
        return None
    matches = [c for c in comments if c.get("id") == comment_id]
    if len(matches) != 1:
        return None
    comment = matches[0]
    body = comment.get("body")
    if (comment.get("author_id") != notifier_id or not isinstance(body, str)
            or not body.startswith("[ops-review-ready:" + identity + "]\n")
            or hashlib.sha256(body.encode()).hexdigest() != sha):
        return None
    return {"request_id": identity, "comment_id": comment_id, "notification_sha256": sha,
            "publication": publication}


def event_text(evidence: dict) -> str:
    return ("Board ops-review eligibility receipt " + evidence["comment_id"]
            + " for pinned request " + evidence["request_id"] + ". "
            "Recheck HEAD/package/manifest/criteria, holds, lease, route and absence of dispatch receipt. "
            "Then execute one ordinary --deliver as the source owner; normal router/admission/checkout "
            "gates remain mandatory. This event is neither admission nor SHIP.")
