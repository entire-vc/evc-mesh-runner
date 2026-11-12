"""Wait-root radar: where the card waits of this runner end, and how many cards stand behind each end.

Every few minutes the runner follows each open ``WAIT card:`` attempt of its own store along the chain
of card waits to the card that does not wait on another card: the root. A root is classified by what
holds it (a lane working it, a pipeline, a person, another workspace, a card nobody runs, ...) and
counted: how many waiting cards stand behind it and how old the oldest of those waits is.

Only one kind of root is acted on: a card of one of this runner's agents that has no open attempt
and is ready to start (``ready``). Its lane starts it before its other ordinary work, heaviest first
(``AgentRunner._work_order``). Every other root is only shown (status file, ``mesh-runner roots``).
The radar writes nothing: no comment, no store row, no Mesh write; a card read that fails or is out
of budget classifies the root as unread, never as ready.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .store import OPEN

log = logging.getLogger("mesh_runner")

REFRESH_SECONDS = 180.0      # one graph per three minutes; the lanes read the last one in between
CARD_CACHE_SECONDS = 150.0   # a chain end read in the last pass is not read again
MAX_READS = 60               # chain ends read from Mesh per pass; the rest stay "unread"
READ_TIMEOUT = 5.0
OLD_HOURS = 72.0             # the measured share: card waits older than three days
STATUS_ROOTS = 25

BOOST = "ready"
ACTIVE_STATES = ("running", "starting")
HUMAN = "human"


@dataclass(frozen=True)
class Root:
    task_id: str
    kind: str          # ready, working, queued, unknown-state, human, foreign, no-runner, parked, ...
    holder: str        # the lane, @person, pipeline/MR ref or "-"
    behind: int        # waiting cards whose chain ends here
    oldest: str        # since of the oldest of those waits (ISO), "" unknown


@dataclass(frozen=True)
class Radar:
    at: str
    card_waits: int
    older: int                                     # card waits older than OLD_HOURS
    roots: tuple[Root, ...]
    depth: dict = field(default_factory=dict)       # chain depth -> waits
    reads: int = 0

    def weights(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.roots:
            if r.kind == BOOST:
                out[r.task_id] = out.get(r.task_id, 0) + r.behind
        return out

    def kinds(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.roots:
            out[r.kind] = out.get(r.kind, 0) + r.behind
        return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def open_attempts(db: sqlite3.Connection) -> dict[str, list[dict]]:
    """task id -> its open attempts (any agent): agent, state, wait."""
    out: dict[str, list[dict]] = {}
    q = f"SELECT agent, task_id, state, wait, updated_at FROM attempts WHERE state IN ({','.join('?' * len(OPEN))})"
    for agent, task, state, wait, updated in db.execute(q, OPEN):
        try:
            w = json.loads(wait or "{}")
        except ValueError:
            w = {}
        if not isinstance(w, dict):
            w = {}
        out.setdefault(task, []).append({"agent": agent, "state": state, "wait": w, "updated_at": updated or ""})
    return out


def _since(row: dict) -> str:
    return str(row["wait"].get("since") or row["updated_at"] or "")


def _hours(since: str, now: dt.datetime) -> float | None:
    try:
        t = dt.datetime.fromisoformat(since.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return (now - t).total_seconds() / 3600


def classify_card(card: dict | None, agents: frozenset[str]) -> tuple[str, str]:
    """(kind, holder) of a chain end that has no open attempt here, from its Mesh card.
    ``None``: the card could not be read with the waiter's key (another workspace, or gone)."""
    if card is None:
        return "foreign", "-"
    if card.get("_unread"):
        return "unread", "-"
    cat = card.get("status_category") or ""
    labels = [str(x) for x in (card.get("labels") or [])]
    name = card.get("assignee_name") if isinstance(card.get("assignee_name"), str) else ""
    atype = card.get("assignee_type") or ""
    if cat in ("done", "cancelled"):
        return "closed", "-"                     # the wait was not released yet
    if card.get("human_gate") is True or any(x.startswith("park:wait-human") for x in labels):
        return HUMAN, "human_gate"
    if "no-runner" in labels:
        return "no-runner", name or "-"
    if any(x.startswith("park:") for x in labels):
        return "parked", name or "-"
    if not card.get("assignee_id") and not name:
        return "unassigned", "-"
    if atype not in ("agent", ""):
        return HUMAN, "@" + (name or "?")         # a person holds the card
    if name not in agents:
        return "other-agent", name or "?"         # an agent no lane of this runner runs
    if cat in ("todo", "in_progress", "review"):
        return BOOST, name
    return "not-queued", name + ":" + (cat or "?")


def _wait_holder(row: dict) -> tuple[str, str]:
    w = row["wait"]
    kind = str(w.get("kind") or "")
    if kind == "blocking":
        return HUMAN, "@" + str(w.get("handle") or "?")
    if kind == "decision":
        return "decision", "@" + str(w.get("handle") or "?")
    if kind in ("pipeline", "mr", "merge"):
        return kind, str(w.get("target") or "?")
    if kind == "time":
        return kind, str(w.get("until") or w.get("target") or "?")
    return (kind or "unknown-state"), row["agent"]


def build(open_: dict[str, list[dict]], read_card: Callable[[str, str], dict | None],
          agents: frozenset[str], at: dt.datetime | None = None) -> Radar:
    """The roots of every open card wait in ``open_`` (see ``open_attempts``).
    ``read_card(task_id, agent)`` reads a chain end with that lane's key; ``None`` = unreadable."""
    now = at or dt.datetime.now(dt.timezone.utc)
    by_short = {t[:8]: t for t in open_}
    ends: dict[tuple[str, str], tuple[str, str]] = {}
    reads = 0

    def end_of(task: str, via: str) -> tuple[str, str]:
        nonlocal reads
        key = (task, via)
        if key not in ends:
            reads += 1
            ends[key] = classify_card(read_card(task, via), agents)
        return ends[key]

    def target_of(row: dict) -> str:
        w = row["wait"]
        return str(w.get("target_id") or by_short.get(str(w.get("target") or ""), "") or "")

    def root(task: str, seen: tuple[str, ...], via: str) -> tuple[str, str, str, int]:
        rows = open_.get(task)
        d = len(seen)
        if not rows:
            kind, holder = end_of(task, via)
            return task, kind, holder, d
        r = sorted(rows, key=lambda x: x["state"] != "running")[0]
        if r["state"] in ACTIVE_STATES:
            return task, "working", r["agent"], d
        if r["state"] == "ready":
            return task, "queued", r["agent"], d
        if r["state"] != "waiting":
            return task, "unknown-state", r["agent"], d
        if r["wait"].get("kind") == "card":
            nxt = target_of(r)
            if not nxt:
                return task, "unknown-target", str(r["wait"].get("target") or "?"), d
            if nxt in seen or nxt == task:
                return task, "cycle", "-", d
            return root(nxt, seen + (task,), r["agent"])
        kind, holder = _wait_holder(r)
        return task, kind, holder, d

    # Different lane keys may see different ends for the same UUID. Keep both views,
    # so a denied read neither hides another lane's root nor inherits its ready weight.
    behind: dict[tuple[str, str, str], list[str]] = {}
    depth: dict[int, int] = {}
    waits = older = 0
    for task, rows in open_.items():
        for r in rows:
            if r["state"] != "waiting" or r["wait"].get("kind") != "card":
                continue
            waits += 1
            nxt = target_of(r)
            if nxt and nxt != task:
                rid, kind, holder, d = root(nxt, (task,), r["agent"])
            else:
                rid, kind, holder, d = task, ("unknown-target" if not nxt else "cycle"), "-", 0
            depth[d] = depth.get(d, 0) + 1
            since = _since(r)
            behind.setdefault((rid, kind, holder), []).append(since)
            h = _hours(since, now)
            if h is not None and h >= OLD_HOURS:
                older += 1
    roots = [Root(rid, kind, holder, len(s), min((x for x in s if x), default=""))
             for (rid, kind, holder), s in behind.items()]
    roots.sort(key=lambda r: (-r.behind, r.oldest or "~", r.task_id))
    return Radar(at=now.isoformat(timespec="seconds"), card_waits=waits, older=older, roots=tuple(roots),
                 depth=dict(sorted(depth.items())), reads=reads)


def status(radar: Radar | None, limit: int = STATUS_ROOTS) -> dict | None:
    if radar is None:
        return None
    now = dt.datetime.fromisoformat(radar.at)
    rows = []
    for r in radar.roots[:limit]:
        h = _hours(r.oldest, now) if r.oldest else None
        rows.append({"task": r.task_id[:8], "kind": r.kind, "holder": r.holder, "behind": r.behind,
                     "oldest_wait_hours": None if h is None else round(h, 1)})
    return {"at": radar.at, "card_waits": radar.card_waits, "older_than_3d": radar.older,
            "roots_total": len(radar.roots), "by_kind": radar.kinds(),
            "boosted": {k[:8]: v for k, v in sorted(radar.weights().items(), key=lambda kv: -kv[1])},
            "roots": rows}


def render(radar: Radar) -> str:
    s = status(radar, limit=len(radar.roots)) or {}
    out = [f"wait roots at {radar.at}: {radar.card_waits} card waits, {radar.older} older than 3d, "
           f"{len(radar.roots)} roots, {radar.reads} cards read",
           "by kind: " + ", ".join(f"{k}={v}" for k, v in s["by_kind"].items()),
           "chain depth: " + json.dumps(radar.depth),
           f"{'root':9} {'kind':15} {'behind':>6} {'oldest':>7}  holder"]
    for r in s["roots"]:
        age = "?" if r["oldest_wait_hours"] is None else f"{r['oldest_wait_hours']:.0f}h"
        out.append(f"#{r['task']:8} {r['kind']:15} {r['behind']:>6} {age:>7}  {r['holder']}")
    return "\n".join(out)


class CardReader:
    """Reads a chain end with the waiting lane's own key (that lane's workspace decides "foreign"),
    one bounded GET per card, cached between passes. Never writes."""

    def __init__(self, clients: dict[str, object], budget: int = MAX_READS,
                 cache_seconds: float = CARD_CACHE_SECONDS):
        self.clients = clients
        self.budget, self.cache_seconds = budget, cache_seconds
        self._cache: dict[tuple[str, str], tuple[float, dict | None]] = {}
        self.spent = 0

    def new_pass(self) -> None:
        self.spent = 0
        now = time.monotonic()
        self._cache = {k: v for k, v in self._cache.items() if now - v[0] < self.cache_seconds}

    def __call__(self, task_id: str, agent: str) -> dict | None:
        key = (task_id, agent)
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < self.cache_seconds:
            return hit[1]
        client = self.clients.get(agent)
        if client is None or self.spent >= self.budget:
            return {"_unread": True}
        self.spent += 1
        from .mesh import MeshError
        try:
            row = client.bounded_get("/tasks/" + task_id, timeout=READ_TIMEOUT)
        except MeshError as e:
            if e.status in (403, 404):
                self._cache[key] = (time.monotonic(), None)
                return None
            return {"_unread": True}                     # transport trouble is not "foreign"
        if isinstance(row, dict):
            row = row.get("task", row)
        card = row if isinstance(row, dict) and row.get("id") == task_id else {"_unread": True}
        if not card.get("_unread"):
            self._cache[key] = (time.monotonic(), card)
        return card


def clients_for(cfg) -> dict[str, object]:
    from .mesh import MeshClient
    return {a.name: MeshClient(cfg.mesh_url, a.key, basic_auth=cfg.mesh_basic_auth) for a in cfg.agents}


def compute(db_path: Path, reader: CardReader, agents: frozenset[str]) -> Radar:
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        open_ = open_attempts(db)
    finally:
        db.close()
    reader.new_pass()
    return build(open_, reader, agents)


class RootRadar:
    """The runner's radar: refreshed off the main loop every REFRESH_SECONDS; lanes read the last one."""

    def __init__(self, cfg, clients: dict[str, object] | None = None, db_path: Path | None = None):
        self.cfg = cfg
        self.db_path = db_path or cfg.state_dir / "runner.sqlite"
        self.agents = frozenset(a.name for a in cfg.agents)
        self._clients = clients
        self._reader: CardReader | None = None
        self.radar: Radar | None = None
        self._weights: dict[str, int] = {}
        self._next = 0.0
        self._worker: threading.Thread | None = None
        self._failing = False

    def weight(self, task_id: str) -> int:
        return self._weights.get(task_id, 0)

    def refresh(self) -> Radar | None:
        try:
            if self._reader is None:
                self._reader = CardReader(self._clients if self._clients is not None else clients_for(self.cfg))
            radar = compute(self.db_path, self._reader, self.agents)
        except Exception as e:                      # the radar never stops the runner
            if not self._failing:
                self._failing = True
                log.warning("wait roots: refresh failed: %s", type(e).__name__)
            return self.radar
        if self._failing:
            self._failing = False
            log.info("wait roots: refresh recovered")
        self.radar, self._weights = radar, radar.weights()
        return radar

    def refresh_if_due(self) -> None:
        if time.monotonic() < self._next:
            return
        if self._worker is not None and self._worker.is_alive():
            return
        self._next = time.monotonic() + REFRESH_SECONDS
        self._worker = threading.Thread(target=self.refresh, name="wait-roots", daemon=True)
        self._worker.start()

    def status(self) -> dict | None:
        return status(self.radar)
