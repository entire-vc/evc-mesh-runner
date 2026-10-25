"""Durable attempt registry (SQLite).

One row per execution of one task by one agent. An attempt owns a native
harness session (Claude session id / Codex thread id), a workspace lease and,
while it runs, the Mesh checkout. It survives runner restarts: the harness
process writes its event stream to a file, so a restarted runner re-attaches
by pid and log path instead of starting a second writer.

States:
    starting   process spawned, native session id not yet confirmed
    running    a turn is in progress
    waiting    turn ended with a WAIT; slot free, session and workspace kept
    ready      wake condition met (or NEXT); needs a slot to resume
    completed  task left the agent's active statuses
    failed     gave up (no outcome, repeated crashes); workspace kept for a human
    unknown    process outcome cannot be established; counts as occupied
    aborted    never started (checkout refused, no workspace); nothing to clean up
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from dataclasses import dataclass, field, fields
from pathlib import Path

from .selection import parse_time

ACTIVE = ("starting", "running", "unknown")          # occupy a slot
OPEN = ACTIVE + ("waiting", "ready")                  # hold the task for this agent

SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    task_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    harness TEXT NOT NULL,
    state TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    pid INTEGER NOT NULL DEFAULT 0,
    log_path TEXT NOT NULL DEFAULT '',
    workspaces TEXT NOT NULL DEFAULT '[]',
    checkout TEXT NOT NULL DEFAULT '{}',
    wait TEXT NOT NULL DEFAULT '{}',
    wake_text TEXT NOT NULL DEFAULT '',
    turns INTEGER NOT NULL DEFAULT 0,
    no_outcome INTEGER NOT NULL DEFAULT 0,
    turn_started_at TEXT NOT NULL DEFAULT '',
    stamp TEXT NOT NULL DEFAULT '',
    leader_done_at TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'work',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS attempts_agent_state ON attempts(agent, state);
CREATE INDEX IF NOT EXISTS attempts_task ON attempts(task_id);
CREATE UNIQUE INDEX IF NOT EXISTS attempts_one_open_per_task
    ON attempts(agent, task_id) WHERE state IN ('starting','running','unknown','waiting','ready');
CREATE TABLE IF NOT EXISTS marks (
    agent TEXT NOT NULL, task_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, at TEXT NOT NULL,
    PRIMARY KEY (agent, task_id, key)
);
CREATE TABLE IF NOT EXISTS events (
    at TEXT NOT NULL, attempt TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL DEFAULT ''
);
"""


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(t: dt.datetime | None = None) -> str:
    return (t or now()).isoformat(timespec="milliseconds")


@dataclass
class Attempt:
    id: str
    agent: str
    task_id: str
    harness: str
    state: str
    title: str = ""
    session_id: str = ""
    pid: int = 0
    log_path: str = ""
    workspaces: list = field(default_factory=list)
    checkout: dict = field(default_factory=dict)
    wait: dict = field(default_factory=dict)
    wake_text: str = ""
    turns: int = 0
    no_outcome: int = 0
    turn_started_at: str = ""
    stamp: str = ""
    leader_done_at: str = ""
    mode: str = "work"                  # work: todo/in_progress task; review: a task in review
    created_at: str = ""
    updated_at: str = ""
    note: str = ""

    @property
    def task8(self) -> str:
        return self.task_id[:8]


_JSON = {"workspaces", "checkout", "wait"}


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # created on the main thread, used by one agent thread at a time (Runner.run_forever)
        self.db = sqlite3.connect(str(path), isolation_level=None, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(attempts)")}
        for col, default in (("stamp", ""), ("leader_done_at", ""), ("mode", "work")):
            if col not in cols:
                self.db.execute(f"ALTER TABLE attempts ADD COLUMN {col} TEXT NOT NULL DEFAULT '{default}'")

    def _row(self, row: sqlite3.Row) -> Attempt:
        # columns this version does not know come from a newer runner sharing the
        # file (a plan run, or a rollback after an upgrade): ignore them
        known = {f.name for f in fields(Attempt)}
        data = {k: v for k, v in dict(row).items() if k in known}
        for k in _JSON:
            data[k] = json.loads(data[k] or ("[]" if k == "workspaces" else "{}"))
        return Attempt(**data)

    def mark(self, agent: str, task_id: str, key: str, value: str | None = None) -> tuple[str, str] | None:
        """Small per-task memory (review feeds, parent fingerprints). With a value:
        write it and return None; without: return (value, at) or None."""
        if value is not None:
            self.db.execute("INSERT INTO marks VALUES (?,?,?,?,?) ON CONFLICT(agent,task_id,key) "
                            "DO UPDATE SET value=excluded.value, at=excluded.at", (agent, task_id, key, value, iso()))
            return None
        row = self.db.execute("SELECT value, at FROM marks WHERE agent=? AND task_id=? AND key=?",
                              (agent, task_id, key)).fetchone()
        return (row[0], row[1]) if row else None

    def claim(self, agent: str, task_id: str, key: str, value: str) -> bool:
        """Set the mark to ``value`` unless it already holds it, in one statement: True for
        the one caller that set it. Concurrent agent threads cannot both claim the same value."""
        cur = self.db.execute(
            "INSERT INTO marks VALUES (?,?,?,?,?) ON CONFLICT(agent,task_id,key) "
            "DO UPDATE SET value=excluded.value, at=excluded.at WHERE marks.value != excluded.value",
            (agent, task_id, key, value, iso()))
        return cur.rowcount == 1

    def clear_marks(self, agent: str, key: str) -> None:
        """Empty ``key`` on every task of ``agent`` (a streak that a hold of the whole runner broke)."""
        self.db.execute("UPDATE marks SET value='' WHERE agent=? AND key=? AND value!=''", (agent, key))

    def marked(self, agent: str, key: str) -> dict[str, str]:
        """Every task with a non-empty ``key`` mark: task id -> value."""
        rows = self.db.execute("SELECT task_id, value FROM marks WHERE agent=? AND key=? AND value!=''", (agent, key))
        return {r[0]: r[1] for r in rows}

    def tasks_seen(self, agent: str, since: dt.datetime) -> set[str]:
        """Tasks this agent had an attempt on, or kept a mark for, since ``since``."""
        cutoff = iso(since)
        rows = self.db.execute("SELECT task_id FROM attempts WHERE agent=? AND updated_at>=? UNION "
                               "SELECT task_id FROM marks WHERE agent=? AND at>=? AND task_id NOT LIKE '\\_%' "
                               "ESCAPE '\\'", (agent, cutoff, agent, cutoff))
        return {r[0] for r in rows}

    def create(self, agent: str, task_id: str, harness: str, title: str = "", mode: str = "work") -> Attempt:
        """Reserve a new attempt. Raises sqlite3.IntegrityError when the agent
        already holds an open attempt on this task (one writer per task)."""
        a = Attempt(id=str(uuid.uuid4()), agent=agent, task_id=task_id, harness=harness,
                    state="starting", title=title, created_at=iso(), updated_at=iso(), mode=mode)
        cols = [f.name for f in fields(Attempt)]
        vals = [json.dumps(getattr(a, c)) if c in _JSON else getattr(a, c) for c in cols]
        self.db.execute(f"INSERT INTO attempts ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
        self.event(a.id, "created", task_id)
        return a

    def update(self, a: Attempt, expect: str | tuple[str, ...] | None = None, **changes) -> bool:
        """Write ``changes``. With ``expect``, only if the stored state is one of
        those; returns False (and leaves ``a`` untouched) when it is not."""
        keys = list(changes) + ["updated_at"]
        stamp = iso()
        vals = [json.dumps(changes[k]) if k in _JSON else changes[k] for k in changes] + [stamp]
        q = f"UPDATE attempts SET {','.join(k + '=?' for k in keys)} WHERE id=?"
        args = vals + [a.id]
        if expect:
            expect = (expect,) if isinstance(expect, str) else expect
            q += f" AND state IN ({','.join('?' * len(expect))})"
            args += list(expect)
        if self.db.execute(q, args).rowcount != 1:
            return False
        for k, v in changes.items():
            setattr(a, k, v)
        a.updated_at = stamp
        if "state" in changes:
            self.event(a.id, "state", changes["state"])
        return True

    def update_with_marks(self, a: Attempt, marks: dict[str, str], expect=None, expect_wait=None, validate=None, **changes) -> bool:
        """Commit an owned state transition and its observation receipts together.

        A failed CAS never acknowledges delivery. For waiting -> ready, the
        durable wake text is the outbox; restart can resume it without re-reading CI.

        The transaction opens with BEGIN IMMEDIATE. A bare SAVEPOINT starts a
        deferred transaction: the CAS read below would pin a WAL snapshot, and
        the first write would then fail at once with SQLITE_BUSY_SNAPSHOT
        ("database is locked", busy timeout not honoured) whenever another
        connection committed in between. Taking the write lock first makes
        the wait for a concurrent writer an ordinary busy-timeout wait.
        """
        owned = not self.db.in_transaction
        if owned:
            self.db.execute("BEGIN IMMEDIATE")
        try:
            result = self._update_with_marks(a, marks, expect, expect_wait, validate, changes)
        except BaseException:
            if owned and self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise
        if owned and self.db.in_transaction:
            self.db.execute("COMMIT")
        return result

    def _update_with_marks(self, a: Attempt, marks: dict[str, str], expect, expect_wait, validate, changes: dict) -> bool:
        original = {f.name: getattr(a, f.name) for f in fields(Attempt)}
        self.db.execute("SAVEPOINT observation_delivery")
        try:
            if expect_wait is not None:
                saved = self.db.execute('SELECT wait FROM attempts WHERE id=?', (a.id,)).fetchone()
                if not saved or json.loads(saved[0]) != expect_wait:
                    self.db.execute("ROLLBACK TO observation_delivery")
                    self.db.execute("RELEASE observation_delivery")
                    return False
            if not self.update(a, expect=expect, **changes):
                self.db.execute("ROLLBACK TO observation_delivery")
                self.db.execute("RELEASE observation_delivery")
                return False
            for key, value in marks.items():
                self.mark(a.agent, a.task_id, key, value)
            if validate is not None and not validate():
                for name, value in original.items():
                    setattr(a, name, value)
                self.db.execute("ROLLBACK TO observation_delivery")
                self.db.execute("RELEASE observation_delivery")
                return False
            self.db.execute("RELEASE observation_delivery")
            return True
        except Exception:
            for key, value in original.items():
                setattr(a, key, value)
            self.db.execute("ROLLBACK TO observation_delivery")
            self.db.execute("RELEASE observation_delivery")
            raise

    def get(self, attempt_id: str) -> Attempt | None:
        row = self.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        return self._row(row) if row else None

    def by_state(self, agent: str, states: tuple[str, ...]) -> list[Attempt]:
        q = f"SELECT * FROM attempts WHERE agent=? AND state IN ({','.join('?' * len(states))}) ORDER BY created_at"
        return [self._row(r) for r in self.db.execute(q, (agent, *states))]

    def waiting_on_task(self, task8: str) -> list[Attempt]:
        """Waiting attempts of any agent on the task whose id starts with ``task8``."""
        q = "SELECT * FROM attempts WHERE state='waiting' AND task_id LIKE ?"
        return [self._row(r) for r in self.db.execute(q, (task8.replace("%", "") + "%",))]

    def open_on_task(self, task8: str) -> list[Attempt]:
        """Open attempts (running, waiting, ready) of any agent on the task whose id starts with ``task8``."""
        q = f"SELECT * FROM attempts WHERE state IN ({','.join('?' * len(OPEN))}) AND task_id LIKE ?"
        return [self._row(r) for r in self.db.execute(q, (*OPEN, task8.replace("%", "") + "%"))]

    def on_task(self, task_id: str, since: dt.datetime | None = None) -> list[Attempt]:
        """Attempts of any agent on ``task_id`` (open ones always, closed ones updated
        since ``since`` when given), oldest first."""
        q = "SELECT * FROM attempts WHERE task_id=?"
        args: list = [task_id]
        if since is not None:
            q += f" AND (state IN ({','.join('?' * len(OPEN))}) OR updated_at>=?)"
            args += [*OPEN, iso(since)]
        return [self._row(r) for r in self.db.execute(q + " ORDER BY created_at", args)]

    def open_for_task(self, agent: str, task_id: str) -> Attempt | None:
        rows = self.by_state(agent, OPEN)
        return next((a for a in rows if a.task_id == task_id), None)

    def recent(self, agent: str, task_id: str, state: str, within_seconds: int) -> bool:
        cutoff = iso(now() - dt.timedelta(seconds=within_seconds))
        return self.db.execute("SELECT 1 FROM attempts WHERE agent=? AND task_id=? AND state=? AND updated_at>=?",
                               (agent, task_id, state, cutoff)).fetchone() is not None

    def outcomes_today(self, agent: str, task_id: str) -> dict[str, int]:
        """Outcome kinds declared on this task by this agent since UTC midnight."""
        midnight = iso(now().replace(hour=0, minute=0, second=0, microsecond=0))
        rows = self.db.execute(
            "SELECT e.detail, COUNT(*) FROM events e JOIN attempts a ON a.id = e.attempt "
            "WHERE a.agent=? AND a.task_id=? AND e.kind='outcome' AND e.at>=? GROUP BY e.detail",
            (agent, task_id, midnight))
        used = {k: n for k, n in rows}
        # a NEXT after which the leased worktree changed (commit, diff) did work: it is not spent budget
        worked = self.db.execute(
            "SELECT COUNT(*) FROM events e JOIN attempts a ON a.id = e.attempt "
            "WHERE a.agent=? AND a.task_id=? AND e.kind='next_work' AND e.at>=?",
            (agent, task_id, midnight)).fetchone()[0]
        if "next" in used:
            used["next"] = max(0, used["next"] - worked)
        return used

    def outcome(self, a: Attempt, kind: str, ci_progress: str = "") -> None:
        """A CI receipt resets idle waits only with its first settled step.

        The provider receipt identity is shared across MR/pipeline aliases. Keep
        its history across attempts and days: replay/flapping cannot reset again.
        Both events commit together, including when recording the receipt fails.
        """
        self.db.execute("SAVEPOINT task_outcome")
        try:
            self.event(a.id, "outcome", kind)
            if kind == "step" and ci_progress:
                self.db.execute(
                    "INSERT INTO events SELECT ?,?,?,? WHERE NOT EXISTS ("
                    "SELECT 1 FROM events e JOIN attempts a ON a.id=e.attempt "
                    "WHERE a.agent=? AND a.task_id=? AND e.kind='ci_progress' AND e.detail=?)",
                    (iso(), a.id, "ci_progress", ci_progress, a.agent, a.task_id, ci_progress))
            self.db.execute("RELEASE task_outcome")
        except Exception:
            self.db.execute("ROLLBACK TO task_outcome")
            self.db.execute("RELEASE task_outcome")
            raise

    def idle_waits_today(self, agent: str, task_id: str) -> int:
        """Today's idle waits after the last verified CI step, over all attempts."""
        midnight = iso(now().replace(hour=0, minute=0, second=0, microsecond=0))
        row = self.db.execute(
            "SELECT COUNT(*) FROM events e JOIN attempts a ON a.id=e.attempt "
            "WHERE a.agent=? AND a.task_id=? AND e.kind='outcome' AND e.detail='wait' AND e.at>=? "
            "AND e.rowid>COALESCE((SELECT MAX(p.rowid) FROM events p JOIN attempts b ON b.id=p.attempt "
            "WHERE b.agent=? AND b.task_id=? AND p.kind='ci_progress'),0)",
            (agent, task_id, midnight, agent, task_id)).fetchone()
        return int(row[0])

    def recent_outcomes(self, attempt_id: str, limit: int) -> list[str]:
        """The attempt's last ``limit`` declared turn outcomes (step/next/wait), newest first."""
        return [r[0] for r in self.db.execute(
            "SELECT detail FROM events WHERE attempt=? AND kind='outcome' ORDER BY rowid DESC LIMIT ?",
            (attempt_id, limit))]

    def auth_turns(self, attempt_id: str) -> int:
        """Physical turns that terminated at authentication, before any product work.
        DISTINCT makes replaying settlement after a crash idempotent."""
        return int(self.db.execute("SELECT COUNT(DISTINCT detail) FROM events WHERE attempt=? AND kind='auth_only_turn'",
                                   (attempt_id,)).fetchone()[0])

    def starts_today(self, agent: str, task_id: str) -> int:
        """Attempts this agent began on this task since UTC midnight that ran at least
        one turn (a start the gate held before it ran is not a start)."""
        midnight = iso(now().replace(hour=0, minute=0, second=0, microsecond=0))
        row = self.db.execute("SELECT COUNT(*) FROM attempts WHERE agent=? AND task_id=? AND created_at>=? "
                              "AND turns>0 AND mode='work'", (agent, task_id, midnight)).fetchone()
        return int(row[0]) if row else 0

    def last_work_end(self, agent: str, task_id: str) -> "dt.datetime | None":
        """When this agent's latest session on this task that ran a turn last changed."""
        row = self.db.execute("SELECT MAX(updated_at) FROM attempts WHERE agent=? AND task_id=? "
                              "AND turns>0 AND mode='work'", (agent, task_id)).fetchone()
        return parse_time(row[0]) if row and row[0] else None

    def active_count(self, agent: str) -> int:
        return len(self.by_state(agent, ACTIVE))

    def event(self, attempt_id: str, kind: str, detail: str = "") -> None:
        self.db.execute("INSERT INTO events VALUES (?,?,?,?)", (iso(), attempt_id, kind, detail[:2000]))

    def events(self, attempt_id: str) -> list[tuple[str, str, str]]:
        return [tuple(r)[0:1] + tuple(r)[2:] for r in
                self.db.execute("SELECT * FROM events WHERE attempt=? ORDER BY rowid", (attempt_id,))]
