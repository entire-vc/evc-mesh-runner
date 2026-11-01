"""Local circuit holds for a harness profile whose authentication failed.

This is not an account health authority or a rotation policy. Only the operator
clears a hold after repairing authentication; normal admission still applies.
No credential file or keychain is read here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from pathlib import Path

from .store import Store, iso

OWNER = "_runtime"
KEY = "auth_hold"
PREFIX = "_auth_profile_"
PROFILE = re.compile(r"[0-9a-f]{32}")
AUTH_BANNER = re.compile(
    r"(?:Failed to authenticate:\s*OAuth session expired and could not be refreshed"
    r"|Not logged in\s*[·.—-]?\s*Please run /login[.!]?)", re.I)


def is_auth_banner(text: str) -> bool:
    # A quoted report, an ordinary 401 or assistant prose is not a CLI auth banner.
    return bool(AUTH_BANNER.fullmatch(text.strip()))


def is_verification_required(text: str) -> bool:
    # The provider's whole terminal reason, nothing else: quoted prose or a longer message is not it.
    return text.strip() == "verification_required"


def profile_id(env: dict[str, str], cwd: Path) -> str:
    """Fingerprint the Claude profile actually passed to spawn, across lanes.

    Model and lane are deliberately absent. Credential values never participate;
    reauth in the same keychain/config dir must not silently lift a hold.
    """
    home = Path(env.get("HOME") or Path.home())
    directory = Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    directory = directory if directory.is_absolute() else cwd / directory
    kind = ("api" if env.get("ANTHROPIC_API_KEY") else
            "oauth-env" if env.get("CLAUDE_CODE_OAUTH_TOKEN") else "native")
    identity = ["claude", os.path.realpath(directory), kind, env.get("ANTHROPIC_BASE_URL", "")]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:32]


def hold(store: Store, profile: str) -> None:
    store.mark(OWNER, PREFIX + profile, KEY, iso())


def held(store: Store, profile: str) -> bool:
    got = store.mark(OWNER, PREFIX + profile, KEY)
    return bool(got and got[0])


def clear(store: Store, profile: str) -> bool:
    if not PROFILE.fullmatch(profile):
        raise ValueError("expected the exact profile fingerprint from status")
    cur = store.db.execute("UPDATE marks SET value='' WHERE agent=? AND task_id=? AND key=? AND value!=''",
                           (OWNER, PREFIX + profile, KEY))
    cleared = cur.rowcount == 1
    if cleared:
        store.db.execute("DELETE FROM marks WHERE task_id=? AND key='auth_route_attempt'", (PREFIX + profile,))
    return cleared


def status(store: Store, config_path: str = "<runner.toml>") -> list[dict]:
    return [{"profile": task.removeprefix(PREFIX), "reason": "authentication failed; repair auth then clear explicitly",
             "since": at, "clear_command": shlex.join(["mesh-runner", "clear-auth-hold", "--config", config_path,
                                                        "--profile", task.removeprefix(PREFIX)])}
            for task, at in sorted(store.marked(OWNER, KEY).items())
            if task.startswith(PREFIX) and PROFILE.fullmatch(task.removeprefix(PREFIX))]
