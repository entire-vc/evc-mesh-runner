"""Two-step reasoning effort of a lane: a base level on the first pass, one step up on a real signal.

A lane may name two optional fields in runner.toml (``[[agents]]``):

  default_effort  = "medium"   # every turn of the lane, unless a trigger fires
  escalate_effort = "high"     # a turn whose card shows a trigger below

Neither set: nothing changes, the lane's ``args`` decide as before (no ``with_effort`` call, the argv
is byte-identical). Only ``escalate_effort``: the base is whatever ``args`` carry, a trigger lifts it.
Only ``default_effort``: one fixed level, never escalated. An ``effort:<level>`` label on the card
outranks both: an operator's word on one card beats the lane's policy.

Triggers are deterministic, read at every launch (first start and resume) from what the runner already
holds, first match wins:

  do_not_ship        the newest review verdict on the card is DO-NOT-SHIP
  ci_failed_again    two or more distinct pipelines of the card were delivered to the lane as failed;
                     it stays on after a later green pipeline (the card proved hard, not the head)
  failed_attempt     an earlier attempt of this lane on the card ran a turn and gave up on the work
                     itself (WORK_FAILURE_NOTES); provider, harness, checkout and budget ends do not count
  label:<name>       the card carries one of ESCALATION_LABELS

An invalid field is logged and ignored (the lane keeps its previous behaviour); it never stops the
fleet. Stdlib only, no I/O: the runner passes the facts in.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("mesh_runner")

EFFORTS = ("low", "medium", "high", "xhigh", "max")
ESCALATION_LABELS = ("security", "architecture", "urgent", "incident")
CI_FAILURES_TO_ESCALATE = 2
# the ``failed`` attempt notes that say the work itself did not converge: no readable outcome, or the
# per-card turn limit. A provider limit, a harness error, a lost checkout or the daily turn budget is
# an infrastructure end and never escalates.
WORK_FAILURE_NOTES = ("without a readable outcome", "turns on this task", "no outcome declared")
MARK = "effort_policy"          # per-card mark: "<effort>|<trigger>" decided at the latest launch
EVENT = "effort_policy"         # per-attempt event, one per launched turn


def rank(effort: str) -> int:
    return EFFORTS.index(effort) if effort in EFFORTS else -1


def fields(default, escalate, where: str = "") -> tuple[str, str]:
    """The lane's (default_effort, escalate_effort), each '' when unset or invalid.

    An unknown level is ignored with a warning; an escalate level not above the default is ignored too
    (escalation never lowers or keeps the level)."""
    out = []
    for name, value in (("default_effort", default), ("escalate_effort", escalate)):
        v = str(value).strip().lower() if value not in (None, "") else ""
        if v and v not in EFFORTS:
            log.warning("%s: %s=%r ignored: not one of %s", where or "agent", name, str(value)[:20], ",".join(EFFORTS))
            v = ""
        out.append(v)
    base, up = out
    if base and up and rank(up) <= rank(base):
        log.warning("%s: escalate_effort=%s ignored: not above default_effort=%s", where or "agent", up, base)
        up = ""
    return base, up


def work_failure(note: str) -> bool:
    return any(marker in (note or "") for marker in WORK_FAILURE_NOTES)


def failed_pipelines(values) -> int:
    """Distinct failed pipelines among CI delivery mark values (``<sha>:<pipeline>:<status>``): a wait on
    named jobs keeps its own mark of the same pipeline, which must not count twice."""
    ids = set()
    for value in values:
        parts = str(value).rsplit(":", 2)
        if len(parts) == 3 and parts[2] == "failed":
            ids.add(parts[1])
    return len(ids)


def trigger(*, verdict: str = "", ci_failures: int = 0, failed_attempts: int = 0, labels=()) -> str:
    """The first escalation trigger the facts show, '' when none."""
    if verdict == "DO-NOT-SHIP":
        return "do_not_ship"
    if ci_failures >= CI_FAILURES_TO_ESCALATE:
        return "ci_failed_again"
    if failed_attempts >= 1:
        return "failed_attempt"
    names = {str(l).strip().lower() for l in labels or ()}
    for name in ESCALATION_LABELS:
        if name in names:
            return f"label:{name}"
    return ""


@dataclass(frozen=True)
class Choice:
    effort: str         # the level put on the command line; '' = leave the argv alone
    base: str           # the first-pass level ('' = the lane's args)
    escalated: bool
    trigger: str        # why it escalated; '' when it did not

    def mark(self) -> str:
        return f"{self.effort}|{self.trigger}"

    def detail(self, turn: int, launched: str, args_effort: str = "", label: str = "") -> str:
        """The one-line record of a launched turn (store event, runner.log): fixed keys, no free text.

        ``launched`` is the level on the turn's real command line, so the record never claims a level
        the turn did not get (an alt-harness route keeps its own args). ``source``: ``label`` when an
        ``effort:`` label set it, ``policy`` when this choice did, ``args`` otherwise."""
        if label and launched == label:
            source = "label"
        elif self.effort and launched == self.effort:
            source = "policy"
        else:
            source = "args"
        escalated = source == "policy" and self.escalated
        return (f"turn={turn} effort={launched or '-'} base={self.base or args_effort or '-'} "
                f"escalated={int(escalated)} trigger={self.trigger if escalated else '-'} source={source}")


def choose(default: str, escalate: str, fired: str) -> Choice:
    """The level of one turn from the lane's fields and the trigger ('' = none)."""
    if fired and escalate:
        return Choice(escalate, default, True, fired)
    return Choice(default, default, False, "")


def configured(default: str, escalate: str) -> bool:
    return bool(default or escalate)


def parse_mark(value: str) -> tuple[str, str]:
    """(effort, trigger) of a stored mark; ('', '') for anything not written by Choice.mark."""
    effort, _, fired = (value or "").partition("|")
    return (effort, fired) if effort in EFFORTS else ("", "")
