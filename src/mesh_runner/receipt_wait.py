"""Opt-in receipt WAIT: one durable, scoped observer of provider-accepted Telegram text receipts.

``⏳ WAIT receipt:#<this-card-8hex>`` sleeps until the FIRST new accepted, correlated text
receipt appears in the journals an operator registered for this card. Everything lives in
Runner's own store (marks of the source task), nothing is written behind its back:

* registration (``receipt_reg``): reg id, source task, owner, UTC ``installed_at`` floor and one
  cursor per journal (device, inode, offset). Written by ``arm`` before it acknowledges.
* scanning advances the cursors and records the first valid receipt as ``receipt_pending``
  whether or not the source is waiting yet, so a receipt arriving between arming and the
  declaration is kept, not lost.
* delivery (Runner, ``_wake_receipt``) commits ready + wake text + ``receipt_delivered`` in
  one transaction; the delivered mark makes the registration one-shot across retry/restart.

Journals are never rewritten. A cursor never passes a partial line. Missing journals are a
registered absent source. Rotation, truncation, an unsafe file or a registration that does not
parse fail closed (no wake, no reset to history) until the operator re-baselines explicitly.
Only allowlisted metadata is kept: never a message body, document name or provider text.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path

REG_KEY = "receipt_reg"
PENDING_KEY = "receipt_pending"
DELIVERED_KEY = "receipt_delivered"
SEEN_PREFIX = "receipt_seen:"
JOURNAL_NAME = "telegram-receipts.jsonl"
PRODUCERS = ("owner-gate-alert", "owner-digest")
MAX_READ = 256 * 1024          # bytes per journal per scan
MAX_LINE = 16 * 1024
MAX_JOURNALS = 8
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_BOT = re.compile(r"[\w.:-]{1,64}")
_CHAT = re.compile(r"-?[0-9]{1,20}")

WAIT_GUIDANCE = (
    "invalid receipt wait: `⏳ WAIT receipt:#<this-card-8hex>` is accepted only when the receipt "
    "observer is enabled on this runner and an operator armed a live registration for this card "
    "and this owner (`mesh-runner receipt-wait arm`). It wakes once on the first new accepted "
    "correlated provider text receipt after that registration. Nothing is armed here: declare "
    "another condition (card, mr, pipeline, decision) or ask the operator to arm it."
)


class _Stale(Exception):
    """The registration changed under a scan."""


def _uuid(value) -> str | None:
    try:
        text = str(uuid.UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None
    return text if text == value else None


def _owner(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[\w-]{1,64}", value) is not None


def _int(value) -> int | None:
    return value if type(value) is int and value > 0 else None


def validate(rec, floor: int) -> dict | None:
    """Allowlisted evidence of an accepted, correlated, provider-timed text receipt, else None.
    ``floor``: UTC second of the registration; an older provider time is history."""
    if not isinstance(rec, dict):
        return None
    if (rec.get("kind") != "text" or rec.get("event") != "accepted" or type(rec.get("schema_version")) is not int
            or rec["schema_version"] != 1 or rec.get("producer") not in PRODUCERS
            or rec.get("time_source") != "provider"):
        return None
    request_id, batch_id = _uuid(rec.get("request_id")), _uuid(rec.get("batch_id"))
    bot = rec.get("bot_identity")
    chat = rec.get("chat_id")
    chat = str(chat) if type(chat) is int else chat
    message_id, provider_ts = _int(rec.get("message_id")), _int(rec.get("provider_ts"))
    if (not request_id or not batch_id or not isinstance(bot, str) or not _BOT.fullmatch(bot)
            or not isinstance(chat, str) or not _CHAT.fullmatch(chat) or not message_id
            or not provider_ts or provider_ts < floor):
        return None
    ids = [_uuid(t) for t in rec["task_ids"]] if isinstance(rec.get("task_ids"), list) else []
    asks = rec.get("asks")
    if isinstance(asks, list):
        ids += [_uuid(x.get("task_id")) for x in asks if isinstance(x, dict)]
    task_id = next((t for t in ids if t), None)
    if task_id is None:
        return None
    return {"producer": rec["producer"], "request_id": request_id, "batch_id": batch_id,
            "task_id": task_id, "bot_identity": bot, "chat_id": chat, "message_id": message_id,
            "provider_ts": provider_ts}


def provider_key(evidence: dict) -> str:
    """One provider message, whatever event_id or retry delivered it."""
    raw = f"{evidence['bot_identity']}|{evidence['chat_id']}|{evidence['message_id']}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ---------------------------------------------------------------- registration
def _load(store, agent: str, task_id: str, key: str):
    row = store.mark(agent, task_id, key)
    if not row or not row[0]:
        return None
    try:
        value = json.loads(row[0])
    except ValueError:
        raise ValueError("corrupt receipt state") from None
    if not isinstance(value, dict):
        raise ValueError("corrupt receipt state")
    return value


def registration(store, agent: str, task_id: str) -> dict | None:
    """The stored registration; ValueError when it does not parse (fail closed, not reset)."""
    reg = _load(store, agent, task_id, REG_KEY)
    if reg is None:
        return None
    try:
        if (reg["version"] != 1 or _uuid(reg["reg_id"]) is None or reg["source_task_id"] != task_id
                or not _owner(reg["owner_id"]) or reg["state"] not in ("armed", "disarmed")
                or type(reg["installed_at"]) is not int or not isinstance(reg["journals"], dict)
                or not 1 <= len(reg["journals"]) <= MAX_JOURNALS):
            raise ValueError("unknown receipt registration")
        for name, j in reg["journals"].items():
            if not _NAME.fullmatch(name) or j["state"] not in ("absent", "tracking", "blocked") \
                    or not Path(j["path"]).is_absolute() or Path(j["path"]).name != JOURNAL_NAME:
                raise ValueError("unknown receipt journal")
    except (KeyError, TypeError):
        raise ValueError("unknown receipt registration") from None
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


def _eof(path: Path) -> dict:
    """Cursor at the EOF of an existing safe file, or an absent source. A row that was partial at
    arm time is history: the cursor is marked ``resync`` and the first read drops its remainder."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return {"path": str(path), "state": "absent"}
    if not stat.S_ISREG(st.st_mode):
        raise ValueError(f"journal is not a regular file: {path.name}")
    offset = st.st_size     # exactly EOF: a row still being written at arm time is history
    with open(path, "rb") as fh:
        fst = os.fstat(fh.fileno())
        if (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
            raise ValueError("journal changed while arming")
        tail_hash = _tail(fh.fileno(), offset)
        # mid-row at arm time: the rest of that row, up to its newline, is skipped by the first read
        resync = offset > 0 and os.pread(fh.fileno(), 1, offset - 1) != b"\n"
    cur = {"path": str(path), "state": "tracking", "device": st.st_dev, "inode": st.st_ino,
           "offset": offset, "tail": tail_hash}
    if resync:
        cur["resync"] = True
    return cur


TAIL = 64


def _tail(fd: int, offset: int) -> str:
    """Hash of the bytes just before ``offset``: content rewritten under a kept inode and regrown
    past the cursor is a truncation too, though the size alone looks fine."""
    start = max(0, offset - TAIL)
    if offset == 0:
        return hashlib.sha256(b"").hexdigest()[:16]
    return hashlib.sha256(os.pread(fd, offset - start, start)).hexdigest()[:16]


def _put(store, agent: str, task_id: str, key: str, value: dict) -> None:
    store.mark(agent, task_id, key, json.dumps(value, sort_keys=True))


def arm(store, agent: str, task_id: str, owner_id: str, journals: dict[str, str], now: float) -> dict:
    """Register (idempotently) and return the registration. A repeat with the same source,
    owner and journals returns the stored one with its baseline; re-arming a disarmed
    registration keeps its cursors and dedup history; a delivered one starts a new generation."""
    if _uuid(task_id) is None or not _owner(owner_id):
        raise ValueError("task must be a full canonical UUID and owner a Mesh agent id")
    if not 1 <= len(journals) <= MAX_JOURNALS or not all(_NAME.fullmatch(n) for n in journals):
        raise ValueError("journal names: 1..8 of [a-z][a-z0-9_-]{0,31}")
    paths = {n: Path(p) for n, p in journals.items()}
    if any(not p.is_absolute() or p.name != JOURNAL_NAME for p in paths.values()):
        raise ValueError(f"journals must be absolute paths to {JOURNAL_NAME}")
    old = registration(store, agent, task_id)
    if old and not delivered(store, agent, task_id, old["reg_id"]):
        if old["owner_id"] != owner_id or {n: j["path"] for n, j in old["journals"].items()} != \
                {n: str(p) for n, p in paths.items()}:
            raise ValueError("a live registration for this card has another owner or journals: disarm it first")
        if old["state"] == "disarmed":
            old["state"] = "armed"
            _put(store, agent, task_id, REG_KEY, old)
        return old
    reg = {"version": 1, "reg_id": str(uuid.uuid4()), "source_task_id": task_id, "owner_id": owner_id,
           "state": "armed", "installed_at": int(now),
           "journals": {n: _eof(p) for n, p in sorted(paths.items())}}
    _put(store, agent, task_id, REG_KEY, reg)       # acknowledged only after this write
    return reg


def disarm(store, agent: str, task_id: str) -> dict | None:
    """Stop observing; cursors, pending evidence and dedup history stay."""
    reg = registration(store, agent, task_id)
    if reg and reg["state"] == "armed":
        reg["state"] = "disarmed"
        _put(store, agent, task_id, REG_KEY, reg)
    return reg


def rebaseline(store, agent: str, task_id: str, name: str) -> dict:
    """Operator-explicit exit from a blocked journal: continue from its current last complete line."""
    reg = registration(store, agent, task_id)
    if reg is None or name not in reg["journals"]:
        raise ValueError("no such registered journal")
    reg["journals"][name] = _eof(Path(reg["journals"][name]["path"]))
    _put(store, agent, task_id, REG_KEY, reg)
    return reg


def inspect(store, agent: str, task_id: str) -> dict:
    """Metadata only: registration, cursors, whether evidence is pending or delivered."""
    try:
        reg = registration(store, agent, task_id)
    except ValueError:
        return {"registered": False, "error": "corrupt registration (fail closed)"}
    if reg is None:
        return {"registered": False}
    return {"registered": True, "reg_id": reg["reg_id"], "state": reg["state"], "owner_id": reg["owner_id"],
            "installed_at": reg["installed_at"], "journals": reg["journals"],
            "pending": pending(store, agent, task_id, reg["reg_id"]),
            "delivered": delivered(store, agent, task_id, reg["reg_id"])}


# ---------------------------------------------------------------------- scan
def _read(path: Path, cur: dict):
    """(new cursor, complete lines). A blocked cursor carries its reason; nothing is skipped."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        if cur["state"] == "absent":
            return cur, []
        return {**cur, "state": "blocked", "reason": "removed"}, []
    if not stat.S_ISREG(st.st_mode):
        return {**cur, "state": "blocked", "reason": "unsafe"}, []
    if cur["state"] == "absent":
        cur = {"path": cur["path"], "state": "tracking", "device": st.st_dev, "inode": st.st_ino,
               "offset": 0, "tail": _tail(-1, 0)}
    if (st.st_dev, st.st_ino) != (cur["device"], cur["inode"]):
        return {**cur, "state": "blocked", "reason": "rotated"}, []
    if st.st_size < cur["offset"]:
        return {**cur, "state": "blocked", "reason": "truncated"}, []
    if st.st_size == cur["offset"]:
        return cur, []
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        fst = os.fstat(fd)
        if (fst.st_dev, fst.st_ino) != (cur["device"], cur["inode"]):
            return {**cur, "state": "blocked", "reason": "rotated"}, []
        if _tail(fd, cur["offset"]) != cur.get("tail"):
            return {**cur, "state": "blocked", "reason": "truncated"}, []
        chunk = os.pread(fd, MAX_READ, cur["offset"])
        if cur.get("resync"):
            nl = chunk.find(b"\n")
            if nl < 0:
                if len(chunk) >= MAX_LINE:
                    return {**cur, "state": "blocked", "reason": "line_too_long"}, []
                return cur, []              # the pre-arm row is still unfinished
            if nl + 1 > MAX_LINE:           # the row that was partial at arm time is over-long too
                return {**cur, "state": "blocked", "reason": "line_too_long"}, []
            skipped = {k: v for k, v in cur.items() if k != "resync"}
            skipped.update(offset=cur["offset"] + nl + 1, tail=_tail(fd, cur["offset"] + nl + 1))
        else:
            skipped = None
        cut = chunk.rfind(b"\n")
        new_tail = _tail(fd, cur["offset"] + cut + 1) if cut >= 0 else ""
    finally:
        os.close(fd)
    if skipped is not None:
        return _read(path, skipped)
    if cut < 0:
        if len(chunk) >= MAX_LINE:
            return {**cur, "state": "blocked", "reason": "line_too_long"}, []
        return cur, []                      # partial line: the cursor never passes it
    lines = chunk[:cut + 1].split(b"\n")[:-1]
    for i, raw in enumerate(lines):
        if len(raw) > MAX_LINE:
            # a complete over-long row blocks the journal; the rows before it are still delivered
            good = sum(len(x) + 1 for x in lines[:i])
            head = cur["offset"] + good
            fd = os.open(path, flags)
            try:
                before = _tail(fd, head)
            finally:
                os.close(fd)
            return {**cur, "offset": head, "tail": before, "state": "blocked", "reason": "line_too_long"}, lines[:i]
    return {**cur, "offset": cur["offset"] + cut + 1, "tail": new_tail}, lines


def scan(store, agent: str, task_id: str) -> dict | None:
    """Advance every journal of an armed registration; keep the first valid receipt pending.
    Cursor, dedup marks and pending evidence commit in one transaction, so a crash re-reads the
    same lines and the dedup marks make that harmless. Returns the pending evidence, if any."""
    try:
        reg = registration(store, agent, task_id)
    except ValueError:
        return None
    if reg is None or reg["state"] != "armed" or delivered(store, agent, task_id, reg["reg_id"]):
        return None
    reg_id = reg["reg_id"]
    snapshot = copy.deepcopy(reg)
    keep = pending(store, agent, task_id, reg_id)
    moves, found = {}, []
    for name, cur in reg["journals"].items():
        if cur["state"] == "blocked":
            continue
        try:
            new, lines = _read(Path(cur["path"]), cur)
        except OSError:
            continue                        # unreadable now: retry the same cursor next tick
        moves[name] = new
        for raw in lines:
            if len(raw) > MAX_LINE:
                continue
            try:
                ev = validate(json.loads(raw), reg["installed_at"])
            except ValueError:
                continue
            if ev:
                found.append((name, ev))
    owned = not store.db.in_transaction
    if owned:
        store.db.execute("BEGIN IMMEDIATE")
    try:
        # the operator may have disarmed, re-armed or rebaselined while the journals were read:
        # commit only against the exact registration these reads were based on
        if registration(store, agent, task_id) != snapshot:
            raise _Stale()
        for name, ev in found:
            seen_key = SEEN_PREFIX + reg_id[:8] + ":" + provider_key(ev)
            if store.mark(agent, task_id, seen_key):
                continue                    # the same provider message under another event_id / retry
            store.mark(agent, task_id, seen_key, "1")
            if keep is None:
                keep = {"reg_id": reg_id, "journal": name, **ev}
                _put(store, agent, task_id, PENDING_KEY, keep)
        if moves != {n: reg["journals"][n] for n in moves}:
            reg["journals"].update(moves)
            _put(store, agent, task_id, REG_KEY, reg)
    except BaseException as exc:
        if owned and store.db.in_transaction:
            store.db.execute("ROLLBACK")
        if isinstance(exc, _Stale):
            return None                     # nothing committed; the next tick starts from the new state
        raise
    if owned and store.db.in_transaction:
        store.db.execute("COMMIT")
    return keep


def event_text(evidence: dict) -> str:
    return ("a new provider-accepted Telegram text receipt arrived for this card's registration "
            f"(journal {evidence['journal']}, producer {evidence['producer']}, request {evidence['request_id']}, "
            f"batch {evidence['batch_id']}, delivered ask {evidence['task_id']}, message {evidence['message_id']}, "
            f"provider time {evidence['provider_ts']}): the receipt wait is complete")
