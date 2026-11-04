"""Opt-in recovery WAIT: one durable observer of a FUTURE shared recovery card.

``⏳ WAIT recovery:#<this-card-8hex>`` sleeps until the shared recovery card that a producer is
allowed to create (title/description carries the registered marker, assigned to the registered
recovery owner) has been created, bound, finished as ``done`` AND carries an explicit receipt
comment by the registered receipt author. Nothing about the future card is guessed: no UUID, no
timer, no relay card. State lives in Runner's own store (marks of the source card):

* registration (``recovery_reg``): reg id, source card, owner, project, marker, recovery owner,
  receipt author, UTC ``installed_at``. Written by ``arm`` before it acknowledges. It may be armed
  before OR after the recovery card exists: ``scan`` reconciles the actual Mesh cards every tick,
  so the gap between create and registration (and the reverse) loses nothing.
* binding (``recovery_bound``): the first actual matching card, written once per registration
  generation. A cancelled bound card is rejected (kept in ``rejected``) and the next real
  candidate may bind; unrelated cards (no marker, other project, other assignee) never bind.
* completion (``recovery_pending``): bound card is ``done`` and a receipt comment by the receipt
  author names ``carrier-receipt: <ref>``. Absent receipt or non-done status gives nothing.
* delivery (Runner, ``_wake_recovery``) commits ready + wake text + ``recovery_delivered`` in one
  transaction; the delivered mark makes the registration one-shot across retry/restart.

This delivers EVIDENCE only. It is never a security clearance: the root owner still verifies the
basis, old-auth negative / new-own-auth positive, carriers and runtime. Secrets are never read;
only ids, a short receipt reference and statuses are kept.
"""
from __future__ import annotations

import json
import re
import uuid

REG_KEY = "recovery_reg"
BOUND_KEY = "recovery_bound"
PENDING_KEY = "recovery_pending"
DELIVERED_KEY = "recovery_delivered"
_RECEIPT_LINE = re.compile(r"(?m)^[ \t]*carrier-receipt:[ \t]*(?P<ref>[\w][\w.:/#@+-]{3,127})[ \t]*$")
_MARKER = re.compile(r"[^\n\r]{3,80}")

WAIT_GUIDANCE = (
    "invalid recovery wait: `⏳ WAIT recovery:#<this-card-8hex>` is accepted only when the recovery "
    "observer is enabled on this runner and an operator armed a live registration for this card and "
    "this owner (`mesh-runner recovery-wait arm`). It wakes once when the shared recovery card is "
    "done and carries an explicit receipt comment. Nothing is armed here: declare another condition "
    "(card, mr, pipeline, decision) or ask the operator to arm it."
)


def _uuid(value) -> str | None:
    try:
        text = str(uuid.UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None
    return text if text == value else None


def _owner(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[\w-]{1,64}", value) is not None


def _load(store, agent: str, task_id: str, key: str):
    row = store.mark(agent, task_id, key)
    if not row or not row[0]:
        return None
    try:
        value = json.loads(row[0])
    except ValueError:
        raise ValueError("corrupt recovery state") from None
    if not isinstance(value, dict):
        raise ValueError("corrupt recovery state")
    return value


def _put(store, agent: str, task_id: str, key: str, value: dict) -> None:
    store.mark(agent, task_id, key, json.dumps(value, sort_keys=True))


def registration(store, agent: str, task_id: str) -> dict | None:
    """The stored registration; ValueError when it does not parse (fail closed, not reset)."""
    reg = _load(store, agent, task_id, REG_KEY)
    if reg is None:
        return None
    try:
        if (reg["version"] != 1 or _uuid(reg["reg_id"]) is None or reg["source_task_id"] != task_id
                or not _owner(reg["owner_id"])
                or any(_uuid(reg[k]) is None for k in ("project_id", "recovery_owner_id", "receipt_author_id"))
                or not isinstance(reg["marker"], str) or not _MARKER.fullmatch(reg["marker"])
                or reg["state"] not in ("armed", "disarmed") or type(reg["installed_at"]) is not int):
            raise ValueError("unknown recovery registration")
    except (KeyError, TypeError):
        raise ValueError("unknown recovery registration") from None
    return reg


def delivered(store, agent: str, task_id: str, reg_id: str) -> bool:
    row = store.mark(agent, task_id, DELIVERED_KEY)
    return bool(row and row[0] == reg_id)


def pending(store, agent: str, task_id: str, reg_id: str) -> dict | None:
    try:
        value = _load(store, agent, task_id, PENDING_KEY)
    except ValueError:
        return None
    return value if value and value.get("reg_id") == reg_id else None


def bound(store, agent: str, task_id: str, reg_id: str) -> dict | None:
    try:
        value = _load(store, agent, task_id, BOUND_KEY)
    except ValueError:
        return None
    return value if value and value.get("reg_id") == reg_id else None


def arm(store, agent: str, task_id: str, owner_id: str, project_id: str, marker: str,
        recovery_owner_id: str, receipt_author_id: str, now: float) -> dict:
    """Register (idempotently) and return the registration. A repeat with the same contract returns
    the stored one; re-arming a disarmed registration keeps binding/pending; a delivered one starts a
    new generation; a live one with another contract must be disarmed first."""
    if _uuid(task_id) is None:
        raise ValueError("task must be a full canonical UUID")
    contract = {"owner_id": owner_id, "project_id": project_id, "marker": marker,
                "recovery_owner_id": recovery_owner_id, "receipt_author_id": receipt_author_id}
    if not _owner(owner_id) or any(_uuid(contract[k]) is None for k in ("project_id", "recovery_owner_id", "receipt_author_id")) \
            or not _MARKER.fullmatch(marker):
        raise ValueError("owner must be a Mesh agent id; project/recovery-owner/receipt-author UUIDs; marker 3..80 chars, one line")
    old = registration(store, agent, task_id)
    if old and not delivered(store, agent, task_id, old["reg_id"]):
        if any(old[k] != v for k, v in contract.items()):
            raise ValueError("a live registration for this card has another contract: disarm it first")
        if old["state"] == "disarmed":
            old["state"] = "armed"
            _put(store, agent, task_id, REG_KEY, old)
        return old
    reg = {"version": 1, "reg_id": str(uuid.uuid4()), "source_task_id": task_id, "state": "armed",
           "installed_at": int(now), **contract}
    _put(store, agent, task_id, REG_KEY, reg)       # acknowledged only after this write
    return reg


def disarm(store, agent: str, task_id: str) -> dict | None:
    reg = registration(store, agent, task_id)
    if reg and reg["state"] == "armed":
        reg["state"] = "disarmed"
        _put(store, agent, task_id, REG_KEY, reg)
    return reg


def inspect(store, agent: str, task_id: str) -> dict:
    """Metadata only."""
    try:
        reg = registration(store, agent, task_id)
    except ValueError:
        return {"registered": False, "error": "corrupt registration (fail closed)"}
    if reg is None:
        return {"registered": False}
    b = bound(store, agent, task_id, reg["reg_id"])
    return {"registered": True, **{k: reg[k] for k in ("reg_id", "state", "owner_id", "project_id", "marker",
            "recovery_owner_id", "receipt_author_id", "installed_at")},
            "bound": b, "pending": pending(store, agent, task_id, reg["reg_id"]),
            "delivered": delivered(store, agent, task_id, reg["reg_id"])}


def _candidate(task: dict, reg: dict) -> bool:
    """An actual shared recovery card: right project, marker, and the registered recovery owner."""
    text = (task.get("title") or "") + "\n" + (task.get("description") or "")
    return (_uuid(task.get("id")) is not None and task["id"] != reg["source_task_id"]
            and task.get("project_id") == reg["project_id"] and reg["marker"] in text
            and task.get("assignee_id") == reg["recovery_owner_id"])


def receipt_ref(comments, reg: dict) -> tuple[str, str] | None:
    """(ref, comment id) of the first explicit receipt comment by the registered author."""
    for c in comments or []:
        if not isinstance(c, dict) or c.get("author_id") != reg["receipt_author_id"]:
            continue
        m = _RECEIPT_LINE.search(c.get("body") or "")
        if m:
            return m["ref"], str(c.get("id") or "")
    return None


def scan(store, agent: str, task_id: str, mesh, category) -> dict | None:
    """Reconcile the actual Mesh cards with the registration; return the pending evidence, if any.
    ``category(task)`` maps a card to its status category. Mesh errors propagate to the caller
    (it logs and retries next tick); nothing is committed on a failed read."""
    try:
        reg = registration(store, agent, task_id)
    except ValueError:
        return None
    if reg is None or reg["state"] != "armed" or delivered(store, agent, task_id, reg["reg_id"]):
        return None
    reg_id = reg["reg_id"]
    keep = pending(store, agent, task_id, reg_id)
    ev = _evaluate(store, agent, task_id, reg, mesh, category)
    if ev is None and keep and registration(store, agent, task_id) == reg:
        # evidence that no longer holds (cancelled, reassigned, receipt gone) is withdrawn, never replayed
        store.mark(agent, task_id, PENDING_KEY, "")
    return ev


def _evaluate(store, agent, task_id, reg, mesh, category) -> dict | None:
    """Re-read the actual cards every time, also when evidence is already pending."""
    reg_id = reg["reg_id"]
    cur = bound(store, agent, task_id, reg_id)
    rejected = list((cur or {}).get("rejected") or [])
    target = None
    if cur and cur.get("task_id"):
        target = mesh.task(cur["task_id"])
        if category(target) == "cancelled" or not _candidate(target, reg):
            # cancelled, or edited/reassigned so it no longer matches the registered contract
            rejected.append(cur["task_id"])
            target = None
    if target is None:
        found = [t for t in mesh.find_tasks_all(reg["project_id"], reg["marker"])
                 if _candidate(t, reg) and t["id"] not in rejected and category(t) != "cancelled"]
        found.sort(key=lambda t: (t.get("created_at") or "", t["id"]))
        target = found[0] if found else None
        if target is None:
            if cur and cur.get("task_id"):
                _put_bound(store, agent, task_id, reg_id, None, rejected, reg)
            return None
    new = {"reg_id": reg_id, "task_id": target["id"], "rejected": rejected}
    if not cur or cur.get("task_id") != target["id"] or cur.get("rejected") != rejected:
        _put_bound(store, agent, task_id, reg_id, target["id"], rejected, reg)
    if category(target) != "done":
        return None
    got = receipt_ref(mesh.comments_all(target["id"]), reg)
    if got is None:
        return None
    ev = {**new, "receipt_ref": got[0], "receipt_comment_id": got[1], "target_status": "done"}
    # commit only against the exact registration these reads were based on
    owned = not store.db.in_transaction
    if owned:
        store.db.execute("BEGIN IMMEDIATE")
    try:
        now_reg = registration(store, agent, task_id)
        if now_reg != reg or delivered(store, agent, task_id, reg_id):
            if owned and store.db.in_transaction:
                store.db.execute("ROLLBACK")
            return None
        _put(store, agent, task_id, PENDING_KEY, ev)
    except BaseException:
        if owned and store.db.in_transaction:
            store.db.execute("ROLLBACK")
        raise
    if owned and store.db.in_transaction:
        store.db.execute("COMMIT")
    return ev


def _put_bound(store, agent, task_id, reg_id, target_id, rejected, reg) -> None:
    if registration(store, agent, task_id) != reg:
        return
    _put(store, agent, task_id, BOUND_KEY, {"reg_id": reg_id, "task_id": target_id, "rejected": rejected})


def event_text(evidence: dict) -> str:
    return ("the shared recovery card bound to this card's registration is done and carries an explicit "
            f"receipt (recovery card {evidence['task_id']}, receipt {evidence['receipt_ref']}, comment "
            f"{evidence['receipt_comment_id']}): the recovery wait is complete. This is evidence, not a "
            "security clearance: verify the basis and the result yourself")
