"""End-of-turn outcomes.

An agent ends a turn on a task through one of Mesh's turn commands (``wait_for``, ``continue_next``,
``ask_agent``, ``ask_human``, ``finish_task``, ``hand_off``). Mesh validates it and stores a turn-outcomes
row and, for a wait, a task-wait row; ``from_typed`` builds the runner's ``Outcome`` from those rows.

Text is never read as an outcome: a ``⏳ WAIT`` / ``↻ NEXT`` / ``❓ Blocking`` line that an
agent writes in a comment or in its final message is prose, and a turn that recorded no turn command has
no outcome. ``Outcome.line`` is only a rendering of the typed outcome in the old marker spelling, kept
for the attempt's wait record and the prompts that show it; it is never parsed back.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import re
from dataclasses import dataclass, field

# The typed conditions a card wait may name (Mesh ``target_condition``) instead of the broad task_done.
CARD_CONDITIONS = ("access_ready", "contract_ready", "artifact_published", "shipped", "deployed", "accepted")


MAX_ABSOLUTE = dt.timedelta(days=400)

TIME_WAIT_GUIDANCE = (
    "invalid external time wait: use `wait_for on=time until_at=<RFC3339 with Z or offset> "
    "event_ref=<stable reference> source=<https-url|comment:<uuid>|artifact:<uuid>> why=<what happens then>`. "
    "The future timestamp must come from the cited external event/window, scheduled job, "
    "or measurement endpoint. Describe cancellation in the comment. "
    "Provider quota, account resets and Retry-After belong to account routing, never a task wait. "
    "Relative intervals, bare dates, `poll:` and invented retries are not time events. "
    "MR/CI/card/answer dependencies use `wait_for on=mr`, `wait_for on=pipeline`, `wait_for on=card`, "
    "`ask_agent` or `ask_human`. "
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
    # Provenance only: left out of equality, so two reads of the same declaration compare equal.
    typed: dict = field(default_factory=dict, compare=False)

    @property
    def is_wait(self) -> bool:
        return self.kind in {"pipeline", "mr", "merge", "card", "decision", "time", "queue", "receipt", "lifecycle", "recovery", "stall", "native", "gmail", "manual"}


GITHUB_MR_CI_UNSUPPORTED = (
    "invalid VCS wait: a GitHub pull request carries no head pipeline, so `wait_for on=mr provider=github "
    "until=green|changed` has nothing to observe; wait on the Actions run itself with `wait_for on=pipeline "
    "provider=github project_path=<org>/<repo> pipeline_id=<run-id> until=green|changed`, or on the merge with "
    "`wait_for on=mr provider=github project_path=<org>/<repo> mr_iid=<n> until=merged after_sha=<full-head-sha> "
    "after_status=opened`")


MERGE_NOT_ENABLED = (
    "invalid VCS wait: `wait_for on=merge` is not enabled on this runner; run the merge gate yourself and, "
    "to await the merge, use `wait_for on=mr project_path=<group>/<project> mr_iid=<iid> until=merged "
    "after_sha=<full-head-sha> after_status=opened`")


# ----------------------------------------------------------------------------- typed outcomes
# An agent ends its turn through Mesh's turn commands (`wait_for`, `continue_next`, `ask_agent`, `ask_human`).
# Mesh has then validated the target and stored a turn-outcomes row and, for a wait, a task-wait row
# (evc-mesh internal/domain/typed_outcome.go: TurnOutcome, TaskWait). ``from_typed`` builds the Outcome
# straight from those rows. ``line`` is a rendering for the attempt's wait record and the prompts that show
# it; nothing reads it back (a card wait's condition is ``extra["card_condition"]``).

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
        "end the turn again with a turn command this runner supports"))


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
    typed_cond = cond if cond in CARD_CONDITIONS else ""
    criterion = " ".join(str(wait.get("criterion") or "").split()) if cond == "artifact_published" else ""
    line = (f"⏳ WAIT card:#{target}" + (f" until={typed_cond}" if typed_cond else "")
            + (f" criterion={criterion}" if criterion else "") + _tail(note))
    return Outcome("card", target, note=note, line=line,
                   extra={"card_condition": typed_cond} if typed_cond else {})


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
    """An mr/pipeline wait with an ``until=``.
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
    ...). The runner validates the registration itself when it settles."""
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
    target and no timer, and only on a card labelled ``wake:manual``; the runner checks the label again
    when it settles."""
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
    runs the merge gate; without it a merge wait is refused (MERGE_NOT_ENABLED)."""
    build = TYPED_OUTCOMES.get(str(row.get("kind") or ""))
    if build is None:
        return typed_unparsed(row, wait, f"outcome kind `{row.get('kind') or 'missing'}` does not settle a "
                                         "turn on a card that is still yours in a working status")
    out = build(row, wait, now, self_task8)
    if out.kind == "merge" and not merge:
        out = Outcome("unparsed", note=MERGE_NOT_ENABLED, line=out.line)
    return out if out.typed else dataclasses.replace(out, typed=_provenance(row, wait))


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
    "invalid queue wait: `wait_for on=registered registration=queue` is accepted only on a card whose thread holds a "
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
resolves it. An answered question is refused (`already_answered`): read its answer; a new question
says what changed. A known external scheduled event may still use wait_for on=time while a question is open.
"""


# What every agent sees at the end of a turn's prompt. The turn commands are the evc-mesh MCP tools
# (internal/mcp/turn_outcomes.go): their names, parameters and values are copied from there. There is no text
# fallback: the runner reads no marker line, so a refused command is reported, not replaced.
_CONTRACT_TEMPLATE = """\
End every turn on this task by calling exactly ONE Mesh turn command (Mesh MCP tool, task_id = this card).
It records the outcome, writes the comment and keeps or releases your checkout; a refusal writes nothing,
so fix what it names and call again.
  wait_for on=card target_task_id=<card> [target_condition=shipped|deployed|accepted|artifact_published]
      — another card must close (or reach that state); artifact_published criterion=<named result>: one part
      it posts, not its closure
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
Text is not an outcome: a line in a comment or in your final message ends nothing, whatever it says.
When Mesh refused or errored for a reason you cannot fix, post ONE comment quoting the command and its
error; the turn then ends without an outcome and the runner hands the card on.
""" + DECISION_WAIT_GUIDANCE


# Only for a runner that has the merge gate configured: elsewhere a merge wait is refused (MERGE_NOT_ENABLED).
MERGE_WAIT_GUIDANCE = (
    "  wait_for on=merge project_path=<group>/<project> mr_iid=<iid> after_sha=<full head sha>  — after a SHIP\n"
    "      verdict on the current head: the runner runs the merge gate itself and wakes you when merged or\n"
    "      blocked. Do not re-run the merge gate in a loop yourself.\n")


def contract(merge: bool = False) -> str:
    """The outcome contract text; ``merge``: include the merge wait."""
    return _CONTRACT_TEMPLATE.replace("@@MERGE_WAIT@@", MERGE_WAIT_GUIDANCE if merge else "")


CONTRACT = contract()
