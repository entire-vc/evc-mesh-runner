"""Opt-in reviewer merge-only handoff.

Default stays "the author merges their own MR". This module adds an explicit request: a
separate merge-only card, assigned to the project's reviewer chosen by the stock router, that
the reviewer's own runner runs through ``WAIT merge:`` under the reviewer's fresh env. The
product card (assignee, checkout, deploy, live QA) is never touched.

``evaluate`` is pure: it turns facts into a plan or a named refusal. Anything unknown refuses.
``request`` reads the facts and, with ``apply``, creates the one card (stable key, no duplicates).
``check_identity`` is the gate-side guard: the token the gate would run under must be the
selected reviewer's GitLab user, else the merge does not start.
"""
from __future__ import annotations

import hashlib
import contextlib
import fcntl
import json
import os
import re
import subprocess
import urllib.request
from dataclasses import dataclass, field

LABEL = "merge-only"
MARKER = "reviewer-merge:v1"
BLOCK = re.compile(r"<!-- reviewer-merge:v1 (\{.*?\}) -->", re.S)
SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
TARGET = re.compile(r"(?P<project>[^\s!]+/[^\s!]+)!(?P<iid>[1-9][0-9]{0,11})")
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
SELECT_REASONS = ("cross-family", "same-family: quota")   # the router's only assignable reasons
CI_OK = ("success", "manual")                              # post-review jobs wait for the gate itself
DONE = ("done", "cancelled")


class Refused(Exception):
    """A named reason; the handoff does not happen."""


def request_key(source_task: str, project: str, iid: str | int, sha: str) -> str:
    return "rm-" + hashlib.sha256(f"{source_task}|{project}|{iid}|{sha}".encode()).hexdigest()[:12]


def parse_request(description: str | None) -> dict | None:
    """The machine block of a merge-only card, or None (an ordinary card, or a corrupt block)."""
    m = BLOCK.search(description or "")
    if not m:
        return None
    try:
        req = json.loads(m.group(1))
    except ValueError:
        return None
    if not isinstance(req, dict):
        return None
    text = lambda k: isinstance(req.get(k), str) and req[k] != ""       # noqa: E731
    if not (all(text(k) for k in ("reviewer_lane", "reviewer_username", "project", "iid", "sha", "key", "source_task"))
            and SHA.fullmatch(req["sha"]) and re.fullmatch(r"[1-9][0-9]{0,11}", req["iid"])
            and re.fullmatch(r"rm-[0-9a-f]{12}", req["key"]) and UUID.fullmatch(req["source_task"])
            and TARGET.fullmatch(f"{req['project']}!{req['iid']}")):
        return None
    return req


def run_selector(argv: list[str], *, project_id: str, author: str, timeout: float = 60) -> dict:
    """Run the configured router adapter: JSON ``{lane, reason, username, author_family}`` on stdout.

    ``{project_id}`` and ``{author}`` are substituted into argv (no shell). A failure or a malformed
    answer refuses; the adapter output is never echoed beyond its own ``reason``."""
    if not argv:
        raise Refused("reviewer-select-command-not-configured")
    argv = [p.replace("{project_id}", project_id).replace("{author}", author) for p in argv]
    try:
        res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        got = json.loads(res.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        raise Refused("reviewer-select-unreadable") from None
    if res.returncode != 0 or not isinstance(got, dict):
        raise Refused("reviewer-select-failed:" + str((got or {}).get("reason", res.returncode))[:80]
                      if isinstance(got, dict) else "reviewer-select-failed")
    return got


@dataclass
class Plan:
    ok: bool
    refusals: list[str] = field(default_factory=list)
    request: dict | None = None


def evaluate(*, source: dict, project: str, iid: str, sha: str, mr: dict, selection: dict,
             author: str) -> Plan:
    """Facts -> plan. ``source`` is the product card, ``mr`` the GitLab merge request row,
    ``selection`` the router adapter's answer, ``author`` the author lane name."""
    bad: list[str] = []
    if not SHA.fullmatch(sha or ""):
        bad.append("expected-sha-not-full")
    labels = {str(l.get("name") if isinstance(l, dict) else l).casefold() for l in source.get("labels") or []}
    labels |= {str(l.get("name") if isinstance(l, dict) else l).casefold() for l in mr.get("labels") or []}
    if source.get("status_category") in DONE or str(source.get("status") or "").casefold() in DONE:
        bad.append("source-task-closed")
    if source.get("human_gate") or source.get("human_gate_active"):
        bad.append("human-gate")
    if "hold" in labels:
        bad.append("hold-label")
    explicit = sorted(l for l in labels if l.startswith("verify:") and l.split(":", 1)[1].strip() != author.casefold())
    if explicit:
        bad.append("explicit-verify:" + ",".join(explicit))       # a named acceptor is not bypassed
    if mr.get("state") != "opened":
        bad.append(f"mr-not-open:{mr.get('state')}")
    elif str(mr.get("sha") or "") != sha:
        bad.append("head-drift:" + str(mr.get("sha") or "")[:12])
    if not any(l.startswith("verified-by:") and l != "verified-by:" for l in labels):
        bad.append("no-ship-label")
    pipe = (mr.get("head_pipeline") or {})
    if pipe.get("status") not in CI_OK:
        bad.append(f"ci:{pipe.get('status') or 'unknown'}")
    elif pipe.get("sha") and pipe["sha"] != sha:
        bad.append("ci-not-on-head")
    lane, reason = selection.get("lane"), selection.get("reason")
    if not isinstance(reason, str):
        reason = ""
    if (lane is not None and not isinstance(lane, str)) or not isinstance(selection.get("username", ""), str):
        bad.append("reviewer-select-malformed")             # a list or number is not a lane: nothing below runs on it
        lane = None
    elif not lane or reason not in SELECT_REASONS:
        bad.append("no-reviewer:" + (reason or "unknown")[:60])
    elif str(lane).casefold() == author.casefold():
        bad.append("reviewer-is-author")
    if lane and not selection.get("username"):
        bad.append("reviewer-username-unknown")
    if not isinstance(selection.get("author_family", ""), str):
        bad.append("reviewer-select-malformed")
    if bad:
        return Plan(False, bad)
    return Plan(True, [], {
        "key": request_key(source["id"], project, iid, sha), "source_task": source["id"],
        "project": project, "iid": str(iid), "sha": sha, "reviewer_lane": lane,
        "reviewer_username": selection["username"], "reason": reason,
        "author_lane": author, "author_family": selection.get("author_family") or ""})


def card_fields(req: dict, project_id: str) -> dict:
    target = f"{req['project']}!{req['iid']}"
    src8 = req["source_task"][:8]
    body = (
        f"Merge-only поручение reviewer'у по MR {target}; продуктовая карточка #{src8} остаётся у автора "
        f"({req['author_lane']}): checkout, deploy и live QA не меняются.\n\n"
        f"Reviewer: {req['reviewer_lane']} (GitLab `{req['reviewer_username']}`), выбран router: {req['reason']}.\n"
        f"Expected HEAD: `{req['sha']}`.\n\n"
        f"Сделай ровно одно: завершить ход строкой\n\n⏳ WAIT merge:{target} after={req['sha']}\n\n"
        "Runner запустит штатный gate под твоим fresh env и сверит GitLab /user с этой lane до мутации. "
        "Review заново не запускать. HEAD drift, hold, чужой receipt или красный CI — gate откажет; "
        "тогда прокомментируй причину и верни карточку автору.\n\n"
        f"<!-- reviewer-merge:v1 {json.dumps(req, sort_keys=True, separators=(',', ':'))} -->")
    return {"title": f"[merge-only] {target} @{req['sha'][:12]} ({req['key']})", "description": body,
            "priority": "urgent", "labels": [LABEL, "kind:ops", req["key"]], "status": "todo"}


def request(*, mesh, gitlab, source_id: str, target: str, sha: str, selector_argv: list[str],
            agent_ids: dict[str, str], usernames: dict[str, str] | None = None, apply: bool = False,
            lock_dir: str | None = None,
            selector=run_selector) -> dict:
    """Dry-run by default: reads only. ``apply`` creates (or finds) the one merge-only card."""
    m = TARGET.fullmatch(target or "")
    if not m or not UUID.fullmatch(source_id or ""):
        raise Refused("bad-arguments")
    project, iid = m["project"], m["iid"]
    source = mesh.task(source_id)
    source = source.get("task", source) if isinstance(source, dict) else {}
    if source.get("id") != source_id:
        raise Refused("source-task-unreadable")
    author = str(source.get("assignee_name") or "")
    if not author:
        raise Refused("author-unknown")
    mr = gitlab.get_mr(target)
    selection = selector(selector_argv, project_id=str(source.get("project_id") or ""), author=author)
    lane = selection.get("lane") if isinstance(selection.get("lane"), str) else ""
    known = {k.casefold(): v for k, v in (usernames or {}).items()}
    configured = known.get(lane.casefold()) if lane else None
    if configured:
        given = selection.get("username")
        if given and isinstance(given, str) and given.casefold() != configured.casefold():
            selection = {**selection, "username": ""}                    # the gate would refuse this card: not ready
        else:
            selection = {**selection, "username": configured}            # the runner's own lane config decides
    plan = evaluate(source=source, project=project, iid=iid, sha=sha, mr=mr, selection=selection, author=author)
    if plan.ok and not agent_ids.get(plan.request["reviewer_lane"]):
        plan = Plan(False, ["reviewer-agent-id-unknown"])   # dry-run says what apply would: not ready
    out: dict = {"mode": "apply" if apply else "dry-run", "ready": plan.ok, "refusals": plan.refusals,
                 "request": plan.request, "created": False, "card": None}
    if not plan.ok or not apply:
        return out
    req = plan.request
    reviewer_id = agent_ids[req["reviewer_lane"]]
    def twins(besides: str) -> list[str]:
        return sorted(t["id"] for t in mesh.find_tasks(source["project_id"], req["key"])
                      if (parse_request(t.get("description")) or {}).get("key") == req["key"] and t["id"] != besides)

    with _serialized(lock_dir, req["key"]):              # find + create is not atomic in Mesh: one writer per key
        for t in mesh.find_tasks(source["project_id"], req["key"]):
            found = parse_request(t.get("description"))
            if found and found["key"] == req["key"]:
                out["card"] = t["id"]
                if found["reviewer_lane"].casefold() != req["reviewer_lane"].casefold() \
                        or str(found["reviewer_username"]).casefold() != req["reviewer_username"].casefold():
                    # the router now picks someone else: the card's request names the old reviewer, and
                    # moving the assignee would leave a card its new holder's gate refuses
                    out.update(ready=False, refusals=[f"reviewer-changed:{found['reviewer_lane']}->{req['reviewer_lane']}"
                                                      ": cancel the old merge-only card, then apply again"])
                    return out
                if t.get("assignee_id") != reviewer_id:
                    mesh.assign(t["id"], reviewer_id)    # a first apply that died between create and assign
                    out["repaired"] = True
                break                                    # a repeat or a restart: the one card stays
        else:
            made = mesh.create_task(source["project_id"], card_fields(req, source["project_id"]))
            if not made.get("id"):
                raise Refused("card-not-created")
            mesh.assign(made["id"], reviewer_id)
            out.update(created=True, card=made["id"])
        # Another host with its own state dir can have created one in the same instant. Say so, on a
        # fresh apply and on every retry: the operator keeps one, and a second gate run on the same
        # head finds the MR merged.
        if twins(out["card"]):
            out["duplicates"] = twins(out["card"])
    return out


@contextlib.contextmanager
def _serialized(lock_dir: str | None, key: str):
    """An exclusive per-key file lock (this host). Without a directory there is no lock."""
    if not lock_dir:
        yield
        return
    os.makedirs(lock_dir, exist_ok=True)
    with open(os.path.join(lock_dir, key + ".lock"), "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def check_identity(env: dict, req: dict, base_url: str, *, opener=urllib.request.urlopen) -> str:
    """Raise ``Refused`` unless the GitLab user behind ``env``'s token is the selected reviewer.

    Only the username is read from /user; the token, the env and the response body are never
    printed or returned."""
    token = env.get("GITLAB_TOKEN") or env.get("GITLAB_PRIVATE_TOKEN") or ""
    if not token or not base_url:
        raise Refused("identity-missing")
    r = urllib.request.Request(f"{base_url.rstrip('/')}/api/v4/user", headers={"PRIVATE-TOKEN": token})
    try:
        with opener(r, timeout=20) as resp:
            who = str(json.loads(resp.read()).get("username") or "")
    except Exception:                                    # noqa: BLE001 - any failure refuses
        raise Refused("identity-unreadable") from None
    if who.casefold() != str(req["reviewer_username"]).casefold():
        raise Refused(f"identity-mismatch:{who or 'unknown'}!={req['reviewer_username']}")
    return who
