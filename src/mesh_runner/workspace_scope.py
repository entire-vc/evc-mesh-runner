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


def assignment_problem(task: dict, *, lane: str, workspace_id: str, mode: str = "work") -> dict | None:
    """Explain configured scope; metadata-only admission never expands identity.

    A missing task workspace is unknown, not a reason to route across workspaces.
    Which projects a lane serves is Mesh's membership, not a lane option: the per-lane
    ``project_ids`` allow-list silently held cards Mesh admitted, and was removed.
    The caller still applies every ordinary gate after this check succeeds.
    """
    workspace = task.get("workspace_id")
    project = task.get("project_id")
    common = {"lane": lane, "workspace_id": workspace_id, "task_workspace_id": workspace,
              "project_id": project,
              "requested_scope": "metadata-only" if metadata_only(task, mode) else "workspace",
              "next_actor": "operator", "eligible_owner": None}
    if workspace and workspace_id and workspace != workspace_id:
        return dict(common, reason="workspace_scope_mismatch", required_delta=None,
                    action="route the existing card to an admitted owner in its workspace; "
                           "no cross-workspace identity or grant is inferred")
    return None


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
