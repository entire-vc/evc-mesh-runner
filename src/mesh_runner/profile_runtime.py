"""The lane's Mesh profile follows the runtime its turns actually ran on.

The team directory's ``agent_type``/``model`` is what review routing and reviewers read to tell model
families apart. A lane moved to another harness (an operator switch, a quota fallback to a Codex
profile) kept announcing the family it was registered with, so a "cross-family" reviewer could be the
same family and a correct review could be blocked as same-family.

After every settled turn on the lane's own route the Runner compares the harness and the model that
harness itself recorded (Claude's stream init, Codex's turn_context) with the profile and sends only
what differs. A configured default is never presented as observed: a turn without an observed model
reports the family change alone and clears the model (Kimi and Z Code record none), or nothing when the
family is unchanged. A card routed to another harness by itself (label, provider refusal) leaves the
lane's entry alone; its per-attempt truth stays in the execution observations."""
from __future__ import annotations

# the runner's harness names → Mesh's agent_type enum (a harness Mesh has no value for is ``custom``)
AGENT_TYPE = {"claude": "claude_code", "codex": "codex"}
MODEL_MAX = 128                                  # Mesh: agents.model width


def agent_type(harness: str) -> str:
    return AGENT_TYPE.get(str(harness or ""), "custom")


def profile_patch(me: dict | None, harness: str, model: str | None) -> dict | None:
    """The agent_type/model report that brings ``me`` to the observed runtime, or None when it already
    shows it or nothing was observed."""
    model = str(model or "").strip()
    if len(model) > MODEL_MAX:
        return None
    me = me or {}
    want = {"agent_type": agent_type(harness), "model": model}
    if not model:                                # nothing observed: the family alone, never an old model
        if str(me.get("agent_type") or "") == want["agent_type"]:
            return None
        return want
    diff = {k: v for k, v in want.items() if str(me.get(k) or "") != v}
    return diff or None
