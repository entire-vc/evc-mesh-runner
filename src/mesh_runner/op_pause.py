"""Operation-scoped pause tokens.

An operation (drain before a config change, a pool migration) must pause an agent without ever
touching the shared marker ``<state_dir>/pause/<Agent>``: that name has uncoordinated writers, and
no syscall can make "remove it" conditional on who wrote it. Instead each operation owns one file
whose name is unique to it:

    <state_dir>/pause.d/<Agent>/<token>     pauses one agent
    <state_dir>/pause.d/_all/<token>        pauses every agent of this runner

Taking a token is one ``O_CREAT|O_EXCL`` of the final name (no temp file, so a SIGKILL leaves either
nothing or a complete token). Releasing unlinks only that token. A token left behind by a crash keeps
the agent paused (fail closed) and is listed in status and metrics until its owner removes it.
Any agent is paused while at least one token exists, in addition to config ``paused`` and the
shared marker, whose behaviour is unchanged.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

DIRNAME = "pause.d"
ALL = "_all"
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _check(name: str, what: str) -> str:
    if not _TOKEN.match(name or "") or name in (".", ".."):
        raise ValueError(f"invalid pause {what}: {name!r}")
    return name


def _dir(state_dir: Path, agent: str) -> Path:
    return Path(state_dir) / DIRNAME / (agent if agent == ALL else _check(agent, "agent"))


def tokens(state_dir: Path, agent: str) -> list[str]:
    """Live tokens pausing ``agent``: its own namespace and the host-wide one, sorted, as
    ``<Agent>/<token>`` / ``_all/<token>``. Any unreadable namespace counts as paused (fail closed)."""
    out: list[str] = []
    for ns in (agent, ALL):
        try:
            d = _dir(state_dir, ns)
        except ValueError:
            continue
        try:
            names = os.listdir(d)
        except FileNotFoundError:
            continue
        except OSError:
            out.append(f"{ns}/<unreadable>")
            continue
        out += [f"{ns}/{n}" for n in names]
    return sorted(out)


def any_token(state_dir: Path, agent: str) -> bool:
    return bool(tokens(state_dir, agent))


def take(state_dir: Path, agent: str, token: str) -> Path:
    """Create this operation's token. Idempotent for the same token; touches no shared name."""
    p = _dir(state_dir, agent) / _check(token, "token")
    for _ in range(5):
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.close(os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
        except FileExistsError:
            pass
        except FileNotFoundError:     # the namespace vanished between mkdir and open: recreate it
            continue
        return p
    raise OSError(f"could not create pause token {p}")


def release(state_dir: Path, agent: str, token: str) -> bool:
    """Remove only this operation's token. True if it existed."""
    p = _dir(state_dir, agent) / _check(token, "token")
    try:
        p.unlink()
    except FileNotFoundError:
        return False
    return True                   # the namespace directory stays: removing it would race with take()
