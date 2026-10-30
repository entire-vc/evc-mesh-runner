"""Wake conditions for waiting attempts.

A wait ends when its own condition is met, or earlier when somebody other than
the agent writes on the task: a reviewer's verdict or a lead's answer must not
sit behind a timer.
"""
from __future__ import annotations

import datetime as dt
import json
import hashlib
import logging
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .mesh import MeshClient, MeshError
from .outcome import declaration_lines
from .selection import CLOSED, StatusCategories, _read_elsewhere, parse_time, satisfied

log = logging.getLogger("mesh_runner")

PIPELINE_DONE = {"success", "failed", "canceled", "cancelled", "skipped", "manual"}
# A job's final states (``manual``: it starts only by hand) and every state GitLab gives a job.
JOB_FINAL = frozenset({"success", "failed", "canceled", "skipped", "manual"})
JOB_STATES = JOB_FINAL | {"cancelled", "created", "pending", "running", "preparing",
                          "waiting_for_resource", "scheduled", "canceling", "waiting_for_callback"}
JOB_PAGES = 10                 # 1000 jobs: past that a pipeline is not one a wait names jobs in
# Queue and run states of an unfinished pipeline. For ``until=change`` they are ONE phase: a hop
# between them on the same commit and pipeline (pending -> running) is not news to an agent that
# waits for the pipeline to settle or to move to another commit/pipeline.
PIPELINE_IN_PROGRESS = frozenset({"created", "waiting_for_resource", "preparing", "pending", "running", "scheduled"})
PIPELINE_STATES = PIPELINE_DONE | PIPELINE_IN_PROGRESS
NO_PIPELINE_GRACE = dt.timedelta(minutes=15)    # a push takes a moment to get its pipeline


# GitHub Actions run -> the status vocabulary of a GitLab pipeline.
RUN_STATUSES = {"queued": "pending", "requested": "pending", "pending": "pending",
                "in_progress": "running", "waiting": "waiting_for_resource"}
RUN_CONCLUSIONS = {"success": "success", "neutral": "success", "failure": "failed", "timed_out": "failed",
                   "startup_failure": "failed", "cancelled": "canceled", "stale": "canceled",
                   "skipped": "skipped", "action_required": "manual"}


@dataclass
class GitHub:
    """Reads a GitHub pull request as the merge-request dict the wait evaluators already
    understand (``state`` opened/merged/closed, ``sha``, ``merge_commit_sha``, ``labels``,
    ``target_branch``), and a GitHub Actions run as the pipeline dict (``id``, ``sha``, ``ref``,
    ``status`` in GitLab's vocabulary), so ``mr:`` / ``merge:`` / ``pipeline:`` waits on a GitHub
    target need no second code path."""
    token: str = ""
    api_url: str = "https://api.github.com"
    orgs: tuple = ()

    def __repr__(self) -> str:
        return f"GitHub({self.api_url!r})"

    def owns(self, target: str) -> bool:
        """True for ``org/repo!N`` (pull request) and ``org/repo#N`` (Actions run) whose org (or
        full org/repo) is configured as GitHub-hosted. One repo can be listed alone, because the
        same org may also exist on GitLab."""
        project = re.split(r"[!#]", target, maxsplit=1)[0]
        return any(project == o or project.startswith(o.rstrip("/") + "/") for o in self.orgs)

    def get(self, path: str) -> dict:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(f"{self.api_url.rstrip('/')}/{path}", headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def get_mr(self, target: str) -> dict:
        project, number = target.rsplit("!", 1)
        pr = self.get(f"repos/{project}/pulls/{number}")
        state = "merged" if pr.get("merged") or pr.get("merged_at") else \
            ("closed" if pr.get("state") == "closed" else "opened")
        head = pr.get("head") or {}
        return {"state": state, "sha": head.get("sha") or "", "merged_at": pr.get("merged_at"),
                "closed_at": pr.get("closed_at"), "updated_at": pr.get("updated_at"),
                "merge_commit_sha": pr.get("merge_commit_sha") if state == "merged" else None,
                "target_branch": (pr.get("base") or {}).get("ref"),
                "labels": [l.get("name") for l in pr.get("labels") or [] if isinstance(l, dict)],
                "head_pipeline": None, "web_url": pr.get("html_url")}

    def get_run(self, target: str) -> dict:
        """A GitHub Actions run as a GitLab pipeline row. ``finished_at`` is the run's last update
        once it is completed: a re-run moves it, as a retried GitLab pipeline moves its own."""
        project, run_id = target.rsplit("#", 1)
        run = self.get(f"repos/{project}/actions/runs/{run_id}")
        status, conclusion = run.get("status"), run.get("conclusion")
        if status == "completed":
            if conclusion not in RUN_CONCLUSIONS:
                raise ValueError(f"unknown GitHub run conclusion {conclusion!r}")
            mapped = RUN_CONCLUSIONS[conclusion]
        elif status in RUN_STATUSES:
            mapped = RUN_STATUSES[status]
        else:
            raise ValueError(f"unknown GitHub run status {status!r}")
        return {"id": run.get("id"), "sha": run.get("head_sha"), "ref": run.get("head_branch"),
                "status": mapped, "created_at": run.get("created_at"), "updated_at": run.get("updated_at"),
                "finished_at": run.get("updated_at") if status == "completed" else None,
                "web_url": run.get("html_url")}


@dataclass
class GitLab:
    base_url: str
    token: str
    github: "GitHub | None" = None

    def __repr__(self) -> str:
        return f"GitLab({self.base_url!r})"

    def get(self, path: str) -> dict:
        if not self.base_url:                  # GitHub-only configuration: no GitLab to read
            raise ValueError("no [gitlab] base_url configured")
        req = urllib.request.Request(f"{self.base_url.rstrip('/')}/api/v4/{path}",
                                     headers={"PRIVATE-TOKEN": self.token})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    @staticmethod
    def project(path: str) -> str:
        return urllib.parse.quote(path, safe="")

    def github_owns(self, target: str) -> bool:
        return self.github is not None and self.github.owns(target)

    def host(self, target: str) -> str:
        """The host a CI/MR target is read from: github.com for a GitHub-hosted repo, else GitLab's."""
        if self.github_owns(target):
            host = urllib.parse.urlsplit(self.github.api_url).hostname or "github.com"
            return "github.com" if host == "api.github.com" else host
        return urllib.parse.urlsplit(self.base_url).hostname or self.base_url or "GitLab"

    def get_pipeline(self, target: str) -> dict:
        if self.github_owns(target):
            return self.github.get_run(target)
        project, pid = target.rsplit("#", 1)
        return self.get(f"projects/{self.project(project)}/pipelines/{pid}")

    def pipeline_status(self, target: str) -> str:
        return self.get_pipeline(target).get("status", "")

    def pipeline_has_active_jobs(self, target: str) -> bool:
        """True while any job of the pipeline is queued or running (a pipeline can read
        ``canceled`` for a moment with jobs still winding down)."""
        if self.github_owns(target):
            return self.github.get_run(target)["status"] in PIPELINE_IN_PROGRESS
        project, pid = target.rsplit("#", 1)
        scopes = "&".join(f"scope[]={s}" for s in sorted(PIPELINE_IN_PROGRESS))
        return bool(self.get(f"projects/{self.project(project)}/pipelines/{pid}/jobs?{scopes}&per_page=1"))

    def pipeline_jobs(self, target: str) -> list[dict]:
        """The jobs of pipeline ``target`` (the latest try of each; GitLab leaves retried ones out).
        A GitHub run has no per-job reads here: a ``jobs=`` wait on one is refused."""
        if self.github_owns(target):
            raise ValueError("jobs= waits read GitLab pipeline jobs; a GitHub run has none")
        project, pid = target.rsplit("#", 1)
        rows: list[dict] = []
        for page in range(1, JOB_PAGES + 1):
            batch = self.get(f"projects/{self.project(project)}/pipelines/{pid}/jobs?per_page=100&page={page}")
            if not isinstance(batch, list):
                raise ValueError("pipeline jobs unreadable")
            rows += batch
            if len(batch) < 100:
                break
        return rows

    def newer_pipeline(self, target: str, row: dict | None = None) -> dict | None:
        """The latest pipeline of the same ref that is newer than ``target``, or None (always None
        for a GitHub run: the runs of a branch are not listed)."""
        if self.github_owns(target):
            return None
        project, pid = target.rsplit("#", 1)
        row = row if row is not None else self.get_pipeline(target)
        ref = row.get("ref")
        if not ref:
            return None
        rows = self.get(f"projects/{self.project(project)}/pipelines?ref={self.project(str(ref))}"
                        "&order_by=id&sort=desc&per_page=5")
        newer = [r for r in rows if isinstance(r, dict) and type(r.get("id")) is int and r["id"] > int(pid)]
        return newer[0] if newer else None

    def pipeline_event(self, target: str, last: dict) -> str:
        """Wake on a settled status that differs from the one seen when the wait
        began: re-waiting on a pipeline still in `manual` must not loop."""
        status = self.pipeline_status(target)
        if status in PIPELINE_DONE and status != last.get("pipeline"):
            baseline_known = last.get("pipeline") in PIPELINE_STATES
            last["pipeline"] = status
            if baseline_known:
                last["ci_progress"] = ci_delivery_key(target, int(target.rsplit("#", 1)[1]))
            return f"pipeline {target} finished: {status}"
        if status not in PIPELINE_DONE:
            last["pipeline"] = status
        return ""

    def mr_head_key(self, target: str) -> str:
        project, iid = target.rsplit("!", 1)
        head = self.get(f"projects/{self.project(project)}/merge_requests/{iid}").get("head_pipeline") or {}
        return f"{head.get('id')}:{head.get('status')}"

    def get_mr(self, target: str) -> dict:
        if self.github is not None and self.github.owns(target):
            return self.github.get_mr(target)
        project, iid = target.rsplit("!", 1)
        return self.get(f"projects/{self.project(project)}/merge_requests/{iid}")

    def seed(self, kind: str, target: str, now: dt.datetime) -> dict:
        """Wait bookkeeping at the moment the wait is declared: ``seen`` (the
        baseline a later change is measured against) and, when the awaited result
        is already final, ``settled`` (the text that wakes the wait at once).

        Measured on the fleet: waits declared on a pipeline that had already
        failed, or on a merge request whose head pipeline had already passed,
        waited for a change that could never come."""
        if kind == "pipeline":
            status = self.pipeline_status(target)
            out: dict = {"seen": {"pipeline": status}}
            if status == "manual":
                out["settled"] = (f"pipeline {target} was already waiting for a manual job when you "
                                  "declared the wait; it will not move on its own")
            elif status in PIPELINE_DONE:
                out["settled"] = f"pipeline {target} had already finished ({status}) when you declared the wait"
            return out
        mr = self.get_mr(target)
        head = mr.get("head_pipeline") or {}
        key = f"{head.get('id')}:{head.get('status')}"
        out = {"seen": {"mr_pipeline": key, "mr_state": mr.get("state", "")}}
        if mr.get("state") in ("merged", "closed"):
            out["settled"] = f"MR {target} was already {mr['state']} when you declared the wait"
        elif not _on_head(mr, head):
            out["seen"].update(no_pipeline_since=now.isoformat(), no_pipeline_sha=str(mr.get("sha", "")))
        elif head.get("status") == "manual":
            out["settled"] = (f"MR {target}: pipeline {head.get('id')} on the head was already waiting for "
                              "a manual job when you declared the wait; it will not move on its own")
        elif head.get("status") in PIPELINE_DONE:
            out["settled"] = (f"MR {target}: pipeline {head.get('id')} on the head "
                              f"{str(mr.get('sha', ''))[:8]} had already finished ({head.get('status')}) "
                              "when you declared the wait")
        return out

    def mr_event(self, target: str, last: dict, now: dt.datetime | None = None) -> str:
        mr = self.get_mr(target)
        if mr.get("state") in ("merged", "closed"):
            # once per state: a wait declared again on an MR already merged must not
            # wake on every check
            if last.get("mr_state") == mr["state"]:
                return ""
            baseline_known = last.get("mr_state") in {"opened", "locked", "merged", "closed"}
            last["mr_state"] = mr["state"]
            if baseline_known:
                last["ci_progress"] = "mr-state:" + target
            return f"MR {target} is {mr['state']}"
        last["mr_state"] = mr.get("state", "")
        head = mr.get("head_pipeline") or {}
        key = f"{head.get('id')}:{head.get('status')}"
        baseline_known = _known_pipeline_key(last.get("mr_pipeline"))
        if head.get("status") in PIPELINE_DONE and key != last.get("mr_pipeline"):
            last["mr_pipeline"] = key
            last.pop("no_pipeline_since", None)
            if baseline_known and _on_head(mr, head) and type(head.get("id")) is int and head["id"] > 0:
                last["ci_progress"] = ci_delivery_key(target, head["id"])
            return f"MR {target}: pipeline {head.get('id')} {head.get('status')} on {str(mr.get('sha', ''))[:8]}"
        now = now or dt.datetime.now(dt.timezone.utc)
        if _on_head(mr, head):
            # A failed/empty seed can recover a baseline from a later valid read.
            # The first terminal read still wakes, but cannot prove a transition.
            if not baseline_known and _known_pipeline_key(key):
                last["mr_pipeline"] = key
            last.pop("no_pipeline_since", None)
            return ""
        # the head commit has no pipeline (pushed with ci.skip, or CI did not trigger):
        # nothing will come; after a grace for a slow trigger, say so
        sha = str(mr.get("sha", ""))
        since = parse_time(last.get("no_pipeline_since"))
        if since is None or last.get("no_pipeline_sha", sha) != sha:
            # the grace runs per head commit: a new push gets its own
            last.update(no_pipeline_since=now.isoformat(), no_pipeline_sha=sha)
            return ""
        if now - since >= NO_PIPELINE_GRACE and last.get("no_pipeline_told") != str(mr.get("sha", "")):
            last["no_pipeline_told"] = str(mr.get("sha", ""))
            return (f"MR {target}: the head {str(mr.get('sha', ''))[:8]} has no pipeline "
                    f"{int((now - since).total_seconds() // 60)} min after the wait began "
                    "(pushed with ci.skip / SKIP_CI?); start one or change the wait")
        return ""


def _known_pipeline_key(key: str | None) -> bool:
    pid, _, status = str(key or "").partition(":")
    return bool(re.fullmatch(r"[1-9][0-9]*", pid)) and status in PIPELINE_STATES


def _on_head(mr: dict, head: dict) -> bool:
    """The MR's head pipeline belongs to its current head commit. A merged-results
    pipeline runs on a merge ref whose sha is not the head's: GitLab only makes it
    the head pipeline for the current head, so it counts."""
    if not head or not mr.get("sha"):
        return False
    if str(head.get("ref", "")).startswith("refs/merge-requests/") and str(head.get("ref", "")).endswith(
            ("/merge", "/train")):
        return True
    return str(head.get("sha", "")) == str(mr.get("sha"))


def _after(value: str | None, since: dt.datetime) -> bool:
    at = parse_time(value)
    return at is not None and at > since


def changed_since(gitlab, kind: str, target: str, since: dt.datetime,
                  echo: dt.timedelta = dt.timedelta(0)) -> str:
    """What happened to a merge request or pipeline after ``since``, by GitLab's own
    timestamps ('' = nothing). Stateless: no baseline is needed, so a restart or a
    wait that was never registered loses nothing. A merge, a close or a new head
    pipeline must come ``echo`` after ``since``: one right after the agent's last word
    is the echo of its own merge or push."""
    if kind == "pipeline":
        p = gitlab.get_pipeline(target)
        if p.get("status") in PIPELINE_DONE and _after(p.get("finished_at") or p.get("updated_at"), since):
            return f"pipeline {target} finished: {p['status']}"
        return ""
    mr = gitlab.get_mr(target)
    for state, field in (("merged", "merged_at"), ("closed", "closed_at")):
        if mr.get("state") == state and _after(mr.get(field) or mr.get("updated_at"), since + echo):
            return f"MR {target} was {state}"
    head = mr.get("head_pipeline") or {}
    sha = str(mr.get("sha") or head.get("sha") or "")[:8]
    if head.get("status") in PIPELINE_DONE and _after(head.get("finished_at") or head.get("updated_at"), since):
        return f"MR {target}: pipeline {head.get('id')} {head['status']} on {sha}"
    if _after(head.get("created_at"), since + echo):
        return f"MR {target}: new head {sha} (pipeline {head.get('id')} {head.get('status')})"
    return ""


GITLAB_MR_URL = re.compile(r"^https?://([^/]+)/(.+?)/-/merge_requests/(\d+)")


def gitlab_mr_targets(links: list[dict], base_url: str) -> list[str]:
    """``group/project!iid`` of every linked merge request on this GitLab."""
    host = urllib.parse.urlsplit(base_url).netloc
    out = []
    for link in links:
        m = GITLAB_MR_URL.match(str(link.get("url") or ""))
        if m and m[1] == host and f"{m[2]}!{m[3]}" not in out:
            out.append(f"{m[2]}!{m[3]}")
    return out


def vcs_event(links: list[dict], since: dt.datetime, echo: dt.timedelta = dt.timedelta(0)) -> str:
    """A change Mesh recorded on the task's VCS links after ``since`` ('' = none): a
    link added (a new MR, commit or branch), an MR merged or closed, its pipeline
    finished. Mesh stamps these from provider webhooks, a moment after the fact: a
    link or a state stamped within ``echo`` of ``since`` is the agent's own last
    push or merge arriving, not news."""
    for link in links:
        what = link.get("title") or link.get("url") or link.get("external_id") or "a link"
        kind = "MR" if link.get("link_type") in ("pr", "pull_request") else (link.get("link_type") or "link")
        if link.get("status") in ("merged", "closed") and _after(link.get("status_synced_at"), since + echo):
            return f"{kind} {what} was {link['status']}"
        if link.get("pipeline_status") in PIPELINE_DONE and _after(link.get("pipeline_updated_at"), since + echo):
            return f"{kind} {what}: pipeline {link['pipeline_status']}"
        if _after(link.get("created_at"), since + echo):
            return f"{kind} linked: {what}"
    return ""


SYSTEM_AUTHOR = "00000000-0000-0000-0000-000000000000"


def is_foreign(c: dict, agent_id: str) -> bool:
    """Written by someone who can tell the agent something: not the agent, not a
    runner note, not Mesh's own automation (gate armed / gate cleared notices).
    Measured: a "gate armed" notice woke a decision wait before the human answered."""
    author = c.get("author_id") or (c.get("author") or {}).get("id")
    body = c.get("body") or ""
    if not author or author == agent_id or author == SYSTEM_AUTHOR:
        return False
    if c.get("author_type") == "system" or c.get("auto_summary"):
        return False
    return not body.startswith("[runner]")


def foreign_comments(comments: list[dict], agent_id: str, since: dt.datetime) -> list[dict]:
    """Every foreign comment after ``since``, oldest first."""
    out = []
    for c in reversed(comments):
        at = parse_time(c.get("created_at"))
        if at is None or at <= since:
            break
        if is_foreign(c, agent_id):
            out.append(c)
    return out[::-1]


def own_next_after(comments: list[dict], agent_id: str, since: dt.datetime) -> dict | None:
    """The agent's own newest declaring comment after ``since`` when it ends in ``↻ NEXT``: a later
    word than the wait it sits on. None when the newest declaration is a WAIT or an ask."""
    for c in reversed(comments):
        at = parse_time(c.get("created_at"))
        if at is None or at <= since:
            break
        author = c.get("author_id") or (c.get("author") or {}).get("id")
        body = c.get("body") or ""
        if author != agent_id or body.startswith("[runner"):
            continue
        lines = declaration_lines(body)
        if lines:
            return c if re.match(r"^\s*↻\s*NEXT", lines[-1]) else None
    return None


def foreign_comment(comments: list[dict], agent_id: str, since: dt.datetime) -> dict | None:
    """Newest comment after ``since`` written by anyone but the agent itself."""
    found = foreign_comments(comments, agent_id, since)
    return found[-1] if found else None


ANSWER_MARKER = re.compile(r"^\s*(?:[✅*_>\s]*)(?:answer|decision|ответ|решение)\s*[:：]", re.I | re.M)


def answers_wait(c: dict, wait: dict) -> bool:
    """A comment is the answer to a decision/blocking wait only when the addressee wrote it
    (``handle``) or it carries an explicit answer marker. Anyone else's remark wakes the
    agent but does not make the wait *met*: the next wait stays idle time, not progress."""
    handle = str(wait.get("handle") or "").lstrip("@").casefold()
    author = {str(v).casefold() for v in (c.get("author_name"), (c.get("author") or {}).get("name"),
                                          c.get("author_id")) if v}
    recipient_id = str(wait.get("recipient_id") or "").casefold()
    if (handle and handle in author) or (recipient_id and recipient_id in author):
        return True
    return bool(ANSWER_MARKER.search(c.get("body") or ""))


# Waits whose own event (a pipeline result, an MR change, a deadline, an answer, the awaited card
# closing, the MR merging) is what the agent sleeps for, so a stranger's remark is the cheapest
# thing to defer. Blocking waits keep waking on every comment: a person's ask is news.
ADDRESSED_FILTERED_KINDS = frozenset({"decision", "pipeline", "mr", "time", "card", "merge"})

_MENTION = re.compile(r"(?<![\w.\-])@([\w\-]+(?:\.[\w\-]+)*)")


def mentions(body: str, name: str) -> bool:
    """True when ``body`` @-mentions ``name`` anywhere (not only at the start of a line)."""
    name = (name or "").lstrip("@").casefold()
    return bool(name) and any(h.casefold() == name for h in _MENTION.findall(body or ""))


def wakes_on_comment(c: dict, wait: dict, agent_name: str, always_from=()) -> bool:
    """Whether a foreign comment may end ``wait`` early under ``comment_wake = "addressed"``.

    Only a comment provably written by another agent (``author_type == "agent"``) is ever
    held back, and only on pipeline/MR/time/decision/card/merge waits (a card wait ends when the
    awaited card closes or ships, a merge wait when the MR merges or closes: neither is a comment).
    Always wakes: a person's comment (or one whose author kind is not stated), an @-mention of the
    waiting agent, an author the operator listed in ``comment_wake_always_from`` unless the comment
    opens with an FYI marker (``is_fyi``: it asks nothing, so authorship alone does not wake), and
    on a decision wait the addressee or an explicit answer marker (``answers_wait``). A held-back
    comment is not lost: the agent reads every comment since its previous turn when the wait ends
    for its own reason."""
    if wait.get("kind") not in ADDRESSED_FILTERED_KINDS:
        return True
    if c.get("author_type") != "agent":
        return True
    if mentions(c.get("body") or "", agent_name):
        return True
    who = str(c.get("author_name") or (c.get("author") or {}).get("name") or "").casefold()
    if who and who in {str(x).casefold() for x in always_from} and not is_fyi(c.get("body") or ""):
        return True
    return wait.get("kind") == "decision" and answers_wait(c, wait)


# A comment that opens with an answer word, colon or no colon: «Answer: ...», «Ответ на запрос ...»,
# «Решение Ray: ...», «[decision-answer] ...». Measured: addressees answer this way far more often
# than with the strict ``Answer:`` form, and those answers carry no @-mention.
ANSWER_LEAD = re.compile(r"^[\s*_>#\[\]✅:\-]*(?:decision-answer|answer|ответ|решение|decision)\b", re.I)


# A comment that only informs: it opens with FYI («FYI», «**FYI», «[FYI]», «fyi:»), or its first
# bold or heading line carries «(FYI ...)» like «**Combined handback (FYI, same grant).**».
# Cross-posted handbacks and notes look like this and ask the reader nothing.
FYI_LEAD = re.compile(r"^[\s*_>#\[\](){}:\-]*fyi\b", re.I)
_FYI_MARKED_LINE = re.compile(r"^\s*[*_>#]+[^\n]*\(\s*fyi\b", re.I)


def is_fyi(body: str) -> bool:
    """True when ``body`` opens as a pure notification (see ``FYI_LEAD``). Callers check an
    @-mention of the reader first; a mention still wakes."""
    body = body or ""
    if FYI_LEAD.match(body):
        return True
    first = next((ln for ln in body.splitlines() if ln.strip()), "")
    return bool(_FYI_MARKED_LINE.match(first))


def answers_on_source(c: dict, wait: dict, agent_name: str, always_from=()) -> bool:
    """``decision_wake = "answer"``: whether a foreign comment on the waiting card may end a decision
    wait that has a request card. A person's comment (or one whose author kind is not stated) does;
    so does one by an author in ``comment_wake_always_from``. Another agent's comment does only when
    it @-mentions the waiting agent, carries an answer marker (``ANSWER_MARKER``/``ANSWER_LEAD``) or
    quotes the request card's id. The addressee's plain progress note is none of these."""
    if c.get("author_type") != "agent":
        return True
    body = c.get("body") or ""
    if mentions(body, agent_name) or ANSWER_MARKER.search(body) or ANSWER_LEAD.match(body):
        return True
    who = str(c.get("author_name") or (c.get("author") or {}).get("name") or "").casefold()
    if who and who in {str(x).casefold() for x in always_from}:
        return True
    request = str((wait.get("request") or {}).get("id") or "").casefold()
    return bool(request) and (request in body.casefold() or request[:8] in body.casefold())


def request_answer(mesh: MeshClient, cats: StatusCategories, wait: dict, agent_id: str,
                   since: dt.datetime, closure_only: bool = False) -> tuple[str, bool]:
    """``decision_wake = "answer"``: the wake event for the request card of a decision wait, and
    whether it is the answer itself. A comment on it from a person or from the addressee, or a card
    closed since the wait began (done: answered; cancelled: no answer, but the asker must know).
    ('', False) while it holds or cannot be read: an unreadable request is never an answer."""
    rid = str((wait.get("request") or {}).get("id") or "")
    if not rid or mesh is None:
        return "", False
    try:
        card = mesh.task(rid)
        card = card.get("task", card)
        comments = [] if closure_only else foreign_comments(mesh.comments(rid), agent_id, since)
        closed = cats.category(card) if cats is not None else ""
    except Exception as e:                    # noqa: BLE001 - a read that fails is not an answer
        log.debug("decision request %s: %s", rid[:8], e)
        return "", False
    for c in reversed(comments):
        if c.get("author_type") != "agent" or answers_wait(c, wait):
            who = c.get("author_name") or (c.get("author") or {}).get("name") or "someone"
            return f"{who} commented on decision request #{rid[:8]}: {(c.get('body') or '')[:300]}", True
    if closed in CLOSED:
        changed = parse_time(card.get("updated_at") or card.get("completed_at"))
        if changed is None or changed > since:
            return f"decision request #{rid[:8]} is {closed}", closed == "done"
    return "", False


def _watched_comment(mesh: MeshClient, target: dict, wait: dict, since: dt.datetime) -> str:
    """A card wait made from a question to the target's own holder (``watch_comments_by``) also
    ends when that holder comments on the target: its handback or verdict is the answer."""
    watch = wait.get("watch_comments_by") or {}
    rid, tid = str(watch.get("id") or ""), str(target.get("id") or "")
    if not rid or not tid:
        return ""
    name = str(watch.get("name") or "").casefold()
    for c in reversed(mesh.comments(tid)):
        at = parse_time(c.get("created_at"))
        if at is None or at <= since:
            break
        author = c.get("author_id") or (c.get("author") or {}).get("id")
        who = str(c.get("author_name") or (c.get("author") or {}).get("name") or "")
        if author == rid or (name and who.casefold() == name):
            return f"{who or watch.get('name') or 'the holder'} commented on #{tid[:8]}: {(c.get('body') or '')[:300]}"
    return ""


HUMAN_ASK_RECHECK = dt.timedelta(hours=24)


def _hours(d: dt.timedelta) -> str:
    h = d.total_seconds() / 3600
    return f"{h:.0f}h" if h >= 1 else f"{d.total_seconds() // 60:.0f} min"


def ci_delivery_key(target: str, pipeline: int, jobs: tuple[str, ...] | list[str] = ()) -> str:
    # MR and pipeline spellings of the same project's observation share memory. A wait on named
    # jobs is a different observation of the same pipeline: it never shares the pipeline's mark.
    project = re.split(r"[!#]", target)[0]
    ident = f"{project}#{pipeline}" + (f" jobs={','.join(jobs)}" if jobs else "")
    return "ci_delivery:" + hashlib.sha256(ident.encode()).hexdigest()


class UnknownJobs(ValueError):
    """A ``jobs=`` wait names jobs the pipeline does not have: it could never complete."""
    def __init__(self, pipeline: int, names: list[str]):
        super().__init__(f"pipeline {pipeline} has no job named {', '.join(names)}")
        self.pipeline, self.names = pipeline, names


def target_jobs_state(gitlab, target: str, pipeline: int, names) -> tuple[str, str]:
    """(status, identity) of the named jobs of ``pipeline``. Status: ``running`` while any of them
    is not final, else ``success`` when all succeeded, else the worst final status among them
    (failed, canceled, skipped, manual). Jobs outside ``names`` (manual siblings included) are not
    read. Identity names each job's id, so a retried job is a new observation. Raises
    ``UnknownJobs`` for a name the pipeline has no job for; anything unreadable raises too."""
    project = re.split(r"[!#]", target)[0]
    latest: dict[str, dict] = {}
    for job in gitlab.pipeline_jobs(f"{project}#{pipeline}"):
        if not isinstance(job, dict) or type(job.get("id")) is not int or not isinstance(job.get("name"), str):
            continue
        if job["name"] not in latest or job["id"] > latest[job["name"]]["id"]:
            latest[job["name"]] = job
    missing = [n for n in names if n not in latest]
    if missing:
        raise UnknownJobs(pipeline, missing)
    statuses = []
    for n in names:
        status = latest[n].get("status")
        if not isinstance(status, str) or status not in JOB_STATES:
            raise ValueError("job status unavailable")
        statuses.append("canceled" if status == "cancelled" else status)
    identity = ",".join(f"{n}={latest[n]['id']}" for n in names)
    if any(st not in JOB_FINAL for st in statuses):
        return "running", identity
    for st in ("failed", "canceled", "skipped", "manual"):
        if st in statuses:
            return st, identity
    return "success", identity


class Delivered(str):
    """A stored delivery mark that remembers when it was stored (``at``, ISO time, '' = unknown).
    Compares as the plain string, so every existing equality check keeps working."""
    at: str = ""

    def __new__(cls, value: str, at: str = ""):
        obj = super().__new__(cls, value)
        obj.at = at or ""
        return obj


# A retry of a failed job leaves the pipeline id unchanged; only the terminal timestamp of its
# next settled state differs. The clock of the provider and of the runner may disagree a little.
RETRY_CLOCK_SKEW = dt.timedelta(minutes=2)


def _terminal_generation(head: dict) -> str:
    """The provider's ``finished_at`` of a pipeline row as a UTC ISO string: it moves every time
    the pipeline settles again after a retry and stays put across polls of an unchanged
    terminal state. '' when absent or malformed (the caller then keys without a generation)."""
    raw = head.get("finished_at") if isinstance(head, dict) else None
    if not isinstance(raw, str) or len(raw) > 64 or not _MR_TERMINAL_TIME.fullmatch(raw):
        return ""
    when = parse_time(raw.upper())
    return when.astimezone(dt.timezone.utc).isoformat() if when else ""


def pipeline_host(gitlab, target: str) -> str:
    """The host a pipeline target is read from, for messages; works on any observer."""
    host = getattr(gitlab, "host", None)
    if callable(host):
        return host(target)
    return urllib.parse.urlsplit(str(getattr(gitlab, "base_url", "") or "")).hostname or "GitLab"


def ci_observation(gitlab, kind: str, target: str, row: dict | None = None) -> tuple[str, int, str]:
    """The validated (sha, pipeline id, status) of a CI target; raises on anything unreadable,
    off-head or malformed. One validation for the evaluator and for the checks that send a
    wait back at declaration. ``row``: the target's provider row already read by the caller."""
    return ci_observation_gen(gitlab, kind, target, row)[:3]


def ci_observation_gen(gitlab, kind: str, target: str, row: dict | None = None) -> tuple[str, int, str, str]:
    """``ci_observation`` plus the retry generation (see ``_terminal_generation``) of the head."""
    statuses = PIPELINE_DONE | {"created", "waiting_for_resource", "preparing", "pending", "running", "scheduled"}
    if not gitlab:
        raise ValueError("CI observer unavailable")
    if row is None:
        row = gitlab.get_mr(target) if kind == "mr" else gitlab.get_pipeline(target)
    if not isinstance(row, dict):
        raise ValueError("CI observation unavailable")
    sha = row.get("sha")
    head = row.get("head_pipeline") if kind == "mr" else row
    if (not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha)
            or not isinstance(head, dict) or (kind == "mr" and not _on_head(row, head))):
        raise ValueError("CI head unavailable")
    pid, status = head.get("id"), head.get("status")
    if type(pid) is not int or not 0 < pid < 10**12 or status not in statuses:
        raise ValueError("CI pipeline unavailable")
    if kind == "pipeline" and str(pid) != target.rsplit("#", 1)[1]:
        raise ValueError("CI pipeline identity mismatch")
    return sha, pid, status, _terminal_generation(head)


_MR_PIPELINE_REF = re.compile(r"refs/merge-requests/([1-9][0-9]{0,11})/(head|merge|train)")


def pipeline_merge_request(row: dict | None) -> tuple[str, str]:
    """(merge request iid, reviewed head SHA) a pipeline row tells about itself; '' where unknown.

    A merge request pipeline runs on ``refs/merge-requests/<iid>/head`` (the head commit itself) or
    on ``.../merge`` / ``.../train`` (a merge commit whose SHA is NOT the reviewed head). A branch
    pipeline names no merge request, but its SHA is the commit it ran on."""
    if not isinstance(row, dict):
        return "", ""
    ref, sha = row.get("ref"), row.get("sha")
    sha = sha if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha) else ""
    if not isinstance(ref, str) or not ref:
        return "", ""
    m = _MR_PIPELINE_REF.fullmatch(ref)
    if m:
        return m.group(1), (sha if m.group(2) == "head" else "")
    if ref.startswith("refs/"):
        return "", ""
    return "", sha


def manual_next_step(kind: str, target: str, sha: str, row: dict | None, merge_gate: bool) -> str:
    """The way out of a wait on a pipeline blocked on manual jobs, which never reaches success by
    itself. With a merge gate (``merge_command``) the gate starts the post-review jobs once a
    verdict exists and then merges, so the next wait is ``merge:``; without one, the owner arranges
    the review or the authorized manual action. Never suggests another wait on the blocked pipeline:
    measured, agents copied such a hint literally and re-armed the same dead wait."""
    if kind == "mr":
        project, _, iid = target.rpartition("!")
        head = sha
    else:
        project = target.rsplit("#", 1)[0]
        iid, head = pipeline_merge_request(row)
    iid_s, head_s = iid or "<iid>", head or "<full-head-sha>"
    if merge_gate:
        text = (f"Next step: obtain the review verdict on head {head_s}; once it is SHIP, end with "
                f"`⏳ WAIT merge:{project}!{iid_s} after={head_s}` and the merge gate starts the "
                "post-review jobs and merges.")
        if not iid:
            text += (" Fill in the merge request number and its full head SHA. If this pipeline belongs "
                     "to no merge request, arrange the required review or authorized manual action instead.")
        return text
    return ("Next step: arrange the required review or authorized manual action. To await the merge "
            f"after that, end with `⏳ WAIT mr:{project}!{iid_s} until=merged after={head_s}:opened`"
            + (" (fill in the merge request number and its full head SHA)." if not (iid and head) else "."))


def red_merge_statuses(merge_gate: bool) -> frozenset[str]:
    """Head statuses on which an MR will not merge as is. A skipped head counts only behind a
    merge gate, which merges on success alone; without one a project may let it merge."""
    base = {"failed", "canceled", "cancelled"}
    return frozenset(base | {"skipped"} if merge_gate else base)


def mr_merge_blocker(row: dict | None) -> str:
    """Why the provider says an open MR cannot merge into its target as is: ``conflict`` (a merge
    conflict, ``has_conflicts`` or ``detailed_merge_status`` ``conflict``) or ``need_rebase`` (the
    project merges fast-forward only and the branch is behind). '' = no such signal, including the
    provider still checking (``unchecked``/``checking``) or a field of an unknown shape."""
    if not isinstance(row, dict):
        return ""
    detailed = row.get("detailed_merge_status")
    if row.get("has_conflicts") is True or detailed == "conflict":
        return "conflict"
    if detailed == "need_rebase":
        return "need_rebase"
    return ""


def blocked_mr_next_step(target: str, cause: str, merge_gate: bool) -> str:
    """The way out of a wait for the merge of MR ``target`` that will not merge as is: its head is
    red (``cause`` ``red``), it conflicts with its target (``conflict``) or it must be rebased
    (``need_rebase``). The way out is a new head, so the text never suggests another wait for the
    merge of the current one: measured, agents re-armed that same wait and it parked for good."""
    project, _, iid = target.rpartition("!")
    if cause == "red":
        fix = ("open the failed job, fix the cause and push (or retry it once if it is a proven flake), "
               "then obtain the review verdict on the new head")
    elif cause == "conflict":
        fix = ("rebase the branch onto its target, resolve the conflicts and push, then have the review "
               "cover the rebased delta and give its verdict on the new head")
    else:
        fix = ("rebase the branch onto its target and push, then have the review cover the rebased delta "
               "and give its verdict on the new head")
    if merge_gate:
        then = (f"once it is SHIP, end with `⏳ WAIT merge:{project}!{iid or '<iid>'} after=<full-new-head-sha>` "
                "and the merge gate merges it.")
    else:
        then = "once it is SHIP, arrange the merge of the new head with the project's merge tool."
    return f"Next step: {fix}; {then} Do not wait for this MR to merge again until it has a new head."


def manual_pipeline_text(target: str, sha: str, pid: int, row: dict | None, merge_gate: bool,
                         sent_back: bool = False) -> str:
    """Owner notice (``sent_back=False``) or declaration send-back for a wait on pipeline ``target``
    that is blocked on manual jobs."""
    if sent_back:
        head = (f"pipeline {target} is blocked on manual jobs (pipeline {pid}, {sha[:8]}) and will not "
                "reach success by itself, so this wait cannot complete on its own and was sent back. ")
    else:
        head = (f"action required: pipeline {target} on {sha[:8]} is blocked on manual jobs; the success "
                "predicate is not met and this pipeline will not reach success by itself. ")
    return (head + "Do not run jobs or skip a project gate automatically, and do not wait on this "
            "pipeline again while it is manual. " + manual_next_step("pipeline", target, sha, row, merge_gate))


def owner_action_delivered(target: str, sha: str, pid: int, status: str, delivered,
                           generation: str = "") -> bool:
    """The owner notice for this exact head/pipeline/status/retry generation was already delivered
    (read only: nothing is recorded)."""
    return _owner_action_once(target, sha, pid, status, delivered, None, generation) is None


def ci_change_event(gitlab, kind: str, target: str, contract: dict,
                    delivered: str = "", observation: dict | None = None,
                    allow_action_required: bool = False, merge_gate: bool = False) -> str:
    """Evaluate an explicit baseline without acknowledging or mutating it.

    Unknown/malformed observations raise; callers retain the contract. Only
    bounded identity/status metadata is included in event text. ``merge_gate``: the runner
    runs a merge gate for ``merge:`` waits, so owner notices may point at one.
    """
    sha, pid, status, generation = ci_observation_gen(gitlab, kind, target)
    if contract.get("jobs"):
        return _jobs_event(gitlab, kind, target, contract, sha, pid, delivered, observation,
                           allow_action_required)
    if allow_action_required and kind == "pipeline" and contract["predicate"] == "success":
        # A pipeline ID is immutable: a different SHA is not evidence about the
        # exact requested pipeline. Never diagnose an unbound/stale observation.
        if sha != contract["sha"] or pid != contract["pipeline"]:
            raise ValueError("CI pipeline baseline identity mismatch")
        if status in PIPELINE_DONE - {"success"}:
            # Non-success terminal states need the owner's next step. Aggregate
            # manual blocks too, unlike optional jobs in a running/successful CI.
            if _owner_action_once(target, sha, pid, status, delivered, observation, generation) is None:
                return ""
            if status == "manual":
                try:
                    row = gitlab.get_pipeline(target)  # its ref names the merge request, if any
                except Exception:                      # noqa: BLE001 - the hint degrades to placeholders
                    row = None
                return manual_pipeline_text(target, sha, pid, row, merge_gate)
            return (f"action required: pipeline {target} on {sha[:8]} finished: {status}; "
                    "the success predicate is not met. The owner must inspect the outcome and take "
                    "the next authorized step or wait on a new concrete action, rather than wait "
                    "on this finished event again. Do not retry pipelines, run jobs or skip "
                    "a project gate automatically.")
    if allow_action_required and kind == "mr" and contract["predicate"] == "success" \
            and status in PIPELINE_DONE - {"success"}:
        # An MR head moves, so every head gets its own notice; a manual head
        # (jobs played only after review) can never reach success before review.
        notice = _owner_action_once(target, sha, pid, status, delivered, observation, generation)
        if notice is None:
            return ""
        if status == "manual":
            return (f"action required: MR {target} head {sha[:8]} pipeline {pid} is blocked on "
                    "manual jobs; the success predicate is not met before review and the pipeline "
                    "will not reach success by itself. Do not run jobs or skip a project gate "
                    "automatically, and do not wait for its success again. "
                    + manual_next_step("mr", target, sha, None, merge_gate))
        return (f"action required: MR {target} head {sha[:8]} pipeline {pid} finished: {status}; "
                "the success predicate is not met. The owner must inspect the outcome and push a "
                "fix or take the next authorized step, rather than wait on this head again.")
    if (sha, pid, status) == (contract["sha"], contract["pipeline"], contract["status"]):
        return ""
    if (contract["predicate"] == "change" and sha == contract["sha"] and pid == contract["pipeline"]
            and status in PIPELINE_IN_PROGRESS and contract["status"] in PIPELINE_IN_PROGRESS):
        # Same commit, same pipeline, still unfinished on both sides: a queue/run hop. Nothing is
        # recorded as delivered, so the later settled state is still a fresh change.
        return ""
    fingerprint = f"{sha}:{pid}:{status}"
    key = ci_delivery_key(target, pid)
    previous = delivered(key) if callable(delivered) else delivered
    if fingerprint == previous:
        return ""
    if contract["predicate"] == "success" and status != "success":
        return ""
    if observation is not None:
        observation.update(key=key, value=fingerprint)
    return f"{kind} {target}: changed CI {sha[:8]} pipeline {pid} {status} ({contract['predicate']})"


def _jobs_event(gitlab, kind: str, target: str, contract: dict, sha: str, pid: int, delivered,
                observation, allow_action_required: bool) -> str:
    """``ci_change_event`` for a wait on named jobs (``jobs=[...]``): the predicate is read from
    those jobs, never from the aggregate pipeline status, so a pipeline left ``manual`` by an
    unstarted sibling does not hold back a target job that has finished. Once every named job is
    final the wait wakes: ``until=change`` on any final result, ``until=success`` when all
    succeeded; a non-success result under ``until=success`` wakes once as an owner notice. A
    retried job (new id) is a new observation. An MR wait reads the jobs of its current head."""
    if kind == "pipeline" and (sha != contract["sha"] or pid != contract["pipeline"]):
        raise ValueError("CI pipeline baseline identity mismatch")
    jobs = tuple(contract["jobs"])
    names = ",".join(jobs)
    try:
        status, identity = target_jobs_state(gitlab, target, pid, jobs)
    except UnknownJobs as e:
        # an MR head can move to a pipeline without the job; it would never wake by itself
        if not allow_action_required or _owner_action_once(
                target, sha, pid, f"jobs[{names}]:missing", delivered, observation) is None:
            return ""
        return (f"action required: {kind} {target} pipeline {pid} on {sha[:8]}: {e}, so this wait cannot "
                "complete. Wait on jobs this pipeline has, or on the pipeline itself.")
    if status == "running":
        return ""
    if contract["predicate"] == "success" and status != "success":
        if not allow_action_required or _owner_action_once(
                target, sha, pid, f"jobs[{names}]:{status}", delivered, observation, identity) is None:
            return ""
        return (f"action required: {kind} {target} pipeline {pid} on {sha[:8]}: job(s) {names} finished: "
                f"{status}; the success predicate is not met. Inspect the job, fix and push or retry it "
                "once, then wait on the new result. Do not run sibling jobs or skip a project gate "
                "automatically.")
    key = ci_delivery_key(target, pid, jobs)
    fingerprint = f"{sha}:{pid}:{identity}:{status}"
    previous = delivered(key) if callable(delivered) else delivered
    if fingerprint == previous:
        return ""
    if observation is not None:
        observation.update(key=key, value=fingerprint)
    return (f"{kind} {target}: job(s) {names} finished: {status} (pipeline {pid}, {sha[:8]}, "
            f"{contract['predicate']}); other jobs of the pipeline were not considered")


def _owner_action_once(target: str, sha: str, pid: int, status: str, delivered, observation,
                       generation: str = ""):
    """Record one owner-action notice per exact head/pipeline/status/retry generation; None if
    already delivered.

    The generation (the terminal ``finished_at``) tells a new settled outcome of the SAME pipeline
    after a retry from a repeated poll of the unchanged one. Without it the second failure of a
    retried pipeline carried the identity of the first, was taken for delivered, and the owner was
    never woken. No generation (the provider row has none) keys exactly as before.

    Backward compatibility: a notice delivered before generations existed is stored under the
    generation-less identity. It still covers a terminal state that settled no later than it was
    stored (an unchanged failure must not wake twice just because the key format changed); a state
    that settled after it, or any state when the stored time is unknown, is judged as above."""
    legacy = f"{target}:{sha}:{pid}:{status}"
    identity = f"{legacy}@{generation}" if generation else legacy
    key = "ci_action_required:" + hashlib.sha256(identity.encode()).hexdigest()
    previous = delivered(key) if callable(delivered) else delivered
    if previous == identity:
        return None
    if generation:
        legacy_key = "ci_action_required:" + hashlib.sha256(legacy.encode()).hexdigest()
        old = delivered(legacy_key) if callable(delivered) else delivered
        if old == legacy and _settled_before_delivery(generation, getattr(old, "at", "")):
            return None
    if observation is not None:
        observation.update(key=key, value=identity, action_required=True)
    return identity


def _settled_before_delivery(generation: str, stored_at: str) -> bool:
    """True when the terminal state settled (``generation``) no later than the stored delivery
    (``stored_at``), allowing for clock skew. An unreadable stored time counts as covered: the
    pre-generation mark is all that is known, and a repeat wake is worse than a deferred one."""
    stored, settled = parse_time(stored_at) if stored_at else None, parse_time(generation)
    if stored is None or settled is None:
        return True
    return settled <= stored + RETRY_CLOCK_SKEW


def typed_wait(wait: dict) -> bool:
    return bool(wait.get("ci_change") or wait.get("mr_state"))


_MR_TERMINAL_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt](?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(?:\.[0-9]+)?(?:[Zz]|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])")


def typed_event(gitlab, kind: str, target: str, wait: dict, delivered="", observation=None,
                allow_action_required=False, merge_gate=False) -> str:
    """Shared evaluator; receipt acknowledgement belongs to the state transition."""
    if not wait.get("mr_state"):
        return ci_change_event(gitlab, kind, target, wait["ci_change"], delivered, observation,
                               allow_action_required=allow_action_required, merge_gate=merge_gate)
    contract = wait["mr_state"]
    if kind != "mr" or contract.get("predicate") != "merged" or contract.get("state") != "opened":
        raise ValueError("invalid MR state contract")
    row = gitlab.get_mr(target) if gitlab else None
    if not isinstance(row, dict) or row.get("state") not in {"opened", "merged", "closed"}:
        raise ValueError("MR state unavailable")
    state = row["state"]
    if state == "opened":
        head = row.get("head_pipeline")
        sha = row.get("sha")
        if (allow_action_required and isinstance(head, dict) and isinstance(sha, str)
                and _on_head(row, head) and head.get("status") in red_merge_statuses(merge_gate)
                and type(head.get("id")) is int):
            status, pid = head["status"], head["id"]
            if _owner_action_once(target, sha, pid, status, delivered, observation,
                                  _terminal_generation(head)) is not None:
                return (f"action required: MR {target} head {sha[:8]} pipeline {pid} finished: "
                        f"{status}; it will not merge as is. The owner must push a fix or take "
                        "the next authorized step rather than wait for the merge. "
                        + blocked_mr_next_step(target, "red", merge_gate))
        cause = mr_merge_blocker(row)
        if (allow_action_required and cause and isinstance(sha, str)
                and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha)
                and _owner_action_once(target, sha, 0, cause, delivered, observation) is not None):
            # One notice per head and cause: a new head (the rebase) is judged afresh.
            what = ("has a merge conflict with its target branch" if cause == "conflict"
                    else "must be rebased onto its target branch before it can merge")
            return (f"action required: MR {target} head {sha[:8]} {what}; it will not merge as is. "
                    + blocked_mr_next_step(target, cause, merge_gate))
        return ""
    raw = row.get("merged_at" if state == "merged" else "closed_at")
    # The general time parser also accepts dates and assumes UTC for naive
    # values. A provider terminal receipt needs a full, zoned timestamp.
    if not isinstance(raw, str) or len(raw) > 64 or not _MR_TERMINAL_TIME.fullmatch(raw):
        raise ValueError("MR terminal timestamp unavailable")
    when = parse_time(raw.upper())
    if when is None:
        raise ValueError("MR terminal timestamp unavailable")
    fingerprint = state + ":" + when.astimezone(dt.timezone.utc).isoformat()
    key = "mr-state:" + target
    previous = delivered(key) if callable(delivered) else delivered
    if fingerprint == previous:
        return ""
    if observation is not None:
        observation.update(key=key, value=fingerprint)
    return f"mr {target}: " + ("merged" if state == "merged" else "closed without merge (terminal failure)")


def window_complete(comments: list[dict], since: dt.datetime, limit: int = 40) -> bool:
    """True when ``comments`` provably holds every comment since ``since``: the read came
    back short of its page size (so it is the whole thread) or reaches back past ``since``.
    Decided from the rows already read; it never asks the provider for more."""
    if len(comments) < limit:
        return True
    oldest = parse_time(comments[0].get("created_at")) if comments else None
    return oldest is not None and oldest <= since


def wait_generation(wait: dict) -> str:
    """Stable id of one declared wait: kind, target and typed contract plus its start. Free
    of observer bookkeeping (seen, errors), so it survives a restart and changes on re-declare."""
    ident = {k: wait.get(k) for k in ("kind", "target", "handle", "ci_change", "mr_state",
                                      "since", "started", "rechecks")}
    return hashlib.sha256(json.dumps(ident, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _negative_observation(wait: dict, kind: str, comments: list[dict], since: dt.datetime,
                          started: dt.datetime, interval: dt.timedelta, now: dt.datetime) -> None:
    """Record, on ``wait``, that a COMPLETE negative observation just ran at a moment when the
    age re-check would have woken the agent. Only the paths that read their target to the end
    qualify: a typed CI/MR contract that evaluated ``unchanged``, and a decision wait whose
    comment window is provably complete. Everything else (card, legacy pipeline/MR, time,
    blocking, errors, UNKNOWN) is unsupported: absence of a start is never taken as proof."""
    due = started + interval
    if now < due or wait.get("recheck_due"):
        return
    if typed_wait(wait):
        if wait.get("observation_state") != "unchanged" or kind not in ("pipeline", "mr"):
            return
        observer = "typed_mr" if wait.get("mr_state") else "typed_ci"
    elif kind == "decision":
        if not window_complete(comments, since):
            return
        observer = "decision_comments"
    else:
        return
    contract = wait.get("mr_state") or wait.get("ci_change") or {}
    wait["negative_observation"] = {
        "observer": observer, "wait_kind": kind, "generation": wait_generation(wait),
        "contract": hashlib.sha256(json.dumps(contract, sort_keys=True, default=str).encode()).hexdigest()[:16],
        "legacy_due_at": due.astimezone(dt.timezone.utc).isoformat(),
        "interval_s": int(interval.total_seconds())}


def check(mesh: MeshClient, cats: StatusCategories, gitlab: GitLab | None, task: dict,
          wait: dict, agent_id: str, comments: list[dict], now: dt.datetime,
          max_wait: dt.timedelta = dt.timedelta(hours=6), max_errors: int = 5,
          first_recheck: dt.timedelta | None = None, delivery_reader=None,
          allow_action_required: bool = False, comment_wake: str = "all", agent_name: str = "",
          comment_wake_always_from=(), decision_wake: str = "any", merge_gate: bool = False) -> str:
    """Wake event text, or '' while the wait holds. Mutates ``wait`` bookkeeping
    (seen pipeline, consecutive check errors; ``recheck_due`` when the wake is a re-check;
    ``met`` when the awaited thing itself happened: the time came, the card closed, the
    pipeline or MR moved, the gate lifted, the awaited answer arrived).

    With ``decision_wake = "answer"`` a decision wait that has a request card ignores the
    addressee's progress notes on the waiting card and wakes on an answer only (see
    ``answers_on_source`` and ``request_answer``).

    Observable waits are checked before any age-based wake. A healthy unchanged target
    stays asleep; time waits wake only at their deadline. Age re-checks remain for human
    gates and targets this runner cannot read, where there is no successful observation."""
    action_required = False
    if typed_wait(wait):
        wait["observation_state"] = "unknown"
    wait.pop("ci_progress", None)
    if wait.get("settled") and not typed_wait(wait):
        wait["met"] = True
        return str(wait["settled"])          # the awaited result was final when the wait began
    since = parse_time(wait.get("since")) or now
    kind = wait.get("kind")
    if kind in ("time", "card", "decision", "pipeline", "mr") and (c := own_next_after(comments, agent_id, since)):
        # (a blocking ask is left out on purpose: a person's gate is lifted by the person, not by a NEXT)
        # the agent's newest word after the wait was declared is "continue now": the old WAIT line
        # (and its until) no longer stands
        wait["met"] = True
        return f"you declared ↻ NEXT after this wait ({(c.get('created_at') or '')[:16]}): the wait is superseded"
    foreign = foreign_comments(comments, agent_id, since)
    if comment_wake == "addressed":
        foreign = [x for x in foreign if wakes_on_comment(x, wait, agent_name, comment_wake_always_from)]
    answer_mode = decision_wake == "answer" and kind == "decision" and bool(wait.get("request"))
    held = []
    if answer_mode:
        # the addressee's progress notes stay in the thread: only an answer ends the wait
        held = [x for x in foreign if not answers_on_source(x, wait, agent_name, comment_wake_always_from)]
        foreign = [x for x in foreign if x not in held]
    c = foreign[-1] if foreign else None
    if c:
        who = c.get("author_name") or (c.get("author") or {}).get("name") or "someone"
        if kind in ("decision", "blocking") and any(answers_wait(x, wait) for x in foreign):
            wait["met"] = True               # a decision or an ask waits for exactly this
        return f"{who} commented: {(c.get('body') or '')[:300]}"
    if answer_mode:
        event, answered = request_answer(mesh, cats, wait, agent_id, since)
        if event:
            wait["met"] = answered
            return event
    elif kind == "decision" and wait.get("request") and not wait.get("attached_closed"):
        # A request closed with no comment on the waiting card is no answer, but nothing will ever
        # resume the wait: the asker gets it back to read the thread and decide (any mode).
        event, answered = request_answer(mesh, cats, wait, agent_id, since, closure_only=True)
        if event:
            wait["met"] = answered
            return event
    started = parse_time(wait.get("started")) or since
    first = first_recheck if first_recheck is not None else max_wait
    interval = max_wait if int(wait.get("rechecks", 0) or 0) else first
    if held:
        # An answer written as a plain comment would otherwise never end this wait: the first
        # held-back comment older than the re-check interval wakes the agent to read the thread.
        at = parse_time(held[0].get("created_at"))
        if at is not None and now - at >= interval:
            wait["recheck_due"] = True
            who = held[0].get("author_name") or (held[0].get("author") or {}).get("name") or "someone"
            return (f"re-check: {who} commented {_hours(now - at)} ago on the waiting card without an answer "
                    "marker, an @-mention of you or the request card id; read the thread, it may be the answer")
    if kind == "blocking":
        # a person's ask stands behind the human gate, which wakes it on the answer: an early
        # re-check only spends a turn and risks the question being asked again
        interval = max(interval, HUMAN_ASK_RECHECK)
    if (kind == "decision" and wait.get("person_ask") and not wait.get("recipient_id")
            and task.get("human_gate") is False):
        # A question to a person that stands behind no gate (cancelled, refused, or never raised by
        # Mesh) and no agent request: nobody can answer it, and only a comment would ever wake it.
        # Bounded re-check, never a verdict: an unread gate (key absent) or a request to an agent
        # keeps the wait asleep, and the owner decides in the woken turn.
        if now - started >= interval:
            wait["recheck_due"] = True
            return (f"re-check: decision wait @{wait.get('handle') or ''} has lasted {_hours(now - started)} "
                    "and no human gate or agent request stands behind it; the question may be gone. "
                    "Continue the work, or ask again with `❓ Blocking @person` if an answer is still needed")
        return ""
    if kind == "decision" and wait.get("attached_closed"):
        # the same question again, attached to its closed request without reopening it: only a
        # comment answers it, so after the re-check interval the asker gets it back, once per wait
        if now - started >= interval:
            wait["recheck_due"] = True
            return (f"re-check: decision wait @{wait.get('handle') or ''} repeats the question of request "
                    f"#{wait['attached_closed']}, which is closed and was not reopened, so nobody was asked again. "
                    "Continue with what the thread holds, or ask a materially different question")
        return ""
    if kind == "time":
        until = parse_time(wait.get("until"))
        if until and now >= until:
            wait["met"] = True
            return f"wait until {wait.get('until')} elapsed"
        return ""
    if kind == "blocking":
        if task.get("human_gate"):
            if now - started >= interval:
                wait["recheck_due"] = True
                return (f"re-check: the wait ({kind} {wait.get('target') or wait.get('handle') or ''}) has lasted "
                        f"{_hours(now - started)} with no change; re-check and decide")
            return ""
        wait["met"] = True
        return "the human gate was lifted"
    try:
        if kind == "card":
            try:
                target = mesh.request("GET", f"/tasks/by-short-id/{wait['target']}")
            except MeshError as e:
                if e.status in (404, 410):
                    wait["met"] = True
                    return f"task #{wait['target']} is gone"
                if e.status != 403:
                    raise
                # a card of another workspace: this agent may not read it, another agent of
                # this process (its own grant) may. Nobody can: a denial is not transient, so
                # it is not counted toward a wake (the wake re-declared the same wait, a loop);
                # the wait's own re-check still comes back to the agent.
                target = _read_elsewhere(f"s:{wait['target']}", mesh,
                                         lambda r: r.mesh.task_by_short(wait["target"]), strict=True)
                if target is None:
                    wait["unreadable"], wait["errors"] = True, 0
                    if now - started >= interval:
                        wait["recheck_due"] = True
                        return (f"re-check: the wait ({kind} {wait.get('target') or ''}) has lasted "
                                f"{_hours(now - started)}; its target is still unreadable")
                    return ""
            target = target.get("task", target)
            cat = cats.category(target)
            if cat in CLOSED:
                event = f"task #{wait['target']} is {cat}"
            elif satisfied(cats, target):
                event = f"task #{wait['target']} has shipped (still {cat})"
            else:
                event = _watched_comment(mesh, target, wait, since)
        elif kind in ("pipeline", "mr"):
            if gitlab is None:
                wait["integration_blocked"] = "GitLab observer unavailable"
                # UNKNOWN integration state, never a time result or an age-based recheck.
                return ""
            wait.pop("integration_blocked", None)
            if typed_wait(wait):
                observation = {}
                event = typed_event(gitlab, kind, wait["target"], wait,
                                    delivery_reader or "", observation,
                                    allow_action_required=allow_action_required, merge_gate=merge_gate)
                wait["observation_state"] = "changed" if event else "unchanged"
                if event:
                    action_required = bool(observation.pop("action_required", False))
                    wait["ci_action_observation" if action_required else "ci_observation"] = observation
                    if action_required:
                        wait["observation_state"] = "action_required"
                    elif wait.get("mr_state") or observation.get("value", "").rsplit(":", 1)[-1] in PIPELINE_DONE:
                        wait["ci_progress"] = observation["key"]
            elif kind == "pipeline":
                event = gitlab.pipeline_event(wait["target"], wait.setdefault("seen", {}))
            else:
                event = gitlab.mr_event(wait["target"], wait.setdefault("seen", {}), now)
            if not typed_wait(wait):
                progress = wait.get("seen", {}).pop("ci_progress", "")
                if event and progress:
                    wait["ci_progress"] = progress
        else:
            event = ""             # decision: only a comment wakes it (above)
    except Exception as e:         # transient or misconfigured: count, then tell the agent
        if kind == "card" and isinstance(e, MeshError) and e.status == 403:
            wait["unreadable"], wait["errors"] = True, 0     # e.g. its project's statuses: a denial, not a fault
            if now - started >= interval:
                wait["recheck_due"] = True
                return (f"re-check: the wait ({kind} {wait.get('target') or ''}) has lasted "
                        f"{_hours(now - started)}; its target is still unreadable")
            return ""
        wait["errors"] = int(wait.get("errors", 0)) + 1
        if wait.get("mr_state"):
            return ""
        if wait["errors"] >= max_errors:
            if typed_wait(wait):
                return f"cannot check the {kind} wait (UNKNOWN CI observation); baseline retained"
            return f"cannot check the {kind} wait ({type(e).__name__}: {str(e)[:120]}); check it yourself"
        return ""
    wait["errors"] = 0
    if not event:
        _negative_observation(wait, kind, comments, since, started, interval, now)
    if kind not in ("card", "pipeline", "mr", "decision") and now - started >= interval:
        wait["recheck_due"] = True
        return (f"re-check: the wait ({kind} {wait.get('target') or wait.get('handle') or ''}) has lasted "
                f"{_hours(now - started)} with no change; re-check and decide")
    if event and not action_required:
        wait["met"] = True
    return event
