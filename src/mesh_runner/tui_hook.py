"""Hook target of an interactive session: appends the harness's event to a file.

Claude Code runs it as a command hook (the event as JSON on stdin); Codex runs it as
its ``notify`` program (the event as JSON in the last argument). One JSON object per
line, appended with O_APPEND, so concurrent hooks never interleave a line.

    python3 tui_hook.py <events-file> [<event json>]
"""
from __future__ import annotations

import json
import os
import sys
import time


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return 0
    path = argv[1]
    raw = argv[2] if len(argv) > 2 else sys.stdin.read()
    try:
        event = json.loads(raw)
        if not isinstance(event, dict):
            event = {"value": event}
    except ValueError:
        event = {"raw": raw[:2000]}
    event["_at"] = time.time()
    line = (json.dumps(event, ensure_ascii=False) + "\n").encode()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError:
        return 0                    # a hook must never break the session
    try:
        os.write(fd, line)
    finally:
        os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
