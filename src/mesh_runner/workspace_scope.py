"""Explicit per-card repository admission; this is not a tool sandbox.

The default is normal workspace admission. Typed decisions and separately
opted-in alert diagnostics may omit Git leases. Review and ambiguous requests
always retain normal admission.
"""
from __future__ import annotations

import json

METADATA_ONLY = "runner:metadata-only"
ALERT_DIAGNOSTICS = "runner:alert-diagnostics"
WORKSPACE_REQUIRED = "runner:workspace-required"
RECEIPT_KEY = "workspace_scope"


def recorded_scope(value: str | None, attempt_id: str) -> str:
    """Only this exact attempt can inherit either side of a scope transition."""
    try:
        data = json.loads(value) if isinstance(value, str) else None
    except ValueError:
        return ""
    if (isinstance(data, dict) and data.get("attempt") == attempt_id
            and data.get("scope") in ("metadata-only", "workspace")):
        return data["scope"]
    return ""


def metadata_receipt(value: str | None, attempt_id: str) -> bool:
    return recorded_scope(value, attempt_id) == "metadata-only"


def metadata_only(task: dict, mode: str = "work") -> bool:
    """Fail closed on malformed labels; never infer scope from task prose.

    This describes the *requested* scope of a new turn. Existing repository
    leases and checkpoint journals take precedence and must not be discarded
    just because a label changed.
    """
    labels = task.get("labels")
    if mode != "work" or not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
        return False
    if METADATA_ONLY not in labels or WORKSPACE_REQUIRED in labels:
        return False
    alert = bool({"alert", "auto-incident"}.intersection(labels))
    if ALERT_DIAGNOSTICS in labels:
        return alert and "decision-request" not in labels
    return "decision-request" in labels and not alert


def read_only(task: dict | None) -> bool:
    """Decision requests are confined by the harness; alert diagnostics need their tools.

    Fails closed: an unreadable or malformed task gets the sandbox.
    """
    labels = (task or {}).get("labels")
    return not (isinstance(labels, list) and ALERT_DIAGNOSTICS in labels)


def metadata_instructions(sandboxed: bool = True) -> str:
    if not sandboxed:
        return ("\n\n[runner] This typed request explicitly opts into metadata-only work. "
                "No Git worktrees are leased to this attempt. Keep your normal identity, profile, tools "
                "and permissions; use task metadata and existing diagnostic evidence. "
                "Do not modify source, configuration, running services, a shared repository or another "
                "attempt's checkout. If diagnosis requires repair or code work, add the "
                "runner:workspace-required label and end with ↻ NEXT before making those changes. "
                "The next turn must pass normal workspace admission and obtain its leased checkouts. "
                "This admission scope is not a tool sandbox.\n")
    return ("\n\n[runner] This typed decision request explicitly opts into metadata-only work. "
            "No Git worktrees are leased to this attempt and the harness runs it read-only: file "
            "edits and shell commands are refused. Decide using task metadata and existing "
            "evidence through the Mesh tools. If this task requires "
            "code work, add the runner:workspace-required label and end with ↻ NEXT. "
            "The next turn must pass normal workspace admission before editing code.\n")
