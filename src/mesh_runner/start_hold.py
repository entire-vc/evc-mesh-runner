"""A headless CLI that refuses to start on several cards of the host holds those launches, not the cards.

When the provider withdraws the model the lanes' agy is configured with, the cards of several lanes are
refused before any conversation within minutes, often seconds apart, and every later cycle repeats
it. A single refused start is the card's own matter
(``headless-start-unsent``: the prompt stays unsent and after ``max_startup_failures`` in a row the card goes
up with the CLI's words). A mass refusal is the host's: handing each card up spreads the cards over leads and
queues while the operator hears nothing.

Here each refused start is an observation of its (lane, card) for the harness. Two different (lane, card)
pairs within ``WINDOW_MINUTES`` open a hold of that harness: every launch with a refused launch configuration
(``fingerprint``: the harness, its flags and model, never the binary's file identity) is held, the cards keep
their unsent prompts and their executors, and the operator is told once per harness and reason. One card
refused again and again never opens it.

The hold lifts for a launch whose configuration changed (another model or flags: a new fingerprint is no
longer covered) and for every launch of a fingerprint after one launch of it got a conversation: one probe
launch per ``PROBE_MINUTES`` is let through (single-flight); its success lifts the fingerprint. Upgrading the
binary changes nothing here (``harness_hold`` is the outdated-CLI hold). A fingerprint no launch refused for
``STALE_HOURS`` drops out, so a configuration nobody runs any more does not stay held in status.

Agents run in threads over one SQLite store: the record of a harness is rewritten by compare-and-set, so
concurrent refusals of many lanes all count and open one hold.

A lane whose route has a fallback leaves the held harness as at a provider limit: its held launch
calls the lane's routing command once per hold generation (``since``) with ``MESH_RUNNER_LIMIT_SOURCE=
start_hold``; account routing checks the hold against this store and follows the lane's quota_edges. The lane's
record (``route``: its mark on the harness) says where the lane stands: ``calling`` while the command runs,
``moved`` off the harness, ``refused`` (the command switched nothing: the lane stays held), ``returned`` (back
to the harness: the hold no longer covers the lane's configuration, or the lane takes the probe), ``probing``
(its probe launch went through). Account routing ends the fallback when it reads ``returned``. A moved lane
launches nothing on the held harness, so it takes its turn at the probe like a held lane does.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re

from .store import Store, iso

OWNER = "_runtime"
PREFIX = "_harness_start_"
KEY = "hold"
PROBE_KEY = "probe"
WINDOW_MINUTES = 10.0
PROBE_MINUTES = 30.0
STALE_HOURS = 6.0
# a held card is looked at again after this long (its unsent prompt stays); a first refused start waits as
# long before its own retry, so another card refused the same way can show the refusal is the host's
PAUSE_SECONDS = 120
_TRIES = 20
ROUTE_KEY = "start_route"
CALLING, MOVED, REFUSED, RETURNED, PROBING = "calling", "moved", "refused", "returned", "probing"
CALL_STALE_SECONDS = 300           # the route call times out at 120 s

_VOLATILE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\b[0-9a-f]{12,}\b|\d+",
                       re.I)


def fingerprint(harness: str, argv: list[str]) -> str:
    """The launch configuration a refusal is about: the harness and the argv of a start without a session
    or working copies (flags, model, effort). The binary's size or mtime is not part of it."""
    raw = json.dumps([harness, [str(x) for x in argv]], separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def digest(excerpt: str) -> str:
    """The refusal's reason with request ids, trace ids and numbers taken out: the same cause on many cards
    is one reason."""
    flat = _VOLATILE.sub("#", " ".join((excerpt or "").lower().split()))
    return hashlib.sha256(flat.encode()).hexdigest()[:12]


def _now(now: dt.datetime | None) -> dt.datetime:
    return now or dt.datetime.now(dt.timezone.utc)


def _age(at: str, now: dt.datetime) -> float:
    try:
        t = dt.datetime.fromisoformat(str(at))
    except ValueError:
        return float("inf")
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return (now - t).total_seconds()


def _read_raw(store: Store, harness: str) -> tuple[str | None, dict]:
    row = store.mark(OWNER, PREFIX + harness, KEY)
    raw = row[0] if row else None
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        value = {}
    return raw, value if isinstance(value, dict) else {}


def _write(store: Store, harness: str, raw: str | None, value: dict) -> bool:
    new = json.dumps(value, sort_keys=True)
    if raw is None:
        return store.add_mark(OWNER, PREFIX + harness, KEY, new)
    return store.compare_mark(OWNER, PREFIX + harness, KEY, raw, new)


def _fresh(value: dict, now: dt.datetime) -> dict:
    """The record without observations past the window and fingerprints past STALE_HOURS."""
    obs = {k: o for k, o in (value.get("obs") or {}).items()
           if isinstance(o, dict) and _age(o.get("at", ""), now) < WINDOW_MINUTES * 60}
    hold = value.get("hold") if isinstance(value.get("hold"), dict) else {}
    fps = {fp: at for fp, at in (hold.get("fps") or {}).items() if _age(at, now) < STALE_HOURS * 3600}
    out: dict = {"obs": obs}
    if hold.get("since") and fps:
        out["hold"] = {**hold, "fps": fps}
    return out


def observe(store: Store, harness: str, lane: str, task_id: str, fp: str, excerpt: str,
            now: dt.datetime | None = None) -> tuple[dict, list[str]]:
    """Record one refused start of ``lane``/``task_id`` with launch configuration ``fp``. Returns the hold of
    the harness after it ({} while none is open) and the reason digests this call added to it (each is the
    caller's to signal: only the caller that added a reason gets it)."""
    now = _now(now)
    reason = digest(excerpt)
    for _ in range(_TRIES):
        raw, value = _read_raw(store, harness)
        value = _fresh(value, now)
        value["obs"][f"{lane}/{task_id}"] = {"at": iso(now), "fp": fp, "reason": reason,
                                             "excerpt": excerpt[:300]}
        hold = value.get("hold")
        added: list[str] = []
        if hold:
            hold["fps"][fp] = iso(now)
            if reason not in hold["reasons"]:
                hold["reasons"][reason] = {"since": iso(now), "excerpt": excerpt[:300]}
                added.append(reason)
        elif len(value["obs"]) >= 2:
            hold = {"since": iso(now), "harness": harness, "fps": {}, "reasons": {},
                    "cards": sorted(value["obs"])}
            for o in value["obs"].values():
                hold["fps"][o["fp"]] = iso(now)
                if o["reason"] not in hold["reasons"]:
                    hold["reasons"][o["reason"]] = {"since": iso(now), "excerpt": o.get("excerpt", "")}
                    added.append(o["reason"])
            value["hold"] = hold
        if _write(store, harness, raw, value):
            return dict(hold or {}), added
    return active(store, harness, now), []


def active(store: Store, harness: str, now: dt.datetime | None = None) -> dict:
    """The open hold of ``harness`` (fresh fingerprints only), or {}."""
    return dict(_fresh(_read_raw(store, harness)[1], _now(now)).get("hold") or {})


def covers(store: Store, harness: str, fp: str, now: dt.datetime | None = None) -> dict:
    """The hold that applies to a launch of ``harness`` with configuration ``fp`` now, or {}."""
    hold = active(store, harness, now)
    return hold if fp in (hold.get("fps") or {}) else {}


def probe(store: Store, harness: str, hold: dict, now: dt.datetime | None = None) -> bool:
    """True for the one launch per PROBE_MINUTES of a hold that goes through to see whether the harness
    starts again; never in the hold's first interval."""
    n = int(_age(hold.get("since", ""), _now(now)) // (PROBE_MINUTES * 60))
    if n < 1:
        return False
    return store.claim(OWNER, PREFIX + harness, PROBE_KEY, f"{hold.get('since')}#{n}")


def succeeded(store: Store, harness: str, fp: str, now: dt.datetime | None = None) -> bool:
    """A launch of ``fp`` got a conversation: the fingerprint is no longer held (nor counted towards a new
    hold). True when this call lifted something."""
    now = _now(now)
    for _ in range(_TRIES):
        raw, value = _read_raw(store, harness)
        if raw is None:
            return False
        fresh = _fresh(value, now)
        hold = fresh.get("hold")
        held = bool(hold and fp in hold["fps"])
        counted = any(o.get("fp") == fp for o in fresh["obs"].values())
        if not held and not counted:
            return False
        fresh["obs"] = {k: o for k, o in fresh["obs"].items() if o.get("fp") != fp}
        if held:
            hold["fps"].pop(fp)
            if not hold["fps"]:
                fresh.pop("hold")
        if _write(store, harness, raw, fresh):
            return held
    return False


def clear(store: Store, harness: str) -> bool:
    raw, _ = _read_raw(store, harness)
    if not raw:
        return False
    return store.compare_mark(OWNER, PREFIX + harness, KEY, raw, "")


def route(store: Store, lane: str, harness: str) -> tuple[str | None, dict]:
    """The lane's record of its route around a hold of ``harness`` (raw for compare-and-set, value)."""
    row = store.mark(lane, PREFIX + harness, ROUTE_KEY)
    raw = row[0] if row else None
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        value = {}
    return raw, value if isinstance(value, dict) else {}


def set_route(store: Store, lane: str, harness: str, raw: str | None, value: dict) -> bool:
    """Write the lane's route record if it is still ``raw``: True for the one caller that wrote it."""
    new = json.dumps(value, sort_keys=True)
    if raw is None:
        return store.add_mark(lane, PREFIX + harness, ROUTE_KEY, new)
    return store.compare_mark(lane, PREFIX + harness, ROUTE_KEY, raw, new)


def routes(store: Store, lane: str) -> dict[str, tuple[str, dict]]:
    """Every harness the lane keeps a route record for: harness -> (raw, value)."""
    out = {}
    for task in store.marked(lane, ROUTE_KEY):
        if task.startswith(PREFIX):
            harness = task.removeprefix(PREFIX)
            out[harness] = route(store, lane, harness)
    return out


def may_move(record: dict, since: str, now: dt.datetime | None = None) -> bool:
    """The lane's routing may be asked to move it off the held harness for the generation ``since``: once per
    generation, again only after the lane came back (``returned``) and was held again; never while its call or
    its probe is out. A call or probe the Runner lost (a restart mid-call, a probe launch that ended neither
    refused nor started) ages out: ``calling`` after CALL_STALE_SECONDS, ``probing`` after one probe interval,
    or the lane would sit held, its fallback unused, for the rest of the hold."""
    if record.get("since") != since or record.get("state") == RETURNED:
        return True
    limit = {CALLING: CALL_STALE_SECONDS, PROBING: PROBE_MINUTES * 60}.get(record.get("state"))
    return limit is not None and _age(str(record.get("at") or ""), _now(now)) > limit


def status(store: Store, now: dt.datetime | None = None) -> list[dict]:
    out = []
    for task in sorted(store.marked(OWNER, KEY)):
        if not task.startswith(PREFIX):
            continue
        harness = task.removeprefix(PREFIX)
        hold = active(store, harness, now)
        if hold:
            out.append({"harness": harness, "since": hold.get("since", ""),
                        "configurations": len(hold.get("fps") or {}),
                        "reasons": [r.get("excerpt", "") for r in (hold.get("reasons") or {}).values()],
                        "lanes": {lane: _route_summary(raw) for lane, raw
                                  in sorted(store.mark_holders(PREFIX + harness, ROUTE_KEY).items())},
                        "lifts": "when the launch configuration (model, flags) changes, or after a probe "
                                 f"launch (one every {PROBE_MINUTES:g} min) gets a conversation"})
    return out


def _route_summary(raw: str) -> str:
    try:
        value = json.loads(raw)
    except ValueError:
        return "unreadable"
    if not isinstance(value, dict):
        return "unreadable"
    return f"{value.get('state', '')} since {value.get('since', '')}".strip()
