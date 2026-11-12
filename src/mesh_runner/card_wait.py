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
    if resolution == "met":
        return True, "Mesh resolved the awaited card condition as met"
    if resolution == "invalid" and receipt.get("resolve_cause") == "grant_revoked":
        return False, "Mesh invalidated the card wait because workspace membership was revoked"
    if resolution in {"invalid", "cancelled", "superseded", "expired"}:
        return False, f"Mesh ended the source card wait as {resolution}; review the next step"
    raise ValueError("card wait receipt has an unknown resolution")
