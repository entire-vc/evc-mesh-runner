"""Operator resolution of an attempt whose prompt delivery cannot be proven.

An attempt in ``unknown`` keeps its card, checkout and worktrees until native evidence settles the
delivery (see ``Runner._reconcile_interactive_prompt``). When that evidence can never appear (the
provider refused the harness, the session is gone), nothing ends the attempt and the card stands with it.
The operator ends it with one file:

    <state_dir>/resolve.d/<attempt id>        first line: who decided and why

The runner that owns the attempt reads the token on its next pass, ends the attempt the ordinary way
(checkout, gate and worktrees released), records the reason on the attempt and on the card, keeps the
unconsumed intent in a mark and removes the token. The card then starts in a new attempt. A token never
ends an attempt whose process group is still alive, and never one that is not in ``unknown``: there it
stays until it applies or the operator removes it.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

DIRNAME = "resolve.d"
MAX_REASON = 500
_ATTEMPT = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def path(state_dir: Path, attempt_id: str) -> Path:
    if not _ATTEMPT.match(attempt_id or ""):
        raise ValueError(f"invalid attempt id: {attempt_id!r}")
    return Path(state_dir) / DIRNAME / attempt_id


def read(state_dir: Path, attempt_id: str) -> str | None:
    """The operator's reason, or None when no usable token exists. Only a regular file with a reason is
    a decision: a symlink, a directory, an unreadable or empty file ends nothing."""
    try:
        token = path(state_dir, attempt_id)
        fd = os.open(token, os.O_RDONLY | os.O_NOFOLLOW)
    except (OSError, ValueError):
        return None
    try:
        with os.fdopen(fd, "r", errors="replace") as fh:
            if not os.path.isfile(token):
                return None
            line = fh.readline(MAX_REASON * 4)
    except OSError:
        return None
    reason = " ".join(line.split())[:MAX_REASON]
    return reason or None


def consume(state_dir: Path, attempt_id: str) -> None:
    try:
        path(state_dir, attempt_id).unlink()
    except (OSError, ValueError):
        pass


def pending(state_dir: Path) -> list[str]:
    """Attempt ids with a token, for status: a token that stays listed has not applied."""
    try:
        return sorted(n for n in os.listdir(Path(state_dir) / DIRNAME) if _ATTEMPT.match(n))
    except OSError:
        return []
