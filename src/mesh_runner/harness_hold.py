"""A harness CLI the provider refuses as outdated holds every lane that launches it.

The provider once answered every Codex turn with HTTP 400 "The '<model>' model
requires a newer version of Codex. Please upgrade to the latest app or CLI" until the
operator upgraded the CLI. The turn ended in a second with no reply; the runner took it
for a turn without an outcome, spent the cards' miss counters and sent them back to the
queue. The refusal is a fault of the host's binary, not of the
card: the lane is held, the card keeps its counters, the operator is told once.

The hold names the binary it was observed with and that binary's file identity (resolved
path, size, mtime). Upgrading the CLI changes it and lifts the hold by itself. Without a
change the hold still lets one turn through every ``RETRY_HOURS`` (the provider may have
reverted); a refused probe opens a new generation and signals again.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil

from .store import Store, iso

OWNER = "_runtime"
PREFIX = "_harness_upgrade_"
KEY = "hold"
RETRY_HOURS = 6.0

# The provider's typed refusal text, matched only on a typed error or the runner's own
# no-reply record of one (never assistant prose).
UPGRADE_REQUIRED = re.compile(
    r"requires a newer version of (?:Codex|Claude Code)"
    r"|please upgrade to the latest (?:app or )?(?:CLI|version)", re.I)


def is_upgrade_required(text: str) -> bool:
    return bool(text) and bool(UPGRADE_REQUIRED.search(text))


_SENTENCE = re.compile(r"[^\"{}\n]{0,160}?(?:requires a newer version of|please upgrade to the latest)[^\"{}\n]{0,160}", re.I)


def reason(text: str) -> str:
    """Only the provider's refusal sentence (no request ids, paths or other turn text)."""
    got = _SENTENCE.search(text or "")
    return got.group(0).strip()[:300] if got else "the provider refused this CLI version"


def identity(binary: str) -> dict:
    """Resolved path, size and mtime of the binary a lane launches; {} when it cannot be read."""
    found = shutil.which(binary) if binary and os.sep not in binary else binary
    if not found:
        return {}
    try:
        real = os.path.realpath(found)
        st = os.stat(real)
    except OSError:
        return {}
    return {"path": real, "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _read(store: Store, harness: str) -> dict:
    row = store.mark(OWNER, PREFIX + harness, KEY)
    try:
        value = json.loads(row[0]) if row and row[0] else {}
    except ValueError:
        value = {}
    return value if isinstance(value, dict) else {}


def hold(store: Store, harness: str, binary: str, reason: str) -> tuple[dict, bool]:
    """Record the refusal. Returns (hold, new): ``new`` only for the caller that opened a
    generation, so many lanes refused at once open (and signal) one incident."""
    current = active(store, harness, binary)
    if current:
        return current, False
    value = {"since": iso(), "harness": harness, "binary": identity(binary), "reason": reason[:300]}
    previous = store.mark(OWNER, PREFIX + harness, KEY)
    raw = json.dumps(value, sort_keys=True)
    if previous is None:
        new = store.add_mark(OWNER, PREFIX + harness, KEY, raw)
    else:
        new = store.compare_mark(OWNER, PREFIX + harness, KEY, previous[0], raw)
    return (value, True) if new else (_read(store, harness), False)


def active(store: Store, harness: str, binary: str, now: dt.datetime | None = None) -> dict:
    """The hold that applies to a launch of ``binary`` now, or {}.

    A different binary, a changed binary (upgraded) or a hold older than RETRY_HOURS does
    not apply; the record stays for status until it is replaced or cleared."""
    value = _read(store, harness)
    if not value.get("since"):
        return {}
    seen = value.get("binary") or {}
    if seen and identity(binary) != seen:
        return {}
    try:
        since = dt.datetime.fromisoformat(value["since"])
    except ValueError:
        return {}
    if ((now or dt.datetime.now(dt.timezone.utc)) - since).total_seconds() >= RETRY_HOURS * 3600:
        return {}
    return value


def clear(store: Store, harness: str) -> bool:
    row = store.mark(OWNER, PREFIX + harness, KEY)
    if not row or not row[0]:
        return False
    return store.compare_mark(OWNER, PREFIX + harness, KEY, row[0], "")


def status(store: Store) -> list[dict]:
    out = []
    for task, raw in sorted(store.marked(OWNER, KEY).items()):
        if not task.startswith(PREFIX):
            continue
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        if isinstance(value, dict):
            out.append({"harness": task.removeprefix(PREFIX), "since": value.get("since", ""),
                        "binary": (value.get("binary") or {}).get("path", ""),
                        "reason": value.get("reason", ""),
                        "lifts": "when the binary changes (upgrade), or one retry every "
                                 f"{RETRY_HOURS:g} h"})
    return out
