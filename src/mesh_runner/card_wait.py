"""Server-observed card waits use only the receipt on their source card."""
from __future__ import annotations


def receipt_event(wait: dict, receipt: dict, task_id: str, agent_id: str) -> tuple[bool, str]:
    """Validate receipt identity before consuming its resolution. No target state is needed."""
    if (receipt.get("id") != wait.get("mesh_wait_id")
            or receipt.get("task_id") != task_id or receipt.get("agent_id") != agent_id
            or receipt.get("kind") != "card" or receipt.get("server_observed") is not True):
        raise ValueError("card wait receipt does not match the source wait")
    if not receipt.get("resolved_at"):
        return False, ""
    resolution = receipt.get("resolution")
    if resolution == "met" and receipt.get("released_by") == "closure_without_evidence":
        return True, closure_without_result(receipt)
    if resolution == "met":
        return True, "Mesh resolved the awaited card condition as met"
    if resolution == "invalid" and receipt.get("resolve_cause") == "grant_revoked":
        return False, "Mesh invalidated the card wait because workspace membership was revoked"
    if resolution in {"invalid", "cancelled", "superseded", "expired"}:
        return False, f"Mesh ended the source card wait as {resolution}; review the next step"
    raise ValueError("card wait receipt has an unknown resolution")


def closure_without_result(receipt: dict) -> str:
    """Wake note for an ``artifact_published`` wait that Mesh released because the target card closed,
    not because the named result was attested on it (``released_by=closure_without_evidence``).
    The closure is no delivery: the note names the card, its final status, who closed it and the
    missing result, so the agent reads the closing comment instead of assuming the result exists."""
    target = str(receipt.get("target_task_id") or "")[:8] or "?"
    status = str(receipt.get("target_status") or receipt.get("target_status_category") or "status unknown")
    closer = str(receipt.get("target_closed_by") or "an unknown actor")
    when = receipt.get("target_closed_at")
    criterion = " ".join(str(receipt.get("criterion") or "").split()) or "(no criterion recorded)"
    return (f"card #{target} closed ({status}, by {closer}{f', at {when}' if when else ''}) without the named "
            f"result «{criterion}». The closure released your wait; it is not that result. Read its closing "
            "comment; if the result is not there, ask its owner (ask_agent) or wait on the card that will "
            "produce it.")
