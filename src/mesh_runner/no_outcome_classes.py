"""Read-only classifier: why a Claude turn was counted as "no outcome".

For every ``no_outcome`` event of a Claude attempt in a window it reads the attempt's session
transcript (``<projects>/*/<session_id>.jsonl``) and sorts the event into one class:

  no_transcript             the session's transcript is not found
  no_activity               no assistant record in the 15 minutes before the event
  command_rejected          the turn's last Mesh turn command came back as an error
  command_ok                the turn called a turn command that did not fail (a late or lost read)
  text_background           the turn ended on text, without a turn command, while a background task
                            it started (background Bash, Monitor, background agent) was still running
  text_no_command           the turn ended on text without a turn command, nothing in the background

The turn is the transcript from the last prompt the runner pasted (a user text that is no task
notification and no tool result) up to the event. Nothing is written: the database is opened
read-only and transcripts are only read.

    python -m mesh_runner.no_outcome_classes --db ~/.mesh-runner/runner.sqlite \\
        --since 2026-01-01T00:00:00Z --until 2026-01-02T00:00:00Z [--examples 10]
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import glob
import json
import os
from pathlib import Path
import re
import sqlite3

UTC = dt.timezone.utc
CLASSES = ("text_background", "text_no_command", "command_ok", "command_rejected",
           "no_activity", "no_transcript")
ACTIVITY_WINDOW = dt.timedelta(minutes=15)
TURN_COMMAND_RE = re.compile(r"^mcp__.+__(wait_for|continue_next|ask_agent|ask_human|finish_task|hand_off)$")
NOTIFICATION_RE = re.compile(r"^\s*<task-notification>")
NOTIFIED_ID_RE = re.compile(r"<task-id>\s*([\w-]+)\s*</task-id>")
# how Claude Code names a task it started in the background, in that tool's result
STARTED_ID_RES = (re.compile(r"running in background with ID: ([\w-]+)"),
                  re.compile(r"moved to the background \(ID: ([\w-]+)\)"),
                  re.compile(r"^Monitor started \(task ([\w-]+)"),
                  re.compile(r"agentId: ([\w-]+)"))
STOP_TOOLS = {"TaskStop", "KillShell", "KillBash"}
DEFAULT_PROJECTS = ("~/.claude-accounts/*/projects", "~/.claude/projects")


def timestamp(value: str) -> dt.datetime:
    result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamps need an explicit timezone")
    return result.astimezone(UTC)


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(b.get("text") or "") if isinstance(b, dict) else str(b) for b in content)
    return ""


def _blocks(rec: dict) -> list[dict]:
    content = (rec.get("message") or {}).get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _is_prompt(rec: dict) -> bool:
    """A user text the runner pasted: not a tool result, not a background task's notification."""
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isSidechain"):
        return False
    if (rec.get("origin") or {}).get("kind") == "task-notification":
        return False
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list) and any(b.get("type") == "tool_result" for b in _blocks(rec)):
        return False
    text = _text(content)
    return bool(text.strip()) and not NOTIFICATION_RE.match(text)


def classify_turn(records: list[dict], at: dt.datetime) -> tuple[str, dict]:
    """The class of a no_outcome event at ``at`` from the session's transcript records, and the
    facts it stands on (last text, open background tasks, the command)."""
    timed = []
    for rec in records:
        ts = rec.get("timestamp")
        if not ts:
            continue
        try:
            when = timestamp(ts)
        except ValueError:
            continue
        if when <= at:
            timed.append((when, rec))
    start = max((i for i, (_, rec) in enumerate(timed) if _is_prompt(rec)), default=0)
    turn = timed[start:]
    facts: dict = {"turn_started_at": turn[0][0].isoformat() if turn else ""}
    assistant = [(when, rec) for when, rec in turn if rec.get("type") == "assistant"]
    if not assistant or at - assistant[-1][0] > ACTIVITY_WINDOW:
        return "no_activity", facts
    texts = [b.get("text") or "" for _, rec in assistant for b in _blocks(rec) if b.get("type") == "text"]
    facts["last_text"] = (texts[-1] if texts else "").strip()[:200]
    commands, errors = [], set()
    for _, rec in turn:
        for b in _blocks(rec):
            if b.get("type") == "tool_use" and TURN_COMMAND_RE.match(str(b.get("name") or "")):
                commands.append((str(b.get("id") or ""), str(b.get("name")).rsplit("__", 1)[-1]))
            elif b.get("type") == "tool_result" and b.get("is_error"):
                errors.add(str(b.get("tool_use_id") or ""))
    if commands:
        facts["command"] = commands[-1][1]
        return ("command_rejected" if commands[-1][0] in errors else "command_ok"), facts
    # Background tasks the session started before the turn ended and nothing reported finished by then.
    end = assistant[-1][0]
    started, finished, uses = {}, set(), {}
    for when, rec in timed:
        if when > end:
            break
        for b in _blocks(rec):
            if b.get("type") == "tool_use":
                uses[str(b.get("id") or "")] = b
                if b.get("name") in STOP_TOOLS:
                    inp = b.get("input") or {}
                    finished.add(str(inp.get("task_id") or inp.get("shell_id") or inp.get("bash_id") or ""))
            elif b.get("type") == "tool_result":
                body = _text(b.get("content"))[:2000]
                use = uses.get(str(b.get("tool_use_id") or "")) or {}
                for rx in STARTED_ID_RES:
                    m = rx.search(body)
                    if m and use.get("name") in ("Bash", "Monitor", "Agent", "Task"):
                        started[m.group(1)] = (use.get("name"), when)
                        break
        if rec.get("type") == "user" or rec.get("type") == "queue-operation":
            finished.update(NOTIFIED_ID_RE.findall(_text((rec.get("message") or {}).get("content"))
                                                   + str(rec.get("content") or "")))
    still = {tid: v for tid, v in started.items() if tid not in finished}
    open_tasks = sorted(f"{name}:{tid}" for tid, (name, _) in still.items())
    facts["background"] = open_tasks
    if still:   # how long the youngest open task had run when the turn ended
        facts["background_age_s"] = int((end - max(w for _, w in still.values())).total_seconds())
    return ("text_background" if open_tasks else "text_no_command"), facts


def find_transcript(session_id: str, roots: list[str]) -> str:
    if not session_id:
        return ""
    for root in roots:
        hits = glob.glob(os.path.join(os.path.expanduser(root), "*", session_id + ".jsonl"))
        if hits:
            return max(hits, key=os.path.getmtime)
    return ""


def load_transcript(path: str) -> list[dict]:
    records = []
    with open(path, "rb") as fh:
        for raw in fh:
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if isinstance(rec, dict):
                records.append(rec)
    return records


def report(rows: list[tuple], roots: list[str], examples: int = 0) -> dict:
    """``rows``: (event at, lane, session id) of the window's Claude no_outcome events."""
    classes, lanes = collections.Counter(), collections.defaultdict(collections.Counter)
    samples: dict[str, list] = collections.defaultdict(list)
    cache: dict[str, list[dict]] = {}
    for at_text, lane, session_id in rows:
        at = timestamp(at_text)
        path = find_transcript(session_id, roots)
        if not path:
            kind, facts = "no_transcript", {}
        else:
            if path not in cache:
                cache[path] = load_transcript(path)
            kind, facts = classify_turn(cache[path], at)
        classes[kind] += 1
        lanes[lane][kind] += 1
        if len(samples[kind]) < examples:
            samples[kind].append({"at": at.isoformat(), "lane": lane, "session": session_id[:8], **facts})
    result = {"total": sum(classes.values()),
              "classes": {k: classes[k] for k in CLASSES},
              "lanes": {lane: {k: c[k] for k in CLASSES if c[k]}
                        for lane, c in sorted(lanes.items(), key=lambda x: -sum(x[1].values()))}}
    if examples:
        result["examples"] = dict(samples)
    return result


def events(conn: sqlite3.Connection, since: dt.datetime, until: dt.datetime) -> list[tuple]:
    rows = conn.execute(
        "SELECT e.at, a.agent, a.session_id FROM events e JOIN attempts a ON a.id = e.attempt "
        "WHERE e.kind = 'no_outcome' AND a.harness = 'claude' ORDER BY e.at").fetchall()
    return [r for r in rows if since <= timestamp(r[0]) < until]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, required=True, help="Runner SQLite database (read only)")
    parser.add_argument("--since", type=timestamp, required=True, help="window start, UTC or offset")
    parser.add_argument("--until", type=timestamp, help="window end (exclusive); defaults to now")
    parser.add_argument("--projects", action="append",
                        help="Claude projects directory glob (repeatable); default: " + ", ".join(DEFAULT_PROJECTS))
    parser.add_argument("--examples", type=int, default=0, help="examples per class in the output")
    args = parser.parse_args(argv)
    until = args.until or dt.datetime.now(UTC)
    try:
        with sqlite3.connect(args.db.expanduser().resolve().as_uri() + "?mode=ro", uri=True) as conn:
            rows = events(conn, args.since, until)
    except (ValueError, sqlite3.Error) as exc:
        parser.exit(2, f"no_outcome classes: {exc}\n")
    data = {"since": args.since.isoformat(), "until": until.isoformat(),
            **report(rows, args.projects or list(DEFAULT_PROJECTS), args.examples)}
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
