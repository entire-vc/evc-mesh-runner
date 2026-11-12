"""One explicit operator signal per lane incident, repeated while it lasts.

An authentication hold or a harness the provider no longer accepts stops lanes on the
whole host. Each lane meets it on every admission tick, so a log line per occurrence is
noise nobody reads.
Here an incident is one ``key`` with a ``since`` generation: the first occurrence signals,
then once per ``REPEAT_HOURS`` while the same generation still holds. Agents run in
threads over one SQLite store; the slot is claimed with a single statement, so two lanes
meeting the same incident at once signal it once.

The signal is one ``operator_alert`` line in events.jsonl (structured fields plus an English
text) and one warning; status.json carries the incident while it lasts. Delivery to a person
is outside the runner (the host's events bridge), so the runner stays policy-free and grows
no [runner] option.
"""
from __future__ import annotations

import datetime as dt
import logging

from .store import Store

log = logging.getLogger("mesh_runner")

OWNER = "_runtime"
PREFIX = "_operator_alert_"
KEY = "sent"
REPEAT_HOURS = 6.0


def _parse(at: str) -> dt.datetime | None:
    try:
        t = dt.datetime.fromisoformat(at.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def slot(since: str, now: dt.datetime, repeat_hours: float = REPEAT_HOURS) -> str:
    """The incident's signal slot: ``<since>#0`` at once, ``#1`` after repeat_hours, ..."""
    began = _parse(since)
    n = 0 if began is None else max(0, int((now - began).total_seconds() // (repeat_hours * 3600)))
    return f"{since}#{n}"


def claim(store: Store, key: str, since: str, now: dt.datetime | None = None,
          repeat_hours: float = REPEAT_HOURS) -> str:
    """The slot this caller must signal, or "" when it was already signalled."""
    value = slot(since, now or dt.datetime.now(dt.timezone.utc), repeat_hours)
    return value if store.claim(OWNER, PREFIX + key, KEY, value) else ""


def signal(store: Store, key: str, since: str, text: str, emit=None,
           now: dt.datetime | None = None, **fields) -> bool:
    """Signal ``text`` once for this slot of the incident. True when this caller signalled."""
    got = claim(store, key, since, now)
    if not got:
        return False
    log.warning("operator alert [%s]: %s", key, text)
    if emit is not None:
        emit("operator_alert", None, alert=key, slot=got, since=since, text=text[:500], **fields)
    return True
