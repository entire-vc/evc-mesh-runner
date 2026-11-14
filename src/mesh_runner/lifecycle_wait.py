"""Opt-in lifecycle WAIT: one durable observer of the native Codex SessionStart hook of one lane.

``⏳ WAIT lifecycle:#<this-card-8hex>`` sleeps until the FIRST new, correlated lifecycle event of
the registered lane profile after the registration: a native ``startup`` or ``compact``
SessionStart that the hook recorded as ``ok`` (and whose session has the helper ACK with the
same epoch and a rollout file), or a recorded hook ``failed``. The two outcomes are distinct
classes (``candidate`` vs ``hook_failure``) and the wake text says so: a candidate is evidence
for the operator's acceptance, never the acceptance itself.

It is not a second scheduler: registration, cursors, pending evidence and the one-shot delivered
mark are Runner store marks of the source task; the wake is the same ``waiting -> ready`` +
wake text transition as every other event wait. The journal machinery (cursor, tail hash,
partial-line, rotation fail-closed) is shared with ``receipt_wait``.

Only metadata is read: journal rows, the file NAMES of ``<pid>-<session>.ack`` and the ACK's
``session_id``/``epoch`` fields, and the existence of a rollout file. Never a dialog body.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import time
import uuid
from pathlib import Path

from . import receipt_wait as _rw
from .receipt_wait import _Stale, _uuid, _owner, _load, _put, _eof, _read, MAX_LINE

REG_KEY = "lifecycle_reg"
PENDING_KEY = "lifecycle_pending"
DELIVERED_KEY = "lifecycle_delivered"
UNRESOLVED_KEY = "lifecycle_unresolved"
SEEN_PREFIX = "lifecycle_seen:"
PROFILE = "khan"                        # the one Codex profile this observer serves
UNRESOLVED_MAX = 16                     # valid rows whose session artifacts have not appeared yet
UNRESOLVED_TTL = 600                    # seconds after the row's own ts
JOURNAL_NAME = "khan-lifecycle.jsonl"
PRODUCER = "khan-session-start-hook"
SOURCES = ("startup", "resume", "compact")  # clear is recorded by the hook but never wakes
_PROFILE = re.compile(r"[a-z][a-z0-9_-]{0,31}")

WAIT_GUIDANCE = (
    "invalid lifecycle wait: `wait_for on=registered registration=lifecycle` is accepted only when the lifecycle "
    "observer is enabled on this runner and an operator armed a live registration for this card and "
    "this owner (`mesh-runner lifecycle-wait arm`). It wakes once on the first new correlated "
    "startup/resume/compact SessionStart candidate or recorded hook failure after that registration. "
    "Nothing is armed here: wait on another condition (card, mr, pipeline, ask_agent) or ask the "
    "operator to arm it."
)


def _int(value) -> int | None:
    return value if type(value) is int and value > 0 else None


def validate(rec, floor: int) -> dict | None:
    """Allowlisted metadata of a lifecycle row, else None (history, wrong schema, other source)."""
    if not isinstance(rec, dict):
        return None
    if (rec.get("kind") != "lifecycle" or type(rec.get("schema_version")) is not int
            or rec["schema_version"] != 1 or rec.get("producer") != PRODUCER
            or rec.get("hook_event") != "SessionStart" or rec.get("source") not in SOURCES
            or rec.get("result") not in ("ok", "failed")):
        return None
    sid, ts = _uuid(rec.get("session_id")), _int(rec.get("ts"))
    if not sid or not ts or ts < floor:
        return None
    epoch = None
    if rec["result"] == "ok":
        epoch = _uuid(rec.get("epoch"))
        if not epoch:
            return None                 # an ok row without the helper epoch proves nothing
    return {"source": rec["source"], "result": rec["result"], "session_id": sid, "epoch": epoch, "ts": ts}


def event_key(ev: dict) -> str:
    """One hook invocation, whatever retry wrote it."""
    raw = f"{ev['session_id']}|{ev['source']}|{ev['result']}|{ev['ts']}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


MAX_DIRS = 64            # newest day directories of the sessions tree looked at
MAX_ENTRIES = 20000      # directory entries examined per correlation, all directories together


def _names(path: str, budget: list[int]):
    """Entry names of one directory, newest first, drawn from a shared entry budget. Exhausting the
    budget stops the walk: correlation fails closed (no wake) instead of growing with the tree."""
    out = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                if budget[0] <= 0:
                    return out
                budget[0] -= 1
                out.append(entry.name)
    except OSError:
        return []
    return sorted(out, reverse=True)


def _rollout_exists(home: str, sid: str) -> bool:
    """A rollout file of this session in the newest ``MAX_DIRS`` day directories, within the entry budget."""
    budget, days = [MAX_ENTRIES], 0
    root = os.path.join(home, "sessions")
    suffix = f"-{sid}.jsonl"
    for year in _names(root, budget):
        for month in _names(os.path.join(root, year), budget):
            for day in _names(os.path.join(root, year, month), budget):
                if days >= MAX_DIRS or budget[0] <= 0:
                    return False
                days += 1
                for name in _names(os.path.join(root, year, month, day), budget):
                    if name.startswith("rollout-") and name.endswith(suffix):
                        return True
    return False


def _ack_epochs(ack_root: str, sid: str) -> set[str]:
    """Epochs of the helper ACKs `<pid>-<sid>.ack` of this session (bounded directory + file reads)."""
    found, suffix = set(), f"-{sid}.ack"
    for name in _names(ack_root, [MAX_ENTRIES]):
        if not name.endswith(suffix):
            continue
        path = os.path.join(ack_root, name)
        try:
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode) or st.st_size > 4096:
                continue
            with open(path, "rb") as fh:
                data = json.loads(fh.read(4097))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("session_id") == sid and isinstance(data.get("epoch"), str):
            found.add(data["epoch"])
    return found


def classify(ev: dict, reg: dict) -> dict | None:
    """Correlate a row with the real session artifacts of the registered profile.
    ``hook_failure``: a recorded failure of a session that has its rollout file (a failing hook never
    writes the ACK, so the ACK cannot be required). ``candidate``: ok + helper ACK with the same
    epoch + rollout file. Anything uncorrelated is not a wake (it stays unresolved for a short TTL).
    Every filesystem look is bounded (``MAX_DIRS`` / ``MAX_ENTRIES``) and fails closed."""
    sid = ev["session_id"]
    if not _rollout_exists(reg["codex_home"], sid):
        return None
    if ev["result"] == "failed":
        return {**ev, "class": "hook_failure", "rollout_seen": True, "ack_epoch_match": False}
    if ev["epoch"] not in _ack_epochs(reg["ack_root"], sid):
        return None
    return {**ev, "class": "candidate", "rollout_seen": True, "ack_epoch_match": True}


# ---------------------------------------------------------------- registration
def registration(store, agent: str, task_id: str) -> dict | None:
    reg = _load(store, agent, task_id, REG_KEY)
    if reg is None:
        return None
    try:
        j = reg["journal"]
        if (reg["version"] != 1 or _uuid(reg["reg_id"]) is None or reg["source_task_id"] != task_id
                or not _owner(reg["owner_id"]) or reg["state"] not in ("armed", "disarmed")
                or type(reg["installed_at"]) is not int or not _PROFILE.fullmatch(reg["profile"])
                or not Path(reg["codex_home"]).is_absolute() or not Path(reg["ack_root"]).is_absolute()
                or not isinstance(reg["manifest_sha"], str) or not isinstance(reg["config_path"], str)
                or j["state"] not in ("absent", "tracking", "blocked")
                or not Path(j["path"]).is_absolute() or Path(j["path"]).name != JOURNAL_NAME):
            raise ValueError("unknown lifecycle registration")
    except (KeyError, TypeError):
        raise ValueError("unknown lifecycle registration") from None
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


def _identity(manifest: Path) -> tuple[str, str, str, str | None]:
    """(manifest sha256[:16], config_path, hook_key) read from that profile's manifest by named fields."""
    st = os.lstat(manifest)
    if not stat.S_ISREG(st.st_mode) or st.st_size > 65536:
        raise ValueError("manifest is not a small regular file")
    raw = manifest.read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("config_path"), str) \
            or not isinstance(data.get("hook_key"), str):
        raise ValueError("manifest lacks config_path/hook_key")
    home = data.get("codex_home")
    return hashlib.sha256(raw).hexdigest()[:16], data["config_path"], data["hook_key"], \
        home if isinstance(home, str) else None


def arm(store, agent: str, task_id: str, owner_id: str, profile: str, manifest: str,
        codex_home: str, journal: str, now: float) -> dict:
    """Register idempotently. Same card/owner/profile/journal returns the stored registration
    (same baseline); a delivered one starts a new generation; another owner/journal needs disarm."""
    if _uuid(task_id) is None or not _owner(owner_id) or profile != PROFILE:
        raise ValueError(f"task must be a full UUID, owner a Mesh agent id, profile exactly {PROFILE!r}")
    mpath, jpath, home = Path(manifest), Path(journal), Path(codex_home)
    if not mpath.is_absolute() or not jpath.is_absolute() or not home.is_absolute() \
            or jpath.name != JOURNAL_NAME:
        raise ValueError(f"manifest/journal/codex-home must be absolute; journal named {JOURNAL_NAME}")
    sha, config_path, hook_key, manifest_home = _identity(mpath)
    cfg = Path(config_path)
    if cfg.name != f"{profile}.config.toml":
        raise ValueError("manifest config does not belong to this profile")
    # the profile's config must live in the Codex home whose rollouts are correlated, and the
    # manifest's own codex_home (required) must be that same directory
    if not cfg.is_absolute() or cfg.parent.resolve() != home.resolve() or manifest_home is None \
            or Path(manifest_home).resolve() != home.resolve():
        raise ValueError("codex-home does not hold this profile's config")
    ack_root = str(mpath.resolve().parent)
    old = registration(store, agent, task_id)
    if old and not delivered(store, agent, task_id, old["reg_id"]):
        if (old["owner_id"], old["profile"], old["journal"]["path"], old["manifest_sha"]) != \
                (owner_id, profile, str(jpath), sha):
            raise ValueError("a live registration for this card has another owner, profile, journal or manifest: disarm it first")
        if old["state"] == "disarmed":
            old["state"] = "armed"
            _put(store, agent, task_id, REG_KEY, old)
        return old
    reg = {"version": 1, "reg_id": str(uuid.uuid4()), "source_task_id": task_id, "owner_id": owner_id,
           "state": "armed", "installed_at": int(now), "profile": profile, "manifest_sha": sha,
           "config_path": config_path, "hook_key": hook_key, "codex_home": str(home), "ack_root": ack_root,
           "journal": _eof(jpath)}
    _put(store, agent, task_id, REG_KEY, reg)       # acknowledged only after this write
    return reg


def disarm(store, agent: str, task_id: str) -> dict | None:
    reg = registration(store, agent, task_id)
    if reg and reg["state"] == "armed":
        reg["state"] = "disarmed"
        _put(store, agent, task_id, REG_KEY, reg)
    return reg


def rebaseline(store, agent: str, task_id: str) -> dict:
    """Explicit exit from a blocked journal: continue from its current last complete line."""
    reg = registration(store, agent, task_id)
    if reg is None:
        raise ValueError("no registration")
    reg["journal"] = _eof(Path(reg["journal"]["path"]))
    _put(store, agent, task_id, REG_KEY, reg)
    return reg


def inspect(store, agent: str, task_id: str) -> dict:
    try:
        reg = registration(store, agent, task_id)
    except ValueError:
        return {"registered": False, "error": "corrupt registration (fail closed)"}
    if reg is None:
        return {"registered": False}
    return {"registered": True, "reg_id": reg["reg_id"], "state": reg["state"], "owner_id": reg["owner_id"],
            "installed_at": reg["installed_at"], "profile": reg["profile"], "manifest_sha": reg["manifest_sha"],
            "hook_key": reg["hook_key"], "journal": reg["journal"],
            "pending": pending(store, agent, task_id, reg["reg_id"]),
            "delivered": delivered(store, agent, task_id, reg["reg_id"])}


# ---------------------------------------------------------------------- scan
def scan(store, agent: str, task_id: str) -> dict | None:
    """Advance the journal cursor of an armed registration; keep the first correlated event
    pending. Cursor, dedup mark and pending evidence commit in one transaction."""
    try:
        reg = registration(store, agent, task_id)
    except ValueError:
        return None
    if reg is None or reg["state"] != "armed" or delivered(store, agent, task_id, reg["reg_id"]):
        return None
    reg_id = reg["reg_id"]
    snapshot = copy.deepcopy(reg)
    keep = pending(store, agent, task_id, reg_id)
    cur, found = reg["journal"], []
    now = int(time.time())
    try:
        held = _load(store, agent, task_id, UNRESOLVED_KEY) or {}
    except ValueError:
        held = {}
    rows = held.get("rows", []) if held.get("reg_id") == reg_id and isinstance(held.get("rows"), list) else []
    rows = [r for r in rows if isinstance(r, dict) and validate({**r, "kind": "lifecycle", "schema_version": 1,
            "producer": PRODUCER, "hook_event": "SessionStart"}, reg["installed_at"])]
    unresolved = []
    for ev in rows:
        done = classify(ev, reg)
        if done:
            found.append(done)
        elif now - ev["ts"] < UNRESOLVED_TTL:
            unresolved.append(ev)
    if cur["state"] != "blocked":
        try:
            new, lines = _read(Path(cur["path"]), cur)
        except OSError:
            return keep                     # unreadable now: retry the same cursor next tick
        for raw in lines:
            if len(raw) > MAX_LINE:
                continue
            try:
                ev = validate(json.loads(raw), reg["installed_at"])
            except ValueError:
                continue
            if not ev:
                continue
            if done := classify(ev, reg):
                found.append(done)
            elif now - ev["ts"] < UNRESOLVED_TTL:
                unresolved.append(ev)
        reg["journal"] = new
    owned = not store.db.in_transaction
    if owned:
        store.db.execute("BEGIN IMMEDIATE")
    try:
        if registration(store, agent, task_id) != snapshot:
            raise _Stale()
        for ev in found:
            seen_key = SEEN_PREFIX + reg_id[:8] + ":" + event_key(ev)
            if store.mark(agent, task_id, seen_key):
                continue
            store.mark(agent, task_id, seen_key, "1")
            if keep is None:
                keep = {"reg_id": reg_id, **ev}
                _put(store, agent, task_id, PENDING_KEY, keep)
        unresolved = list({event_key(e): e for e in unresolved}.values())      # a duplicate row is one row
        if (held.get("reg_id") == reg_id and held.get("rows") != unresolved[:UNRESOLVED_MAX]) or \
                (held.get("reg_id") != reg_id and unresolved):
            _put(store, agent, task_id, UNRESOLVED_KEY, {"reg_id": reg_id, "rows": unresolved[:UNRESOLVED_MAX]})
        if reg["journal"] != snapshot["journal"]:
            _put(store, agent, task_id, REG_KEY, reg)
    except BaseException as exc:
        if owned and store.db.in_transaction:
            store.db.execute("ROLLBACK")
        if isinstance(exc, _Stale):
            return None
        raise
    if owned and store.db.in_transaction:
        store.db.execute("COMMIT")
    return keep


def event_text(ev: dict) -> str:
    if ev["class"] == "hook_failure":
        what = "a recorded SessionStart HOOK FAILURE (not an acceptance)"
    else:
        what = ("a native SessionStart candidate with helper ACK epoch match and rollout present; this is "
                "candidate evidence for acceptance, not the acceptance itself")
    return (f"lifecycle event for this card's registration: {what} (source {ev['source']}, session "
            f"{ev['session_id']}, ts {ev['ts']}, rollout_seen {ev['rollout_seen']}): the lifecycle wait is complete")
