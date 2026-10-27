"""Prompts the runner sends. Operators extend them with their own preamble
file (team rules, review gates); the runner itself stays policy-free."""
from __future__ import annotations

from pathlib import Path

from .outcome import DECISION_WAIT_GUIDANCE, contract


def _comments_block(comments: list[dict], limit: int = 6) -> str:
    rows = []
    for c in comments[-limit:]:
        who = c.get("author_name") or (c.get("author") or {}).get("name") or "?"
        body = " ".join((c.get("body") or "").split())
        rows.append(f"- {c.get('created_at', '')[:16]} {who}: {body[:600]}")
    return "\n".join(rows) or "- (none)"


# Work whose result is not a merge request: it is closed by its evidence. The label is
# ``kind:<value>``; change, defect and no label keep the plain contract.
DELIVERY_EVIDENCE = {
    "research": "a report with its sources and a conclusion",
    "content": "a link to the artifact and the criteria it meets",
    "ops": "a verifiable receipt of the change, e.g. command output or a GET showing the new state",
}


def delivery_note(task: dict) -> str:
    """One short paragraph for kind:research / kind:content / kind:ops cards, else ''."""
    labels = [str(x).strip().lower() for x in task.get("labels") or []]
    for kind, evidence in DELIVERY_EVIDENCE.items():
        if f"kind:{kind}" in labels:
            return (f"\nDelivery (kind:{kind}): this card is closed by its evidence: {evidence}. "
                    "No MR, CI or deploy is invented for it. Put the evidence in the closing comment and move the "
                    "card to done (to review when its acceptor is someone else).\n")
    return ""


def start_prompt(task: dict, agent_name: str, workspaces: list[str], comments: list[dict],
                 preamble: str = "", merge: bool = False) -> str:
    short = task["id"][:8]
    where = "\n".join(f"  - {p}" for p in workspaces) or "  - (no repository configured)"
    return f"""{preamble.strip()}

[runner] Task #{short} — {task.get('title', '')}
Mesh task id: {task['id']} · priority {task.get('priority', '')}

You are {agent_name}. This session belongs to this one task. Your writable checkouts for it:
{where}
Work only in these checkouts; other clones of the same repositories belong to other sessions.
The task is already checked out for you — do not check it out or release it yourself.

Read the full task (description, dependencies, all comments) with your Mesh tools before acting.
Latest comments:
{_comments_block(comments)}

{contract(merge)}{delivery_note(task)}""".strip() + "\n"


REVIEW_PROTOCOL = """This task is in REVIEW and assigned to you. Decide it in this turn:
  - accept: everything the task asks for is done and proven (merged, deployed, evidence on the task)
    → move it to done with a short comment naming the evidence;
  - return: something is missing → move it back to todo for its author with a comment saying what;
  - a human must decide → a comment starting with "❓ Blocking @<human>: <question>".
Leaving it in review without one of these is wrong: it comes back to you only when someone writes on it.
If the decision itself needs time (CI, a deploy), end with the usual outcome line: ⏳ WAIT … or ↻ NEXT.
"""


UNREVIEWED_PROTOCOL = """This task is in REVIEW, assigned to you, and it is YOUR OWN work: nobody else has reviewed it
yet ({hours:g} h since your last turn on it). You must not accept your own work: do not move it to done.
Do one of these in this turn:
  - if what changed asks more work of you (a DO-NOT-SHIP verdict, a failed pipeline), move it back to
    in progress and fix it;
  - hand it to whoever reviews or accepts this kind of work (assign it, or ask them by name in a comment);
  - if nobody fits, hand it to your lead with a comment saying it waits for a review;
  - if the review itself waits on something real (CI, a deploy), end with ⏳ WAIT … for it;
  - if your last comment is a ❓ Blocking question to a person, keep waiting for the answer:
    end with that same line (❓ Blocking @<person>: <question>); a question to an agent waits
    with ⏳ WAIT decision:@<agent>.
"""


def review_prompt(task: dict, agent_name: str, workspaces: list[str], comments: list[dict],
                  preamble: str = "", protocol: str = "", changed: str = "", news: str = "",
                  unreviewed_hours: float | None = None) -> str:
    """``changed``: the task is the agent's own recent work, fed back because of this
    event; ``news``: others' comments since its last turn (new_comments_block);
    ``unreviewed_hours``: its own work nobody reviewed for that long: routed on, never
    accepted (this replaces the review protocol, an operator's one included)."""
    short = task["id"][:8]
    where = "\n".join(f"  - {p}" for p in workspaces) or "  - (no repository configured)"
    since = ""
    if changed:
        since = (f"\nYou worked on this task yourself. It comes back to you because something changed since "
                 f"your last turn on it:\n  {changed}\n{news}")
    elif news:
        since = news
    rules = (UNREVIEWED_PROTOCOL.format(hours=unreviewed_hours) if unreviewed_hours is not None
             else (protocol or REVIEW_PROTOCOL))
    return f"""{preamble.strip()}

[runner] Review #{short} — {task.get('title', '')}
Mesh task id: {task['id']}

You are {agent_name}. Checkouts you may use to verify it (read or test, do not push to others' branches):
{where}
The task is already checked out for you — do not check it out or release it yourself.

Read the full task and all its comments with your Mesh tools before deciding.
Latest comments:
{_comments_block(comments)}
{since}
{rules.strip()}
""".strip() + "\n"


def wake_prompt(task: dict, event: str) -> str:
    return f"""[runner] Resumed on task #{task['id'][:8]} — {task.get('title', '')}
What changed: {event}

Re-read the task's new comments, continue from where you stopped, and end the turn with the same
outcome contract (a closing comment with ⏳ WAIT / ↻ NEXT / ❓ Blocking, or move the task on).

{DECISION_WAIT_GUIDANCE}{delivery_note(task)}"""


def new_comments_block(comments: list[dict], limit: int = 8, size: int = 1500) -> str:
    """Comments by others since the agent's previous turn, as of the moment the
    turn starts. A wake text is written when the wait ends; a resume held after
    that (a gate, a full pool) must not hand the agent a stale picture."""
    if not comments:
        return ""
    shown = comments[-limit:]
    lines = [f"\nComments by others since your previous turn ({len(comments)}"
             + (f", last {len(shown)} shown" if len(shown) < len(comments) else "") + "), oldest first:"]
    for c in shown:
        who = c.get("author_name") or (c.get("author") or {}).get("name") or "someone"
        lines.append(f"--- {who} at {c.get('created_at', '')}:\n{(c.get('body') or '')[:size]}")
    return "\n".join(lines) + "\n"


def no_outcome_prompt(task: dict, line: str = "", why: str = "", merge: bool = False) -> str:
    if line:
        # the agent did declare something: say exactly what was not understood,
        # or it will post the same line again
        head = (f"[runner] Your last turn on task #{task['id'][:8]} ended with a declaration the runner "
                f"cannot read:\n  {line.strip()[:300]}\n" + (f"({why})\n" if why else ""))
    else:
        head = f"[runner] Your last turn on task #{task['id'][:8]} ended without an outcome.\n"
    return head + f"""Post the closing comment now; its last line must be exactly one of these forms:
{contract(merge)}{delivery_note(task)}"""


def ask_not_raised_prompt(task: dict, handle: str, refusal: str = "") -> str:
    """The agent's turn ended on a question to a person that Mesh did not raise."""
    head = f"[runner] Task #{task['id'][:8]}: your turn ended waiting on @{handle}, but Mesh raised no question to them.\n"
    if refusal:
        return head + f"""Mesh refused it as already answered:
{refusal.strip()[:600]}

Work from that answer. A new question needs a different subject and a line
`supersedes: <what the earlier answer does not cover>` in the same comment.
Do not raise the gate any other way. End the turn with the outcome contract.
"""
    return head + f"""A person is asked only by a comment on this task whose last line is
  ❓ Blocking @{handle}: <one question, with your recommended default>
Mesh raises the human gate from that line and checks that the question was not answered before.
`⏳ WAIT decision:@<handle>` is for agents. If the decision is yours to make, make it and continue.
"""


def login_ask_refused_prompt(task: dict, handle: str, registry: str) -> str:
    """The turn ended on a login question to a person that the account registry answers."""
    return (f"[runner] Task #{task['id'][:8]}: your question to @{handle} was not raised: the account "
            f"registry already knows this login.\n\n{registry.strip()[:2500]}\n\n"
            "Do the step yourself as described, or hand it to the lane the registry names. If the page "
            "really shows a login form, ask again with a line `session-dead: <what you saw, where>`. "
            "End the turn with the outcome contract.\n")


def load_preamble(path: str | None) -> str:
    return Path(path).expanduser().read_text() if path else ""
