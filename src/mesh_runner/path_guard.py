"""Claude Code ``PreToolUse`` hook: refuse a tool call that names a working copy the session no longer owns.

    python3 path_guard.py <moves.json>

When a suspended attempt is restored into a different slot, the runner keeps the native session (a fresh
start costs the agent its whole orientation) but the session's history is full of the OLD slot path, and
that path may now hold another attempt's work. The agents run with ``--dangerously-skip-permissions``, so
nothing but this hook stands between a remembered path and a write into someone else's tree. The runner
keeps the session only when this hook is installed for the launch (see ``runner._path_guard_ready``).

The mapping file is ``{stale path: current path}``. Claude Code passes the call as JSON on stdin
(``tool_name``, ``tool_input``); every string in ``tool_input`` is searched. Exit 2 refuses the call and
stderr is shown to the model; exit 0 lets it run.

Failure policy: no mapping file, or input that is not a tool call, lets the call run (the guard must not
stop a session over its own plumbing). A mapping file that exists but cannot be read refuses every call:
the session was kept only because this guard exists, so a corrupt map must not silently disable it.

Stdlib only: Claude Code runs it as a plain script, outside the runner's package.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# characters that continue a path component: ``slot-1`` followed by one of them is another name (``slot-10``,
# ``slot-1.bak``), followed by anything else (end, ``/``, a quote, whitespace, ``:``...) it is that path
_PATH_CHAR = r"[\w.\-]"


class CorruptMap(ValueError):
    pass


def moves_path(directory: Path, key: str) -> Path:
    """Where the driver of window ``key`` finds the session's map (next to its other per-window files)."""
    return Path(directory) / f"{key}.moves.json"


def parse(text: str) -> dict[str, str]:
    """The map in ``text``; CorruptMap unless it is an object of absolute paths to absolute paths."""
    try:
        data = json.loads(text)
    except ValueError as e:
        raise CorruptMap(f"not JSON: {e}") from None
    if not isinstance(data, dict):
        raise CorruptMap("not an object")
    for old, new in data.items():
        if not (isinstance(old, str) and isinstance(new, str)
                and old.startswith("/") and new.startswith("/") and old.rstrip("/")):
            raise CorruptMap(f"bad entry {old!r}")
    return data


def read(path: Path) -> dict[str, str] | None:
    """The map at ``path``; None when there is no file; CorruptMap when it exists but cannot be used."""
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        raise CorruptMap(f"unreadable: {e}") from None
    return parse(text)


def write(path: Path, mapping: dict[str, str]) -> None:
    write_text(path, json.dumps(mapping, sort_keys=True))


def write_text(path: Path, text: str) -> None:
    """Replace the map atomically: a hook never reads half a file (it would refuse every call)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def chain(previous: dict[str, str], moves: dict[str, str], current) -> dict[str, str]:
    """The map after one more restore. ``previous`` is what the session was already told, ``moves`` this
    restore's ``{old: new}``, ``current`` every path the attempt holds now. An earlier stale path follows
    its target's move (A->B then B->C: A and B both lead to C); a path the attempt holds again is not stale."""
    out = {old: moves.get(new, new) for old, new in previous.items()}
    out.update(moves)
    current = set(current)
    return {old: new for old, new in out.items() if old not in current and old != new}


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def stale_hits(mapping: dict[str, str], tool_input) -> list[tuple[str, str]]:
    """``(stale, new)`` for every stale path some string of ``tool_input`` names, as the path itself or
    as the prefix of a path under it."""
    patterns = [(old, new, re.compile(re.escape(old.rstrip("/")) + rf"(?!{_PATH_CHAR})"))
                for old, new in sorted(mapping.items(), key=lambda kv: -len(kv[0]))]
    hits = []
    for text in _strings(tool_input):
        for old, new, pattern in patterns:
            if (old, new) not in hits and pattern.search(text):
                hits.append((old, new))
    return hits


def message(hits: list[tuple[str, str]]) -> str:
    return "\n".join(f"This working copy moved: {old} is now {new}. Use the new path; "
                     "the old one belongs to other work." for old, new in hits)


def main(argv: list[str], stdin=None, stderr=None) -> int:
    stdin, stderr = stdin or sys.stdin, stderr or sys.stderr
    if len(argv) < 2:
        return 0
    try:
        mapping = read(Path(argv[1]))
    except CorruptMap as e:
        stderr.write(f"Path guard: the map of moved working copies ({argv[1]}) cannot be read ({e}); "
                     "every tool call is refused until the runner rewrites it.\n")
        return 2
    if not mapping:
        return 0
    try:
        event = json.loads(stdin.read())
    except ValueError:
        return 0
    if not isinstance(event, dict):
        return 0
    hits = stale_hits(mapping, event.get("tool_input"))
    if not hits:
        return 0
    stderr.write(message(hits) + "\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
