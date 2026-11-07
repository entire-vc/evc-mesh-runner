"""Pre-start check: does this lane's admitted configuration cover the repository a card needs?

Pure observation. It never clones, leases, edits config or touches another actor's
worktree. The required repository comes only from typed Mesh VCS links (a merge request
link on this GitLab); task prose and arbitrary URLs never grant or name a repository.
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

    @property
    def blocking(self) -> bool:
        return self.reason not in (OK, NO_REPO)

    @property
    def durable(self) -> bool:
        """Needs a one-time handoff to a named actor (transient slot waits do not)."""
        return self.blocking and self.reason not in TRANSIENT

    def as_dict(self) -> dict:
        return {"reason": self.reason, "project": self.project, "repo": self.repo,
                "next_actor": self.next_actor, "action": self.action}

    def text(self) -> str:
        return (f"[runner] repository preflight: {self.reason}; required={self.project or '-'}; "
                f"next_actor={self.next_actor}; action: {self.action}")


def required_projects(links: list[dict], base_url: str) -> list[str]:
    """``group/project`` of every typed MR link on this GitLab. Nothing is read from prose."""
    host = urllib.parse.urlsplit(base_url).netloc
    out: list[str] = []
    for link in links or []:
        if not isinstance(link, dict):
            continue
        m = re.match(r"^https?://([^/]+)/(.+?)/-/merge_requests/\d+", str(link.get("url") or ""))
        if (link.get("link_type") in MR_TYPES and m and m[1] == host and SAFE_PATH.fullmatch(m[2])
                and m[2] not in out):
            out.append(m[2])
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
    if not wanted:
        return Verdict(NO_REPO, fingerprint=_digest(["none"]))
    mine = {project_of(s): s for s in agent_repos}
    theirs = {project_of(s): s for s in (approved_repos or [])}
    for project in wanted:
        def no(reason, spec=None):
            actor, action = ACTIONS[reason]
            repo = getattr(spec, "name", "")
            return Verdict(reason, project, repo, actor, action, _digest([reason, project, repo]))

        spec = mine.get(project)
        if spec is None:
            return no(NO_MEMBERSHIP if project in theirs else UNDECLARED, theirs.get(project))
        if not (Path(spec.base) / ".git").exists() and not Path(spec.base).joinpath("HEAD").exists():
            return no(MISSING_CLONE, spec)
        if config_only:
            continue                      # a resume restores its own leases; only the config must cover the repo
        if checkpoint_open and spec.name not in (leased_repos or set()):
            return no(CHECKPOINT, spec)
        if resuming and spec.name not in (leased_repos or set()):
            return no(NO_CHECKOUT, spec)
        if spec.name not in (leased_repos or set()) and pool_full(spec.name):
            return no(NO_SLOT, spec)
    return Verdict(OK, ",".join(wanted), fingerprint=_digest([OK, wanted]))
