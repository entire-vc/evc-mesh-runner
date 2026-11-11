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
Any agent is paused while at least one token exists, or while config ``paused`` is set.

Tokens are the only file mechanism. The shared marker ``<state_dir>/pause/<Agent>`` of earlier releases is
turned into the token ``pause.d/<Agent>/legacy-pause-file`` once, when the runner starts (``migrate_legacy``);
only a marker whose token could not be written is still read, for that run (``legacy_paused``).
"""
from __future__ import annotations

import logging
import os
import re
import stat
from pathlib import Path

log = logging.getLogger("mesh_runner")

DIRNAME = "pause.d"
LEGACY_DIRNAME = "pause"                  # <state_dir>/pause/<Agent>: read only through migrate_legacy
LEGACY_TOKEN = "legacy-pause-file"
EVERY = "*"                               # in a fallback set: the legacy directory could not even be listed
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


def migrate_legacy(state_dir: Path, agents=()) -> set[str]:
    """Turn every regular file ``<state_dir>/pause/<name>`` into the token ``pause.d/<name>/legacy-pause-file``
    with the file's text, then remove the file: token first, so the lane is never unpaused in between. Run once
    by the runner that owns the state directory, before its first tick.

    Returns the fallback: names whose marker stays (the token could not be written, or the name cannot be a
    token) and is still honoured for this run; ``{EVERY}`` when the directory could not be listed. A name that
    is not one of ``agents`` is migrated as well (an agent added later finds it); a symlink or a directory is
    left alone. A second run finds nothing to do. One log line per entry handled."""
    legacy = Path(state_dir) / LEGACY_DIRNAME
    try:
        entries = sorted(os.scandir(legacy), key=lambda e: e.name)
    except FileNotFoundError:
        return set()
    except OSError as e:
        log.warning("pause: %s unreadable (%s); its files are honoured as before this run", legacy, type(e).__name__)
        return {EVERY}
    known = set(agents)
    fallback: set[str] = set()
    for entry in entries:
        name = entry.name
        try:
            mode = os.lstat(entry.path).st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(mode):
            log.warning("pause: %s is not a regular file; left alone and not read", entry.path)
            continue
        try:
            _migrate_one(Path(state_dir), entry.path, name)
        except (OSError, ValueError) as e:
            fallback.add(name)
            log.warning("pause: %s could not become a token (%s); the file stays and pauses %s this run",
                        entry.path, type(e).__name__, name)
            continue
        log.info("pause: %s migrated to %s/%s/%s%s", entry.path, DIRNAME, name, LEGACY_TOKEN,
                 "" if name in known or not known else " (not a configured agent)")
    return fallback


def _migrate_one(state_dir: Path, path: str, name: str) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as fh:
        text = fh.read(65536)
    token = _dir(state_dir, name) / LEGACY_TOKEN
    token.parent.mkdir(parents=True, exist_ok=True)
    try:
        out = os.open(token, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        pass                      # an earlier start made it and stopped before the unlink: the token holds
    else:
        with os.fdopen(out, "wb") as fh:
            fh.write(text)
    os.unlink(path)


def legacy_paused(state_dir: Path, name: str, fallback) -> bool:
    """The marker of a name whose migration failed this run; any other marker is no longer read."""
    if not fallback or (name not in fallback and EVERY not in fallback):
        return False
    return (Path(state_dir) / LEGACY_DIRNAME / name).exists()
