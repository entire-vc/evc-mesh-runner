"""Opt-in stall WAIT: one durable observer of the FIRST new mesh-stall-control result on one card.

``⏳ WAIT stall:#<this-card-8hex>`` sleeps until the reviewed mesh-stall-control notifier posts a
NEW result comment on this very card. Nothing else wakes it: not a timer, not the generic
own-``↻ NEXT`` / foreign-comment path (a stall wait is dispatched to its own predicate first),
not Telegram receipts. State lives in Runner's own store (marks of the source card):

* registration (``stall_reg``): reg id, source card, owner (Mesh agent id), UTC ``installed_at``
  floor and the event UUIDs that already existed at arm time (``baseline``). Written by ``arm``
  before it acknowledges, so an event that lands between arming and the declaration is kept.
* evidence (``stall_pending``): the first comment on the source card that is
  - authored by the registered owner (own author id),
  - created at or after the floor,
  - shaped exactly like the notifier output: first line ``[mesh-stall-control:<uuid4>]``, one
    fenced json block holding an object (the bounded diagnostic), last line ``↻ NEXT``,
  - carrying an event UUID not seen before (baseline, earlier events, replays are ignored).
* delivery (Runner, ``_wake_stall``) commits ready + wake text + ``stall_delivered`` in one
  transaction; the delivered mark makes the registration one-shot across retry/restart.

Evidence only: it says a result was posted, never that the stall is repaired. Missing auth, a
Mesh read error or a registration that does not parse fail closed (no wake).
"""
from __future__ import annotations

import datetime as dt
import json
import re
import uuid

REG_KEY = "stall_reg"
PENDING_KEY = "stall_pending"
DELIVERED_KEY = "stall_delivered"
SEEN_PREFIX = "stall_seen:"
MAX_BASELINE = 4096
_FIRST = re.compile(r"\[mesh-stall-control:(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\]")
_FENCE = re.compile(r"```json\n(?P<body>.*?)\n```", re.S)
_LAST = re.compile(r"↻\s*NEXT")

WAIT_GUIDANCE = (
    "invalid stall wait: `wait_for on=registered registration=stall` is accepted only when the stall observer "
    "is enabled on this runner and an operator armed a live registration for this card and this "
    "owner (`mesh-runner stall-wait arm`). It wakes once on the first NEW own-author "
    "[mesh-stall-control:<uuid>] result comment on this card. Nothing is armed here: wait on "
    "another condition (card, mr, pipeline, ask_agent) or ask the operator to arm it."
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
        raise ValueError("corrupt stall state") from None
    if not isinstance(value, dict):
        raise ValueError("corrupt stall state")
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
                or not _owner(reg["owner_id"]) or reg["state"] not in ("armed", "disarmed")
                or type(reg["installed_at"]) is not int or type(reg["floor"]) is not float
                or not isinstance(reg["baseline"], list) or len(reg["baseline"]) > MAX_BASELINE
                or any(_uuid(x) is None for x in reg["baseline"])):
            raise ValueError("unknown stall registration")
    except (KeyError, TypeError):
        raise ValueError("unknown stall registration") from None
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


def _when(value) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t.timestamp() if t.tzinfo else None


def event_of(comment, owner_id: str) -> tuple[str, float, dict] | None:
    """(event uuid, created epoch, diagnostic) of a well-formed own-author notifier result, else None."""
    if not isinstance(comment, dict) or comment.get("author_id") != owner_id:
        return None
    body = comment.get("body")
    created = _when(comment.get("created_at"))
    if not isinstance(body, str) or created is None:
        return None
    lines = body.strip().split("\n")
    m = _FIRST.fullmatch(lines[0].strip())
    if not m or not _LAST.fullmatch(lines[-1].strip()) or _uuid(m["id"]) is None:
        return None
    blocks = _FENCE.findall(body)
    if len(blocks) != 1:
        return None
    try:
        diag = json.loads(blocks[0])
    except ValueError:
        return None
    if not isinstance(diag, dict):
        return None
    return m["id"], created, diag


def baseline_ids(comments, owner_id: str) -> list[str]:
    """Event UUIDs already on the card (any age): they are history, never a wake."""
    ids = []
    for c in comments or []:
        ev = event_of(c, owner_id)
        if ev and ev[0] not in ids:
            ids.append(ev[0])
    if len(ids) > MAX_BASELINE:
        # never drop history: a dropped id could be replayed after arm
        raise ValueError(f"card has more than {MAX_BASELINE} notifier events: refusing to arm")
    return ids


def arm(store, agent: str, task_id: str, owner_id: str, comments, now: float) -> dict:
    """Register (idempotently) and return the registration. ``comments`` is the card's CURRENT full
    comment history: its notifier events become the baseline. A repeat with the same owner returns
    the stored registration; re-arming a disarmed one keeps its baseline and dedup history; a
    delivered one starts a new generation."""
    if _uuid(task_id) is None or not _owner(owner_id):
        raise ValueError("task must be a full canonical UUID and owner a Mesh agent id")
    old = registration(store, agent, task_id)
    if old and not delivered(store, agent, task_id, old["reg_id"]):
        if old["owner_id"] != owner_id:
            raise ValueError("a live registration for this card has another owner: disarm it first")
        if old["state"] == "disarmed":
            old["state"] = "armed"
            _put(store, agent, task_id, REG_KEY, old)
        return old
    reg = {"version": 1, "reg_id": str(uuid.uuid4()), "source_task_id": task_id, "owner_id": owner_id,
           "state": "armed", "installed_at": int(now), "floor": float(now), "baseline": baseline_ids(comments, owner_id)}
    _put(store, agent, task_id, REG_KEY, reg)       # acknowledged only after this write
    return reg


def disarm(store, agent: str, task_id: str) -> dict | None:
    reg = registration(store, agent, task_id)
    if reg and reg["state"] == "armed":
        reg["state"] = "disarmed"
        _put(store, agent, task_id, REG_KEY, reg)
    return reg


def inspect(store, agent: str, task_id: str) -> dict:
    """Metadata only: registration, baseline, whether evidence is pending or delivered."""
    try:
        reg = registration(store, agent, task_id)
    except ValueError:
        return {"registered": False, "error": "corrupt registration (fail closed)"}
    if reg is None:
        return {"registered": False}
    return {"registered": True, "reg_id": reg["reg_id"], "state": reg["state"], "owner_id": reg["owner_id"],
            "installed_at": reg["installed_at"], "floor": reg["floor"], "baseline": reg["baseline"],
            "pending": pending(store, agent, task_id, reg["reg_id"]),
            "delivered": delivered(store, agent, task_id, reg["reg_id"])}


def scan(store, agent: str, task_id: str, mesh) -> dict | None:
    """Read the card's comments and keep the first new valid notifier result pending; return the
    pending evidence, if any. Mesh errors propagate (the caller logs and retries next tick) and
    nothing is committed on a failed read."""
    try:
        reg = registration(store, agent, task_id)
    except ValueError:
        return None
    if reg is None or reg["state"] != "armed" or delivered(store, agent, task_id, reg["reg_id"]):
        return None
    reg_id = reg["reg_id"]
    keep = pending(store, agent, task_id, reg_id)
    if keep is not None:
        return keep
    found = None
    for c in mesh.comments_all(task_id):
        ev = event_of(c, reg["owner_id"])
        if ev is None or ev[1] < reg["floor"] or ev[0] in reg["baseline"]:
            continue
        if store.mark(agent, task_id, SEEN_PREFIX + reg_id[:8] + ":" + ev[0]):
            continue                        # a replayed UUID, under any comment id
        found = (c, ev)
        break
    if found is None:
        return None
    c, (event_id, _, diag) = found
    evidence = {"reg_id": reg_id, "event_id": event_id, "comment_id": str(c.get("id") or ""),
                "diagnostic_keys": sorted(diag)[:32]}
    owned = not store.db.in_transaction
    if owned:
        store.db.execute("BEGIN IMMEDIATE")
    try:
        now_reg = registration(store, agent, task_id)
        if now_reg != reg or delivered(store, agent, task_id, reg_id):
            if owned and store.db.in_transaction:
                store.db.execute("ROLLBACK")
            return None
        store.mark(agent, task_id, SEEN_PREFIX + reg_id[:8] + ":" + event_id, "1")
        _put(store, agent, task_id, PENDING_KEY, evidence)
    except BaseException:
        if owned and store.db.in_transaction:
            store.db.execute("ROLLBACK")
        raise
    if owned and store.db.in_transaction:
        store.db.execute("COMMIT")
    return evidence


def event_text(evidence: dict) -> str:
    return ("a new mesh-stall-control result was posted on this card by its owner "
            f"(event {evidence['event_id']}, comment {evidence['comment_id']}): the stall wait is complete. "
            "This is evidence that the bounded control ran, not proof of a repair")
