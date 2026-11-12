"""End-of-turn outcome markers.

An agent ends a turn on a task by posting a comment whose declaration line says
what happens next. The runner reads the assignee's newest comment written after
the attempt started and takes the last declaration line in it:

    ⏳ WAIT pipeline:<group>/<project>#<id>[ jobs=[a,b]]   CI pipeline settles
    ⏳ WAIT mr:<group>/<project>!<iid>                     merge request changes
    ⏳ WAIT merge:<group>/<project>!<iid> after=<sha>      the runner runs the merge gate (opt-in)
    ⏳ WAIT card:#<8hex>                                   another task closes
    ⏳ WAIT decision:@<agent>                              another agent answers
    ⏳ WAIT gmail:gmr_<12hex>                              a registered Gmail observation fires (docs/gmail-watch.md)
    ⏳ WAIT <absoluteISO>: external=<reference> source=<https-url|card:#8hex> <why>
    ⏳ WAIT pipeline:<proj>#<id> until=success|change     a changed/successful pipeline (``after=`` optional)
    ⏳ WAIT pipeline:<proj>#<id> until=... jobs=[a,b]     the same, read from the named jobs only
    ↻ NEXT[: note]                                         continue right away
    ❓ Blocking @<person>: <question>                      a person must decide (Mesh raises the gate)

Anything after the target (": why", " — why", " | why") is an explanation and is
kept. ``mr:`` / ``pipeline:`` targets may carry an explicit provider, ``mr:github:org/repo!N``,
``mr:gitlab:group/project!N``, ``pipeline:github:org/repo#N``; the explicit selector always wins over the
configured GitHub allowlist and is never read from the other provider. A bare target keeps the allowlist
inference. ``merge:`` has no qualified form. ``pipeline:<project>!<iid>`` (a merge request number under the pipeline
kind) is read as the MR wait it names.
"""
from __future__ import annotations

import dataclasses
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
    _WAIT_HEAD + r"pipeline:(?P<project>(?:(?:github|gitlab):)?[\w.\-/]+)#(?P<id>\d+)(?:\s+jobs=\[(?P<jobs>[^\]]*)\])?" + _EXPLANATION)
_MR = re.compile(_WAIT_HEAD + r"(?:mr|pipeline):(?P<project>(?:(?:github|gitlab):)?[\w.\-/]+)!(?P<id>\d+)" + _EXPLANATION)
_CI_CHANGE = re.compile(
    _WAIT_HEAD + r"(?P<kind>mr|pipeline):(?P<project>(?:(?:github|gitlab):)?[\w.\-/]+)(?P<sep>[!#])(?P<id>[1-9]\d{0,11})"
    r"\s+until=(?P<predicate>change|success)\s+after=(?P<sha>[0-9a-f]{40}|[0-9a-f]{64}):"
    r"(?P<pipeline>[1-9]\d{0,11}):(?P<status>created|waiting_for_resource|preparing|pending|running|success|failed|canceled|cancelled|skipped|manual|scheduled)"
    + _JOBS + _EXPLANATION)
# ``until=`` without ``after=``: the baseline is the state at the moment the wait is declared. The
# parser cannot read GitLab, so it only marks the wait (``extra["ci_default"]`` / ``["mr_default"]``)
# and the runner fills the baseline in (or falls back to the plain wait when it cannot read it).
_CI_DEFAULT = re.compile(
    _WAIT_HEAD + r"(?P<kind>mr|pipeline):(?P<project>(?:(?:github|gitlab):)?[\w.\-/]+)(?P<sep>[!#])(?P<id>[1-9]\d{0,11})"
    r"\s+until=(?P<predicate>change|success)" + _JOBS + _EXPLANATION)
_MR_MERGED_DEFAULT = re.compile(
    _WAIT_HEAD + r"mr:(?P<project>(?:(?:github|gitlab):)?[\w.\-/]+)!(?P<id>[1-9]\d{0,11})\s+until=merged" + _EXPLANATION)
_MR_STATE = re.compile(
    _WAIT_HEAD + r"mr:(?P<project>(?:(?:github|gitlab):)?[\w.\-/]+)!(?P<id>[1-9]\d{0,11})"
    r"\s+until=merged\s+after=(?P<sha>[0-9a-f]{40}|[0-9a-f]{64}):opened" + _EXPLANATION)
_MERGE = re.compile(
    _WAIT_HEAD + r"merge:(?P<project>\w[\w.\-]*(?:/[\w.\-]+)+)!(?P<id>[1-9]\d{0,11})"
    r"\s+after=(?P<sha>[0-9a-f]{40}|[0-9a-f]{64})" + _EXPLANATION)
_MERGE_HEAD = re.compile(_WAIT_HEAD + r"merge:")
_RECEIPT = re.compile(_WAIT_HEAD + r"receipt:#(?P<id>[0-9a-f]{8})\b" + _EXPLANATION)
_LIFECYCLE = re.compile(_WAIT_HEAD + r"lifecycle:#(?P<id>[0-9a-f]{8})\b" + _EXPLANATION)
_STALL = re.compile(_WAIT_HEAD + r"stall:#(?P<id>[0-9a-f]{8})\b" + _EXPLANATION)
# A lead-decided passive wait (card labelled `wake:manual`): no observer, no timer; only a
# comment by someone else (or the owner's own NEXT) resumes it.
_MANUAL = re.compile(_WAIT_HEAD + r"manual:#(?P<id>[0-9a-f]{8})\b" + _EXPLANATION)
_RECOVERY = re.compile(_WAIT_HEAD + r"recovery:#(?P<id>[0-9a-f]{8})\b" + _EXPLANATION)
_GMAIL = re.compile(_WAIT_HEAD + r"gmail:(?P<id>gmr_[0-9a-f]{12})\b" + _EXPLANATION)
_NATIVE = re.compile(_WAIT_HEAD + r"native:(?P<id>[0-9a-f]{64})" + _EXPLANATION)
# ``until=<typed condition>`` names what the edge of this WAIT waits for (instead of the broad task_done).
CARD_CONDITIONS = ("access_ready", "contract_ready", "artifact_published", "shipped", "deployed", "accepted")
_CARD_UNTIL = r"(?:\s+until=(?P<condition>" + "|".join(CARD_CONDITIONS) + r"))?"
_CARD = re.compile(_WAIT_HEAD + r"card:#?(?P<id>[0-9a-f]{8})\b" + _CARD_UNTIL + _EXPLANATION)
_QUEUE = re.compile(_WAIT_HEAD + r"queue:#?(?P<id>[0-9a-f]{8})\b" + _EXPLANATION)
_DECISION = re.compile(_WAIT_HEAD + r"decision:@(?P<handle>[\w.\-]+)" + _EXPLANATION)
# Time is a structured assertion about a specific external schedule, not a retry interval.
# Any other WAIT is a time wait: `2h`, `tomorrow`, `in 2 hours`, `poll:` all get the time guidance.
_TIME_HEAD = re.compile(_WAIT_HEAD + r"(?!(?:pipeline|mr|merge|card|decision|receipt|lifecycle|recovery|stall|gmail|manual)\s*:)\S", re.IGNORECASE)
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
    "or measurement endpoint. Describe cancellation in the comment. "
    "Provider quota, account resets and Retry-After belong to account routing, never a task WAIT. "
    "Relative intervals, bare dates, `poll:` and invented retries are not time events. "
    "MR/CI/card/answer dependencies use `⏳ WAIT mr:<group>/<project>!<iid>`, "
    "`⏳ WAIT pipeline:<group>/<project>#<id>`, `⏳ WAIT card:#<8hex>`, "
    "`⏳ WAIT decision:@<agent>` or `❓ Blocking @<person>: <question>`. "
    "If an observer is missing, report the integration blocker; do not replace the event with a timer."
)


QUOTA_TIME_WAIT_GUIDANCE = (
    "Provider quota/reset/Retry-After is account availability, never a task time WAIT or start_after. "
    "A future account reset alone does not mean exhaustion. Continue this task through the existing "
    "account routing/admission policy; do not change its role, checkpoint, session or human gates. "
    "Use a task WAIT only for its actual external resource/dependency."
)
_PROVIDER_CAPACITY = re.compile(
    r"quota|квот|rate[ _-]?limit|usage[ _-]?limit|weekly[ _-]?limit|лимит.{0,30}аккаунт"
    r"|retry[ _-]?after|provider[ _:/-]+retry"
    r"|(?:codex|claude|provider|account|аккаунт).{0,80}(?:reset|сброс)"
    r"|(?:reset|сброс).{0,80}(?:codex|claude|provider|account|аккаунт)", re.I)


def quota_time_wait(wait: dict) -> bool:
    """Recognize prohibited account-capacity predicates, including persisted old waits.

    Resetting an unrelated resource remains a valid external event. The check
    uses the declaration/evidence only, never an account's reset timestamp alone.
    """
    if wait.get("kind") != "time":
        return False
    external = wait.get("external_time") or {}
    external = external if isinstance(external, dict) else {}
    text = " ".join(str(v or "") for v in (wait.get("line"), wait.get("note"),
        external.get("event"), external.get("why"), external.get("source")))
    return bool(_PROVIDER_CAPACITY.search(text))


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
    # Where a typed outcome came from (Mesh turn-outcome row id, wait id and kind, decision request).
    # Provenance only: left out of equality, so a typed outcome equals the one its marker line parses to.
    typed: dict = field(default_factory=dict, compare=False)

    @property
    def is_wait(self) -> bool:
        return self.kind in {"pipeline", "mr", "merge", "card", "decision", "time", "queue", "receipt", "lifecycle", "recovery", "stall", "native", "gmail", "manual"}


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


GITHUB_MR_CI_UNSUPPORTED = (
    "invalid explicit VCS wait: a GitHub pull request carries no head pipeline, so `mr:github:… until=change|success` "
    "has nothing to observe; wait on the Actions run itself with `⏳ WAIT pipeline:github:<org>/<repo>#<run-id> "
    "until=change|success after=<full-sha>:<run-id>:<status>`, or on the merge with "
    "`⏳ WAIT mr:github:<org>/<repo>!<n> until=merged after=<full-head-sha>:opened`")


MERGE_NOT_ENABLED = (
    "invalid explicit VCS wait: `merge:` waits are not enabled on this runner; run the merge "
    "gate yourself and, to await the merge, use "
    "`⏳ WAIT mr:<group>/<project>!<iid> until=merged after=<full-head-sha>:opened`")


def declaration_lines(text: str) -> list[str]:
    return [line for line in (text or "").splitlines() if DECLARATION.match(line)]


def parse_line(line: str, now: dt.datetime, self_task8: str = "", merge: bool = False) -> Outcome:
    """``merge``: the runner is configured to run a merge gate for ``merge:`` waits."""
    note = lambda m: (m.group("note") or "").strip(" :—–-|")  # noqa: E731
    line = _JOBS_FIRST.sub(r"\1\3\2", line, count=1)
    if _MERGE_HEAD.match(line):
        if not merge:
            return Outcome("unparsed", note=MERGE_NOT_ENABLED, line=line)
        if re.match(_WAIT_HEAD + r"merge:(?:github|gitlab):", line):
            return Outcome("unparsed", note=(
                "invalid explicit VCS wait: `merge:` has no provider-qualified form; the merge gate reads the "
                "configured GitLab only. Use `⏳ WAIT merge:<group>/<project>!<iid> after=<full-head-sha>` for "
                "GitLab, or `⏳ WAIT mr:github:<org>/<repo>!<n> until=merged after=<full-head-sha>:opened` to "
                "observe a GitHub pull request"), line=line)
        if m := _MERGE.match(line):
            return Outcome("merge", f"{m['project']}!{m['id']}", note=note(m), line=line,
                           extra={"merge": {"sha": m["sha"]}})
        return Outcome("unparsed", note=(
            "invalid explicit VCS wait: expected `⏳ WAIT merge:<group>/<project>!<iid> after=<full-head-sha>` "
            "(the full 40 or 64 hex-digit SHA of the reviewed head)"), line=line)
    if m := re.match(_WAIT_HEAD + r"mr:github:\S+\s+until=(?:change|success)\b", line):
        return Outcome("unparsed", note=GITHUB_MR_CI_UNSUPPORTED, line=line)
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
    if re.match(_WAIT_HEAD + r"ops-review:", line):
        return Outcome("unparsed", note="ops-review WAIT is retired; submit the source task with finish_task to=review", line=line)
    if m := _NATIVE.match(line):
        return Outcome("native", m["id"], note=note(m), line=line)
    if re.match(_WAIT_HEAD + r"native:", line):
        return Outcome("unparsed", note="native WAIT requires an exact 64-hex registration", line=line)
    if m := _CARD.match(line):
        if self_task8 and m["id"] == self_task8:
            return Outcome("unparsed", note="a task cannot wait for itself", line=line)
        return Outcome("card", m["id"], note=note(m), line=line)
    if m := _RECEIPT.match(line):
        # Event wait on this card's own armed receipt registration; runner validates it.
        return Outcome("receipt", m["id"], note=note(m), line=line)
    if m := _GMAIL.match(line):
        # Event wait on an operator-registered Gmail observation; runner validates it.
        return Outcome("gmail", m["id"], note=note(m), line=line)
    if re.match(_WAIT_HEAD + r"gmail:", line):
        return Outcome("unparsed", note="gmail WAIT requires an exact registration id gmr_<12 hex>", line=line)
    if m := _STALL.match(line):
        # Event wait on this card's own armed stall registration; runner validates it.
        return Outcome("stall", m["id"], note=note(m), line=line)
    if m := _RECOVERY.match(line):
        # Event wait on this card's own armed recovery registration; runner validates it.
        return Outcome("recovery", m["id"], note=note(m), line=line)
    if m := _LIFECYCLE.match(line):
        # Event wait on this card's own armed lifecycle registration; runner validates it.
        return Outcome("lifecycle", m["id"], note=note(m), line=line)
    if m := _MANUAL.match(line):
        # Passive wait on this card itself; runner validates the target and the `wake:manual` label.
        return Outcome("manual", m["id"], note=note(m), line=line)
    if re.match(_WAIT_HEAD + r"manual:", line):
        return Outcome("unparsed", note=MANUAL_WAIT_GUIDANCE, line=line)
    if m := _QUEUE.match(line):
        # Event wait on this card's own machine queue request; runner validates the source.
        return Outcome("queue", m["id"], note=note(m), line=line)
    if m := _DECISION.match(line):
        return Outcome("decision", handle=m["handle"], note=note(m), line=line)
    if _TIME_HEAD.match(line) and quota_time_wait({"kind": "time", "line": line}):
        return Outcome("unparsed", note=QUOTA_TIME_WAIT_GUIDANCE, line=line)
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


# ----------------------------------------------------------------------------- typed outcomes
# An agent may end its turn through Mesh's turn commands (`wait_for`, `continue_next`, `ask_agent`, `ask_human`)
# instead of a marker line. Mesh has then validated the target and stored a turn-outcomes row and, for a
# wait, a task-wait row (evc-mesh internal/domain/typed_outcome.go: TurnOutcome, TaskWait). ``from_typed``
# builds the Outcome straight from those rows, with the same fields the marker parser gives for the
# equivalent line: the row is never turned into text and parsed back, so what the line grammar cannot
# spell (a ``comment:<uuid>`` source, an event_ref with spaces, fractional seconds) is not lost.
# ``line`` is still filled in: it is what the attempt's wait records and shows, and what a few readers of
# a stored wait look at (a card wait's ``until=`` condition, a time wait's https source).

# Mesh's until states in this grammar's words.
_TYPED_UNTIL = {"merged": "merged", "green": "success", "changed": "change"}
_TYPED_PIPELINE_UNTIL = ("finished", "green", "changed")
# The pipeline statuses a CI baseline may name (the ``after=<sha>:<pipeline>:<status>`` grammar).
CI_STATUSES = ("created", "waiting_for_resource", "preparing", "pending", "running", "success", "failed",
               "canceled", "cancelled", "skipped", "manual", "scheduled")
_HEAD_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_MERGE_PROJECT = re.compile(r"\w[\w.\-]*(?:/[\w.\-]+)+")
_HANDLE = re.compile(r"[\w.\-]+")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Mesh's time evidence sources: an https URL or a Mesh receipt (domain.ParseEvidenceSource); the marker
# grammar's ``card:#<8hex>`` is accepted as well.
_RECEIPT_SOURCE = re.compile(r"(?:comment|artifact):" + _UUID.pattern + r"|card:#[0-9a-f]{8}")
_NOT_TIME_EVENTS = ("mr:", "pipeline:", "card:", "decision:", "poll:")


def _flat(value) -> str:
    return " ".join(str(value or "").split())


def _typed_note(row: dict, wait: dict | None) -> str:
    """The explanation of a typed outcome: the wait's own note, else the outcome's."""
    return _flat((wait or {}).get("note")) or _flat(row.get("note"))


def _provenance(row: dict, wait: dict | None) -> dict:
    out = {"id": str(row.get("id") or ""), "kind": str(row.get("kind") or "")}
    if isinstance(wait, dict):
        out.update(wait_id=str(wait.get("id") or row.get("wait_id") or ""), wait_kind=str(wait.get("kind") or ""))
        if wait.get("kind") == "card" and wait.get("server_observed") is True:
            out["server_observed"] = True
        for key in ("decision_request_id", "human_gate_ref"):
            if wait.get(key):
                out[key] = str(wait[key])
    return out


def _typed_name(row: dict, wait: dict | None) -> str:
    """How a typed outcome is named in a correction: `wait_for card (outcome o-1, wait w-1)`."""
    what = str(row.get("kind") or "?")
    if isinstance(wait, dict):
        what += f" on {wait.get('kind') or '?'}"
    ids = ", ".join(f"{k} {v}" for k, v in (("outcome", row.get("id")), ("wait", (wait or {}).get("id")
                                                                         or row.get("wait_id"))) if v)
    return f"Mesh turn outcome `{what}`" + (f" ({ids})" if ids else "")


def typed_unparsed(row: dict, wait: dict | None, problem: str) -> Outcome:
    """A typed outcome this runner cannot act on. The note names the row and what was not understood,
    so the correction turn tells the agent exactly that (a bare "no outcome" made it repeat the call)."""
    name = _typed_name(row, wait)
    return Outcome("unparsed", line=name, typed=_provenance(row, wait), note=(
        f"{name} was recorded, but this runner cannot act on it: {problem}. Mesh may hold the wait; "
        "end the turn again with an outcome this runner reads (a supported turn command or the "
        "equivalent marker line)"))


def _typed_next(row: dict, wait: dict | None, now: dt.datetime, task8: str) -> Outcome:
    note = _flat(row.get("note"))
    return Outcome("next", note=note, line="↻ NEXT" + (f": {note}" if note else ""))


def _tail(note: str) -> str:
    return f": {note}" if note else ""


def _typed_card(row: dict, wait: dict, now: dt.datetime, task8: str) -> Outcome:
    target_id = str(wait.get("target_task_id") or "").lower()
    if not target_id and wait.get("server_observed") is True and wait.get("resolved_at"):
        # ON DELETE SET NULL may erase the target before the agent exits.
        # The source receipt still settles this turn and wakes it as invalid.
        return Outcome("card", note=_typed_note(row, wait), line="Mesh resolved the source card wait")
    if not _UUID.fullmatch(target_id):
        return typed_unparsed(row, wait, f"target_task_id `{target_id or 'missing'}` is not a card id")
    target = target_id[:8]
    if task8 and target == task8:
        return typed_unparsed(row, wait, "a task cannot wait for itself")
    cond = str(wait.get("target_condition") or "")
    if cond and cond != "task_done" and cond not in CARD_CONDITIONS:
        return typed_unparsed(row, wait, f"target_condition `{cond}` is unknown here")
    note = _typed_note(row, wait)
    line = f"⏳ WAIT card:#{target}" + (f" until={cond}" if cond in CARD_CONDITIONS else "") + _tail(note)
    return Outcome("card", target, note=note, line=line)


def _typed_project(row: dict, wait: dict, number_field: str) -> tuple[str, int] | Outcome:
    project = str(wait.get("project_path") or "")
    try:
        number = int(wait.get(number_field) or 0)
    except (TypeError, ValueError):
        number = 0
    if not re.fullmatch(r"[\w.\-/]+", project) or number < 1:
        return typed_unparsed(row, wait, f"project_path `{project or 'missing'}` / {number_field} "
                                         f"`{wait.get(number_field) or 'missing'}` name no target")
    return project, number


def _typed_jobs(wait: dict) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(j).strip() for j in (wait.get("required_jobs") or []) if str(j).strip()))


def _typed_baseline(row: dict, wait: dict, until: str, number: int) -> dict | Outcome | None:
    """The ``after=`` baseline an mr/pipeline wait was declared against: None without one, the baseline
    in the grammar's words (``sha`` + ``state`` for until merged, ``sha`` + ``pipeline`` + ``status`` for
    green/changed), or a precise ``unparsed`` naming the field the marker grammar could not carry."""
    sha, pid, status = (wait.get(k) for k in ("after_sha", "after_pipeline_id", "after_status"))
    if sha in (None, "") and pid in (None, "", 0) and status in (None, ""):
        return None
    if until not in ("merged", "green", "changed"):
        return typed_unparsed(row, wait, f"until `{until or 'missing'}` takes no baseline "
                                         "(after_sha / after_pipeline_id / after_status)")
    sha = str(sha or "")
    if not _HEAD_SHA.fullmatch(sha):
        return typed_unparsed(row, wait, f"after_sha `{sha[:70] or 'missing'}` is not a full 40 or 64 "
                                         "hex-digit head SHA")
    if until == "merged":
        if status != "opened" or pid not in (None, "", 0):
            return typed_unparsed(row, wait, "the baseline of until merged is after_sha with after_status "
                                             f"`opened` and no after_pipeline_id (got after_status `{status}`)")
        return {"sha": sha, "state": "opened"}
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        pid = 0
    if not 1 <= pid < 10 ** 12:
        return typed_unparsed(row, wait, f"after_pipeline_id `{wait.get('after_pipeline_id') or 'missing'}` "
                                         "is not a pipeline id")
    if wait.get("kind") == "pipeline" and pid != number:
        return typed_unparsed(row, wait, f"after_pipeline_id `{pid}` is not the awaited pipeline_id `{number}`")
    if status not in CI_STATUSES:
        return typed_unparsed(row, wait, f"after_status `{status or 'missing'}` is not a pipeline status")
    return {"sha": sha, "pipeline": pid, "status": status}


def _typed_ci(kind: str, target: str, until: str, baseline: dict | None, jobs: tuple[str, ...],
              note: str) -> Outcome:
    """An mr/pipeline wait with an ``until=``: the Outcome the marker line with the same fields parses to.
    ``baseline`` was checked by ``_typed_baseline``; the plain ``finished`` wait is not built here."""
    if until == "merged":
        line = f"⏳ WAIT mr:{target} until=merged" + (f" after={baseline['sha']}:opened" if baseline else "")
        extra = ({"mr_state": {"predicate": "merged", **baseline}} if baseline
                 else {"mr_default": {"predicate": "merged"}})
        return Outcome("mr", target, note=note, line=line + _tail(note), extra=extra)
    predicate = _TYPED_UNTIL.get(until) or "change"       # a pipeline "finished" with jobs: until=change
    line = f"⏳ WAIT {kind}:{target} until={predicate}"
    if baseline:
        line += f" after={baseline['sha']}:{baseline['pipeline']}:{baseline['status']}"
        contract = {"predicate": predicate, **baseline}
        key = "ci_change"
    else:
        contract, key = {"predicate": predicate}, "ci_default"
    if jobs:
        line += f" jobs=[{','.join(jobs)}]"
        contract["jobs"] = list(jobs)
    return Outcome(kind, target, jobs=jobs, note=note, line=line + _tail(note), extra={key: contract})


def _typed_mr(row: dict, wait: dict, now: dt.datetime, task8: str) -> Outcome:
    where = _typed_project(row, wait, "mr_iid")
    if isinstance(where, Outcome):
        return where
    provider = str(wait.get("provider") or "")
    if provider not in ("", "gitlab", "github"):
        return typed_unparsed(row, wait, f"provider `{provider}` is unknown here")
    until = str(wait.get("until") or "")
    if until and until not in _TYPED_UNTIL:
        return typed_unparsed(row, wait, f"MR until `{until}` is unknown here")
    note = _typed_note(row, wait)
    # A GitLab MR keeps the bare target (the configured provider, as 0.2.1 did); GitHub is always named.
    target = ("github:" if provider == "github" else "") + f"{where[0]}!{where[1]}"
    if provider == "github" and until in ("green", "changed"):
        line = f"⏳ WAIT mr:{target} until={_TYPED_UNTIL[until]}" + _tail(note)
        return Outcome("unparsed", note=GITHUB_MR_CI_UNSUPPORTED, line=line)
    jobs = _typed_jobs(wait)
    if jobs and until not in ("green", "changed"):
        return typed_unparsed(row, wait, f"required_jobs are read only with until green or changed, "
                                         f"not `{until or 'missing'}`")
    baseline = _typed_baseline(row, wait, until, where[1])
    if isinstance(baseline, Outcome):
        return baseline
    if not until:
        return Outcome("mr", target, note=note, line=f"⏳ WAIT mr:{target}" + _tail(note))
    return _typed_ci("mr", target, until, baseline, jobs, note)


def _typed_pipeline(row: dict, wait: dict, now: dt.datetime, task8: str) -> Outcome:
    """A CI pipeline, or a GitHub Actions run (``provider: github``, named in the target as the marker
    grammar's ``pipeline:github:<owner>/<repo>#<run-id>`` is)."""
    where = _typed_project(row, wait, "pipeline_id")
    if isinstance(where, Outcome):
        return where
    provider = str(wait.get("provider") or "")
    if provider not in ("", "gitlab", "github"):
        return typed_unparsed(row, wait, f"provider `{provider}` is unknown here")
    until = str(wait.get("until") or "finished")
    if until not in _TYPED_PIPELINE_UNTIL:
        return typed_unparsed(row, wait, f"pipeline until `{until}` is unknown here")
    jobs = _typed_jobs(wait)
    note = _typed_note(row, wait)
    target = ("github:" if provider == "github" else "") + f"{where[0]}#{where[1]}"
    baseline = _typed_baseline(row, wait, until, where[1])
    if isinstance(baseline, Outcome):
        return baseline
    if until == "finished" and not jobs:
        return Outcome("pipeline", target, note=note, line=f"⏳ WAIT pipeline:{target}" + _tail(note))
    return _typed_ci("pipeline", target, until, baseline, jobs, note)


def _typed_merge(row: dict, wait: dict, now: dt.datetime, task8: str) -> Outcome:
    """The runner's merge gate on a reviewed head (``⏳ WAIT merge:``): GitLab only, no other fields.
    Whether this runner has the gate configured is decided by ``from_typed``, as the parser does."""
    provider = str(wait.get("provider") or "")
    if provider not in ("", "gitlab"):
        return typed_unparsed(row, wait, f"provider `{provider}`: a merge wait is GitLab only")
    project = str(wait.get("project_path") or "")
    try:
        iid = int(wait.get("mr_iid") or 0)
    except (TypeError, ValueError):
        iid = 0
    if not _MERGE_PROJECT.fullmatch(project) or not 1 <= iid < 10 ** 12:
        return typed_unparsed(row, wait, f"project_path `{project or 'missing'}` / mr_iid "
                                         f"`{wait.get('mr_iid') or 'missing'}` name no merge request")
    sha = str(wait.get("after_sha") or "")
    if not _HEAD_SHA.fullmatch(sha):
        return typed_unparsed(row, wait, f"after_sha `{sha[:70] or 'missing'}` is not a full 40 or 64 "
                                         "hex-digit head SHA")
    extra_fields = [k for k in ("until", "after_pipeline_id", "after_status") if wait.get(k)]
    if extra_fields or _typed_jobs(wait):
        return typed_unparsed(row, wait, "a merge wait takes project_path, mr_iid and after_sha only, not "
                                         + ", ".join(extra_fields or ["required_jobs"]))
    note = _typed_note(row, wait)
    target = f"{project}!{iid}"
    return Outcome("merge", target, note=note, line=f"⏳ WAIT merge:{target} after={sha}" + _tail(note),
                   extra={"merge": {"sha": sha}})


# A registered wait's sub-kind -> the shape of its reference and how the marker grammar spells it. The
# card-scoped ones name this card's own 8-hex id; Mesh fills it in when the agent leaves it out.
_REGISTRATIONS = {
    "native": (re.compile(r"[0-9a-f]{64}"), "{}"),
    "gmail": (re.compile(r"gmr_[0-9a-f]{12}"), "{}"),
    "receipt": (re.compile(r"[0-9a-f]{8}"), "#{}"),
    "lifecycle": (re.compile(r"[0-9a-f]{8}"), "#{}"),
    "stall": (re.compile(r"[0-9a-f]{8}"), "#{}"),
    "recovery": (re.compile(r"[0-9a-f]{8}"), "#{}"),
    "queue": (re.compile(r"[0-9a-f]{8}"), "#{}"),
}


def _typed_registered(row: dict, wait: dict, now: dt.datetime, task8: str) -> Outcome:
    """A wait on one of the runner's own registrations (``⏳ WAIT native:``, ``gmail:``, ``receipt:#``,
    ...). The Outcome is the marker's: the runner validates the registration itself when it settles, as
    it does for the marker line."""
    reg = str(wait.get("registration") or "")
    if reg not in _REGISTRATIONS:
        return typed_unparsed(row, wait, f"registration `{reg or 'missing'}` is unknown here")
    shape, spelled = _REGISTRATIONS[reg]
    ref = str(wait.get("registration_ref") or "")
    if not shape.fullmatch(ref):
        return typed_unparsed(row, wait, f"registration_ref `{ref[:70] or 'missing'}` is not a {reg} reference")
    note = _typed_note(row, wait)
    return Outcome(reg, ref, note=note, line=f"⏳ WAIT {reg}:{spelled.format(ref)}" + _tail(note))


def _typed_human(row: dict, wait: dict, now: dt.datetime, task8: str) -> Outcome:
    """A question to a person (``ask_human``). Mesh posted the ``❓ Blocking @<person>: <question>``
    comment, armed the human gate and holds the wait; the Outcome is that line's, and its provenance
    names the gate (``human_gate_ref``), which tells the settle not to raise the question again."""
    who = str(wait.get("addressee") or "").strip().removeprefix("@")
    if not _HANDLE.fullmatch(who):
        return typed_unparsed(row, wait, f"addressee `{who[:70] or 'missing'}` is not a username")
    question = _flat(wait.get("question"))
    if not question:
        return typed_unparsed(row, wait, "the human wait carries no question")
    if not str(wait.get("human_gate_ref") or ""):
        return typed_unparsed(row, wait, "the human wait names no human_gate_ref")
    return Outcome("blocking", handle=who, note=question, line=f"❓ Blocking @{who}: {question}")


def _typed_time(row: dict, wait: dict, now: dt.datetime, task8: str) -> Outcome:
    """A time wait with Mesh's evidence. The marker grammar's spelling limits (no spaces in the
    reference, whole seconds, https or ``card:#`` sources) do not apply; what it refuses on substance
    (an event that is really a dependency or a poll, a deadline without a timezone) is refused here too.
    A deadline that passed while the settle waited stays a time wait: it is due, not invalid."""
    raw = str(wait.get("until_at") or "")
    until = _parse_when(raw) if re.match(r"\d{4}-\d\d-\d\d[T ]", raw) else None
    if until is None or not re.search(r"(?:Z|[+-]\d\d:?\d\d)$", raw):
        return typed_unparsed(row, wait, f"until_at `{raw or 'missing'}` is not a time with a timezone")
    if until > now + MAX_ABSOLUTE:
        return typed_unparsed(row, wait, f"until_at `{raw}` is more than {MAX_ABSOLUTE.days} days ahead")
    event, source, why = (_flat(wait.get(k)) for k in ("event_ref", "source", "why"))
    missing = [k for k, v in (("event_ref", event), ("source", source), ("why", why)) if not v]
    if missing:
        return typed_unparsed(row, wait, f"the time wait has no {', '.join(missing)}")
    if source.startswith("https://"):
        from urllib.parse import urlsplit
        try:
            url = urlsplit(source)
            _ = url.port
            bad_source = not url.hostname or bool(url.username or url.password) or any(c.isspace() for c in source)
        except ValueError:
            bad_source = True
    else:
        bad_source = not _RECEIPT_SOURCE.fullmatch(source)
    if bad_source:
        return typed_unparsed(row, wait, f"source `{source[:120]}` is not an https URL or a Mesh receipt")
    if event.lower().startswith(_NOT_TIME_EVENTS) or why.lower().startswith("poll:"):
        return typed_unparsed(row, wait, "a dependency or a poll is not a time event. " + TIME_WAIT_GUIDANCE)
    external = {"event": event, "source": source, "why": why, "at": until.isoformat()}
    line = f"⏳ WAIT {raw}: external={event} source={source} {why}"
    return Outcome("time", until=until, note=why, line=line, extra={"external_time": external})


def _typed_decision(row: dict, wait: dict, now: dt.datetime, task8: str) -> Outcome:
    """A question to another agent (``ask_agent``). Mesh opened the decision request, addressed the
    agent and released the checkout; the wait row names the request, and the addressee only by its
    note, ``asked <agent name>`` (evc-mesh repository/postgres/typed_outcome_commands.go). The handle
    is that name: it is what an answer comment's author is matched against (wake.answers_wait)."""
    if not wait.get("decision_request_id"):
        return typed_unparsed(row, wait, "the decision wait names no decision_request_id")
    note = _typed_note(row, wait)
    asked = re.match(r"asked\s+(?P<name>\S.*)$", note)
    handle = asked["name"].strip() if asked else ""
    line = f"⏳ WAIT decision:@{handle}" + _tail(note)
    return Outcome("decision", handle=handle, note=note, line=line)


def _typed_manual(row: dict, wait: dict, now: dt.datetime, task8: str) -> Outcome:
    """A lead-decided passive wait (``wait_for on=manual``): Mesh records it on this card itself, with no
    target and no timer, and only on a card labelled ``wake:manual``. The Outcome is the
    ``⏳ WAIT manual:#<this card>`` marker's, so the runner checks the label again when it settles."""
    own = (task8 or str(wait.get("task_id") or row.get("task_id") or "")[:8]).lower()
    if not re.fullmatch(r"[0-9a-f]{8}", own):
        return typed_unparsed(row, wait, "the manual wait names no card")
    other = str(wait.get("target_task_id") or "").lower()
    if other and other[:8] != own:
        return Outcome("unparsed", note=MANUAL_WAIT_GUIDANCE, line=f"⏳ WAIT manual:#{other[:8]}")
    note = _typed_note(row, wait)
    if not note:
        return Outcome("unparsed", note=MANUAL_WAIT_GUIDANCE, line=f"⏳ WAIT manual:#{own}")
    return Outcome("manual", own, note=note, line=f"⏳ WAIT manual:#{own}" + _tail(note))


# Mesh wait kind -> builder. A new kind on the server is one entry here; until it has one, a wait of that
# kind is a precise `unparsed`, never no outcome.
TYPED_WAITS = {
    "card": _typed_card,
    "mr": _typed_mr,
    "pipeline": _typed_pipeline,
    "merge": _typed_merge,
    "registered": _typed_registered,
    "time": _typed_time,
    "decision": _typed_decision,
    "human": _typed_human,
    "manual": _typed_manual,
}


def _typed_wait(row: dict, wait: dict | None, now: dt.datetime, task8: str) -> Outcome:
    if not isinstance(wait, dict):
        return typed_unparsed(row, wait, "its wait record could not be found")
    build = TYPED_WAITS.get(str(wait.get("kind") or ""))
    if build is None:
        return typed_unparsed(row, wait, f"wait kind `{wait.get('kind') or 'missing'}` is not supported "
                                         "by this runner yet")
    return build(row, wait, now, task8)


# Mesh turn-outcome kind -> builder. done/review/handoff/cancelled end the attempt by moving the card,
# which the runner sees before it reads an outcome; on a card that is still the agent's they are refused.
TYPED_OUTCOMES = {
    "next": _typed_next,
    "wait": _typed_wait,
}


def from_typed(row: dict, wait: dict | None, now: dt.datetime, self_task8: str = "",
               merge: bool = False) -> Outcome:
    """The Outcome a Mesh turn-outcomes ``row`` declares; ``wait`` is the task-wait row it points at
    (None for a continuation). Every row gives an Outcome: one this runner cannot act on is an
    ``unparsed`` whose note names the row and the part that was not understood. ``merge``: this runner
    runs the merge gate; without it a merge wait is refused exactly as its marker line is."""
    build = TYPED_OUTCOMES.get(str(row.get("kind") or ""))
    if build is None:
        return typed_unparsed(row, wait, f"outcome kind `{row.get('kind') or 'missing'}` does not settle a "
                                         "turn on a card that is still yours in a working status")
    out = build(row, wait, now, self_task8)
    if out.kind == "merge" and not merge:
        out = Outcome("unparsed", note=MERGE_NOT_ENABLED, line=out.line)
    return out if out.typed else dataclasses.replace(out, typed=_provenance(row, wait))


def card_condition(line: str) -> str:
    """The typed condition a ``WAIT card:#X until=<condition>`` line names, '' for the plain form."""
    m = _CARD.match(line or "")
    return (m["condition"] or "") if m else ""


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


QUEUE_WAIT_GUIDANCE = (
    "invalid queue wait: `⏳ WAIT queue:#<this-card-8hex>` is accepted only on a card whose thread holds a "
    "current, not yet served `[fleet queue-request:...]` machine source, with the queue flag on. It wakes you "
    "once when ANY independent unit becomes ready for the listed targets after that source; it is not "
    "availability and grants no access. An unchanged or already served source has no event to wait for: "
    "declare another condition (card, mr, pipeline, decision) or finish the card."
)


MANUAL_WAIT_GUIDANCE = (
    "invalid manual wait: `wait_for on=manual note=<why>` is accepted only on this card itself and only while it "
    "carries the `wake:manual` label (a lead's decision that no observable event exists). It has no timer and no "
    "age re-check: a comment by someone else on this card resumes it, or your own `continue_next`. Without that "
    "decision, end the turn with an observable condition (wait_for on=card, mr or pipeline; ask_agent) or "
    "finish_task."
)


DECISION_WAIT_GUIDANCE = """\
Asking another agent the same open question again with ask_agent (same agent, same words) reuses its open
request; an unchanged question is no reason for another turn. New comments still wake you; only the addressee's answer
resolves it. A known external scheduled event may still use wait_for on=time while a question is open.
"""


# What every agent sees at the end of a turn's prompt. The turn commands are the evc-mesh MCP tools
# (internal/mcp/turn_outcomes.go): their names, parameters and values are copied from there. The marker
# lines stay as the fallback the parser still reads, until the outcome_source counter shows nobody uses them;
# they are offered only after a refused command (a lane that writes them instead of calling is the "habit"
# cause seen in practice), never as an equal path.
_CONTRACT_TEMPLATE = """\
End every turn on this task by calling exactly ONE Mesh turn command (Mesh MCP tool, task_id = this card).
It records the outcome, writes the comment and keeps or releases your checkout; a refusal writes nothing,
so fix what it names and call again.
  wait_for on=card target_task_id=<card> [target_condition=shipped|deployed|accepted]
      — another card must close (or reach that state)
  wait_for on=mr project_path=<group>/<project> mr_iid=<iid> until=merged|green|changed [provider=github]
      — a merge request is merged, gets a new successful head pipeline, or a new head pipeline result
  wait_for on=pipeline project_path=<group>/<project> pipeline_id=<id> until=finished|green|changed
      — a CI pipeline; a GitHub Actions run: provider=github project_path=<owner>/<repo> pipeline_id=<run id>
  wait_for on=registered registration=queue  — a fresh ready unit after this card's queue-request source
  wait_for on=time until_at=<RFC3339 with Z or offset> event_ref=<stable reference>
      source=<https-url|comment:<uuid>|artifact:<uuid>> why=<what happens then>
      — only a specific external event whose time that source publishes
@@MERGE_WAIT@@  ask_agent to=<agent> question=<question>  — only another agent can answer
  ask_human to=<username> question=<one question, with your recommended default>  — a person must decide;
      Mesh posts it, arms the human gate and refuses a question that was already answered
  continue_next note=<what you did, what is next>  — you did a piece of work (an edit, a commit, a check run,
      a result posted) and continue right away. Reading the task and writing a plan is not a piece of work:
      keep working in the same turn.
  finish_task result_kind=change|defect|research|content|ops evidence=<proof of each criterion> [to=review]
      — the task is finished (to=review: someone must accept a live result)
  hand_off to=<agent> why=<what they must do, what is done>  — another agent does the next part
After observing a CI result, pass the baseline you saw, copied from GitLab, never inferred from prose:
until=merged takes after_sha=<full head sha> after_status=opened (not an approval pin: only the merge completes
it, a close wakes you once); until=green|changed takes after_sha, after_pipeline_id and after_status=<status>.
green needs success, pending does not satisfy it. After you run an authorized manual job, wait for its result
with until=green on that pipeline. until=changed on a final baseline (manual, failed, canceled, success) is sent
back: act first (merge wrapper, start the job, push, retry), then wait on the new state.
required_jobs=[<job>,<job>] reads an mr/pipeline wait from those GitLab jobs only, so manual siblings may stay.
Waits sleep until their event: CI waits never wake because time passes. No relative intervals, polls or
invented 6h/24h retries; a missing observer is an integration blocker. Internal retry/backoff never writes a
task's start_after. Provider quotas, account resets and Retry-After are account routing, never a task wait.
Your session is kept: when the condition is met you are resumed in this same conversation.
Only if the Mesh command is unavailable or fails: call it first, a marker line never replaces a call. When
Mesh refused or errored for a reason you cannot fix, post ONE comment quoting the command and its error,
ending with the same outcome:
  ⏳ WAIT card:#<8hex> | mr:<group>/<project>!<iid> | pipeline:<group>/<project>#<id> (mr:github:<org>/<repo>!<n>)
      CI baseline: until=change|success after=<full-head-sha>:<pipeline-id>:<status>, or for mr
      until=merged after=<full-head-sha>:opened; then optionally jobs=[<job>,<job>]
@@MERGE_MARKER@@  ⏳ WAIT decision:@<agent>: <question> | queue:#<this-card-8hex>
  ⏳ WAIT <absoluteISO-with-timezone>: external=<stable-reference> source=<https-url|card:#8hex> <why>
  ↻ NEXT: <note>      ❓ Blocking @<person>: <question>
""" + DECISION_WAIT_GUIDANCE


# Only for a runner that has the merge gate configured: elsewhere the parser rejects the wait.
MERGE_WAIT_GUIDANCE = (
    "  wait_for on=merge project_path=<group>/<project> mr_iid=<iid> after_sha=<full head sha>  — after a SHIP\n"
    "      verdict on the current head: the runner runs the merge gate itself and wakes you when merged or\n"
    "      blocked. Do not re-run the merge gate in a loop yourself.\n")
# Its marker sits with the other fallback lines, never next to the command as an equal path.
MERGE_MARKER_GUIDANCE = "  ⏳ WAIT merge:<group>/<project>!<iid> after=<full-head-sha>\n"


def contract(merge: bool = False) -> str:
    """The outcome contract text; ``merge``: include the merge wait and its fallback line."""
    return (_CONTRACT_TEMPLATE.replace("@@MERGE_WAIT@@", MERGE_WAIT_GUIDANCE if merge else "")
            .replace("@@MERGE_MARKER@@", MERGE_MARKER_GUIDANCE if merge else ""))


CONTRACT = contract()
