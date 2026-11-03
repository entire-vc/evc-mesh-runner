"""Bind headless native input acceptance to one durable launch intent.

Acceptance is delivery evidence, never a successful task outcome. Missing evidence
cannot prove non-delivery: CLI hooks can be disabled, time out or fail open.
"""
from __future__ import annotations

import hashlib
import json
import shlex
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mesh_runner import interactive


def receipt_key(key: str, token: str) -> str:
    return key + "." + token + ".native-accepted"


def claude_argv(argv: list[str], directory: Path, key: str, intent: dict) -> list[str]:
    command = shlex.join([sys.executable, str(Path(__file__).resolve()),
                          str(directory), key, intent['token'], intent['sha256'], intent['session_id']])
    settings_key = key + "." + intent['token'] + ".native-hooks"
    interactive.write_delivery(directory, settings_key, {"hooks": {"UserPromptSubmit": [
        {"hooks": [{"type": "command", "command": command}]}]}})
    return [*argv, "--settings", str(interactive.delivery_path(directory, settings_key))]


def accepted(directory: Path, key: str, intent: dict) -> bool:
    try:
        record = interactive.read_delivery(directory, receipt_key(key, intent['token']))
    except (OSError, ValueError):
        return False
    return record == {"version": 1, "token": intent['token'], "sha256": intent['sha256'],
                      "session_id": intent['session_id'], "hook_event_name": "UserPromptSubmit"}


def main(argv: list[str]) -> int:
    try:
        directory, key, token, digest, sid = Path(argv[1]), argv[2], argv[3], argv[4], argv[5]
        event = json.load(sys.stdin)
        journal = interactive.read_delivery(directory, key)
        if (journal.get('token') != token or journal.get('sha256') != digest
                or event.get('hook_event_name') != 'UserPromptSubmit'
                or not sid or event.get('session_id') != sid
                or not isinstance(event.get('prompt'), str)
                or hashlib.sha256(event['prompt'].encode()).hexdigest() != digest):
            return 2                     # native UserPromptSubmit rejects this input
        interactive.write_delivery(directory, receipt_key(key, token),
            {"version": 1, "token": token, "sha256": digest, "session_id": sid,
             "hook_event_name": "UserPromptSubmit"})
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return 2                         # no durable receipt, no hook authorization
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
