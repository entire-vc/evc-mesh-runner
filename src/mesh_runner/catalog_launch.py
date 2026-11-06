"""Actual-launch evidence for the native catalog reader (read-only).

Consumes the reviewed per-account cache written by the host's launch recorder and the independent
enrollment (binding) file. Neither grants capacity: they become ordinary quota records that go through
the same thresholds, hysteresis and partial-window rules as every other observation, and the gate still
decides. An enrolled account without valid, fresh evidence is unknown (never a fallback to another
source); accounts that are not enrolled keep their ordinary records.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path

from . import launch_probe

DEFAULT_BINDINGS = "~/.fiddler/runtime-catalog/codex-launch-bindings.json"
DEFAULT_CACHE = "~/.fiddler/runtime-controller/codex-launch-observations.json"
MAX_FILE = 131072
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_REF = re.compile(r"codex/[a-zA-Z0-9_.-]{1,80}\Z")
_FIELDS = ("status", "reason", "account_ref", "quota_bucket", "observed_at", "cleanup_ok",
           "primary_used_percent", "secondary_used_percent")
_FIELDS_V1 = _FIELDS[:6]
_STATUS_REASON = {"available": "available", "quota_blocked": "quota_blocked"}


def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def private_json(path):
    """A bounded JSON object from a private regular file owned by us; anything else is None."""
    try:
        fd = os.open(os.path.expanduser(str(path)), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as fh:
            info = os.fstat(fh.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > MAX_FILE):
                return None
            raw = fh.read(MAX_FILE + 1)
        if len(raw) > MAX_FILE:
            return None
        doc = json.loads(raw, object_pairs_hook=_unique)
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError, RecursionError):
        return None


def bindings(path) -> dict:
    doc = private_json(path) if path else None
    if (doc is None or set(doc) != {"version", "accounts"} or type(doc["version"]) is not int
            or doc["version"] != 1 or type(doc["accounts"]) is not dict or len(doc["accounts"]) > 64):
        return {}
    if any(type(k) is not str or not _REF.fullmatch(k) or type(v) is not str or not _HASH.fullmatch(v)
           for k, v in doc["accounts"].items()):
        return {}
    return doc["accounts"]


def binding_digest(catalog: dict, ref: str, expected: str) -> str:
    return hashlib.sha256(json.dumps(
        {"account": (catalog.get("accounts") or {}).get(ref), "provider_ref": expected},
        sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _iso(epoch) -> str:
    return datetime.fromtimestamp(float(epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(ref, pool, window, status, observed, max_age, *, fraction=None, error=None):
    known = status in ("available", "exhausted")
    return {"provider": "openai", "account_ref": ref, "pool_id": pool, "catalog_pool": pool,
            "unit": "percent", "window": window, "status": status,
            "used_fraction": round(fraction, 4) if known and fraction is not None else None,
            "remaining": None, "reset_at": None, "observed_at": _iso(observed),
            "source": "codex_app_server", "confidence": "high" if known else "none",
            "max_age": int(max_age), "error_kind": error}


def _unknown_rows(ref, account, now, max_age):
    pools = account.get("pools")
    return [_row(ref, pool, "unknown", "unknown", now, max_age, error="launch_evidence_unavailable")
            for pool in (pools if isinstance(pools, dict) else {})]


def _int(value) -> bool:
    return type(value) is int


def _records_for(snapshot, catalog, ref, expected, now, max_age) -> list:
    """Raises ValueError unless the snapshot is a complete, bound, fresh, clean measurement."""
    if type(snapshot) is not dict or set(snapshot) != set(_FIELDS):
        raise ValueError("fields")
    status, reason = snapshot["status"], snapshot["reason"]
    if (status not in _STATUS_REASON or reason != _STATUS_REASON[status] or reason not in launch_probe.REASONS
            or snapshot["account_ref"] != expected or snapshot["quota_bucket"] != f"openai:{expected}:codex"
            or snapshot["cleanup_ok"] is not True):
        raise ValueError("binding")
    observed = snapshot["observed_at"]
    if not _int(observed) or not 0 <= now - observed <= max_age:
        raise ValueError("fresh")
    slots = ("primary", "secondary")
    pct = {s: snapshot[f"{s}_used_percent"] for s in slots}
    if any(p is not None and (not _int(p) or not 0 <= p <= 100) for p in pct.values()):
        raise ValueError("measurement")
    if status == "available" and 100 in pct.values():
        raise ValueError("inconsistent")
    account = (catalog.get("accounts") or {}).get(ref)
    pools = account.get("pools") if isinstance(account, dict) else None
    mode = (account.get("modes") or {}).get("subscription") if isinstance(account, dict) \
        and isinstance(account.get("modes"), dict) else None
    if (not isinstance(account, dict) or account.get("provider") != "openai" or account.get("harness") != "codex"
            or account.get("enabled") is not True or not isinstance(pools, dict) or len(pools) != 1
            or not isinstance(mode, dict) or mode.get("pools") != list(pools)
            or not isinstance(next(iter(pools.values())), dict)
            or next(iter(pools.values())).get("window") != "unknown"):
        raise ValueError("catalog subset")
    pool = next(iter(pools))
    blocked = status == "quota_blocked"
    any100 = 100 in pct.values()
    rows = []
    for slot in slots:
        p = pct[slot]
        if p is None:
            continue
        exhausted = blocked and (p == 100 or not any100)
        rows.append(_row(ref, pool, slot, "exhausted" if exhausted else "available", observed, max_age,
                         fraction=p / 100.0))
    if not rows:
        # no numbers at all: the helper's own verdict stands (quota_blocked = exhausted, never unknown)
        rows.append(_row(ref, pool, "unknown", "exhausted" if blocked else "available", observed, max_age))
    elif len(rows) < 2:
        # an absent slot stays unknown so the partial-window guard cannot read the other as a free account
        missing = [s for s in slots if pct[s] is None][0]
        rows.append(_row(ref, pool, missing, "unknown", observed, max_age, error="measurement_missing"))
    return rows


def launch_records(catalog: dict, now: float, bindings_path, cache_path, max_age) -> tuple[list, set]:
    """(records, overridden account refs). Never raises."""
    try:
        enrolled = bindings(bindings_path)
        cached = private_json(cache_path) if cache_path else None
        entries = cached.get("accounts") if cached and cached.get("version") == 1 else None
        entries = entries if isinstance(entries, dict) else {}
        accounts = catalog.get("accounts") if isinstance(catalog, dict) else None
        accounts = accounts if isinstance(accounts, dict) else {}
    except Exception:
        return [], set()
    result, overrides = [], set()
    if not (isinstance(max_age, (int, float)) and math.isfinite(max_age) and max_age > 0):
        max_age = 1800
    for ref, expected in enrolled.items():
        account = accounts.get(ref)
        if not isinstance(account, dict) or account.get("provider") != "openai" or account.get("harness") != "codex":
            continue
        overrides.add(ref)
        try:
            entry = entries[ref]
            if entry["binding"] != binding_digest(catalog, ref, expected):
                raise ValueError("binding digest")
            result += _records_for(entry["observation"], catalog, ref, expected, now, max_age)
        except Exception:
            result += _unknown_rows(ref, account, now, max_age)
    return result, overrides
