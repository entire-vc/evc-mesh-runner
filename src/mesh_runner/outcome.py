"""End-of-turn outcome markers.

An agent ends a turn on a task by posting a comment whose declaration line says
what happens next. The runner reads the assignee's newest comment written after
the attempt started and takes the last declaration line in it:

    ⏳ WAIT pipeline:<group>/<project>#<id>[ jobs=[a,b]]   CI pipeline settles
    ⏳ WAIT mr:<group>/<project>!<iid>                     merge request changes
    ⏳ WAIT merge:<group>/<project>!<iid> after=<sha>      the runner runs the merge gate (opt-in)
    ⏳ WAIT card:#<8hex>                                   another task closes
    ⏳ WAIT decision:@<agent>                              another agent answers
    ⏳ WAIT <absoluteISO>: external=<reference> source=<https-url|card:#8hex> <why>
    ⏳ WAIT pipeline:<proj>#<id> until=success|change     a changed/successful pipeline (``after=`` optional)
    ⏳ WAIT pipeline:<proj>#<id> until=... jobs=[a,b]     the same, read from the named jobs only
    ↻ NEXT[: note]                                         continue right away
    ❓ Blocking @<person>: <question>                      a person must decide (Mesh raises the gate)

Anything after the target (": why", " — why", " | why") is an explanation and is
kept. ``pipeline:<project>!<iid>`` (a merge request number under the pipeline
kind) is read as the MR wait it names.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

DECLARATION = re.compile(r"^\s*(⏳\s*WAIT\b|↻\s*NEXT(?:\s*:|\s*$)|❓\s+(?:\*\*)?Blocking\b)")
_EXPLANATION = r"(?P<note>(?:\s*:.*|\s*[—–].*|\s*\|.*|\s+-(?:\s.*)?)?)$"
_WAIT_HEAD = r"^\s*⏳\s*WAIT\s+"

_JOBS = r"(?:\s+jobs=\[(?P<jobs>[^\]]*)\])?"
# ``jobs=[...]`` written between the target and ``until=`` is moved behind the baseline.
_JOBS_FIRST = re.compile(r"^(\s*⏳\s*WAIT\s+(?:mr|pipeline):\S+)(\s+jobs=\[[^\]]*\])(\s+until=\S+(?:\s+after=\S+)?)")
_PIPELINE = re.compile(
    _WAIT_HEAD + r"pipeline:(?P<project>[\w.\-/]+)#(?P<id>\d+)(?:\s+jobs=\[(?P<jobs>[^\]]*)\])?" + _EXPLANATION)
_MR = re.compile(_WAIT_HEAD + r"(?:mr|pipeline):(?P<project>[\w.\-/]+)!(?P<id>\d+)" + _EXPLANATION)
_CI_CHANGE = re.compile(
    _WAIT_HEAD + r"(?P<kind>mr|pipeline):(?P<project>[\w.\-/]+)(?P<sep>[!#])(?P<id>[1-9]\d{0,11})"
    r"\s+until=(?P<predicate>change|success)\s+after=(?P<sha>[0-9a-f]{40}|[0-9a-f]{64}):"
    r"(?P<pipeline>[1-9]\d{0,11}):(?P<status>created|waiting_for_resource|preparing|pending|running|success|failed|canceled|cancelled|skipped|manual|scheduled)"
    + _JOBS + _EXPLANATION)
# ``until=`` without ``after=``: the baseline is the state at the moment the wait is declared. The
# parser cannot read GitLab, so it only marks the wait (``extra["ci_default"]`` / ``["mr_default"]``)
# and the runner fills the baseline in (or falls back to the plain wait when it cannot read it).
_CI_DEFAULT = re.compile(
    _WAIT_HEAD + r"(?P<kind>mr|pipeline):(?P<project>[\w.\-/]+)(?P<sep>[!#])(?P<id>[1-9]\d{0,11})"
    r"\s+until=(?P<predicate>change|success)" + _JOBS + _EXPLANATION)
_MR_MERGED_DEFAULT = re.compile(
    _WAIT_HEAD + r"mr:(?P<project>[\w.\-/]+)!(?P<id>[1-9]\d{0,11})\s+until=merged" + _EXPLANATION)
_MR_STATE = re.compile(
    _WAIT_HEAD + r"mr:(?P<project>[\w.\-/]+)!(?P<id>[1-9]\d{0,11})"
    r"\s+until=merged\s+after=(?P<sha>[0-9a-f]{40}|[0-9a-f]{64}):opened" + _EXPLANATION)
_MERGE = re.compile(
    _WAIT_HEAD + r"merge:(?P<project>\w[\w.\-]*(?:/[\w.\-]+)+)!(?P<id>[1-9]\d{0,11})"
    r"\s+after=(?P<sha>[0-9a-f]{40}|[0-9a-f]{64})" + _EXPLANATION)
_MERGE_HEAD = re.compile(_WAIT_HEAD + r"merge:")
_CARD = re.compile(_WAIT_HEAD + r"card:#?(?P<id>[0-9a-f]{8})\b" + _EXPLANATION)
_DECISION = re.compile(_WAIT_HEAD + r"decision:@(?P<handle>[\w.\-]+)" + _EXPLANATION)
# Time is a structured assertion about a specific external schedule, not a retry interval.
# Any other WAIT is a time wait: `2h`, `tomorrow`, `in 2 hours`, `poll:` all get the time guidance.
_TIME_HEAD = re.compile(_WAIT_HEAD + r"(?!(?:pipeline|mr|merge|card|decision)\s*:)\S", re.IGNORECASE)
_EXTERNAL_TIME = re.compile(
    _WAIT_HEAD + r"(?P<when>\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?(?:Z|[+-]\d\d:\d\d)):"
    r"\s+external=(?P<external>[A-Za-z0-9][A-Za-z0-9._:/@#%+!?=~-]{0,255})"
    r"\s+source=(?P<source>https://[^\s]+|card:#[0-9a-f]{8})\s+(?P<why>\S.*)$")
_NEXT = re.compile(r"^\s*↻\s*NEXT(?:\s*:\s*(?P<note>.*))?\s*$")
_BLOCKING = re.compile(r"^\s*❓\s+(?:\*\*)?Blocking\s+@(?P<handle>[\w.\-]+)(?:\*\*)?\s*:?\s*(?P<note>.*)$")

MAX_ABSOLUTE = dt.timedelta(days=400)

TIME_WAIT_GUIDANCE = (
    "invalid external time wait: use `⏳ WAIT <absoluteISO-with-timezone>: "
    "external=<stable-reference> source=<https-url|card:#8hex> <why>`. "
    "The future timestamp must come from the cited external event/window, scheduled job, "
    "measurement endpoint, or provider Retry-After/quota reset. Describe cancellation in the comment. "
    "Relative intervals, bare dates, `poll:` and invented retries are not time events. "
    "MR/CI/card/answer dependencies use `⏳ WAIT mr:<group>/<project>!<iid>`, "
    "`⏳ WAIT pipeline:<group>/<project>#<id>`, `⏳ WAIT card:#<8hex>`, "
    "`⏳ WAIT decision:@<agent>` or `❓ Blocking @<person>: <question>`. "
    "If an observer is missing, report the integration blocker; do not replace the event with a timer."
)


@dataclass(frozen=True)
class Outcome:
    """What the agent declared. ``kind`` is one of: pipeline, mr, merge, card, decision,
    time, next, blocking, unparsed."""
    kind: str
    target: str = ""
    until: dt.datetime | None = None
    jobs: tuple[str, ...] = ()
    handle: str = ""
    note: str = ""
    line: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def is_wait(self) -> bool:
        return self.kind in {"pipeline", "mr", "merge", "card", "decision", "time"}


def _explicit_wait_problem(line: str) -> str:
    """Name what is wrong with a malformed typed VCS wait, so the agent can fix the line
    instead of repeating it (a bare "invalid" note led to the same line twice and a give-up)."""
    if re.search(r"until=change\b.*\bafter=[0-9a-f]{40,64}:opened\b", line):
        return ("invalid explicit VCS wait: the baseline `<sha>:opened` belongs to `until=merged`; "
                "`until=change`/`until=success` need `after=<sha>:<pipeline-id>:<status>`")
    if re.search(r"\bafter=[0-9a-f]{40,64}:(?:none|null|unknown|-)?(?::(?:none|null|unknown|-)?)?(?:\s|$)", line):
        return ("invalid explicit VCS wait: the baseline needs a real pipeline id and status, "
                "`after=<sha>:<pipeline-id>:<status>`; the head has no pipeline yet, so wait without "
                "until/after (`⏳ WAIT mr:<group>/<project>!<iid>`) or wait for the merge with "
                "`until=merged after=<sha>:opened`")
    return ("invalid explicit VCS wait: expected `until=change|success after=<full-sha>:<pipeline-id>:<status>` "
            "(optionally followed by `jobs=[<job>,<job>]`) or `until=merged after=<full-sha>:opened`")


def _jobs(m: re.Match) -> tuple[str, ...] | None:
    """The job names of a ``jobs=[...]`` field: () without the field, None when it names none."""
    if m.group("jobs") is None:
        return ()
    names = tuple(dict.fromkeys(j.strip() for j in m.group("jobs").split(",") if j.strip()))
    return names or None


JOBS_EMPTY = ("invalid explicit VCS wait: `jobs=[]` names no job; list the job names "
              "(`jobs=[browser-tests,lint]`) or drop the field to wait on the whole pipeline")


def declaration_lines(text: str) -> list[str]:
    return [line for line in (text or "").splitlines() if DECLARATION.match(line)]


def parse_line(line: str, now: dt.datetime, self_task8: str = "", merge: bool = False) -> Outcome:
    """``merge``: the runner is configured to run a merge gate for ``merge:`` waits."""
    note = lambda m: (m.group("note") or "").strip(" :—–-|")  # noqa: E731
    line = _JOBS_FIRST.sub(r"\1\3\2", line, count=1)
    if _MERGE_HEAD.match(line):
        if not merge:
            return Outcome("unparsed", note=(
                "invalid explicit VCS wait: `merge:` waits are not enabled on this runner; run the merge "
                "gate yourself and, to await the merge, use "
                "`⏳ WAIT mr:<group>/<project>!<iid> until=merged after=<full-head-sha>:opened`"), line=line)
        if m := _MERGE.match(line):
            return Outcome("merge", f"{m['project']}!{m['id']}", note=note(m), line=line,
                           extra={"merge": {"sha": m["sha"]}})
        return Outcome("unparsed", note=(
            "invalid explicit VCS wait: expected `⏳ WAIT merge:<group>/<project>!<iid> after=<full-head-sha>` "
            "(the full 40 or 64 hex-digit SHA of the reviewed head)"), line=line)
    if m := _MR_STATE.match(line):
        return Outcome("mr", f"{m['project']}!{m['id']}", note=note(m), line=line,
                       extra={"mr_state": {"predicate": "merged", "sha": m["sha"], "state": "opened"}})
    if m := _CI_CHANGE.match(line):
        kind = "mr" if m["sep"] == "!" else "pipeline"
        if (m["kind"] == "mr" and kind != "mr") or (kind == "pipeline" and m["pipeline"] != m["id"]):
            return Outcome("unparsed", note="CI baseline does not match target", line=line)
        jobs = _jobs(m)
        if jobs is None:
            return Outcome("unparsed", note=JOBS_EMPTY, line=line)
        contract = {"predicate": m["predicate"], "sha": m["sha"],
                    "pipeline": int(m["pipeline"]), "status": m["status"]}
        if jobs:
            contract["jobs"] = list(jobs)
        return Outcome(kind, f"{m['project']}{m['sep']}{m['id']}", jobs=jobs, note=note(m), line=line,
                       extra={"ci_change": contract})
    if m := _MR_MERGED_DEFAULT.match(line):
        return Outcome("mr", f"{m['project']}!{m['id']}", note=note(m), line=line,
                       extra={"mr_default": {"predicate": "merged"}})
    if m := _CI_DEFAULT.match(line):
        kind = "mr" if m["sep"] == "!" else "pipeline"
        if m["kind"] == "mr" and kind != "mr":
            return Outcome("unparsed", note="CI baseline does not match target", line=line)
        jobs = _jobs(m)
        if jobs is None:
            return Outcome("unparsed", note=JOBS_EMPTY, line=line)
        default = {"predicate": m["predicate"], **({"jobs": list(jobs)} if jobs else {})}
        return Outcome(kind, f"{m['project']}{m['sep']}{m['id']}", jobs=jobs, note=note(m), line=line,
                       extra={"ci_default": default})
    # Explicit fields are a contract, including when malformed; never seed a
    # legacy already-terminal wait from an invalid typed declaration.
    if re.match(_WAIT_HEAD + r"(?:mr|pipeline):[^\s]+\s+(?:until|after)\b", line):
        return Outcome("unparsed", note=_explicit_wait_problem(line), line=line)
    if m := _PIPELINE.match(line):
        jobs = _jobs(m)
        if jobs is None:
            return Outcome("unparsed", note=JOBS_EMPTY, line=line)
        if jobs:
            # the older spelling: read as `until=change jobs=[...]` with the baseline of the moment
            return Outcome("pipeline", f"{m['project']}#{m['id']}", jobs=jobs, note=note(m), line=line,
                           extra={"ci_default": {"predicate": "change", "jobs": list(jobs)}})
        return Outcome("pipeline", f"{m['project']}#{m['id']}", note=note(m), line=line)
    if m := _MR.match(line):
        return Outcome("mr", f"{m['project']}!{m['id']}", note=note(m), line=line)
    if m := _CARD.match(line):
        if self_task8 and m["id"] == self_task8:
            return Outcome("unparsed", note="a task cannot wait for itself", line=line)
        return Outcome("card", m["id"], note=note(m), line=line)
    if m := _DECISION.match(line):
        return Outcome("decision", handle=m["handle"], note=note(m), line=line)
    if m := _EXTERNAL_TIME.match(line):
        until = _parse_when(m["when"])
        # Never move a past event to now+5m, clamp a deadline, or invent a timezone.
        if until is None or until <= now or until > now + MAX_ABSOLUTE:
            return Outcome("unparsed", note=TIME_WAIT_GUIDANCE, line=line)
        from urllib.parse import urlsplit
        source = m["source"]
        try:
            url = urlsplit(source)
            _ = url.port  # Reject malformed ports as well as bracketed hosts.
        except ValueError:
            return Outcome("unparsed", note=TIME_WAIT_GUIDANCE, line=line)
        if (source.startswith("https://") and (not url.hostname or url.username or url.password)
                or m["external"].lower().startswith(("mr:", "pipeline:", "card:", "decision:", "poll:"))):
            return Outcome("unparsed", note=TIME_WAIT_GUIDANCE, line=line)
        why = m["why"].strip()
        if not why or why.lower().startswith("poll:"):
            return Outcome("unparsed", note=TIME_WAIT_GUIDANCE, line=line)
        external = {"event": m["external"], "source": source, "why": why, "at": until.isoformat()}
        return Outcome("time", until=until, note=why, line=line, extra={"external_time": external})
    if _TIME_HEAD.match(line):
        return Outcome("unparsed", note=TIME_WAIT_GUIDANCE, line=line)
    if m := _NEXT.match(line):
        return Outcome("next", note=(m["note"] or "").strip(), line=line)
    if m := _BLOCKING.match(line):
        return Outcome("blocking", handle=m["handle"], note=m["note"].strip(), line=line)
    return Outcome("unparsed", line=line)


def parse_outcome(text: str, now: dt.datetime, self_task8: str = "", merge: bool = False) -> Outcome | None:
    """Last declaration line of ``text``; None when the text declares nothing."""
    lines = declaration_lines(text)
    if not lines:
        return None
    return parse_line(lines[-1], now, self_task8, merge)


def _parse_when(value: str) -> dt.datetime | None:
    value = value.replace(" ", "T")
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        when = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return when


DECISION_WAIT_GUIDANCE = """\
If the condition is an unanswered question to another agent, end with
  ⏳ WAIT decision:@<recipient>: <same question>
When re-declaring an unchanged pending question, keep the same recipient and the same question:
Runner reuses the open request.
An unchanged question is not a reason for another polling turn. New comments/evidence
still wake a decision wait; only an addressed answer or explicit answer marker resolves it.
A known external scheduled event may use the structured time WAIT contract below, even if a separate
question remains unanswered. An internal retry/backoff is not a task schedule or Mesh start_after.
"""


_CONTRACT_TEMPLATE = """\
When you stop working on this task, post ONE comment on it whose last line declares what happens next:
  ⏳ WAIT pipeline:<group>/<project>#<id>   — CI must finish
  ⏳ WAIT mr:<group>/<project>!<iid>        — a merge request must change
  ⏳ WAIT card:#<8hex>                      — another task must close
  ⏳ WAIT decision:@<agent>: <question>     — another agent must answer
  ⏳ WAIT <absoluteISO-with-timezone>: external=<stable-reference> source=<https-url|card:#8hex> <why>
      — only a specific external event with a known timestamp and cited source
After observing a known CI result, wait for a NEW observation explicitly:
  ⏳ WAIT mr:<group>/<project>!<iid> until=change after=<full-head-sha>:<pipeline-id>:<status>
Use until=success to require a changed successful head pipeline; pending does not satisfy it.
Use pipeline:<group>/<project>#<id> with the same fields to observe retries of that exact pipeline.
To wait for specific jobs only, append jobs=[<job>,<job>] (GitLab job names): the wait is read from those jobs
and wakes once all are final, even while other jobs (manual siblings) leave the pipeline unfinished.
After you yourself run an authorized manual job, wait for its RESULT: until=success on that pipeline.
until=change on a baseline that is already final (manual, failed, canceled, success) is sent back: nothing changes
without your action. Act first (merge wrapper, start the job, push, retry), then wait on the new state.
Copy the baseline from GitLab, never infer it from prose. Update it after handling an event.
The baseline survives attempt/review changes. Runner durably suppresses an already delivered CI observation.
Typed CI waits do not wake merely because time passes.
To await the merge itself, use:
  ⏳ WAIT mr:<group>/<project>!<iid> until=merged after=<full-head-sha>:opened
Copy the observed opened MR's SHA. This baseline is NOT an approval pin: a later head may merge.
Only merged MR state completes this predicate; closing without merge wakes once as terminal failure.
Pipeline success/manual and head changes do not complete it. UNKNOWN retains the wait, without age wakes.
@@MERGE_WAIT@@Time waits require all structured fields above and a future exact timestamp from the source.
Describe why this time and when the wait is cancelled in the comment. No relative intervals, bare dates,
poll bypasses, or invented 6h/24h retries for dependencies. A missing observer is an integration blocker,
not permission to poll. Internal retry/backoff never writes a task's start_after.
Healthy observable targets stay asleep until an event; external time waits wake at their exact deadline.
  ↻ NEXT                                    — you did a piece of work (an edit, a commit, a check run, a result
                                              posted) and want to continue immediately. Reading the task and
                                              writing a plan is not a piece of work: keep working in the same turn.
  ❓ Blocking @<person>: <question>         — a person must decide; Mesh raises the gate from this line
If the task is finished, move it to its review/done status instead. Your session is kept:
when the condition is met you are resumed in this same conversation.
""" + "\n" + DECISION_WAIT_GUIDANCE


# Only for a runner that has the merge gate configured: elsewhere the parser rejects the wait.
MERGE_WAIT_GUIDANCE = "After a SHIP verdict on the current head, end with ⏳ WAIT merge:<group>/<project>!<iid> after=<full-head-sha>: the runner runs the merge gate itself and wakes you when merged or blocked. Do not re-run the merge gate in a loop yourself.\n"


def contract(merge: bool = False) -> str:
    """The outcome contract text; ``merge``: include the `WAIT merge:` guidance."""
    return _CONTRACT_TEMPLATE.replace("@@MERGE_WAIT@@", MERGE_WAIT_GUIDANCE if merge else "")


CONTRACT = contract()
