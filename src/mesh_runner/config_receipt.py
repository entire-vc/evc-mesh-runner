"""Producer receipt of the config the running Runner actually holds (status.json ``receipt``).

The digest is taken over a stable serialization of the in-memory ``RunnerConfig`` (resolved
defaults included), not over the file on disk: a config edited after start keeps the receipt of
the actual cfg until a supported restart/reload swaps ``Runner.cfg``. Only dataclass fields that
the config declares ``repr=False`` (keys, tokens, env containers) are left out, plus any field
whose name looks like a secret; their absence is part of the scope, never their value.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import time
from enum import Enum
from pathlib import Path

from . import RUNNER_NAME, __version__

SCHEMA = 1
SCOPE = "RunnerConfig dataclass fields with repr=True, resolved defaults, secret-named fields dropped"
_SECRET_NAME = re.compile(r"(token|secret|password|passwd|credential|api_?key|_key$|^key$|basic_auth)", re.I)
_STARTED_AT = time.time()


def _plain(v):
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return {f.name: _plain(getattr(v, f.name)) for f in dataclasses.fields(v)
                if f.repr and not _SECRET_NAME.search(f.name)}
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in sorted(v.items(), key=lambda kv: str(kv[0]))
                if not _SECRET_NAME.search(str(k))}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, (set, frozenset)):
        return sorted((_plain(x) for x in v), key=repr)
    if isinstance(v, Path):
        return str(v)
    if isinstance(v, Enum):
        return v.value
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    return f"<{type(v).__name__}>"      # opaque objects (compiled regex, handles): type only


def serialize(cfg) -> str:
    return json.dumps(_plain(cfg), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(cfg) -> str:
    return hashlib.sha256(serialize(cfg).encode()).hexdigest()


def build(cfg, status_unix: float) -> dict:
    """Receipt for the cfg held in memory right now; raises on a serialization failure."""
    return {"schema": SCHEMA, "pid": os.getpid(), "release": __version__, "runner": RUNNER_NAME,
            "started_unix": _STARTED_AT, "source": str(Path(__file__).resolve().parent),
            "status_unix": status_unix,
            "cfg_sha256": digest(cfg), "scope": SCOPE}
