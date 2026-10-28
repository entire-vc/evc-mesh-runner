"""End-of-turn outcome markers.

An agent ends a turn on a task by posting a comment whose declaration line says
what happens next. The runner reads the assignee's newest comment written after
the attempt started and takes the last declaration line in it:

    ⏳ WAIT pipeline:<group>/<project>#<id>[ jobs=[a,b]]   CI pipeline settles
    ⏳ WAIT mr:<group>/<project>!<iid>                     merge request changes
    ⏳ WAIT merge:<group>/<project>!<iid> after=<sha>      the runner runs the merge gate (opt-in)
    ⏳ WAIT card:#<8hex>                                   another task closes
    ⏳ WAIT decision:@<agent>                              another agent answers
    ⏳ WAIT 30m   (or 2h, 2026-10-20, 2026-10-20T06:00Z)  time passes
    ⏳ WAIT pipeline:<proj>#<id> until=success|change     a changed/successful pipeline (``after=`` optional)
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

_PIPELINE = re.compile(
    _WAIT_HEAD + r"pipeline:(?P<project>[\w.\-/]+)#(?P<id>\d+)(?:\s+jobs=\[(?P<jobs>[^\]]*)\])?" + _EXPLANATION)
_MR = re.compile(_WAIT_HEAD + r"(?:mr|pipeline):(?P<project>[\w.\-/]+)!(?P<id>\d+)" + _EXPLANATION)
_CI_CHANGE = re.compile(
    _WAIT_HEAD + r"(?P<kind>mr|pipeline):(?P<project>[\w.\-/]+)(?P<sep>[!#])(?P<id>[1-9]\d{0,11})"
    r"\s+until=(?P<predicate>change|success)\s+after=(?P<sha>[0-9a-f]{40}|[0-9a-f]{64}):"
    r"(?P<pipeline>[1-9]\d{0,11}):(?P<status>created|waiting_for_resource|preparing|pending|running|success|failed|canceled|cancelled|skipped|manual|scheduled)"
    + _EXPLANATION)
# ``until=`` without ``after=``: the baseline is the state at the moment the wait is declared. The
# parser cannot read GitLab, so it only marks the wait (``extra["ci_default"]`` / ``["mr_default"]``)
# and the runner fills the baseline in (or falls back to the plain wait when it cannot read it).
_CI_DEFAULT = re.compile(
    _WAIT_HEAD + r"(?P<kind>mr|pipeline):(?P<project>[\w.\-/]+)(?P<sep>[!#])(?P<id>[1-9]\d{0,11})"
    r"\s+until=(?P<predicate>change|success)" + _EXPLANATION)
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
_DURATION = re.compile(
    _WAIT_HEAD + r"(?P<n>\d+)\s*(?P<unit>m|min|мин|h|ч)\b" + _EXPLANATION, re.IGNORECASE)
_ABSOLUTE = re.compile(
    _WAIT_HEAD + r"(?P<when>\d{4}-\d\d-\d\d(?:[T ]\d\d:\d\d(?::\d\d)?(?:Z|[+-]\d\d:?\d\d)?)?)" + _EXPLANATION)
_NEXT = re.compile(r"^\s*↻\s*NEXT(?:\s*:\s*(?P<note>.*))?\s*$")
_BLOCKING = re.compile(r"^\s*❓\s+(?:\*\*)?Blocking\s+@(?P<handle>[\w.\-]+)(?:\*\*)?\s*:?\s*(?P<note>.*)$")

# A time wait whose reason names an answer, a verdict, a person or a card is a poll for an event:
# the event has its own wait kind and wakes the session when it happens.
_EVENT_REASON = re.compile(
    r"verdict|answer|decision|\bapprov|вердикт|ответ|решени|павел|\bpavel\b|@[\w.\-]+|#[0-9a-f]{8}\b",
    re.IGNORECASE)

# A scheduled check of a channel outside Mesh: time is the condition by declaration.
_POLL_REASON = re.compile(r"poll\s*:", re.IGNORECASE)

MIN_WAIT = dt.timedelta(minutes=5)
MAX_WAIT = dt.timedelta(hours=24)
MAX_ABSOLUTE = dt.timedelta(days=400)
# An absolute WAIT whose moment is already here (or within this much) means "continue now".
PAST_GRACE = dt.timedelta(minutes=1)


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
            "or `until=merged after=<full-sha>:opened`")


def _event_reason_hint(reason: str) -> str:
    """Send-back text when a time wait's reason is an event (a verdict, an answer, a person, a
    card), else ''. Time is the condition only for a measurement window, a dated start, or a
    `poll:` check of a channel the runner cannot watch (a Telegram inbox, mail, an outside site):
    a person answering there leaves no Mesh event to wait on, so a timer is the only wake."""
    if _POLL_REASON.match(reason or "") or not _EVENT_REASON.search(reason or ""):
        return ""
    return ("a time wait must name time as the condition, but its reason waits for an event "
            "(a verdict, an answer, a decision, a person or a card). Wait on the event itself: "
            "`⏳ WAIT card:#<8hex>` for another task, `⏳ WAIT decision:@<agent>: <question>` for an "
            "agent's answer, `⏳ WAIT mr:<group>/<project>!<iid>` / `⏳ WAIT pipeline:<group>/<project>#<id>` "
            "for review or CI, `❓ Blocking @<person>: <question>` for a person. If the answer comes "
            "through a channel the runner cannot watch (Telegram, mail, an outside site), keep the time "
            "wait and start its reason with `poll:` (`⏳ WAIT 6h: poll: Telegram inbox for the reply`)")


def declaration_lines(text: str) -> list[str]:
    return [line for line in (text or "").splitlines() if DECLARATION.match(line)]


def parse_line(line: str, now: dt.datetime, self_task8: str = "", merge: bool = False) -> Outcome:
    """``merge``: the runner is configured to run a merge gate for ``merge:`` waits."""
    note = lambda m: (m.group("note") or "").strip(" :—–-|")  # noqa: E731
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
        return Outcome(kind, f"{m['project']}{m['sep']}{m['id']}", note=note(m), line=line,
                       extra={"ci_change": {"predicate": m["predicate"], "sha": m["sha"],
                                            "pipeline": int(m["pipeline"]), "status": m["status"]}})
    if m := _MR_MERGED_DEFAULT.match(line):
        return Outcome("mr", f"{m['project']}!{m['id']}", note=note(m), line=line,
                       extra={"mr_default": {"predicate": "merged"}})
    if m := _CI_DEFAULT.match(line):
        kind = "mr" if m["sep"] == "!" else "pipeline"
        if m["kind"] == "mr" and kind != "mr":
            return Outcome("unparsed", note="CI baseline does not match target", line=line)
        return Outcome(kind, f"{m['project']}{m['sep']}{m['id']}", note=note(m), line=line,
                       extra={"ci_default": {"predicate": m["predicate"]}})
    # Explicit fields are a contract, including when malformed; never seed a
    # legacy already-terminal wait from an invalid typed declaration.
    if re.match(_WAIT_HEAD + r"(?:mr|pipeline):[^\s]+\s+(?:until|after)\b", line):
        return Outcome("unparsed", note=_explicit_wait_problem(line), line=line)
    if m := _PIPELINE.match(line):
        jobs = tuple(j.strip() for j in (m.group("jobs") or "").split(",") if j.strip())
        return Outcome("pipeline", f"{m['project']}#{m['id']}", jobs=jobs, note=note(m), line=line)
    if m := _MR.match(line):
        return Outcome("mr", f"{m['project']}!{m['id']}", note=note(m), line=line)
    if m := _CARD.match(line):
        if self_task8 and m["id"] == self_task8:
            return Outcome("unparsed", note="a task cannot wait for itself", line=line)
        return Outcome("card", m["id"], note=note(m), line=line)
    if m := _DECISION.match(line):
        return Outcome("decision", handle=m["handle"], note=note(m), line=line)
    if m := _DURATION.match(line):
        if hint := _event_reason_hint(note(m)):
            return Outcome("unparsed", note=hint, line=line, extra={"event_reason": True})
        unit = m["unit"].lower()
        delta = dt.timedelta(hours=int(m["n"])) if unit in {"h", "ч"} else dt.timedelta(minutes=int(m["n"]))
        delta = min(max(delta, MIN_WAIT), MAX_WAIT)
        return Outcome("time", until=now + delta, note=note(m), line=line)
    if m := _ABSOLUTE.match(line):
        until = _parse_when(m["when"])
        if until is None or until > now + MAX_ABSOLUTE:
            return Outcome("unparsed", note="time target out of range", line=line)
        if until <= now + PAST_GRACE:
            # the moment is already here: nothing to wait for, continue now
            return Outcome("next", note=note(m), line=line, extra={"hint": (
                f"your `⏳ WAIT {m['when']}` named a moment that had already passed, so the runner continued "
                "you at once; declare a future moment, a duration, or the event itself")})
        if hint := _event_reason_hint(note(m)):
            return Outcome("unparsed", note=hint, line=line, extra={"event_reason": True})
        return Outcome("time", until=max(until, now + MIN_WAIT), note=note(m), line=line)
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
An unchanged question is not a reason for another WAIT 2h polling turn. New comments/evidence
still wake a decision wait; only an addressed answer or explicit answer marker resolves it.
Use a time WAIT when the deadline itself requires useful work (a scheduled check or action),
even if a separate question remains unanswered. Do not turn actionable schedules into decision waits.
"""


_CONTRACT_TEMPLATE = """\
When you stop working on this task, post ONE comment on it whose last line declares what happens next:
  ⏳ WAIT pipeline:<group>/<project>#<id>   — CI must finish
  ⏳ WAIT mr:<group>/<project>!<iid>        — a merge request must change
  ⏳ WAIT card:#<8hex>                      — another task must close
  ⏳ WAIT decision:@<agent>: <question>     — another agent must answer
  ⏳ WAIT 2h: <why, and what ends it>       — only when time itself is the condition
  ⏳ WAIT 2026-10-20T06:00Z: <why>           — the same, as a moment (UTC)
After observing a known CI result, wait for a NEW observation explicitly:
  ⏳ WAIT mr:<group>/<project>!<iid> until=change after=<full-head-sha>:<pipeline-id>:<status>
Use until=success to require a changed successful head pipeline; pending does not satisfy it.
Use pipeline:<group>/<project>#<id> with the same fields to observe retries of that exact pipeline.
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
@@MERGE_WAIT@@A time wait over 1h without a reason, or one that names an MR, a card or a pipeline, is sent back:
wait on the event itself. Healthy observable targets stay asleep until an event; time waits wake at
their deadline. Age re-checks apply only where the target cannot be observed or a human gate requires one.
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
