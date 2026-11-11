"""Prompts the runner sends. Operators extend them with their own preamble
file (team rules, review gates); the runner itself stays policy-free."""
from __future__ import annotations

import re
from pathlib import Path

from .outcome import DECISION_WAIT_GUIDANCE, contract


FETCH_HINT = ("full comment bodies: list_comments(task_id, full=true, limit=N, page=P); "
              "list_comments(task_id, full=true, after=<cursor id>) returns only newer comments, "
              "excluding the cursor comment; use a page to fetch its body")

# Comments that carry a standing decision or a gate. When one sits outside the tail it is
# pinned below it, so an old verdict or prohibition is not lost with the history.
_DECISION = re.compile(r"❓\s*\**\s*Blocking|⏳\s*WAIT|↻\s*NEXT|\bROUTE\b|\b(?:DO-NOT-SHIP|SHIP|BLOCKED)\b"
                       r"|\bVERDICT\b|\bdecision\b|запрет|не (?:делай|меняй|трогай)|\bdo not\b|\bnever\b", re.I)


def _who(c: dict, default: str = "?") -> str:
    return c.get("author_name") or (c.get("author") or {}).get("name") or default


def render_comment(c: dict, size: int, default_who: str = "?") -> str:
    """One comment: id, UTC time, author and body. A body over ``size`` keeps its head AND its
    tail (a Blocking/verdict line sits at the end) with the cut named; it is never passed off
    as complete."""
    body = " ".join((c.get("body") or "").split()) if size <= 600 else (c.get("body") or "").strip()
    head = f"{c.get('id', '?')} {c.get('created_at', '')} {_who(c, default_who)}"
    if len(body) <= size:
        return f"{head}: {body}"
    tail = max(size // 3, 80)
    first = size - tail
    return (f"{head} [TRUNCATED {first + tail}/{len(body)} chars, middle omitted; not the full text, "
            f"fetch it before relying on it]: {body[:first]} […] {body[-tail:]}")


def _comments_block(comments: list[dict], limit: int = 6, size: int = 600, skip: frozenset = frozenset()) -> str:
    """The delta the session starts from: the newest ``limit`` comments, and up to three older
    ones that carry a decision or a gate. Not the full history; what is left out is counted and
    the way to fetch it is named."""
    pool = [c for c in comments if c.get("id") not in skip]
    tail = pool[-limit:]
    older = pool[:-limit] if len(pool) > limit else []
    pinned = [c for c in older if _DECISION.search(c.get("body") or "")][-3:]
    rows: list[str] = []
    if pinned:
        rows.append("Older decisions/gates outside the tail (oldest first):")
        rows += [f"- {render_comment(c, size)}" for c in pinned]
        rows.append("Latest comments:")
    rows += [f"- {render_comment(c, size)}" for c in tail]
    left = len(older) - len(pinned)
    if left > 0:
        rows.append(f"- (+{left} older comments not shown; {FETCH_HINT})")
    elif not comments:
        rows.append("- (none)")
    return "\n".join(rows) or "- (none)"


def harness_note(agent_name: str, harness: str = "") -> str:
    """Who the session is: the lane name plus the harness it was actually routed to."""
    return f"{agent_name} (running on the {harness} harness)" if harness else agent_name


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
                 preamble: str = "", merge: bool = False, harness: str = "", stopped: str = "") -> str:
    """``stopped``: the ``where_you_stopped_block`` of a fresh session on a card this lane worked on."""
    short = task["id"][:8]
    where = "\n".join(f"  - {p}" for p in workspaces) or "  - (no repository configured)"
    return f"""{preamble.strip()}

[runner] Task #{short} — {task.get('title', '')}
Mesh task id: {task['id']} · priority {task.get('priority', '')}
{stopped}
You are {harness_note(agent_name, harness)}. This session belongs to this one task. Your writable checkouts for it:
{where}
Work only in these checkouts; other clones of the same repositories belong to other sessions.
The task is already checked out for you — do not check it out or release it yourself.

Read the task description and dependencies with your Mesh tools before acting. The comments below are the
delta, not the whole thread: older history is not required by default; read it only for a concrete gap
({FETCH_HINT}).
Latest comments:
{_comments_block(comments)}

{contract(merge)}{delivery_note(task)}""".strip() + "\n"


CLOSING_COMMENT_MAX = 4000
STATUS_LINES = 20


def where_you_stopped_block(reason: str, closing: dict | None, outcome_line: str, merge_requests: list[str],
                            statuses: list[tuple[str, list[str] | None]]) -> str:
    """Opens a fresh session on a card this lane already worked on. A new session has none of the old one's
    memory: without this it re-read the card and redid or contradicted its own last turn. ``closing``: the
    lane's own last comment on the card, shown whole up to ``CLOSING_COMMENT_MAX``; ``statuses``: (path,
    ``git status --short`` lines, or None when it could not be read) per working copy."""
    rows = ["", f"[runner] Where you stopped. This lane worked on this card before; this session is new because "
                f"{reason}. It has none of the earlier session's memory:"]
    if closing:
        body = (closing.get("body") or "").strip()
        cut = (f"\n[… cut at {CLOSING_COMMENT_MAX} of {len(body)} characters; fetch the comment for the rest]"
               if len(body) > CLOSING_COMMENT_MAX else "")
        rows += [f"Your last comment on the card ({closing.get('id', '?')}, {closing.get('created_at', '')}):",
                 body[:CLOSING_COMMENT_MAX] + cut]
    if outcome_line:
        rows.append(f"Your last declared outcome: {outcome_line.strip()[:400]}")
    if merge_requests:
        rows.append("Linked merge requests:")
        rows += [f"  - {mr}" for mr in merge_requests]
    for path, lines in statuses:
        if lines is None:
            rows.append(f"Working copy {path}: git status unavailable")
        elif not lines:
            rows.append(f"Working copy {path}: clean (git status --short)")
        else:
            more = f"\n    (+{len(lines) - STATUS_LINES} more)" if len(lines) > STATUS_LINES else ""
            rows.append(f"Working copy {path} (git status --short):\n"
                        + "\n".join(f"    {line}" for line in lines[:STATUS_LINES]) + more)
    rows.append("Check this against the card's latest comments before you act; do not redo finished work.")
    return "\n".join(rows) + "\n"


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
                  news_comments: list[dict] | None = None,
                  unreviewed_hours: float | None = None, harness: str = "") -> str:
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

You are {harness_note(agent_name, harness)}. Checkouts you may use to verify it (read or test, do not push to others' branches):
{where}
The task is already checked out for you — do not check it out or release it yourself.

Read the task description and dependencies with your Mesh tools before deciding. The comments below are the
delta, not the whole thread; read older ones only for a concrete gap ({FETCH_HINT}).
Latest comments:
{_comments_block(comments, skip=frozenset(c.get("id") for c in news_comments or [] if c.get("id")))}
{since}
{rules.strip()}
""".strip() + "\n"


def wake_prompt(task: dict, event: str) -> str:
    return f"""[runner] Resumed on task #{task['id'][:8]} — {task.get('title', '')}
What changed: {event}

The new comments by others are listed below when there are any; read older ones only for a concrete gap
({FETCH_HINT}). Continue from where you stopped, and end the turn with one Mesh
turn command (wait_for, continue_next, ask_agent, ask_human, finish_task or hand_off; the marker line only if
the command fails).

{DECISION_WAIT_GUIDANCE}{delivery_note(task)}"""


def wake_event(text: str) -> str:
    """The event a ``wake_prompt`` names ("What changed: ..."), '' when ``text`` is not one."""
    m = re.search(r"^What changed: (.*)$", text or "", re.M)
    return m.group(1).strip() if m else ""


def moved_workspaces_block(moves: dict[str, str]) -> str:
    """Opens the first resumed turn after the session's working copies moved to other slots. The
    session's history still names the old paths, which may now hold someone else's work; the path
    guard refuses them, and this says why before the agent runs into the refusal."""
    if not moves:
        return ""
    lines = ["[runner] Your working copies moved while the task waited:"]
    lines += [f"  {old} -> {new}" for old, new in sorted(moves.items())]
    lines.append("Paths in earlier messages are stale: use the new ones; the old ones belong to other work "
                 "and tool calls naming them are refused.")
    return "\n".join(lines) + "\n\n"


def new_comments_block(comments: list[dict], limit: int = 8, size: int = 1500) -> str:
    """Comments by others since the agent's previous turn, as of the moment the
    turn starts. A wake text is written when the wait ends; a resume held after
    that (a gate, a full pool) must not hand the agent a stale picture. Every comment
    carries its id and time; a cut one says so, and the older ones that did not fit are counted."""
    if not comments:
        return ""
    shown = comments[-limit:]
    lines = [f"\nComments by others since your previous turn ({len(comments)}"
             + (f", last {len(shown)} shown; {len(comments) - len(shown)} older omitted, {FETCH_HINT}"
                if len(shown) < len(comments) else "") + "), oldest first:"]
    for c in shown:
        lines.append("--- " + render_comment(c, size, "someone"))
    return "\n".join(lines) + "\n"


def no_outcome_prompt(task: dict, line: str = "", why: str = "", merge: bool = False) -> str:
    if line:
        # the agent did declare something: say exactly what was not understood,
        # or it will post the same line again
        head = (f"[runner] Your last turn on task #{task['id'][:8]} ended with a declaration the runner "
                f"cannot read:\n  {line.strip()[:300]}\n" + (f"({why})\n" if why else ""))
    else:
        head = f"[runner] Your last turn on task #{task['id'][:8]} ended without an outcome.\n"
    return head + f"""End the turn now with ONE Mesh turn command (wait_for, continue_next, ask_agent, ask_human,
finish_task or hand_off). Only if the command is unavailable or fails, post the closing comment with the marker
line instead.
{contract(merge)}{delivery_note(task)}"""


def ask_not_raised_prompt(task: dict, handle: str, refusal: str = "") -> str:
    """The agent's turn ended on a question to a person that Mesh did not raise."""
    head = f"[runner] Task #{task['id'][:8]}: your turn ended waiting on @{handle}, but Mesh raised no question to them.\n"
    if refusal:
        return head + f"""Mesh refused it as already answered:
{refusal.strip()[:600]}

Work from that answer. A new question needs a different subject and
`ask_human ... supersedes=<what the earlier answer does not cover>` (marker fallback: a line
`supersedes: <...>` in the same comment). Do not raise the gate any other way. End the turn with one Mesh
turn command.
"""
    return head + f"""A person is asked with the Mesh turn command
  ask_human to={handle} question=<one question, with your recommended default>
Mesh posts it as a Blocking question, arms the human gate and checks that it was not answered before. Only if
the command fails, post a comment whose last line is `❓ Blocking @{handle}: <question>`.
`ask_agent` (`⏳ WAIT decision:@<handle>`) is for agents. If the decision is yours to make, make it and continue.
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
