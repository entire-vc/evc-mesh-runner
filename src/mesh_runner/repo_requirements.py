"""Pre-start check: does this lane's admitted configuration cover the repository a card needs?

Pure observation. It never clones, leases, edits config or touches another actor's
worktree. The required repository comes only from typed Mesh VCS links (a merge request
link on this GitLab) that is not merged/closed; task prose and arbitrary URLs never grant or name a
repository, and a finished MR is history the start does not need (reported, never a reason).
A refusal carries one structured reason, the next actor and the supported action, plus
a fingerprint so the handoff is written once per generation of the same facts.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

OK = "ok"
NO_REPO = "no_repo_required"
UNDECLARED = "undeclared_repo"
NO_MEMBERSHIP = "no_membership"
MISSING_CLONE = "missing_approved_clone"
NO_CHECKOUT = "no_assigned_checkout"
CHECKPOINT = "checkpoint_conflict"
NO_SLOT = "no_free_slot"

MR_TYPES = ("pr", "pull_request", "mr", "merge_request")
# A link whose Mesh status says the MR is finished names history, not work this start needs. Any other
# status (open, draft, unknown, missing) stays a hard requirement: a lagging status only errs to holding.
TERMINAL_STATUSES = ("merged", "closed")
SAFE_PATH = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+")   # no markup can ride in a project path

# The only state that clears by itself; every other reason needs a named actor to act.
TRANSIENT = {NO_SLOT}

ACTIONS = {
    UNDECLARED: ("operator", "add the repository to the approved native config and apply it through the reviewed "
                             "config/regeneration path"),
    NO_MEMBERSHIP: ("operator", "add the repository to this agent's repos in the approved config (reviewed "
                                "admission; no other actor key, no nested clone)"),
    MISSING_CLONE: ("operator", "create the approved base clone at the configured path, then let the runner tick "
                                "re-evaluate"),
    NO_CHECKOUT: ("runner", "request scoped repo admission for the waiting attempt (`mesh-runner admit-workspace-repo`)"),
    CHECKPOINT: ("runner", "finish or restore the existing checkpoint; admission is refused while it is mid-phase"),
    NO_SLOT: ("runner", "wait for a free worktree slot; nothing to hand off"),
}


@dataclass(frozen=True)
class Verdict:
    reason: str
    project: str = ""
    repo: str = ""
    next_actor: str = ""
    action: str = ""
    fingerprint: str = ""
    also: tuple = ()          # further blocking (reason, project) pairs found in the same pass
    historical: tuple = ()    # projects named only by merged/closed links: informational, never a reason

    @property
    def blocking(self) -> bool:
        return self.reason not in (OK, NO_REPO)

    @property
    def durable(self) -> bool:
        """Needs a one-time handoff to a named actor (transient slot waits do not)."""
        return self.blocking and self.reason not in TRANSIENT

    def as_dict(self) -> dict:
        out = {"reason": self.reason, "project": self.project, "repo": self.repo,
               "next_actor": self.next_actor, "action": self.action}
        if self.also:
            out["also"] = [{"reason": r, "project": p} for r, p in self.also]
        if self.historical:
            out["historical"] = list(self.historical)
        return out

    def text(self) -> str:
        more = "".join(f"; also {r}={p}" for r, p in self.also)
        return (f"[runner] repository preflight: {self.reason}; required={self.project or '-'}{more}; "
                f"next_actor={self.next_actor}; action: {self.action}")


def _mr_links(links: list[dict], base_url: str) -> list[tuple[str, bool]]:
    """``(group/project, terminal)`` of every typed MR link on this GitLab. Nothing is read from prose."""
    host = urllib.parse.urlsplit(base_url).netloc
    out: list[tuple[str, bool]] = []
    for link in links or []:
        if not isinstance(link, dict):
            continue
        m = re.match(r"^https?://([^/]+)/(.+?)/-/merge_requests/\d+", str(link.get("url") or ""))
        if link.get("link_type") in MR_TYPES and m and m[1] == host and SAFE_PATH.fullmatch(m[2]):
            out.append((m[2], str(link.get("status") or "").strip().lower() in TERMINAL_STATUSES))
    return out


def required_projects(links: list[dict], base_url: str) -> list[str]:
    """``group/project`` with at least one live (not merged/closed) typed MR link, whatever its role."""
    out: list[str] = []
    for project, terminal in _mr_links(links, base_url):
        if not terminal and project not in out:
            out.append(project)
    return out


def historical_projects(links: list[dict], base_url: str) -> list[str]:
    """``group/project`` whose every typed MR link is merged/closed: the lane need not hold it to start."""
    live = set(required_projects(links, base_url))
    out: list[str] = []
    for project, _ in _mr_links(links, base_url):
        if project not in live and project not in out:
            out.append(project)
    return out


def _path_of(url: str) -> str:
    url = url.strip()
    m = re.fullmatch(r"[\w.-]+@[^/:\s]+:(.+)", url)
    path = m[1] if m else urllib.parse.urlsplit(url).path
    path = path.strip("/")
    return path[:-4] if path.endswith(".git") else path


def clone_project(base: Path, remote: str) -> str:
    """Canonical ``group/project`` of an operator clone, read from its configured remote."""
    try:
        done = subprocess.run(["git", "-C", str(base), "remote", "get-url", remote], capture_output=True,
                              text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return _path_of(done.stdout) if done.returncode == 0 else ""


def _digest(parts: list) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]


def check(links: list[dict], base_url: str, *, agent_repos: list, approved_repos: list | None = None,
          leased_repos: set | None = None, checkpoint_open: bool = False, resuming: bool = False,
          config_only: bool = False,
          pool_full: Callable[[str], bool] = lambda name: False,
          project_of: Callable[[object], str] | None = None) -> Verdict:
    """Match the card's required repositories against the lane's actual configuration.

    ``agent_repos``    RepoSpecs this agent will lease at start.
    ``approved_repos``  RepoSpecs approved anywhere in the fleet config (other agents included);
                        used only to tell 'undeclared' from 'declared, but not for this agent'.
    ``leased_repos``    repository names this attempt already holds a lease on.
    ``config_only``     resume of an existing attempt: skip checkpoint/checkout/slot facts.
    """
    project_of = project_of or (lambda spec: clone_project(spec.base, spec.remote))
    wanted = required_projects(links, base_url)
    history = tuple(historical_projects(links, base_url))
    if not wanted:
        return Verdict(NO_REPO, fingerprint=_digest(["none"]), historical=history)
    mine = {project_of(s): s for s in agent_repos}
    theirs = {project_of(s): s for s in (approved_repos or [])}
    found: list[tuple[str, str, object]] = []        # every blocking fact, so one handoff names them all
    for project in wanted:
        spec = mine.get(project)
        if spec is None:
            found.append((NO_MEMBERSHIP if project in theirs else UNDECLARED, project, theirs.get(project)))
            continue
        if not (Path(spec.base) / ".git").exists() and not Path(spec.base).joinpath("HEAD").exists():
            found.append((MISSING_CLONE, project, spec))
            continue
        if config_only:
            continue                      # a resume restores its own leases; only the config must cover the repo
        if checkpoint_open and spec.name not in (leased_repos or set()):
            found.append((CHECKPOINT, project, spec))
        elif resuming and spec.name not in (leased_repos or set()):
            found.append((NO_CHECKOUT, project, spec))
        elif spec.name not in (leased_repos or set()) and pool_full(spec.name):
            found.append((NO_SLOT, project, spec))
    if not found:
        return Verdict(OK, ",".join(wanted), fingerprint=_digest([OK, wanted]), historical=history)
    # canonical order (durable before slot wait, then reason/project): link order never changes the handoff
    found.sort(key=lambda f: (f[0] in TRANSIENT, f[0], f[1]))
    reason, project, spec = found[0]
    actor, action = ACTIONS[reason]
    repo = getattr(spec, "name", "")
    also = tuple((r, p) for r, p, _ in found[1:])
    fp = _digest([reason, project, repo] + ([sorted(also)] if also else []))
    # history is left out of the fingerprint: a finished MR never re-opens a handoff of the same blockers
    return Verdict(reason, project, repo, actor, action, fp, also, history)
