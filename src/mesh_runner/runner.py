"""The runner loop: several sessions per agent, bounded by the agent's own cap.

Every tick, for each agent:
  1. heartbeat the Mesh checkouts of running attempts (first, so nothing below
     can starve them);
  2. settle finished turns (outcome → waiting / ready / completed / failed),
     enforcing the turn time limit and the grace for leftover child processes;
  3. check wake conditions of waiting attempts;
  4. while active attempts < cap: resume ready attempts, then start new tasks;
  5. now and then, return held worktrees that became safe.

Each attempt is processed in its own error boundary: one bad task, one broken
response or one network blip affects that attempt only. A single runner owns a
state directory (file lock), and every state change is conditional on the
state it was decided from.
"""
from __future__ import annotations

import collections
import copy
import datetime as dt
import fcntl
import hashlib
import json
import logging
import os
import re
import signal
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.error
import uuid
from pathlib import Path

from . import asks
from . import inputs
from . import prompt as prompts
from . import workspace_requests
from . import auth
from . import interactive
from . import watchdog
from .adapters.base import Adapter, Launch, TurnResult, group_alive, interrupt_group, pid_reused, proc_stamp
from .adapters.claude import ClaudeAdapter
from .adapters.codex import CodexAdapter
from . import queue_wait
from .config import AgentConfig, RunnerConfig, read_env_file
from .effects import (BACKLOG_BLOCKER, DECISION_REQUEST_LABEL, SKIP_EVALUATE, SKIP_EVERY, Effects, own_schedule,
                      skip_state)
from .chat import ChatConfigError, ChatKeeper, ChatSpec, env_digest
from .hostmem import MemoryGate, Thresholds
from .mesh import Checkout, MeshClient, MeshError
from .outcome import DECISION_WAIT_GUIDANCE, Outcome, declaration_lines, parse_line
from .selection import StatusCategories, blocking_tasks, order, parse_time, static_skip_reason
from .store import ACTIVE, OPEN, Attempt, Store, iso, now
from .wake import (GitLab, red_merge_statuses, wait_generation, changed_since, check as wake_check, foreign_comment, foreign_comments,
                   gitlab_mr_targets, vcs_event, typed_event, typed_wait, ci_delivery_key, ci_observation,
                   ci_observation_gen, manual_pipeline_text, owner_action_delivered, Delivered, PIPELINE_DONE,
                   blocked_mr_next_step, mr_merge_blocker, is_foreign)
from .workspace import DIRTY_HOLD_NOTE, InventoryError, WorkspaceError, WorkspacePool
from .workspace_scope import (RECEIPT_KEY, metadata_instructions, metadata_only, metadata_receipt,
                              read_only, recorded_scope)

log = logging.getLogger("mesh_runner")

WORKING = {"todo", "in_progress"}
REVIEWING = {"review"}
REVIEW_REFEED_MAX = dt.timedelta(days=1)   # default; RunnerConfig.review_refeed_hours sets it
REVIEW_MIN_AGE = dt.timedelta(minutes=10)
DAILY_CAP_NOTES = ("turn budget",)   # failed by the per-day turn budget with no lead to take it
CHECKOUT_PENDING = "checkout_pending"   # store mark: {session_id, request_id} of an acquire whose answer was lost
TURN_CAP_WINDOW = 10                # turn_cap_continue_on_progress: how many recent outcomes are looked at
CAP_WAIT = "cap_wait"                # store mark: a capped task waiting for new input (requeue_on_cap)
CAP_REASSIGNED = "cap_reassigned"    # store mark: the UTC day a cap moved this task to this agent
WAITING_FOR_INPUT = "waiting for new input after "      # hold reason of a capped task
# what a time WAIT's reason may name that the runner can watch instead of a clock
TIME_WAIT_EVENT = re.compile(r"\b(?:pipeline|mr|card):\S+|(?:[\w.\-]+/[\w.\-/]+)?![0-9]+\b"
                             r"|#[0-9a-f]{8}\b|\bpipeline\s*#?[0-9]+\b", re.IGNORECASE)
# a task named in a decision question: a full uuid, or #<8hex>
TASK_REF = re.compile(r"(?<![\w-])(?P<uuid>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?![\w-])"
                      r"|#(?P<short>[0-9a-f]{8})(?![\w-])", re.IGNORECASE)
# a question that carries the asker's own proposed default (decision_default_proceeds)
DEFAULT_MARKER = re.compile(r"(?<!\w)(?:default\s*:|дефолт\s*:|по\s+умолчанию\s*:|proposed\s*:|предлагаю"
                            r"|рекомендую|if\s+no\s+objections?)", re.IGNORECASE)
DEAD_SLOT_RECHECK = 3600            # seconds between looks at the same held slot
DEAD_SLOT_PER_TICK = 3              # held slots examined per maintenance pass
FINISHED_ATTEMPT = ("completed", "failed", "aborted")
TASK_WIDE = "*"     # marks agent for once-per-task limits that must hold across a reassignment
REVIEW_REFUSAL_GAP = dt.timedelta(hours=1)
REVIEW_REFUSALS_AFTER_NOTICE = 2      # refusals a card gets after a comment once its notice was given today    # refusals further apart than this are not one streak
# attempts that did no work of their own: the task left the agent while it was parked
LEFT_WHILE_IDLE = ("task left the agent while waiting", "task left the agent before resuming")
CLOCK_SLACK = dt.timedelta(minutes=2)     # runner clock vs Mesh clock when matching comments to an attempt
SELF_ECHO = dt.timedelta(minutes=2)       # webhooks of the agent's own last push/merge land this late
GITLAB_DOWN_SECONDS = 120                 # after a failed GitLab read, review checks skip GitLab this long
ENUMERATED_VERDICTS = re.compile(r"\b(?:SHIP|DO-NOT-SHIP)\b\s*(?:/|\||,|\bor\b)\s*\b(?:SHIP|DO-NOT-SHIP)\b")
OPEN_CHILD = {"todo", "in_progress", "review", "backlog"}
ENV_PASSTHROUGH = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TMPDIR", "TERM", "TZ", "SSH_AUTH_SOCK")
QUOTA_HINT = re.compile(r"rate.?limit|usage limit|quota|overloaded|429|too many requests", re.I)
POLICY_REFUSAL_HINT = re.compile(r"flagged for possible cybersecurity risk", re.I)
# Codex says "No saved session found with ID …" when it cannot resume a thread
NO_SESSION_HINT = re.compile(r"no conversation found|no saved session found|session .*not found|thread .*not found", re.I)


def public_harness_error(text: str) -> str:
    """A bounded diagnostic for Mesh/prompts; arbitrary provider text stays local.

    Harness errors can quote requests, credentials or URLs. Pattern redaction
    cannot identify an unlabelled secret, so publish only fixed categories and a
    fingerprint which the operator can match against the local turn log.
    """
    if POLICY_REFUSAL_HINT.search(text):
        category = "content policy refusal"
    elif QUOTA_HINT.search(text):
        category = "provider limit or overload"
    elif NO_SESSION_HINT.search(text):
        category = "session not found"
    elif re.search(r"prompt is too long|context (?:window|length)|maximum context", text, re.I):
        category = "context limit"
    elif "Not inside a trusted directory" in text:
        category = "Not inside a trusted directory"
    else:
        category = "harness error"
    fingerprint = hashlib.sha256(text.encode()).hexdigest()[:12]
    return f"{category} [sha256:{fingerprint}; details in local turn log]"


class AlreadyRunning(Exception):
    pass


PROFILE_VAR = "MESH_RUNNER_PROFILE"
HARNESS_VAR = "MESH_RUNNER_HARNESS"         # env command: the harness this agent is routed to now
CHAT_CHECK_SECONDS = 60
GATE_HOLD = 75                      # start gate exit code: hold starts of this agent (EX_TEMPFAIL)
GATE_HOLD_TASK = 76                 # start gate exit code: hold this task only; try the agent's next
GATE_HOLD_SECONDS = 120
HOLD_RELOG_SECONDS = 1800
_EVENTS_LOCK = threading.Lock()
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-\[\]]{0,63}")   # a name, never a flag
EFFORTS = ("low", "medium", "high", "xhigh", "max")
TASK_HOLD_SECONDS = 600                # a hold of one task (gate exit 76): re-asked when the task changes
MENTION_POLL_SECONDS = 120
MENTION_LOOKBACK = dt.timedelta(hours=6)   # on a fresh start: older unseen mentions are history
MENTION_LABELS = ["mention-request", "pin-assignee", "owner-locked"]
MENTION_CARDS_PER_POLL = 5
MENTION_MAX_FAILS = 5


def make_adapter(agent: AgentConfig) -> Adapter:
    if agent.harness == "codex":
        return CodexAdapter(agent.binary or "codex", agent.args, agent.codex_profile)
    return ClaudeAdapter(agent.binary or "claude", agent.args)


def make_alt_adapter(harness: str, spec: dict) -> Adapter:
    if harness == "codex":
        return CodexAdapter(spec.get("binary") or "codex", list(spec.get("args") or []), spec.get("profile", ""))
    if harness == "claude":
        return ClaudeAdapter(spec.get("binary") or "claude", list(spec.get("args") or []))
    raise ValueError(f"unknown harness {harness}")


def agent_env(agent: AgentConfig) -> dict[str, str]:
    """Allowlisted environment: the runner's own secrets never reach a session."""
    env = {k: v for k, v in os.environ.items() if k in ENV_PASSTHROUGH or k.startswith("LC_")}
    env.update(agent.env)
    return env



ADDRESS = re.compile(r"^\s*(?:↻\s*NEXT\s*:?\s*|ROUTE\s*:?\s*|❓\s*(?:\*\*)?Blocking\s+)?((?:@[\w\-]+(?:\.[\w\-]+)*[\s,]*)+)",
                     re.I | re.M)
HANDLE = re.compile(r"@([\w\-]+(?:\.[\w\-]+)*)")


def addressed(text: str, slug: str) -> bool:
    """True when a line of ``text`` opens by addressing ``@slug`` (an ask), not when the
    name only appears inside a sentence (a `⏳ WAIT decision:@slug` line does not open
    with an address: the waiter files that request itself)."""
    slug = slug.casefold()
    return any(h.casefold() == slug for run in ADDRESS.findall(text) for h in HANDLE.findall(run))


MERGE_TAIL_LINES = 40
MERGE_TAIL_CHARS = 6000
MERGE_LINE_CHARS = 2000        # bytes kept of one output line
MERGE_HISTORY = 8
MERGE_ENV_RETRIES = 3
# the merge gate's last line when it is queued behind a pipeline: the one convention the runner parses
MERGE_QUEUED = re.compile(r"^\s*⏳\s*WAIT\s+pipeline:(?P<target>[\w.\-/]+#[1-9]\d{0,11})(?!\d)")


def _drain_tail(stream, tail: "collections.deque[bytes]") -> None:
    """Read ``stream`` to EOF keeping only the last ``tail.maxlen`` non-blank lines, each cut to
    MERGE_LINE_CHARS bytes. Memory is bounded by lines x cap plus one read chunk, however much
    the child prints or however long one line is."""
    cur = bytearray()
    while True:
        chunk = stream.read(65536)
        if not chunk:
            break
        parts = chunk.split(b"\n")
        for n, part in enumerate(parts):
            if len(cur) < MERGE_LINE_CHARS:
                cur += part[:MERGE_LINE_CHARS - len(cur)]
            if n < len(parts) - 1:
                if cur.strip():
                    tail.append(bytes(cur))
                cur.clear()
    if cur.strip():
        tail.append(bytes(cur))


def _clip(tail) -> str:
    """The drained lines as text, the whole bounded by MERGE_TAIL_CHARS."""
    return "\n".join(ln.decode("utf-8", "replace") for ln in tail)[-MERGE_TAIL_CHARS:]


class _GitLabDown(Exception):
    pass


class _TargetUnreadable(Exception):
    pass


GITLAB_TARGET_UNREADABLE_SECONDS = 600


class _GitLabReads:
    """GitLab reads for one review evaluation: each MR or pipeline is read once. A
    network error or a 5xx means GitLab is down: it is skipped for a while (a timeout
    per target per tick otherwise). A 4xx is about that one target (outside the
    token's scope, deleted): only it is skipped, the others are read."""

    def __init__(self, runner: "AgentRunner"):
        self.runner, self.memo = runner, {}

    def _read(self, kind: str, target: str) -> dict:
        key = (kind, target)
        if key not in self.memo:
            mono = time.monotonic()
            if mono < self.runner._gitlab_down_until:
                raise _GitLabDown()
            if mono < self.runner._gitlab_unreadable.get(key, 0):
                raise _TargetUnreadable()
            try:
                self.memo[key] = (self.runner.gitlab.get_mr(target) if kind == "mr"
                                  else self.runner.gitlab.get_pipeline(target))
            except urllib.error.HTTPError as e:
                if 400 <= e.code < 500:
                    self.runner._gitlab_unreadable[key] = mono + GITLAB_TARGET_UNREADABLE_SECONDS
                    log.info("%s: GitLab %s %s: HTTP %s; skipped for %ss", self.runner.agent.name, kind, target,
                             e.code, GITLAB_TARGET_UNREADABLE_SECONDS)
                    raise _TargetUnreadable() from None
                self._down(target, f"HTTP {e.code}")
            except (OSError, ValueError) as e:     # network, timeout, a body that is not JSON
                self._down(target, type(e).__name__)
        return self.memo[key]

    def _down(self, target: str, why: str) -> None:
        self.runner._gitlab_down_until = time.monotonic() + GITLAB_DOWN_SECONDS
        log.info("%s: GitLab read %s failed (%s); GitLab skipped for %ss", self.runner.agent.name, target, why,
                 GITLAB_DOWN_SECONDS)
        raise _GitLabDown() from None

    def get_mr(self, target: str) -> dict:
        return self._read("mr", target)

    def get_pipeline(self, target: str) -> dict:
        return self._read("pipeline", target)


GATE_PUBLIC_REASONS = {
    "human": "waits for a human decision",
    "profile": "runtime profile unavailable",
    "capacity": "runtime capacity unavailable",
    "review": "review admission unavailable",
    "unknown": "held by the start gate",
}


def gate_hold_reason(text: str) -> str:
    """Public diagnostic only: never copy values from hook KEY=VALUE output.

    Operators may emit HOLD_REASON_CODE; unknown codes fail closed. Legacy
    fleet reasons containing '=' are classified by their fixed prefix only.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    codes = [line.split("=", 1)[1].strip() for line in lines
             if line.startswith("HOLD_REASON_CODE=")]
    if codes:
        return (GATE_PUBLIC_REASONS.get(codes[0], GATE_PUBLIC_REASONS["unknown"])
                if len(codes) == 1 else GATE_PUBLIC_REASONS["unknown"])
    for line in lines:
        if re.match(r"[A-Z][A-Z0-9_]{0,40}=", line):
            continue
        if "=" not in line:
            return line[:200]  # existing operator text contract
        if line.startswith("waits for a human"):
            return GATE_PUBLIC_REASONS["human"]
        if (line.startswith("capacity:")
                or line.startswith("route: reserve_refused:pool_full:")
                or line.startswith("route: reserve_refused:capacity_unknown")):
            return GATE_PUBLIC_REASONS["capacity"]
        qa = re.match(r"(?:route: )?(qa_refused|qa_no_selection|qa_fail_closed|qa_artifact_unresolved):", line)
        if qa:
            # Preserve the finite QA tag used by review_gate_hold_pattern, never
            # profile IDs, trace payloads or arbitrary reason suffixes.
            sibling = re.match(r"(?:route: )?(qa_no_selection:sibling_evidence_unknown)(?:\s|$)", line)
            if sibling:
                return sibling[1]
            return qa[1] + ": review admission unavailable"
        if re.match(r"route: (?:binding_denied|unavailable)(?:\s|$)", line):
            return GATE_PUBLIC_REASONS["profile"]
    return GATE_PUBLIC_REASONS["unknown"]


class AgentRunner:
    def __init__(self, cfg: RunnerConfig, agent: AgentConfig, store: Store, mesh: MeshClient | None = None,
                 adapter: Adapter | None = None, gitlab: GitLab | None = None):
        self.cfg, self.agent, self.store = cfg, agent, store
        self.mesh = mesh or MeshClient(cfg.mesh_url, agent.key, basic_auth=cfg.mesh_basic_auth,
                                       bearer=cfg.extension_token)
        # the runner decides how turns run (launch_mode); an agent may override it
        self.launch_mode = agent.launch_mode or cfg.launch_mode
        self._tmux = interactive.Tmux(cfg.tmux_binary)
        self._reap_due = 0.0
        self._dead_slot_seen: dict[str, float] = {}      # slot path -> monotonic time of the last look
        self._boot = time.time()
        self._watchdog_fired: set[tuple[str, int]] = set()   # (attempt, turn): one watchdog kill per turn
        self.adapter = adapter or self._launch(make_adapter(agent))
        # other harnesses the operator's routing may move this agent to (per attempt)
        self.alt_adapters = {h: self._launch(make_alt_adapter(h, spec))
                             for h, spec in agent.alt.items() if h != agent.harness}
        self._routed_harness = agent.harness
        self.gitlab = gitlab
        self.env = agent_env(agent)
        # one gate per runner process (Runner shares it); a lone AgentRunner builds its own
        self.memgate = MemoryGate(Thresholds(cfg.memory_hold_free_pct, cfg.memory_release_free_pct,
                                             cfg.memory_hold_swap_mb, cfg.memory_release_swap_mb))
        self._gate_hold: dict[str, tuple[str, float]] = {}    # task -> (reason, until) from the start gate
        self._agent_gate_hold: tuple[str, float, str] | None = None
        self._gate_failures: dict[tuple[str, str], int] = {}
        self._resume_hold: dict[str, float] = {}              # attempt -> until
        self._hold_log_at: dict[tuple[str, str], tuple[str, float]] = {}   # (task8, episode) -> (reason, last logged)
        self._inventory_failures: dict[tuple, int] = {}       # (attempt, config repos) -> refusals
        self._start_held = False
        self._gate_asked = False
        # task -> the _task_backoff deadline a spent turn budget set; it counts only while that
        # very deadline is the task's current backoff (any later backoff replaces it)
        self._budget_backoff: dict[str, float] = {}
        self._gate_task_only = False                          # the last gate hold was for one task only
        self._mentions_at = -1e18
        self._mention_fails: dict[str, int] = {}
        self.chat: ChatKeeper | None = None
        self._chat_due = 0.0
        self._chat_error = ""
        self._chat_hold = ""
        if agent.chat.get("session"):
            c = agent.chat
            command = list(c.get("command") or [])
            if not command:
                if agent.harness != "claude":
                    raise ChatConfigError(f"{agent.name}: chat.command is required for harness {agent.harness}")
                command = [agent.binary or "claude", *agent.args]
            self.chat = ChatKeeper(ChatSpec(session=str(c["session"]), command=command,
                                            idle_command=list(c.get("idle_command") or []),
                                            max_age_hours=float(c.get("max_age_hours", 0)),
                                            adopt=bool(c.get("adopt", False)),
                                            tmux=[cfg.tmux_binary, *list(c.get("tmux_args") or [])]),
                                   agent.name, agent.profile_dir, cfg.state_dir)
        self.pool = WorkspacePool(store, cfg.pool_root, agent.repos, agent.identity, env=self.env,
                                  owner=agent.name, min_free_gb=cfg.min_free_gb,
                                  checkpoint_exclude_names=cfg.checkpoint_exclude_names)
        self.cats = StatusCategories(self.mesh)
        self.effects = Effects(self.mesh, self.cats, agent.name, enabled=cfg.mesh_effects, store=store,
                               decision_metadata_recipients=tuple(cfg.decision_request_metadata_only_recipients),
                               answer_marker=(agent.decision_wake or cfg.decision_wake) == "answer",
                               decision_reuse_hours=cfg.decision_reuse_hours,
                               identical_no_reopen=cfg.decision_identical_no_reopen)
        self.procs: dict[str, subprocess.Popen] = {}
        self.me: dict = {}
        self._last_beat: dict[str, float] = {}
        self._closed_seen: set[str] = set()      # attempts whose closed card the beat saw once
        self._last_wake_check: dict[str, float] = {}
        # keyed (attempt id, what): a success of one step (wake) must not reset
        # the failure count of another (workspace-suspend) on the same attempt
        self._errors: dict[tuple[str, str], int] = {}
        self._quarantine: dict[tuple[str, str], float] = {}
        self._strikes: dict[tuple[str, str], int] = {}
        self._task_backoff: dict[str, float] = {}
        self._input_checked: dict[str, tuple[str, float, bool]] = {}   # task -> (cap record at, mono, changed)
        # task -> (the _task_backoff deadline it belongs to, the input fingerprint taken when that
        # backoff was set). Only a backoff set by _backoff_on_input is recorded; every other backoff
        # of the task replaces the deadline and so voids the record.
        self._backoff_inputs: dict[str, tuple[float, dict]] = {}
        self._backoff_checked: dict[str, tuple[float, float, bool]] = {}   # task -> (deadline, mono, changed)
        self._skips_due = 0.0
        self._queue_seen: tuple[int, str] | None = None
        self._start_error = ""
        self._pool_hold, self._pool_hold_logged = "", False
        self._starts_paused_until = 0.0
        self._last_recheck = 0.0
        self._hold_cache: dict[str, tuple[str, float, str]] = {}   # task -> (updated_at, mono, verdict)
        self._hold_deadline: dict[str, float] = {}   # task -> monotonic time a cached hold must be re-read
        self._review_why: dict[str, dict] = {}   # task -> why its own work is fed back (event/since/unreviewed_hours)
        self._merge_jobs: dict[str, dict] = {}      # attempt id -> a merge gate run (thread, result)
        self._gitlab_down_until = 0.0
        self._gitlab_unreadable: dict[tuple[str, str], float] = {}   # (kind, target) -> skip until (4xx)
        self._hold_logged: dict[str, str] = {}              # task -> the hold reason last logged
        self.preamble = prompts.load_preamble(agent.preamble_file) if agent.preamble_file else ""

    # ------------------------------------------------------------------ helpers
    def paused(self) -> bool:
        """Operator pause: config ``paused`` or a file <state_dir>/pause/<agent>.
        Paused agents start and resume nothing; waits are still tracked."""
        return self.agent.paused or (self.cfg.state_dir / "pause" / self.agent.name).exists()

    def emit(self, kind: str, a: Attempt | None = None, **detail) -> None:
        """One JSON line per lifecycle event, for metrics and audits."""
        row = {"at": iso(), "agent": self.agent.name, "kind": kind}
        if a is not None:
            row.update(task=a.task_id, attempt=a.id, turn=a.turns)
        row.update(detail)
        try:
            path = self.cfg.state_dir / "events.jsonl"
            with _EVENTS_LOCK:                  # agents emit from parallel threads
                if path.exists() and path.stat().st_size > 50 * 1024 * 1024:
                    path.replace(path.with_suffix(".jsonl.1"))
                with open(path, "a") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as e:
            log.warning("event log: %s", e)

    def _launch(self, base: Adapter) -> Adapter:
        return interactive.wrap(base, self.agent.name, self.launch_mode, self.cfg.state_dir,
                                self.cfg.tmux_binary, self.cfg.interactive_max_sessions,
                                background_wait_minutes=self.cfg.background_wait_minutes)

    def reap_interactive(self) -> None:
        """Close interactive sessions nobody will use warm (see interactive.reap). Runs in
        either launch mode: windows left from an interactive period are closed too."""
        if time.monotonic() < self._reap_due:
            return
        self._reap_due = time.monotonic() + interactive.REAP_EVERY_SECONDS
        try:
            closed = interactive.reap(self._tmux, self.store, self.cfg.state_dir, self.agent.name,
                                      self.cfg.interactive_idle_minutes, OPEN, ACTIVE)
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            log.warning("%s: interactive sessions: %s", self.agent.name, e)
            return
        for window, reason in closed:
            log.info("%s: interactive session %s closed: %s", self.agent.name, window[:8], reason)

    def adapter_for(self, harness: str) -> Adapter:
        if harness == self.agent.harness:
            return self.adapter
        got = self.alt_adapters.get(harness)
        if got is None:
            log.warning("%s: harness %s is not configured any more; using %s", self.agent.name, harness,
                        self.agent.harness)
            return self.adapter
        return got

    def _fresh_env(self) -> dict[str, str]:
        """Rebuild the session environment: env file, then the operator's env
        command (e.g. the account the lane is routed to now), then inline values.
        A failure stops the start: a session on the wrong identity is worse than none."""
        env: dict[str, str] = {}
        if self.agent.env_file:
            try:
                env.update(read_env_file(self.agent.env_file))
            except (OSError, ValueError) as e:
                raise RuntimeError(f"env file unreadable: {type(e).__name__}") from None
        if self.agent.env_command:
            res = subprocess.run(self.agent.env_command, capture_output=True, text=True, timeout=60)
            if res.returncode != 0:
                raise RuntimeError(f"env command failed (rc {res.returncode}): {res.stderr.strip()[:200]}")
            for line in res.stdout.splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.removeprefix("export ").split("=", 1)
                    env[k.strip()] = v.strip().strip("'\"")
        env.update(self.agent.inline_env)
        # the env command may route the lane to another harness profile (a Codex
        # profile on another account); it is an argument, not session env
        profile = env.pop(PROFILE_VAR, "")
        routed = env.pop(HARNESS_VAR, "") or self.agent.harness
        self._routed_harness = routed if (routed == self.agent.harness or routed in self.alt_adapters) \
            else self.agent.harness
        target = self.adapter_for(self._routed_harness)
        if hasattr(target, "profile"):
            if not hasattr(target, "_base_profile"):
                target._base_profile = target.profile
            target.profile = profile or target._base_profile    # not sticky: no value = configured
        self.agent.env = env
        self.env = agent_env(self.agent)
        self.pool.env = self.env
        return self.env

    @property
    def agent_id(self) -> str:
        return self.me.get("id", "")

    def cap(self) -> int:
        if self.agent.max_parallel > 0:
            return self.agent.max_parallel
        return max(1, int(self.me.get("max_concurrent_tasks") or 1))

    def log_dir(self) -> Path:
        d = self.cfg.state_dir / "logs" / self.agent.name
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        return d

    def _guard(self, a: Attempt, what: str, fn) -> None:
        """Error boundary per attempt and step. Repeated failures quarantine the
        step for a growing while (15m, 30m, 1h ... 4h) instead of failing every
        tick (and starving the others); a success clears only its own step."""
        key = (a.id, what)
        if time.monotonic() < self._quarantine.get(key, 0):
            return
        try:
            fn(a)
            self._errors.pop(key, None)
            self._strikes.pop(key, None)
        except Exception as e:
            n = self._errors.get(key, 0) + 1
            self._errors[key] = n
            log.warning("%s: %s #%s failed (%d): %s", self.agent.name, what, a.task8, n, e)
            self.store.event(a.id, f"error:{what}", str(e)[:500])
            if n >= 3:
                strikes = self._strikes.get(key, 0)
                self._strikes[key] = strikes + 1
                self._errors.pop(key, None)
                hold = min(900 * 2 ** strikes, 14400)
                self._quarantine[key] = time.monotonic() + hold
                self.store.event(a.id, f"quarantine:{what}", f"{hold}s after repeated failure: {str(e)[:300]}")

    def _comment(self, task_id: str, text: str) -> None:
        try:
            self.mesh.add_comment(task_id, "[runner] " + text)
        except MeshError as e:
            log.warning("%s: comment on #%s failed: %s", self.agent.name, task_id[:8], e)

    def _task(self, task_id: str) -> dict | None:
        try:
            t = self.mesh.task(task_id)
        except MeshError as e:
            if e.status in (404, 410):
                return None
            raise
        return t.get("task", t) if isinstance(t, dict) else None

    def _still_ours(self, task: dict | None, mode: str = "work") -> bool:
        """The task still belongs to this attempt: assigned to the agent and in the
        statuses the attempt works in (todo/in_progress, or review for a review)."""
        if not task:
            return False
        assignee = task.get("assignee_id") or (task.get("assignee") or {}).get("id")
        return assignee == self.agent_id and self.cats.category(task) in (REVIEWING if mode == "review" else WORKING)

    def _comments(self, task_id: str) -> list[dict]:
        try:
            return self.mesh.comments(task_id)
        except MeshError:
            return []

    def _queue_source(self, task: dict, checked: float):
        if not queue_wait.eligible(task, self.agent_id):
            return None
        # Unlike _comments, a failed read cannot silently reuse an older source.
        found = queue_wait.source(task, self.mesh.comments(task["id"]), self.agent_id, checked)
        if found and found["observed_at"] < queue_wait.floor(self.store, self.agent.name, task["id"]):
            # A read that shows only an older request than one already served is partial
            # (a comment page that came back empty): never re-arm or re-deliver the old one.
            return None
        return found

    def _anchor(self, comments: list[dict]) -> str:
        """A point in Mesh's own clock: the newest comment's created_at. Using the
        server's timestamps avoids losing or double-reading comments on clock skew."""
        times = [c.get("created_at") for c in comments if c.get("created_at")]
        if times:
            return max(times)
        return iso(now() - dt.timedelta(minutes=2))

    def _release_checkout(self, a: Attempt) -> None:
        if a.checkout.get("token"):
            try:
                self.mesh.release(Checkout(a.task_id, a.checkout["token"], a.checkout.get("generation"),
                                           "", a.checkout.get("session_id", a.id), a.checkout.get("request_id", "")))
            except MeshError as e:
                log.warning("%s: release #%s: %s", self.agent.name, a.task8, e)
            self.store.update(a, checkout={})

    def _pending_checkout(self, a: Attempt) -> tuple[str, str] | None:
        """(session_id, request_id) of an acquire whose answer was lost, if one is remembered for this task."""
        row = self.store.mark(self.agent.name, a.task_id, CHECKOUT_PENDING)
        try:
            data = json.loads(row[0]) if row and row[0] else {}
        except ValueError:
            data = {}
        if isinstance(data, dict) and data.get("session_id") and data.get("request_id"):
            return str(data["session_id"]), str(data["request_id"])
        return None

    def _acquire(self, a: Attempt) -> bool:
        """Take the Mesh checkout idempotently across a lost response.

        Mesh treats a repeat POST with the same holder, session_id and request_id on a live checkout
        as a replay and returns the original lease and token. A POST whose answer never arrived
        (timeout, 5xx) may still have created the checkout, so its (session_id, request_id) is
        written down BEFORE the request, kept on an ambiguous failure and sent again on the next
        acquire of this task, even from a later attempt. Any other answer is definitive and
        forgets it; a refused replay (the lease was released or expired) is retried once with
        fresh ids."""
        pending = self._pending_checkout(a)
        for _ in range(2):
            replay = pending is not None
            session_id, request_id = pending or (a.id, str(uuid.uuid4()))
            self.store.mark(self.agent.name, a.task_id, CHECKOUT_PENDING,
                            json.dumps({"session_id": session_id, "request_id": request_id}))
            try:
                co = self.mesh.checkout(a.task_id, session_id, request_id, self.cfg.checkout_ttl_minutes)
            except MeshError as e:
                self.store.event(a.id, "checkout_refused", str(e))
                if e.status == 0 or e.status == 408 or e.status >= 500:
                    log.warning("%s: checkout #%s answer lost (%s); the next acquire replays request %s",
                                self.agent.name, a.task8, e, request_id[:8])
                    return False                  # may exist on the server: keep the ids
                self.store.mark(self.agent.name, a.task_id, CHECKOUT_PENDING, "")
                if replay and e.status == 409:
                    pending = None                # the old lease is gone, or never was ours: start over
                    continue
                return False
            # Persist the lease first: until the token is on the attempt, the pending ids are the
            # only way back to it (a crash or a failed update here must not lose both).
            self.store.update(a, checkout={"token": co.token, "generation": co.generation,
                                           "session_id": co.session_id, "request_id": co.request_id})
            self.store.mark(self.agent.name, a.task_id, CHECKOUT_PENDING, "")
            return True
        return False

    def _finish(self, a: Attempt, state: str, note: str = "", report: bool = False) -> None:
        self._release_gate(a, state)            # no-op unless a gate is still held (crash paths)
        if a.wait:
            wait = dict(a.wait)
            self.effects.on_end(a, wait, f"the attempt is {state}", close_request=False)
        self.effects.close_terminal_decision(a)
        self.effects.clear_state(a.task_id)
        self.effects.taken(a.task_id)          # nothing of a skip state is left to clear
        self._release_checkout(a)
        try:
            kept = self.pool.release(a.id)
        except WorkspaceError as e:
            kept = [("?", f"release failed: {e}")]
        if kept:
            note = (note + "; " if note else "") + "worktrees: " + ", ".join(f"{p} ({r})" for p, r in kept)
        self.store.update(a, state=state, note=note[:1000])
        self.procs.pop(a.id, None)
        self.emit(state, a, note=note[:300])
        if report and state == "completed" and a.turns and self.cfg.completion_comment:
            self._comment(a.task_id, self._completion_line(a)[len("[runner] "):])

    def _report_spend(self, a: Attempt, r: TurnResult) -> None:
        """The turn's tokens and cost on the agent's Mesh record, as the queue feeder reported
        them for its lanes (Mesh cost views read these). Never fails a settle."""
        if not self.cfg.session_reports or not (r.usage or r.cost_usd):
            return
        turn = f"{a.id}:{a.turns}"
        done = self.store.mark(self.agent.name, a.task_id, "spend")
        if done and done[0] == turn:
            return                              # settle ran again for this turn (retry, restart)
        # marked BEFORE the post: a lost report is better than a double count
        self.store.mark(self.agent.name, a.task_id, "spend", turn)
        u = r.usage or {}
        tokens_in = int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) \
            + int(u.get("cache_creation_input_tokens") or 0)
        try:
            self.mesh.session_report(a.task_id, tokens_in, int(u.get("output_tokens") or 0),
                                     r.model or a.harness, float(r.cost_usd or 0.0))
        except MeshError as e:
            log.warning("%s: session report #%s: %s", self.agent.name, a.task8, e)

    def _completion_line(self, a: Attempt) -> str:
        """Same shape as the long-standing completion comments cost tools parse."""
        cost, out_tok, cache_rd, tok_in = 0.0, 0, 0, 0
        for log_path in sorted(self.log_dir().glob(f"{a.id}.turn*.jsonl")):
            parts = log_path.name.split(".")              # <id>.turn<n>[.<harness>].jsonl
            harness = parts[-2] if len(parts) == 4 else a.harness
            r = self.adapter_for(harness).parse(log_path)
            cost += r.cost_usd or 0.0
            u = r.usage or {}
            out_tok += int(u.get("output_tokens") or 0)
            cache_rd += int(u.get("cache_read_input_tokens") or u.get("cached_input_tokens") or 0)
            tok_in += int(u.get("input_tokens") or 0)
        started = parse_time(a.created_at) or now()
        minutes = max(1, int((now() - started).total_seconds() // 60))
        head = f"[runner] ✅ completed in ~{minutes}m · session={a.session_id[:8]} · {a.turns} turns · "
        if a.harness == "codex":
            return head + f"codex · in {tok_in / 1000:.1f}k tok, cached {cache_rd / 1000:.1f}k, out {out_tok / 1000:.1f}k"
        if not cost and self.launch_mode == "interactive":
            return head + f"interactive · out {out_tok / 1000:.1f}k tok, cache_rd {cache_rd / 1e6:.1f}M"
        return head + f"~${cost:.2f} notional (out {out_tok / 1000:.1f}k tok, cache_rd {cache_rd / 1e6:.1f}M)"

    # ------------------------------------------------------------------ turns
    def _auth_blocked(self, a: Attempt, env: dict[str, str]) -> str:
        routed = self._routed_harness
        profile = auth.profile_id(env, self.agent.profile_dir) if routed == "claude" else ""
        unknown = self.store.mark(self.agent.name, a.task_id, "auth_unknown")
        try:
            unknown = json.loads(unknown[0]) if unknown and unknown[0] else {}
        except ValueError:
            unknown = {}
        unrecorded = unknown.get("profile", "") if isinstance(unknown, dict) and unknown.get("attempt") == a.id else ""
        return (unrecorded if routed == "claude" and unrecorded and auth.held(self.store, unrecorded) else
                profile if profile and auth.held(self.store, profile) else "")

    def _spawn(self, a: Attempt, prompt_text: str, resume: bool, expect: tuple[str, ...],
               first_prompt=None, before_spawn=None, env=None) -> bool:
        workspaces = [Path(p) for p in a.workspaces]
        # ONE environment build per spawn: a resume prepares it before restoring
        # checkpoints, so a held profile never leases their worktrees prematurely.
        env = self._fresh_env() if env is None else env
        routed = self._routed_harness
        profile = auth.profile_id(env, self.agent.profile_dir) if routed == "claude" else ""
        blocked = self._auth_blocked(a, env)
        if blocked:
            self._auth_hold_attempt(a, blocked)
            return False
        if routed != a.harness and (routed == self.agent.harness or routed in self.alt_adapters):
            if resume and a.session_id:
                if first_prompt is None:
                    raise RuntimeError(f"routed to {routed}; this resume cannot start a new session")
                # a session cannot move between harnesses: the task continues in a new one
                log.info("%s: #%s moves %s -> %s; new session", self.agent.name, a.task8, a.harness, routed)
                self.store.event(a.id, "harness_switch", f"{a.harness}->{routed}")
                self.store.update(a, harness=routed, session_id="")
                resume, prompt_text = False, first_prompt()
            else:
                self.store.update(a, harness=routed)
            a = self.store.get(a.id) or a
        adapter = self.adapter_for(a.harness)
        log_path = self.log_dir() / f"{a.id}.turn{a.turns + 1}.{a.harness}.jsonl"
        sid = a.session_id or (str(uuid.uuid4()) if adapter.session_id_known_upfront else "")
        anchor = self._anchor(self._comments(a.task_id))
        argv = (adapter.resume_argv if resume else adapter.start_argv)(
            sid, self.agent.profile_dir, workspaces)
        model = self.store.mark(self.agent.name, a.task_id, "model")
        if model and model[0]:
            argv = adapter.with_model(argv, model[0])
        effort = self.store.mark(self.agent.name, a.task_id, "effort")
        if effort and effort[0]:
            argv = adapter.with_effort(argv, effort[0])
        if self.pool.disk_hold():
            self._disk_hold_attempt(a)
            return False
        if resume and sid:
            try:
                note = adapter.prepare_resume(sid, self.agent.profile_dir, env)
            except OSError as e:
                note = f"could not carry the session over: {e}"
            if note:
                self.store.event(a.id, "session_carry", note[:200])
                log.info("%s: #%s %s", self.agent.name, a.task8, note)
        scope = self.store.mark(self.agent.name, a.task_id, RECEIPT_KEY)
        if (not workspaces and metadata_receipt(scope[0] if scope else None, a.id)
                and not self.pool.leased_to(a.id)):
            if reason := self.pool.disk_hold():
                self._release_gate(a, "disk-held")
                self._release_checkout(a)
                self.store.update(a, state="ready", pid=0, stamp="", note=reason)
                self._resume_hold[a.id] = time.monotonic() + GATE_HOLD_SECONDS
                return False
            try:
                sandboxed = read_only(self._task(a.task_id))
            except MeshError:
                sandboxed = True
            prompt_text += metadata_instructions(sandboxed)
            if sandboxed:
                argv = adapter.with_read_only(argv)    # enforced by the harness, not only the prompt
        # Session preparation may have consumed free space.
        # Check again before the durable authorization callback or writer creation.
        if self.pool.disk_hold():
            self._disk_hold_attempt(a)
            return False
        if before_spawn:
            before_spawn()         # durable authorization fence at the process-creation boundary
        if profile:
            # Record the actual launch environment, not the lane's later route. A turn
            # can finish after another task has changed the lane's active profile.
            self.store.mark(self.agent.name, a.task_id, "auth_launch",
                            json.dumps({"attempt": a.id, "turn": a.turns + 1, "profile": profile}))
        proc = adapter.spawn(Launch(argv, self.agent.profile_dir, env, prompt_text), log_path)
        self.procs[a.id] = proc
        delivery = (a.wait or {}).get("ci_delivery") or {}
        marks = {delivery["key"]: delivery["value"]} if delivery else {}
        action = (a.wait or {}).get("ci_action_delivery") or {}
        if action:
            marks[action["key"]] = action["value"]
        first_move = (a.wait or {}).get("queue_delivery") or {}
        if first_move:
            marks[first_move["key"]] = first_move["value"]   # delivered only by a real spawn
            marks[queue_wait.FLOOR_KEY] = first_move["floor"]
        spawn_wait = dict(a.wait or {})
        spawn_wait.pop("queue_delivery", None)
        spawn_wait.pop("ci_delivery", None)
        spawn_wait.pop("ci_action_delivery", None)
        try:
            ok = self.store.update_with_marks(a, marks, expect=expect, state="running", wait=spawn_wait,
                                   pid=proc.pid, stamp=proc_stamp(proc.pid),
                                   log_path=str(log_path), session_id=sid, turns=a.turns + 1,
                                   turn_started_at=anchor, wake_text="", leader_done_at="", note="")
        except Exception:
            interrupt_group(proc.pid, signal.SIGTERM)
            self.procs.pop(a.id, None)
            raise
        if not ok:   # the attempt changed under us: do not leave an untracked writer behind
            interrupt_group(proc.pid, signal.SIGTERM)
            self.procs.pop(a.id, None)
            raise RuntimeError("attempt state changed while starting; session stopped")
        self._last_beat[a.id] = time.monotonic()
        self.emit("resume" if resume else "start", a, title=a.title[:120], session=sid)
        log.info("%s: %s #%s (%s) pid %s", self.agent.name, "resumed" if resume else "started",
                 a.task8, a.title[:60], proc.pid)
        return True

    def _auth_hold_attempt(self, a: Attempt, profile: str) -> None:
        """Preserve work, release writer authority and leave a diagnostic local hold."""
        self._release_gate(a, "auth-held")
        self._release_checkout(a)
        self.store.update(a, state="ready", pid=0, stamp="", note=f"authentication held for profile {profile}")
        self._resume_hold[a.id] = time.monotonic() + GATE_HOLD_SECONDS

    def _disk_hold_attempt(self, a: Attempt) -> None:
        """Return writer authority without changing work, readiness or WAIT context."""
        self._release_gate(a, "disk-held")
        self._release_checkout(a)
        self._resume_hold[a.id] = time.monotonic() + GATE_HOLD_SECONDS

    def _record_authentication_failure(self, a: Attempt, auth_only: bool) -> str:
        """Record the exact profile that produced the terminal auth failure.

        This is deliberately separate from holding the attempt: the task may
        have left our ownership/status while the child process was running.
        """
        launch = self.store.mark(self.agent.name, a.task_id, "auth_launch")
        try:
            launch = json.loads(launch[0]) if launch and launch[0] else {}
        except ValueError:
            launch = {}
        if not isinstance(launch, dict):
            launch = {}
        profile = launch.get("profile", "")
        if (launch.get("attempt") != a.id or launch.get("turn") != a.turns
                or not isinstance(profile, str) or not auth.PROFILE.fullmatch(profile)):
            # Upgraded while an older runner's process was already running: no
            # evidence of that process's profile. Hold this attempt conservatively;
            # never quarantine a guessed/current account in its place.
            profile = hashlib.sha256(f"unrecorded:{a.id}".encode()).hexdigest()[:32]
            self.store.mark(self.agent.name, a.task_id, "auth_unknown", json.dumps({"attempt": a.id, "profile": profile}))
        auth.hold(self.store, profile)
        self.store.event(a.id, "auth_failed", str(a.turns))
        if auth_only:
            self.store.event(a.id, "auth_only_turn", str(a.turns))
        return profile

    def _authentication_failed(self, a: Attempt, profile: str) -> None:
        self._auth_hold_attempt(a, profile)
        log.warning("%s: #%s authentication held; profile %s; repair authentication then use clear-auth-hold",
                    self.agent.name, a.task8, profile)

    @property
    def merge_enabled(self) -> bool:
        """``WAIT merge:`` needs the configured gate command and a GitLab to read the merge request from."""
        return bool(self.cfg.merge_command) and self.gitlab is not None

    def _parse(self, line: str, at: dt.datetime, task8: str = "") -> Outcome:
        return parse_line(line, at, task8, merge=self.merge_enabled)

    def _read_outcome(self, a: Attempt, result: TurnResult, comments: list[dict]) -> tuple[Outcome | None, str]:
        """The agent's newest declaration written after the turn started (Mesh
        clock), else the final message. Returns (outcome, its created_at)."""
        started = parse_time(a.turn_started_at)
        for c in reversed(comments):
            at = parse_time(c.get("created_at"))
            if at is None or (started is not None and at <= started):
                break
            author = c.get("author_id") or (c.get("author") or {}).get("id")
            lines = declaration_lines(c.get("body") or "")
            if author == self.agent_id and lines:
                return self._parse(lines[-1], now(), a.task8), c.get("created_at") or ""
        lines = declaration_lines(result.final_text)
        return (self._parse(lines[-1], now(), a.task8) if lines else None), self._anchor(comments)

    def _turn_over(self, a: Attempt) -> bool:
        """True when the turn's whole process group is gone. Enforces the turn
        time limit and the grace for children left behind by the leader."""
        proc = self.procs.get(a.id)
        if proc is not None:
            proc.poll()                         # reap our child so its pid can disappear
        if not group_alive(a.pid, a.stamp):
            return True
        leader_alive = (proc.returncode is None) if proc is not None else bool(proc_stamp(a.pid))
        current = now()
        if not leader_alive:
            if not a.leader_done_at:
                self.store.update(a, leader_done_at=iso())
                return False
            done_at = parse_time(a.leader_done_at)
            if done_at and current - done_at > dt.timedelta(minutes=self.cfg.straggler_grace_minutes):
                self.store.event(a.id, "kill", "children outlived the agent's turn")
                self._kill(a)
            return False
        turn_start = parse_time(self._turn_wall_start(a))
        if turn_start and current - turn_start > dt.timedelta(minutes=self.cfg.max_turn_minutes):
            self.store.event(a.id, "kill", f"turn exceeded {self.cfg.max_turn_minutes} min")
            self._kill(a)
        elif turn_start:
            self._watch_progress(a, turn_start.timestamp())
        return False

    def _progress_signal(self, a: Attempt, started: float) -> watchdog.Signal | None:
        """Where the running turn stands. An interactive turn's log stays empty until it ends, so
        its driver reports in a progress file; a headless turn is judged by its log."""
        key = Path(a.log_path).name.split(".")[0] if a.log_path else a.id
        report = interactive.read_progress(interactive.agent_dir(self.cfg.state_dir, self.agent.name), key)
        if report and int(report.get("pid") or 0) == a.pid:           # not an earlier turn's file
            return watchdog.Signal("driver", str(report.get("phase") or "launcher"),
                                   float(report.get("progress_at") or started), bool(report.get("external_wait")),
                                   float(report.get("updated") or 0), bool(report.get("held")),
                                   float(report.get("released_at") or 0))
        if isinstance(self.adapter_for(a.harness), interactive.InteractiveAdapter):
            if started < self._boot:
                return None                  # a driver of an older runner reports nothing: cannot be judged
            return watchdog.Signal("driver", "launcher", started)
        try:
            st = Path(a.log_path).stat()
        except OSError:
            return None
        # A headless log is a progress clock only (first output is not judged from it).
        return watchdog.Signal("log", "log", st.st_mtime if st.st_size else started)

    def _watch_progress(self, a: Attempt, started: float) -> None:
        """End a turn that shows no first output / no progress beyond the configured limits.
        The turn is cut off like one past max_turn_minutes: the card's checkout, the worktree
        and everything saved in it stay; the next tick settles it and the usual paths resume
        or hand the task on."""
        cfg = self.cfg
        if not (cfg.turn_first_output_minutes > 0 or cfg.turn_idle_minutes > 0 or cfg.turn_external_wait_minutes > 0):
            return
        if (a.id, a.turns) in self._watchdog_fired or self._watchdog_recorded(a):
            return
        reason = watchdog.judge(self._progress_signal(a, started), started=started, now=time.time(),
                                first_output_minutes=cfg.turn_first_output_minutes,
                                idle_minutes=cfg.turn_idle_minutes,
                                external_wait_minutes=cfg.turn_external_wait_minutes)
        if not reason:
            return
        self._watchdog_fired.add((a.id, a.turns))
        log.warning("%s: #%s turn watchdog: %s", self.agent.name, a.task8, reason)
        self.store.event(a.id, "watchdog", reason)
        self.store.event(a.id, "kill", f"turn watchdog: {reason}")
        self._kill(a)                        # the driver first: no process may type into the session after this
        if isinstance(self.adapter_for(a.harness), interactive.InteractiveAdapter):
            self._close_window(a)

    def _watchdog_recorded(self, a: Attempt) -> bool:
        """A watchdog event already stored for the current turn (since its last `running`
        state): the in-memory set does not survive a runner restart, the store does."""
        for _, kind, detail in reversed(self.store.events(a.id)):
            if kind == "state" and detail == "running":
                return False
            if kind == "watchdog":
                self._watchdog_fired.add((a.id, a.turns))
                return True
        return False

    def _close_window(self, a: Attempt) -> None:
        """Close the attempt's harness window so a stuck session cannot write beside the one
        the next turn resumes (it resumes by session id in a fresh window). Only called after
        the turn's driver is gone."""
        if group_alive(a.pid, a.stamp):
            return                           # the driver outlived the kill: leave the window to it
        key = Path(a.log_path).name.split(".")[0] if a.log_path else a.id
        self._tmux.kill_window(interactive.session_name(self.agent.name), key)
        interactive.forget(interactive.agent_dir(self.cfg.state_dir, self.agent.name), key)

    def _turn_wall_start(self, a: Attempt) -> str:
        for at, kind, detail in reversed(self.store.events(a.id)):
            if kind == "state" and detail == "running":
                return at
        return ""

    def _kill(self, a: Attempt) -> None:
        proc = self.procs.get(a.id)

        def alive() -> bool:
            if proc is not None:
                proc.poll()            # reap our child, or its zombie keeps the group "alive"
            return group_alive(a.pid, a.stamp)

        # A live child of ours holds its pid (even as a zombie until reaped); once it is gone
        # or reaped, the pid can belong to a different process.
        ours_alive = proc is not None and proc.poll() is None
        if not ours_alive and pid_reused(a.pid, a.stamp):
            # The pid now belongs to a different process: the attempt's group is gone, and
            # signalling would hit an unrelated group.
            log.warning("attempt %s: pid %s was reused (stamp %r differs); not signalling",
                        a.id, a.pid, a.stamp)
            return
        interrupt_group(a.pid, signal.SIGTERM)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and alive():
            time.sleep(0.2)
        if alive():
            interrupt_group(a.pid, signal.SIGKILL)
            time.sleep(0.2)
            alive()

    def settle(self, a: Attempt) -> None:
        """A turn finished: decide what happens to the attempt."""
        self._release_gate(a, "turn-end")
        result = self.adapter_for(a.harness).parse(Path(a.log_path)) if a.log_path else TurnResult()
        stderr = ""
        if a.log_path:
            try:
                stderr = Path(a.log_path).with_suffix(".stderr").read_text(errors="replace")[-4000:]
            except OSError:
                pass
        if result.session_id and result.session_id != a.session_id:
            self.store.update(a, session_id=result.session_id)
        self._report_spend(a, result)
        self.procs.pop(a.id, None)
        task = self._task(a.task_id)
        auth_profile = None
        if a.harness == "claude" and result.auth_failed:
            # The process's exact profile is evidence even if ownership/status
            # changed before settle. Record it before releasing this attempt.
            auth_profile = self._record_authentication_failure(a, result.auth_only)
        if not self._still_ours(task, a.mode):
            done = bool(task) and self.cats.category(task) in ("done", "review", "cancelled")
            # A terminal auth failure is operational evidence, not completion
            # evidence for this task; do not write a completion comment onto a
            # card that has moved to another owner/status.
            self._finish(a, "completed", "task left the agent's working statuses",
                         report=done and auth_profile is None)
            return
        if auth_profile is not None:
            self._authentication_failed(a, auth_profile)
            return
        if a.turns - self.store.auth_turns(a.id) >= self.cfg.max_turns and not self._cap_at_resume(a):
            self._give_up(a, task, f"stopped after {a.turns} turns on this task")
            return
        no_session = NO_SESSION_HINT.search(f"{stderr} {result.error_details or result.error}")
        restarted = self.store.mark(self.agent.name, a.task_id, "session_restart")
        if (a.turns > 1 and no_session and (not result.finished or result.is_error)
                and not (restarted and restarted[0] == a.id)):
            # resumed a session the harness does not know (it may report that as an error
            # result, not only on stderr): start the task over, same worktrees; once per
            # attempt, so an error text that merely looks like this cannot loop
            self.store.mark(self.agent.name, a.task_id, "session_restart", a.id)
            self.store.update(a, state="ready", session_id="", wake_text="")
            return
        if (not result.finished and not result.is_error and not result.final_text
                and not result.session_id and stderr.strip()):          # the stream never began
            # the harness died before its stream began (bad flag, untrusted dir, missing
            # binary): a harness error with its own words, not a silent "no outcome"
            result.is_error, result.error = True, stderr.strip()[-300:]
        if result.is_error:
            text = f"{result.error_details or result.error} {result.final_text} {stderr[-500:]}"
            public_error = public_harness_error(text)
            if QUOTA_HINT.search(text):
                if self._switch_on_limit(a):
                    # another account/profile is in place: retry this task on it at once
                    self.store.update(a, state="ready",
                                      wake_text=prompts.wake_prompt(task, "the provider limit was hit; "
                                                                          "you now run on another account"))
                    return
                self._starts_paused_until = time.monotonic() + self.cfg.quota_pause_minutes * 60
                log.warning("%s: harness reports a limit; new starts paused %s min", self.agent.name,
                            self.cfg.quota_pause_minutes)
            failures = a.no_outcome + 1
            if POLICY_REFUSAL_HINT.search(text):
                # the provider refuses this content; the same prompt on the same harness is
                # refused again, so hand the task up now instead of spending max_failures turns
                self._give_up(a, task, f"the harness refused the content: {public_error}")
            elif failures > self.cfg.max_failures:
                self._give_up(a, task, f"stopped after {failures} failed turns: {public_error}")
            else:
                self.store.update(a, state="ready", no_outcome=failures,
                                  wake_text=prompts.wake_prompt(task, f"the previous turn failed: {public_error}"))
            return
        if result.compactions and self._over_compaction_cap(a, task, result.compactions):
            return
        comments = self._comments(a.task_id)
        outcome, at = self._read_outcome(a, result, comments)
        if outcome is None or outcome.kind == "unparsed":
            line = outcome.line if outcome else ""
            if a.no_outcome >= self.cfg.max_no_outcome:
                self._give_up(a, task, "the session ended without a readable outcome twice"
                                       + (f" (last: `{line.strip()[:160]}`)" if line else ""))
            else:
                self.store.update(a, state="ready", no_outcome=a.no_outcome + 1,
                                  wake_text=prompts.no_outcome_prompt(task, line, outcome.note if outcome else "",
                                                                         merge=self.merge_enabled))
            return
        queue_contract = None
        if self.cfg.queue_wait_freshness_v1 and a.mode == "work" and outcome.kind == "time":
            try:
                queue_contract = self._queue_source(task, time.time())
                if queue_contract and queue_wait.consumed(self.store, a, self.agent_id, queue_contract):
                    queue_contract = None
            except MeshError:
                return  # retry settlement, without another turn or a stale source
        told = "" if queue_contract else self._time_wait_told(a, task, outcome)
        if told:
            # sent back before anything is recorded: a rejected declaration spends no budget
            self.store.update(a, state="ready", no_outcome=0, wake_text=prompts.wake_prompt(task, told))
            log.info("%s: #%s time wait sent back: %s", self.agent.name, a.task8, told[:80])
            return
        dead, dead_again = self._canceled_pipeline_wait(a, outcome)
        if dead:
            # Same budget as the other sent-back waits: a session that keeps re-declaring it
            # still reaches the wait budget.
            self.store.outcome(a, "wait")
            if self._over_budget(a, task):
                return
            self.store.update(a, state="ready", no_outcome=0, wake_text=prompts.wake_prompt(task, dead))
            log.info("%s: #%s wait on %s sent back: the pipeline is in a terminal canceled state",
                     self.agent.name, a.task8, outcome.target)
            return
        stale = (self._stale_ci_baseline(a, {"kind": outcome.kind, "target": outcome.target,
                                             "ci_change": outcome.extra["ci_change"]})
                 if not dead_again and outcome.kind in ("pipeline", "mr") and outcome.extra.get("ci_change") else "")
        if stale:
            # This wait could never wake (measured 21.10: one card waited on another with
            # after=...:running, already delivered as failed). Send it back, but count it as an
            # idle wait: a session that keeps re-declaring it still reaches the wait budget.
            self.store.outcome(a, "wait")
            if self._over_budget(a, task):
                return
            self.store.update(a, state="ready", no_outcome=0, wake_text=prompts.wake_prompt(task, stale))
            log.info("%s: #%s CI wait with a stale baseline sent back", self.agent.name, a.task8)
            return
        unattended = "" if dead_again else self._unattended_ci_wait(a, outcome)
        if unattended:
            # Nobody will make this happen: a blocked manual stage, a finished red pipeline, or
            # the task's own MR that no review has passed. Same budget as the stale-baseline
            # bounce: a session that keeps re-declaring it still reaches the wait budget.
            self.store.outcome(a, "wait")
            if self._over_budget(a, task):
                return
            self.store.update(a, state="ready", no_outcome=0, wake_text=prompts.wake_prompt(task, unattended))
            log.info("%s: #%s %s wait on %s sent back: it cannot complete on its own",
                     self.agent.name, a.task8, outcome.kind, outcome.target)
            return
        idle_card = self._card_wait_told(a, outcome)
        if idle_card:
            # same budget as the stale-baseline bounce: a session that keeps re-declaring it
            # still reaches the wait budget
            self.store.outcome(a, "wait")
            if self._over_budget(a, task):
                return
            self.store.update(a, state="ready", no_outcome=0, wake_text=prompts.wake_prompt(task, idle_card))
            log.info("%s: #%s card wait on #%s sent back: it waits on a timer or a person",
                     self.agent.name, a.task8, outcome.target)
            return
        repeat = self._repeated_decision_told(a, task, outcome, comments)
        if repeat:
            # the same budget as the other sent-back waits: asking the same question again and
            # again still reaches the wait budget
            self.store.outcome(a, "wait")
            if self._over_budget(a, task):
                return
            self.store.update(a, state="ready", no_outcome=0, wake_text=prompts.wake_prompt(task, repeat))
            log.info("%s: #%s decision wait to @%s sent back: the same question was already answered",
                     self.agent.name, a.task8, outcome.handle)
            return
        proceed = self._default_proceeds(a, task, outcome)
        if proceed:
            # the asker stated its own default: it continues now, the addressee may object. Counted
            # like a NEXT, so a session that keeps doing this still reaches the NEXT budget.
            self.store.outcome(a, "next")
            self.store.mark(self.agent.name, a.task_id, "cap_observation", "")
            if self._over_budget(a, task):
                return
            self.store.update(a, state="ready", no_outcome=0, wake_text=prompts.wake_prompt(task, proceed))
            return
        outcome = self._live_task_card_wait(a, task, outcome)
        # the wait is accepted from here on: only now may it retire a delivery receipt
        self._forget_spent_ci_delivery(a, outcome)
        recheck = self.store.mark(self.agent.name, a.task_id, "recheck_wake")
        owner, _, count = str(recheck[0] if recheck else "").rpartition(":")
        # a mark left by another attempt (one that ended without an outcome) is not this turn's
        rechecks = int(count) if owner == a.id and count.isdigit() else 0
        if recheck and recheck[0]:
            self.store.mark(self.agent.name, a.task_id, "recheck_wake", "")
        # a wait (or a Blocking ask) declared again after a re-check is recorded as "recheck":
        # the budget does not count it
        # ...and so is the very same wait declared again after any wake (a comment that
        # changed nothing): re-declaring a standing condition is not new work
        met = self.store.mark(self.agent.name, a.task_id, "met_wake")
        progressed = bool(met and met[0] == a.id)
        # ...and a wait declared after the previous one was met (a pipeline finished, a card
        # closed, an answer came) is a step of the work, recorded as "step": the wait budget
        # is for a session that keeps waking and waiting with nothing moving
        prev_wait = a.wait or {}
        same = (outcome.kind != "next" and prev_wait.get("kind") == outcome.kind
                and prev_wait.get("target") == outcome.target
                and (prev_wait.get("handle") or "") == (outcome.handle or "")
                and prev_wait.get("until") == (outcome.until.isoformat() if outcome.until else "")
                and (prev_wait.get("merge") or {}).get("sha") == outcome.extra.get("merge", {}).get("sha"))
        self.store.outcome(a, "next" if outcome.kind == "next"
                           else "recheck" if rechecks or same else "step" if progressed else "wait",
                           prev_wait.get("ci_progress", "") if progressed else "")
        if met and met[0]:
            self.store.mark(self.agent.name, a.task_id, "met_wake", "")
        if outcome.kind == "next":
            # NEXT has no event to observe after a cap.
            self.store.mark(self.agent.name, a.task_id, "cap_observation", "")
            if self._over_budget(a, task):
                return
            self.store.update(a, state="ready", no_outcome=0,
                              wake_text=prompts.wake_prompt(task, "you asked to continue (↻ NEXT)"))
            return
        wait = {"kind": outcome.kind, "target": outcome.target, "handle": outcome.handle,
                "line": outcome.line[:400],
                "until": outcome.until.isoformat() if outcome.until else "",
                "since": at,                 # Mesh clock: only comments after this wake the task
                "started": iso(),            # runner clock: age of the wait for its re-checks
                "note": outcome.note}
        if queue_contract:
            wait.update(kind="queue", until="", queue_request=queue_wait.new_request(a.task_id, queue_contract))
        for key in ("ci_change", "mr_state", "watch_comments_by", "converted_from"):
            if outcome.extra.get(key):
                wait[key] = outcome.extra[key]
        if outcome.kind == "merge":
            wait["merge"] = {**outcome.extra["merge"], "runs": 0}
        if same and prev_wait.get("cap_event_signature"):
            wait["cap_event_signature"] = prev_wait["cap_event_signature"]
        if rechecks:
            wait["rechecks"] = rechecks
        if outcome.kind in ("pipeline", "mr") and self.gitlab is None and not typed_wait(wait):
            # nothing can observe CI here: come back to it after a while instead of never
            wait.update(kind="time", until=iso(now() + dt.timedelta(minutes=30)),
                        note=f"{outcome.kind} {outcome.target} (no GitLab configured)")
        if wait["kind"] in ("mr", "pipeline") and self.gitlab is not None and not typed_wait(wait):
            try:
                # what is already there: a result that is final now wakes the wait on the
                # next check (waiting for it would never end); anything else is the
                # baseline a later change is measured against
                seeded = self.gitlab.seed(wait["kind"], outcome.target, now())
                prev = a.wait or {}
                if prev.get("kind") == wait["kind"] and prev.get("target") == outcome.target:
                    # the same wait again after being woken on this very state: the agent
                    # knows it and waits for it to change; do not wake it in a loop
                    if {k: v for k, v in (prev.get("seen") or {}).items() if k in ("pipeline", "mr_pipeline")} \
                            == {k: v for k, v in seeded.get("seen", {}).items() if k in ("pipeline", "mr_pipeline")}:
                        was_final = seeded.pop("settled", None)
                        rewaits = int(prev.get("dead_rewaits", 0) or 0)
                        if was_final and not rewaits:
                            # a wait on a terminal state that nothing will change is a dead
                            # wait (measured: a card re-declared a failed pipeline and slept
                            # on it for hours). Say so once, with the way out.
                            seeded["settled"] = (
                                f"{was_final}; you were already woken on this result and declared the same wait "
                                "again. It is final and will not change, so waiting on it cannot progress: take "
                                "the next action on it (fix and push, retry the pipeline, merge, or ROUTE to "
                                "the holder), or WAIT on a different event")
                            seeded["dead_rewaits"] = 1
                        elif was_final:
                            # told once and declared it again: hand control back on a clock
                            # instead of sleeping on an event that cannot come
                            seeded["dead_rewaits"] = rewaits + 1
                            wait.update(kind="time", until=iso(now() + dt.timedelta(hours=2)),
                                        note=f"{outcome.kind} {outcome.target} is final and unchanged; re-check")
                    told = (prev.get("seen") or {}).get("no_pipeline_told")
                    if told:
                        seeded.setdefault("seen", {})["no_pipeline_told"] = told
                wait.update(seeded)
            except Exception:
                pass
        # Save the structured target and baseline before a cap finishes this attempt.
        # A finished attempt releases its worktrees: recovery must use a normal new
        # start, never resume paths which the pool may have leased to another writer.
        observable = wait["kind"] in ("pipeline", "mr", "card")
        if observable:
            self.store.update(a, wait=wait)
        self.store.mark(self.agent.name, a.task_id, "cap_observation", a.id if observable else "")
        if outcome.kind == "decision" and outcome.handle and self._budget_spent(a):
            # A cap requeues the task to wait for new input; the question to an agent must already
            # be filed by then, or nobody is asked and no input can ever arrive.
            # Only a plain agent addressee: person asks, unanswerable or invalid asks keep their
            # path (they raise gates and comments that a cap must not trigger).
            try:
                gated = (self._task(a.task_id) or task).get("human_gate")   # a person hold stays authoritative
                matches = {} if gated else self._recipient_matches(outcome.handle)
                addressee = self.effects._resolve_agent(outcome.handle, self.me)
            except MeshError:
                matches, addressee = {}, None
            only = next(iter(matches.values())) if len(matches) == 1 else {}
            if (addressee and only.get("kind") == "agent" and only.get("id") == addressee.get("id")
                    and addressee.get("id") != self.agent_id
                    and not self._unavailable(str(addressee.get("name") or outcome.handle))):
                self.effects._file_decision(a, task, wait, self.me)
        # An accepted ``merge`` wait hands the next step to the runner's own gate, which runs only
        # from the waiting attempt. A cap here would requeue the task to wait for new input
        # instead, and nothing could ever arrive: the merge request's remaining jobs are started
        # by the gate alone. The wait is still counted as an idle wait (so the next idle wait of
        # a session that keeps declaring them is capped), and the NEXT and step budgets still stop it.
        if self._over_budget(a, task, idle_exempt=outcome.kind == "merge"):
            ended = self.store.get(a.id)
            if not ended or ended.state != "failed":
                self.store.mark(self.agent.name, a.task_id, "cap_observation", "")
            return
        self.store.mark(self.agent.name, a.task_id, "cap_observation", "")
        if outcome.kind in ("blocking", "decision") and outcome.handle:
            told = self._route_ask(a, task, outcome, wait, at)
            if (self.store.get(a.id) or a).state in ("failed", "completed", "aborted"):
                return
            if told:
                self.store.update(a, state="ready", no_outcome=0, wake_text=told)
                log.info("%s: #%s ask to @%s not raised by Mesh; told the agent", self.agent.name, a.task8,
                         outcome.handle)
                return
        self._release_checkout(a)
        self.effects.on_wait(a, task, wait, self.me)
        if wait.get("kind") == "queue":
            req = wait["queue_request"]
            mark = queue_wait.intent(a, req, self.agent_id, time.time())
            marks = {queue_wait.key(req): json.dumps(mark)}
            started = parse_time(a.turn_started_at)
            if started is not None and started.timestamp() >= req["contract"]["observed_at"]:
                # The turn that just ended read the thread with this request in it: it is
                # the owner's first move, so it is not woken a second time for it.
                marks[queue_wait.delivered_key(self.agent_id, req["contract"])] = "delivered"
                marks[queue_wait.FLOOR_KEY] = queue_wait.raised_floor(
                    self.store, self.agent.name, a.task_id, req["contract"])
            self.store.update_with_marks(a, marks, expect=a.state, state="waiting", wait=wait, no_outcome=0)
        else:
            self.store.update(a, state="waiting", wait=wait, no_outcome=0)
        self.emit("wait", a, wait=wait["kind"], target=wait.get("target") or wait.get("handle") or wait.get("until"))
        log.info("%s: #%s waits: %s %s", self.agent.name, a.task8, wait["kind"],
                 wait.get("target") or wait.get("handle") or wait.get("until"))

    def _already_posted(self, a, body: str) -> bool:
        """Whether this agent already left exactly ``body`` on the task. An unreadable thread is
        False: posting once more is the lesser harm than dropping the note."""
        try:
            return any(c.get("author_id") == self.agent_id and (c.get("body") or "").strip() == body.strip()
                       for c in self.mesh.comments(a.task_id))
        except MeshError:
            return False

    def _ask_agent(self, task: dict, outcome: Outcome) -> dict | None:
        """The workspace agent a ``decision:@handle`` wait addresses, when that is unambiguously
        another agent (not a person, not the asker) and the task is under no human gate; else None.
        An unreadable roster is None: the usual routing decides."""
        if outcome.kind != "decision" or not outcome.handle or task.get("human_gate"):
            return None
        try:
            matches = self._recipient_matches(outcome.handle)
            addressee = self.effects._resolve_agent(outcome.handle, self.me)
        except MeshError:
            return None
        only = next(iter(matches.values())) if len(matches) == 1 else {}
        if (not addressee or only.get("kind") != "agent" or only.get("id") != addressee.get("id")
                or addressee.get("id") in (None, self.agent_id)):
            return None
        return addressee

    def _default_proceeds(self, a: Attempt, task: dict, outcome: Outcome) -> str:
        """Wake text when a ``decision:@agent`` question carries the asker's own proposed default
        (``DEFAULT_MARKER``) and no hard rule (asks.py) matches it: nothing needs to block on it.
        The question is posted on this task, @-mentioning the addressee, which may object there;
        no request is filed. '' = route the question as usual (hard, to a person, no marker, an
        unreadable roster, or the comment could not be posted)."""
        if not self.cfg.decision_default_proceeds or not DEFAULT_MARKER.search(outcome.note or ""):
            return ""
        verdict = asks.classify(outcome.note, task.get("labels"), self.cfg.hard_ask_patterns,
                                self.cfg.hard_ask_labels)
        if verdict.hard:
            return ""
        addressee = self._ask_agent(task, outcome)
        if not addressee:
            return ""
        name = str(addressee.get("name") or outcome.handle)
        note = outcome.note.strip()
        asked = hashlib.sha1(f"{name.casefold()}\n{note}".encode()).hexdigest()[:12]
        posted = ((self.store.mark(self.agent.name, a.task_id, "default_posted") or ("",))[0] or "").split(",")
        body = f"[runner] Proceeding with the proposed default; @{name}, reply on this task to object:\n> {note}"
        if asked not in posted and self._already_posted(a, body):
            # Posted before a crash that lost the mark: record it, do not post it twice.
            posted.append(asked)
            self.store.mark(self.agent.name, a.task_id, "default_posted", ",".join(posted).strip(","))
        if asked not in posted:
            try:
                # "[runner]": a runner note, so it files no mention request and wakes no wait
                self.mesh.add_comment(a.task_id, body)
            except MeshError as e:
                log.warning("%s: #%s default ask to @%s: post: %s; asking as usual", self.agent.name, a.task8,
                            name, e)
                return ""
            self.store.mark(self.agent.name, a.task_id, "default_posted", ",".join([*posted, asked]).strip(","))
        self.store.event(a.id, "decision_default_proceeds", f"@{name}: {note[:300]}")
        self.emit("decision_default_proceeds", a, handle=name)
        log.info("%s: #%s decision wait to @%s carries a proposed default: no request, proceeding",
                 self.agent.name, a.task8, name)
        return (f"your question to @{name} states your own proposed default, so nothing has to wait for it: "
                f"no request was filed, and the question was posted on this task for @{name} to object. "
                "Proceed with your stated default now and end the turn with your real next outcome "
                f"(the event you then wait on, ↻ NEXT, or the review/done status). An objection from @{name} "
                "reaches you as an ordinary comment.")

    def _live_task_card_wait(self, a: Attempt, task: dict, outcome: Outcome) -> Outcome:
        """A ``decision:@agent`` question about a task that agent itself holds and is working on
        (``#<8hex>`` or its uuid in the question; todo, in_progress or review) becomes a card wait on
        that task. Such a question ("pass the handback from #x when ready", "tell me when #x's verdict
        is in") can only be answered "not ready", and a request card starts a fresh session for the
        addressee just to say so. The card wait wakes when the task closes or ships, or when the
        addressee comments on it; nothing is posted to the addressee. Exactly one other task must be
        named; anything else (none, several, someone else's, a closed one, unreadable) is unchanged."""
        if not self.cfg.decision_live_task_card_wait:
            return outcome
        refs = []
        for m in TASK_REF.finditer(outcome.note or ""):
            ref = (m.group("uuid") or m.group("short")).casefold()
            if ref[:8] != a.task8 and ref[:8] not in (r[:8] for r in refs):
                refs.append(ref)
        if len(refs) != 1:
            return outcome
        addressee = self._ask_agent(task, outcome)
        if not addressee:
            return outcome
        ref = refs[0]
        try:
            target = self.mesh.task(ref) if len(ref) > 8 else self.mesh.task_by_short(ref)
            target = target.get("task", target) if isinstance(target, dict) else {}
            holder = target.get("assignee_id") or (target.get("assignee") or {}).get("id")
            live = target.get("id") and self.cats.category(target) in ("todo", "in_progress", "review")
        except MeshError as e:
            log.info("%s: #%s decision wait names #%s, unreadable (%s): asked as usual", self.agent.name,
                     a.task8, ref[:8], e.status)
            return outcome
        if not live or holder != addressee["id"] or target["id"] == a.task_id:
            return outcome
        name = str(addressee.get("name") or outcome.handle)
        target8 = str(target["id"])[:8]
        self.store.event(a.id, "decision_to_card_wait", f"@{name} #{target8}")
        self.emit("decision_to_card_wait", a, handle=name, target=target8)
        log.info("%s: #%s decision wait to @%s converted to card wait on #%s (its live task): no request filed",
                 self.agent.name, a.task8, name, target8)
        return Outcome("card", target8, note=f"asked @{name}, waits on its live task #{target8}: {outcome.note}",
                       line=outcome.line,
                       extra={"watch_comments_by": {"id": addressee["id"], "name": name},
                              "converted_from": f"decision:@{name}"})

    def _time_wait_told(self, a: Attempt, task: dict, outcome: Outcome) -> str:
        """A time WAIT longer than ``time_wait_reason_after_minutes`` that gives no reason, or
        whose reason names a merge request, a card, a pipeline or another agent, is sent back
        once per task and UTC day: a deferral must say why, and a wait for an event must wait on the event
        (it wakes the moment it happens, not when a guessed hour comes). Declared again, it
        stands."""
        if outcome.kind != "time" or not outcome.until:
            if self.store.mark(self.agent.name, a.task_id, "short_time_waits"):
                self.store.mark(self.agent.name, a.task_id, "short_time_waits", "")
            return ""
        if outcome.until - now() <= dt.timedelta(minutes=self.cfg.time_wait_reason_after_minutes):
            return self._short_series_told(a, outcome)
        self.store.mark(self.agent.name, a.task_id, "short_time_waits", "")
        note = (outcome.note or "").strip()
        event = TIME_WAIT_EVENT.search(note)
        agent = "" if event or not note else self._agent_named(note)
        if note and not event and not agent:
            return ""
        if not self.store.claim(TASK_WIDE, a.task_id, "time_wait_told", now().date().isoformat()):
            return ""                    # told today already (by any agent): this declaration stands
        line = outcome.line.strip()[:160]
        if event:
            return (f"your `{line}` waits for {event.group(0)}, an event the runner can watch. Wait on it "
                    "instead (⏳ WAIT mr:<group>/<project>!<iid>, ⏳ WAIT card:#<8hex>, "
                    "⏳ WAIT pipeline:<group>/<project>#<id>): you are woken the moment it happens. "
                    "If time itself is the condition, declare the time WAIT again with the reason.")
        if agent:
            return (f"your `{line}` waits for {agent}. Another agent's action is an event: wait on "
                    f"it with `⏳ WAIT decision:@{agent}: <what you need from them>` ({agent} gets a "
                    "request card and you are woken when they answer), or `⏳ WAIT card:#<8hex>` "
                    "for their card. If time itself is the condition, declare the time WAIT again "
                    "with the reason.\n\n" + DECISION_WAIT_GUIDANCE)
        return (f"your `{line}` gives no reason. Either do the work now, or declare it again as "
                "`⏳ WAIT <when>: <why not now, and what must happen for the wait to end>`.")

    def _short_series_told(self, a: Attempt, outcome: Outcome) -> str:
        """Short time WAITs declared back to back on one task (each with a new ``until``, so a
        repeated wait is not what catches them) are polling on a timer: it spends the daily turn
        budget for nothing. From the ``short_time_wait_series``-th in a row the declaration is
        sent back once per task and UTC day, naming the events a wait can use; declared again,
        it stands. Any other outcome, or a longer time WAIT, ends the series."""
        limit = self.cfg.short_time_wait_series
        if limit <= 0:
            return ""
        prev = self.store.mark(self.agent.name, a.task_id, "short_time_waits")
        n = (int(prev[0]) if prev and prev[0].isdigit() else 0) + 1
        self.store.mark(self.agent.name, a.task_id, "short_time_waits", str(n))
        if n < limit or not self.store.claim(TASK_WIDE, a.task_id, "short_series_told", now().date().isoformat()):
            return ""
        return (f"your `{outcome.line.strip()[:160]}` is the {n}th short time WAIT in a row on this task: "
                "polling on a timer spends the daily turn budget. Wait on the thing that will change "
                "(⏳ WAIT mr:<group>/<project>!<iid>, ⏳ WAIT pipeline:<group>/<project>#<id>, "
                "⏳ WAIT card:#<8hex>, ⏳ WAIT decision:@<agent>): you are woken the moment it happens. "
                "If time itself is the condition, declare the time WAIT again with the reason and a "
                "longer interval.\n\n" + DECISION_WAIT_GUIDANCE)

    def _agent_named(self, note: str) -> str:
        """Another workspace agent named in a time WAIT's reason, '' if none.

        Matched as written (case-sensitive, a whole word) so that prose does not trip it;
        the agent's own name and ``time_wait_ignore_names`` (agent names that double as
        everyday or product words in a deployment) do not count."""
        mine = {self.agent.name.casefold(), str(self.me.get("name") or "").casefold()}
        ignore = {n.casefold() for n in self.cfg.time_wait_ignore_names}
        for name in sorted(self.effects.agent_names(self.me), key=len, reverse=True):
            if len(name) < 3 or name.casefold() in mine or name.casefold() in ignore:
                continue
            if re.search(r"(?<![\w-])@?" + re.escape(name) + r"(?![\w-])", note):
                return name
        return ""

    def _recipient_matches(self, handle: str) -> dict:
        """The workspace recipients a handle names, keyed (kind, id). An agent's service user
        shares its slug ("Example (QA Mesh)" next to agent Example): that pair counts as the agent."""
        rows = self.mesh.workspace_recipients(self.me.get("workspace_id", ""), handle)
        key = handle.casefold()
        matches = {(row.get("kind"), row.get("id")): row for row in rows
                   if str(row.get("slug") or "").casefold() == key
                   or (row.get("kind") == "agent"
                       and str(row.get("display_name") or "").casefold() == key)}
        if len(matches) > 1:
            # An agent's service user shares its slug ("Example (QA Mesh)" next to agent
            # Example). `WAIT decision` is addressed to an agent; a person is asked by
            # `❓ Blocking`. Mesh marks a service user (is_service_user + agent_id), so a
            # same-slug person, whatever their display name, stays ambiguous.
            agents = [row for row in matches.values() if row.get("kind") == "agent"]
            if len(agents) == 1:
                others = [row for row in matches.values() if row is not agents[0]]
                if len(others) == 1 and all(row.get("kind") == "user"
                                 and row.get("is_service_user") is True
                                 and row.get("agent_id") == agents[0].get("id")
                                 for row in others):
                    matches = {(agents[0]["kind"], agents[0]["id"]): agents[0]}
        return matches

    def _route_ask(self, a: Attempt, task: dict, outcome: Outcome, wait: dict, at: str) -> str:
        """Who a question goes to; sets ``wait["kind"]``. Returns a wake text when the
        agent must hear back at once, else ''.

        An agent is asked through a decision request (effects), never a human gate.
        A person is asked only by Mesh: it raises the gate from the ``❓ Blocking``
        line of the agent's comment after checking that the question was not
        answered already. The runner never sets the gate itself: such a gate skips
        that check, carries no question and only a person can lift it (measured
        17.10: a refused repeat ask, a shadow-mode lead and an ask to an agent all
        sat in the owner's triage). When Mesh raised nothing, the agent is told why,
        once per reason; after that the wait holds until someone comments."""
        # A real gate remains authoritative even if this turn names a bad handle
        # or the roster is unavailable. Never bypass an existing human hold.
        try:
            fresh = self._task(a.task_id) or task
        except MeshError:
            fresh = task
        if fresh.get("human_gate"):
            wait["kind"] = "blocking"
            return ""
        try:
            matches = self._recipient_matches(outcome.handle)
            if len(matches) != 1:
                reason = "ambiguous recipient" if matches else "unknown recipient"
                return self._invalid_ask(a, task, outcome.handle, reason)
            recipient = next(iter(matches.values()))
            if recipient.get("kind") == "agent":
                addressee = self.effects._resolve_agent(outcome.handle, self.me)
                if not addressee or addressee.get("id") != recipient.get("id"):
                    unable = "recipient agent is not resolvable by this runner"
                elif addressee.get("id") == self.agent_id:
                    unable = "it is the asking agent itself"
                else:
                    unable = self._unavailable(str(addressee.get("name") or outcome.handle))
                if unable:
                    # nobody would ever answer: the project lead decides (no timer involved)
                    if self.cfg.unanswerable_ask_to_lead and self._ask_to_lead(
                            a, task, outcome, wait, recipient.get("id"), unable):
                        return ""
                    if not addressee or addressee.get("id") != recipient.get("id") or addressee.get("id") == self.agent_id:
                        # a question to yourself is answered by nobody, and there is no lead to
                        # take it: waiting would park the card (and whatever waits on it) for good
                        return self._invalid_ask(a, task, outcome.handle, unable)
                wait["kind"] = "decision"
                return ""
            if recipient.get("kind") != "user":
                return self._invalid_ask(a, task, outcome.handle, "unsupported recipient kind")
            if outcome.kind == "decision" and (outcome.note or "").strip():
                redirect = self._person_ask_redirect(task, outcome, recipient)
                if redirect and self._ask_to_lead(a, task, outcome, wait, recipient.get("id"), redirect[0],
                                                  soft=redirect[1]):
                    return ""
        except MeshError:
            return self._invalid_ask(a, task, outcome.handle, "recipient roster unavailable; identity unverified")
        wait["kind"] = "decision"
        wait["person_ask"] = True
        # An empty question raises nothing: there is no text to show the person, so no `❓ Blocking`
        # line is posted, no gate is raised and the question is neither classified nor redirected to
        # the lead (both need the text). It stays a person-ask decision wait, which wake.py re-checks
        # on a bounded interval ("no human gate or agent request stands behind it").
        if outcome.kind == "decision" and (outcome.note or "").strip():
            # `WAIT decision:@person` is the agent's question to a person in the wrong
            # form: Mesh raises nothing from it and nobody would ever see it. Put the
            # question where Mesh reads it, the agent's own `❓ Blocking` line, once per
            # question; Mesh still decides (answered already, gate) and a refusal is
            # told to the agent below.
            asked = hashlib.sha1(f"{outcome.handle.casefold()}\n{outcome.note.strip()}".encode()).hexdigest()[:12]
            posted = ((self.store.mark(self.agent.name, a.task_id, "ask_posted") or ("",))[0] or "").split(",")
            if asked not in posted:
                try:
                    self.mesh.add_comment(
                        a.task_id, f"Вопрос из `WAIT decision:@{outcome.handle}` — адресат человек, "
                                   f"поднимаю как запрос.\n\n❓ Blocking @{outcome.handle}: {outcome.note.strip()}")
                    # remembered only once delivered: a refused or failed POST is retried
                    # on the next turn, and every question and addressee is kept, not the last
                    self.store.mark(self.agent.name, a.task_id, "ask_posted", ",".join([*posted, asked]).strip(","))
                    fresh = self._task(a.task_id) or fresh
                except MeshError as e:
                    log.warning("%s: ask #%s: post: %s", self.agent.name, a.task8, e)
                if fresh.get("human_gate"):
                    wait["kind"] = "blocking"
                    return ""
        since = parse_time(at)
        refusal = next((c for c in reversed(self._comments(a.task_id))
                        if (c.get("system_code") == "repeat_ask_refused"
                            or (c.get("body") or "").startswith(("🔁 Auto: human_gate not armed", "🔒 Gate on ")))
                        and (since is None or (parse_time(c.get("created_at")) or since) >= since)), None)
        # keyed on what was refused, not on the notice: Mesh posts a new notice (new id)
        # for every repeat of the same already-answered question
        params = (refusal or {}).get("system_params") or {}
        reason = (f"refused:{params.get('answered_at') or (refusal.get('body') or '')[:300]}" if refusal
                  else f"no-marker:{outcome.kind}")
        prev = self.store.mark(self.agent.name, a.task_id, "ask_not_raised")
        if prev and prev[0] == reason:
            # told once and declared the same ask again: no gate stands behind it and Mesh
            # will not raise one, so a person-ask wait would sleep on a question nobody sees
            # (measured: a card with a secret-rotation ask refused by policy). Hand control
            # back on a clock instead.
            wait.pop("person_ask", None)
            wait.update(kind="time", until=iso(now() + dt.timedelta(hours=6)),
                        note=f"ask to @{outcome.handle} was not raised by Mesh; re-check")
            return ""
        self.store.mark(self.agent.name, a.task_id, "ask_not_raised", reason)
        return prompts.ask_not_raised_prompt(task, outcome.handle, (refusal or {}).get("body") or "")

    def _person_ask_redirect(self, task: dict, outcome: Outcome, recipient: dict) -> tuple[str, bool] | None:
        """(why, soft) when a question to this person goes to the project lead instead of
        raising a human gate; None when the person is asked as before. A configured test or
        service user is never asked. A soft question (no hard rule of asks.py) goes to the lead only
        with ``soft_person_asks_to_lead``."""
        slug = str(recipient.get("slug") or "").casefold()
        flagged = any(recipient.get(k) is True for k in ("is_test", "is_service", "is_bot", "test_user"))
        if self.cfg.unanswerable_ask_to_lead and (flagged or slug in self.cfg.unanswerable_users):
            return f"@{outcome.handle} is a test or service account that does not answer", False
        if not self.cfg.soft_person_asks_to_lead:
            return None
        verdict = asks.classify(outcome.note, task.get("labels"), self.cfg.hard_ask_patterns,
                                self.cfg.hard_ask_labels)
        if verdict.hard:
            return None
        return f"soft question ({verdict.rule})", True

    def _ask_to_lead(self, a: Attempt, task: dict, outcome: Outcome, wait: dict, asked_id: str | None,
                     why: str, soft: bool = False) -> bool:
        """Put the question to the project lead as an agent decision request (effects files it
        from the wait's ``handle``), with a comment on the card saying whom and why. The card waits
        for the lead's answer like for any agent, on the event and never on a timer. False when no
        lead can take it: the caller then behaves as it always did."""
        try:
            leads, _ = self._lead_chain(task)
        except MeshError:
            return False
        leads = [x for x in leads if x.get("id") and x.get("id") != asked_id]
        lead = next((x for x in leads if not self._unavailable(str(x.get("name") or ""))), None)
        # a lead this runner does not run may still be driven from elsewhere; a paused one is not
        lead = lead or next((x for x in leads if x.get("name")
                             and self._unavailable(str(x["name"])) == "it is not run by this runner"), None)
        if not lead or not lead.get("name"):
            return False
        wait.update(kind="decision", handle=str(lead["name"]), asked_handle=outcome.handle, redirected=why[:200])
        wait.pop("person_ask", None)
        key = hashlib.sha1(f"{outcome.handle.casefold()}\n{(outcome.note or '').strip()}\n{lead['name']}"
                           .encode()).hexdigest()[:12]
        told = (self.store.mark(self.agent.name, a.task_id, "ask_redirected") or ("",))[0]
        if told != key:
            self.store.mark(self.agent.name, a.task_id, "ask_redirected", key)
            self._comment(a.task_id,
                          f"Question to @{outcome.handle} goes to the project lead @{lead['name']}: {why}."
                          + (" Decided by lead, owner may override by commenting here." if soft else
                             f" @{lead['name']} answers with a comment on this card."))
        log.info("%s: #%s ask to @%s redirected to lead %s: %s", self.agent.name, a.task8, outcome.handle,
                 lead["name"], why)
        return True

    def _invalid_ask(self, a: Attempt, task: dict, handle: str, reason: str) -> str:
        """One correction per attempt, then a visible release, never a silent WAIT.

        This also bounds roster outages: at most two lookups/turns before the
        normal failed-attempt gate applies its comment/cooldown policy.
        """
        text = (f"Task #{a.task8}: cannot route recipient @{handle}: {reason}. No human ask was posted. "
                "Use an exact workspace agent name or user slug, choose a real card/event to wait on, "
                "or make the decision and continue. Local helper types are not workspace recipients.")
        previous = self.store.mark(self.agent.name, a.task_id, "ask_invalid")
        if not previous or previous[0] != a.id:
            self.store.mark(self.agent.name, a.task_id, "ask_invalid", a.id)
            return "[runner] " + text
        # Keep the diagnostic visible without transferring ownership to an
        # arbitrary person/agent. The normal failure cooldown applies.
        self._to_todo(a, task)
        self._finish(a, "failed", f"invalid recipient: {reason}")
        self._comment(a.task_id, text + " Repeated invalid/unverified recipient; released until a new comment or the failure cooldown passes.")
        return ""

    def _switch_on_limit(self, a: Attempt) -> bool:
        """Ask the operator's command to move the agent to another account or
        profile. Exit 0 means it did; the next spawn reads the new environment."""
        if not self.agent.on_limit_command:
            return False
        env = dict(os.environ, MESH_RUNNER_AGENT=self.agent.name, MESH_RUNNER_TASK=a.task_id)
        try:
            res = subprocess.run(self.agent.on_limit_command, capture_output=True, text=True, timeout=120, env=env)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("%s: on-limit command: %s", self.agent.name, e)
            return False
        self.store.event(a.id, "limit_switch", f"rc={res.returncode} {res.stdout.strip()[:200]}")
        self.emit("limit", a, switched=res.returncode == 0, detail=res.stdout.strip()[:200])
        log.info("%s: provider limit; on-limit command rc=%s %s", self.agent.name, res.returncode,
                 res.stdout.strip()[:120])
        return res.returncode == 0

    def _lead_chain(self, task: dict) -> tuple[list[dict], list[str]]:
        """Leads above this agent for this task, nearest first: its own `lead`, that
        lead's `lead` while it is an agent of this runner, then the project's lead (a
        name or an agent id). Also why a configured lead cannot be used (a name no agent
        of this workspace has: a hand-off cannot reach another workspace)."""
        project_lead = self.cfg.project_leads.get(task.get("project_id", ""), "")
        names, raw = [], self.agent.lead or project_lead
        while raw and raw.casefold() not in {n.casefold() for n in names} and len(names) < 6:
            names.append(raw)
            ours = next((ag for ag in self.cfg.agents if ag.name.casefold() == raw.casefold()), None)
            raw = ours.lead if ours else ""
        if project_lead and project_lead.casefold() not in {n.casefold() for n in names}:
            names.append(project_lead)
        leads, notes = [], []
        for raw in names:
            if raw.casefold() == self.agent.name.casefold():
                continue
            if len(raw) == 36 and raw.count("-") == 4:
                lead = {"id": raw, "name": self.effects.agent_name_by_id(raw, self.me) or raw[:8]}
            else:
                lead = self.effects._resolve_agent(raw, self.me)
            if lead is None:
                notes.append(f"lead {raw} is not in this workspace")
                log.warning("%s: #%s lead %s is not in this workspace", self.agent.name, task.get("id", "")[:8], raw)
            elif lead.get("id") != self.agent_id and lead not in leads:
                leads.append(lead)
        return leads, notes

    def _pick_lead(self, task: dict) -> tuple[dict | None, str]:
        """(the lead to hand this task to now or None, why a nearer lead was passed
        over or none is left; '' = nothing to say). A task this agent
        already handed up today came back (a rule, a person, a lead's own hand-off):
        it stays, or the cards circle between the same agents. A lead that itself
        started the task ``daily_start_cap`` times today is skipped for the next one up."""
        today = now().date().isoformat()
        handed = self.store.mark(self.agent.name, task["id"], "handed_up")
        came_back = bool(handed and handed[0] == today)
        leads, notes = self._lead_chain(task)
        cap = self.cfg.daily_start_cap
        for lead in leads:
            name = str(lead.get("name") or "")
            runner_name = next((ag.name for ag in self.cfg.agents if ag.name.casefold() == name.casefold()), name)
            starts = self.store.starts_today(runner_name, task["id"]) if cap else 0
            if cap and starts >= cap:
                notes.append(f"{name} already started it {starts} times today")
                continue
            if came_back and not self._lead_has_room(lead, runner_name):
                # Back from a lead that has no free slot: keep it, or it circles
                # between two busy agents. A lead with room takes it again: left
                # with an agent past its own cap, the task would wait for tomorrow.
                notes.append(f"{name} has no free slot")
                continue
            if came_back:
                notes.append(f"it came back once today; {name} has a free slot")
            return lead, "; ".join(notes)
        if came_back:
            return None, "; ".join(["it was handed up once today already and came back"] + notes)
        return None, "; ".join(notes)

    def _lead_has_room(self, lead: dict, runner_name: str) -> bool:
        """A lead run by this runner with fewer active attempts than its slots. A lead
        this runner does not run has no visible capacity and counts as busy."""
        ours = next((ag for ag in self.cfg.agents if ag.name.casefold() == runner_name.casefold()), None)
        if ours is None:
            return False
        slots = ours.max_parallel if ours.max_parallel > 0 else max(1, int(lead.get("max_concurrent_tasks") or 1))
        return self.store.active_count(ours.name) < slots

    def _handed(self, task_id: str) -> None:
        self.store.mark(self.agent.name, task_id, "handed_up", now().date().isoformat())

    # ---------------------------------------------------- caps: back to the queue, restart on input
    def _cap_wait(self, task_id: str) -> dict | None:
        """The record a cap left for this task (kind, input fingerprint), None when it is not waiting."""
        mark = self.store.mark(self.agent.name, task_id, CAP_WAIT)
        if not mark or not mark[0]:
            return None
        try:
            rec = json.loads(mark[0])
        except ValueError:
            return None
        return rec if isinstance(rec, dict) else None

    def _input_fingerprint(self, task: dict) -> dict:
        return inputs.fingerprint(self.mesh, self.cats, self.agent_id, task)

    def _inputs_changed(self, task: dict, rec: dict) -> bool:
        """New input since the cap. A read that fails is not news: the task stays where it is.
        The answer is kept for ``wake_poll_seconds`` so a long queue does not read every capped
        task on every tick."""
        tid = task["id"]
        got = self._input_checked.get(tid)
        mono = time.monotonic()
        if got and got[0] == rec.get("at") and mono - got[1] < self.cfg.wake_poll_seconds:
            return got[2]
        try:
            changed = self._input_fingerprint(task) != rec.get("fp")
        except MeshError as e:
            log.info("%s: #%s inputs unreadable after a cap (%s); not started", self.agent.name, tid[:8], e.status)
            return False
        self._input_checked[tid] = (rec.get("at"), mono, changed)
        return changed

    def _backoff_on_input(self, task: dict, until: float) -> None:
        """Set the failed-start backoff of ``task`` until ``until`` and let new input lift it.

        Only for a start that raised unexpectedly: nothing about it says the task itself is the
        problem, so what the task was waiting for arriving (a blocker closed, a linked merge request
        changed, a new assignee, a comment from someone else) is a reason to try again now. The
        fingerprint is taken once, here; if it cannot be read, the backoff simply runs its course.
        Backoffs whose cause new input does not touch never come through here: a refused checkout
        (another writer holds the task), a held workspace or authentication, a refused spawn, the
        daily start/turn budget and gate refusals."""
        tid = task["id"]
        self._task_backoff[tid] = until
        self._backoff_inputs.pop(tid, None)
        self._backoff_checked.pop(tid, None)
        try:
            self._backoff_inputs[tid] = (until, self._input_fingerprint(task))
        except MeshError as e:
            log.info("%s: #%s inputs unreadable at backoff (%s); it runs its course", self.agent.name,
                     tid[:8], e.status)

    def _backoff_lifted(self, task: dict) -> bool:
        """True when the task's running failed-start backoff was recorded with a fingerprint and the
        task has received new input since (and the backoff is then dropped). Read at most once per
        ``wake_poll_seconds`` per task; a read that fails is no news. A backoff without a record, or
        replaced by a later one, is never lifted here; a task with no backoff costs no read."""
        tid = task["id"]
        recorded = self._backoff_inputs.get(tid)
        if recorded is None:
            return False
        deadline = self._task_backoff.get(tid, 0)
        mono = time.monotonic()
        if recorded[0] != deadline or mono >= deadline:    # replaced by another backoff, or over
            self._backoff_inputs.pop(tid, None)
            self._backoff_checked.pop(tid, None)
            return False
        got = self._backoff_checked.get(tid)
        if got and got[0] == deadline and mono - got[1] < self.cfg.wake_poll_seconds:
            changed = got[2]
        else:
            try:
                changed = self._input_fingerprint(task) != recorded[1]
            except MeshError as e:
                log.info("%s: #%s inputs unreadable during a backoff (%s); still held", self.agent.name,
                         tid[:8], e.status)
                changed = False
            self._backoff_checked[tid] = (deadline, mono, changed)
        if changed:
            log.info("%s: #%s new input during a failed-start backoff; backoff lifted", self.agent.name, tid[:8])
            self._task_backoff.pop(tid, None)
            self._backoff_inputs.pop(tid, None)
            self._backoff_checked.pop(tid, None)
        return changed

    def _same_pool(self, other: AgentConfig) -> bool:
        """Another agent that can take this agent's work: the same ``pool`` name when either sets
        one; otherwise the same set of repositories under the same lead (same product, same team)."""
        mine = self.agent
        if mine.pool or other.pool:
            return bool(mine.pool) and mine.pool.casefold() == other.pool.casefold()
        repos = {r.name for r in mine.repos}
        return (bool(repos) and repos == {r.name for r in other.repos}
                and mine.lead.casefold() == other.lead.casefold())

    def _unavailable(self, name: str) -> str:
        """Why the named agent cannot take or answer anything now ('' = it can): it is not run by
        this runner, or it is paused (config ``paused`` or the pause file)."""
        ours = next((ag for ag in self.cfg.agents if ag.name.casefold() == name.casefold()), None)
        if ours is None:
            return "it is not run by this runner"
        if ours.paused or (self.cfg.state_dir / "pause" / ours.name).exists():
            return "it is paused"
        return ""

    def _free_peer(self, task: dict) -> tuple[AgentConfig, dict] | None:
        """A peer of this agent's pool that can start this task now: enabled, not paused, resolvable
        in the workspace, serving the task's project, with a free slot, not capped on this very task
        and not itself waiting for input on it. A task a cap already moved here today is not moved
        on again, or it circles."""
        today = now().date().isoformat()
        moved = self.store.mark(self.agent.name, task["id"], CAP_REASSIGNED)
        if moved and moved[0] == today:
            return None
        cap = self.cfg.daily_start_cap
        plain = dict(task, start_after=None, checked_out_by=None, checkout_expires=None, checkout_expires_at=None)
        for ag in self.cfg.agents:
            if ag.name == self.agent.name or not self._same_pool(ag) or self._unavailable(ag.name):
                continue
            if static_skip_reason(plain, ag.policy, now()):
                continue
            if cap and self.store.starts_today(ag.name, task["id"]) >= cap:
                continue
            if self._cap_wait_of(ag.name, task["id"]):
                continue
            try:
                found = self.effects._resolve_agent(ag.name, self.me)
            except MeshError:
                return None
            if found and found.get("id") != self.agent_id and self._lead_has_room(found, ag.name):
                return ag, found
        return None

    def _cap_wait_of(self, agent: str, task_id: str) -> bool:
        mark = self.store.mark(agent, task_id, CAP_WAIT)
        return bool(mark and mark[0])

    def _requeue_on_event(self, task: dict, kind: str, banner: str, why: str, a: Attempt | None = None,
                          fp: dict | None = None) -> bool:
        """A cap put the task back in the queue (``requeue_on_cap``). Same assignee, status todo,
        no ``start_after``, no backoff: it starts again when its inputs change (see inputs.py), or
        at once on a free peer of its pool for a fresh start. False when it could not be done safely
        (the inputs are unreadable, or the move to todo / clearing start_after failed): nothing is
        marked or said, and the caller keeps the older behaviour."""
        tid = task["id"]
        try:
            fp = fp if fp is not None else self._input_fingerprint(task)
        except MeshError as e:
            log.warning("%s: #%s %s: inputs unreadable (%s); using the hand-up path", self.agent.name, tid[:8],
                        kind, e.status)
            return False
        # Status and start_after first, while the task is still ours: a failure here changes nothing
        # that matters and the caller takes the hand-up path. Nothing retries a start_after left set
        # (it would hold the task before the cap-wait logic ever runs), so success is required.
        if not self._back_to_todo(task):
            log.warning("%s: #%s %s: could not move it to todo / clear start_after; using the hand-up path",
                        self.agent.name, tid[:8], kind)
            return False
        peer = self._free_peer(task)
        moved_to = ""
        if peer:
            try:
                self.mesh.assign(tid, peer[1]["id"])
            except MeshError as e:                  # a pinned task refuses: it stays and waits for input
                log.info("%s: #%s %s: reassignment refused (%s)", self.agent.name, tid[:8], kind, e.status)
            else:
                moved_to = peer[0].name
                self.store.mark(peer[0].name, tid, CAP_REASSIGNED, now().date().isoformat())
        self._task_backoff.pop(tid, None)
        self._budget_backoff.pop(tid, None)
        self.store.mark(self.agent.name, tid, CAP_WAIT,
                        "" if moved_to else json.dumps({"kind": kind, "fp": fp, "at": iso()}))
        self._input_checked.pop(tid, None)
        note = f"cap: requeued ({kind})" + (f"; reassigned to {moved_to}" if moved_to else "")
        if a is not None:
            self._finish(a, "completed", note)
        else:                                       # no attempt: a row of its own for the audit
            try:
                row = self.store.create(self.agent.name, tid, self.agent.harness, task.get("title", ""))
            except sqlite3.IntegrityError:
                row = None
            if row is not None:
                self.store.update(row, state="completed", note=note)
                self.emit("completed", row, note=note)
        self.emit("cap", a, cap=kind, requeued=True, reassigned_to=moved_to, **({} if a else {"task": tid}))
        if moved_to:
            text = (f"{banner}: {why}. Back in the queue and reassigned to {moved_to} for a fresh start; "
                    f"{self.agent.name} no longer holds it.")
        else:
            text = (f"{banner}: {why}. Back in the queue (todo), still assigned to {self.agent.name}. It starts "
                    "again when something new reaches it: a comment from someone else, a change to a linked "
                    "merge request or its pipeline, a closed dependency or a new assignee. Nothing restarts it "
                    "on a timer.")
        self._comment(tid, text)
        log.info("%s: #%s %s: requeued%s", self.agent.name, tid[:8], kind,
                 f", reassigned to {moved_to}" if moved_to else ", waits for new input")
        return True

    def _back_to_todo(self, task: dict) -> bool:
        """Status todo and no start_after. True only when both are in place."""
        tid = task["id"]
        try:
            if self.cats.category(task) != "todo":
                todo = next((s["id"] for s in self.mesh.statuses(task["project_id"])
                             if s.get("category") == "todo"), None)
                if not todo:
                    return False
                self.mesh.move(tid, todo)
            if task.get("start_after"):
                self.mesh.patch_task(tid, {"start_after": None})
        except MeshError as e:
            log.warning("%s: requeue #%s: %s", self.agent.name, tid[:8], e)
            return False
        return True

    def _keep_until_tomorrow(self, task_id: str, text: str, kind: str = "start cap") -> None:
        """No one to hand it to: no start before the next UTC day, said once a day per
        kind of hand-off (a turn cap after a start cap still gets its own banner)."""
        midnight = (now() + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        self._task_backoff[task_id] = time.monotonic() + (midnight - now()).total_seconds()
        if kind == "turn budget":
            self._budget_backoff[task_id] = self._task_backoff[task_id]
        else:
            self._budget_backoff.pop(task_id, None)
        key = "start_cap" if kind == "start cap" else "kept:" + kind.replace(" ", "_")
        told = self.store.mark(self.agent.name, task_id, key)
        today = now().date().isoformat()
        if not (told and told[0] == today):
            self.store.mark(self.agent.name, task_id, key, today)
            self._comment(task_id, text + (" Not started again before tomorrow (UTC) unless someone writes here or "
                                           "takes it over." if kind == "turn budget" else
                                           " Not started again before tomorrow (UTC) unless someone takes it over."))

    def _budget_spent(self, a: Attempt) -> bool:
        used = self.store.outcomes_today(self.agent.name, a.task_id)
        idle = self.store.idle_waits_today(self.agent.name, a.task_id)
        return (used.get("next", 0) > self.cfg.daily_next_budget
                or idle > self.cfg.daily_wait_budget
                or used.get("step", 0) > self.cfg.daily_step_budget)

    def _over_budget(self, a: Attempt, task: dict, idle_exempt: bool = False) -> bool:
        """Per task and UTC day: N NEXT, S steps, M idle waits since verified CI progress.
        CI progress does not forgive NEXT/steps, and ordinary met waits do not reset idle waits.
        Past a cap the task goes to the lead (or is released with a note).
        ``idle_exempt``: the idle-wait budget does not stop this declaration (an accepted
        ``merge`` wait); NEXT and step budgets still do."""
        used = self.store.outcomes_today(self.agent.name, a.task_id)
        idle = self.store.idle_waits_today(self.agent.name, a.task_id)
        if (used.get("next", 0) <= self.cfg.daily_next_budget
                and (idle_exempt or idle <= self.cfg.daily_wait_budget)
                and used.get("step", 0) <= self.cfg.daily_step_budget):
            return False
        why = (f"{used.get('next', 0)} NEXT, {used.get('wait', 0)} waits and {used.get('step', 0)} "
               f"steps today on this task; {idle} idle waits since the last verified CI step")
        self._hand_up(a, task, "🔁 TURN CAP", why, "turn budget")
        return True

    def _over_compaction_cap(self, a: Attempt, task: dict | None, new: int) -> bool:
        """Count the attempt's context compactions; past the cap the task goes up: a
        session compacted that often has lost its own thread of work."""
        got = self.store.mark(self.agent.name, a.task_id, "compactions")
        prev = int(got[0].split(":")[1]) if got and got[0].startswith(a.id + ":") else 0
        total = prev + new
        self.store.mark(self.agent.name, a.task_id, "compactions", f"{a.id}:{total}")
        if not self.cfg.max_compactions or total < self.cfg.max_compactions:
            return False
        self._hand_up(a, task, "🗜 COMPACTION CAP", f"{total} context compactions in one session on this task",
                      "compaction cap")
        return True

    def _start_cap_stale(self, task: dict) -> bool:
        """The tick's snapshot of the task may be minutes old: a first hand-off has already moved
        it to the lead, who holds it now. Acting on the snapshot would hand it up a second time
        (a second banner, a second reassign). Re-read it; a task that is no longer this agent's,
        or is checked out by another writer, is left alone. Only a fresh read that positively shows
        this agent as the assignee lets the hand-off proceed."""
        try:
            fresh = self.mesh.task(task["id"])
        except MeshError as e:
            # cannot tell: acting on the stale snapshot is the double hand-off this guards
            # against; the caller does not start the task either, so the next tick retries
            log.warning("%s: #%s start cap deferred: task not readable (%s)",
                        self.agent.name, task["id"][:8], e)
            return True
        mine = fresh.get("assignee_id") == self.agent_id     # a missing assignee is not ours
        other = fresh.get("checked_out_by") not in (None, "", self.agent_id)
        if mine and not other:
            return False
        log.info("%s: #%s start cap skipped: task already moved on (assignee %s, checked out by %s)",
                 self.agent.name, task["id"][:8], (fresh.get("assignee_id") or "-")[:8],
                 (fresh.get("checked_out_by") or "-")[:8])
        return True

    def _unseen_input(self, task: dict) -> bool:
        """A comment from someone else newer than this agent's last working session on the task.
        Such a comment reached nobody: a START CAP taken now would store a fingerprint that already
        contains it, and the task would wait for input that has arrived. One more start, then the
        cap applies again (the session that reads it ends later than the comment). A read that
        fails is not news."""
        tid = task["id"]
        ended = self.store.last_work_end(self.agent.name, tid)
        if ended is None:
            return False
        try:
            comments = self.mesh.comments(tid)
        except MeshError:
            return False
        return any(is_foreign(c, self.agent_id) and (parse_time(c.get("created_at")) or ended) > ended
                   for c in comments)

    def _fingerprint_or_none(self, task: dict) -> dict | None:
        try:
            return self._input_fingerprint(task)
        except MeshError:
            return None

    def _start_cap(self, task: dict, cap: int, fp: dict | None = None) -> None:
        """This agent was given this task ``cap`` times today: the task goes to the lead
        instead of another start (a hand-off loop between agents, or a task the agent
        cannot finish). Without a lead it waits for tomorrow."""
        if self._start_cap_stale(task):
            return
        why = f"{self.agent.name} started this task {cap} times today"
        if self.cfg.requeue_on_cap and self._requeue_on_event(task, "start cap", "🔁 START CAP", why, fp=fp):
            return
        lead, why_not = self._pick_lead(task)
        tid = task["id"]
        if why_not:
            why += f"; {why_not}"
        if lead:
            try:
                self.mesh.assign(tid, lead["id"])
            except MeshError as e:
                log.warning("%s: start cap #%s: hand-off refused: %s", self.agent.name, tid[:8], e)
                why += f"; hand-off to {lead.get('name')} refused ({e.status})"
            else:
                try:
                    todo = next((s["id"] for s in self.mesh.statuses(task["project_id"])
                                 if s.get("category") == "todo"), None)
                    if todo and self.cats.category(task) != "todo":
                        self.mesh.move(tid, todo)
                except MeshError as e:
                    log.warning("%s: start cap #%s: move: %s", self.agent.name, tid[:8], e)
                self._handed(tid)
                self._comment(tid, f"🔁 START CAP: {why}. Handed to {lead.get('name')} to decide how to "
                                   "unblock it.")
                log.info("%s: #%s start cap: routed to %s", self.agent.name, tid[:8], lead.get("name"))
                return
        log.info("%s: #%s start cap: kept until tomorrow: %s", self.agent.name, tid[:8], why)
        self._keep_until_tomorrow(tid, f"🔁 START CAP: {why}.")

    def _cap_at_resume(self, a: Attempt) -> bool:
        """With ``turn_cap_continue_on_progress`` the cap of a work attempt is applied when it is
        about to resume (``_turn_cap``), not when the capped turn settles: the turn's own outcome is
        then read and recorded first (a WAIT waits, a NEXT counts as progress) instead of being lost
        to the cap. Off: the cap is applied at settle, before the outcome is read."""
        return self.cfg.turn_cap_continue_on_progress and a.mode == "work"

    def _turn_cap(self, a: Attempt, task: dict | None) -> None:
        """The attempt used ``max_turns`` turns (``turn_cap_continue_on_progress`` is on; called from
        ``_resume_one``, after the capped turn's outcome was recorded). An attempt
        that still made progress (>= ``turn_cap_progress_min`` step/next among its last
        ``TURN_CAP_WINDOW`` declared outcomes) ends as completed and stays with the same agent, whose
        next start is a fresh session; the restart is a start, so ``daily_start_cap`` bounds the loop
        (and is required for it). No recent progress, no start cap or a spent one: the task goes up as
        before (RUNNER GAVE UP)."""
        why = f"stopped after {a.turns} turns on this task"
        verdict = ""
        if task:
            recent = self.store.recent_outcomes(a.id, TURN_CAP_WINDOW)
            progress = sum(1 for k in recent if k in ("step", "next"))
            cap = self.cfg.daily_start_cap
            starts = self.store.starts_today(a.agent, a.task_id)
            if progress < self.cfg.turn_cap_progress_min:
                verdict = f"no recent progress ({progress} step/next of the last {len(recent)} outcomes)"
            elif not cap:
                verdict = "daily_start_cap is off, so a restart would not be bounded"
            elif starts >= cap:
                verdict = f"start cap reached ({starts} starts today)"
            else:
                note = (f"turn cap: continued in a fresh session ({progress} step/next of the last "
                        f"{len(recent)} outcomes after {a.turns} turns)")
                log.info("%s: #%s %s", self.agent.name, a.task8, note)
                self.store.event(a.id, "turn_cap", "continued: " + note)
                self.emit("cap", a, cap="turn cap", continued=True, progress=progress, window=len(recent))
                self._finish(a, "completed", note)
                return
            why += f"; not continued: {verdict}"
            log.info("%s: #%s turn cap: handing up: %s", self.agent.name, a.task8, verdict)
        self.store.event(a.id, "turn_cap", "handed up" + (f": {verdict}" if verdict else ""))
        self._give_up(a, task, why)

    def _give_up(self, a: Attempt, task: dict | None, why: str) -> None:
        """The runner cannot carry this task further. It must not stay in the
        agent's working status looking taken: it goes to the lead, or back to todo."""
        self._hand_up(a, task, "⚠️ RUNNER GAVE UP", why, "gave up")

    def _hand_up(self, a: Attempt, task: dict | None, banner: str, why: str, short: str) -> None:
        if (self.cfg.requeue_on_cap and task and short != "gave up" and a.mode == "work"
                and self._requeue_on_event(task, short, banner, why, a)):
            return                          # a cap is work stuck, not mechanics broken: no hand-up
        lead, why_not = self._pick_lead(task) if task else (None, "")
        if why_not:
            why += f"; {why_not}"
        if why_not and not lead:
            # a lead exists but cannot take it now (it came back, the leads are capped, the
            # lead is in another workspace): the agent keeps it, in todo, until tomorrow
            self._to_todo(a, task)
            self._finish(a, "failed", f"{short}: {why}")
            log.info("%s: #%s %s: kept until tomorrow: %s", self.agent.name, a.task8, short, why)
            self._keep_until_tomorrow(a.task_id, f"{banner}: {why}.", short)
            return
        if lead:
            try:
                self.mesh.assign(a.task_id, lead["id"])          # first: a pinned task refuses here
            except MeshError as e:
                self._finish(a, "failed", f"{short}: {why}; hand-off refused: {e}")
                self._comment(a.task_id, f"{banner}: {why}. Could not hand it to {lead.get('name')} "
                                         f"({e.status}); released until someone writes here.")
                return
            self._handed(a.task_id)
            self._to_todo(a, task, always=True)
            self._comment(a.task_id, f"{banner}: {why}. Handed to {lead.get('name')} to decide how to unblock it.")
            self._finish(a, "completed", f"routed to lead {lead.get('name')}: {why}")
            return
        # no one above: the agent keeps it, but in todo, so the board does not show work
        # nobody does; it starts again when someone writes on the task
        self._to_todo(a, task)
        self._finish(a, "failed", f"{short}: {why}")
        self._comment(a.task_id, f"{banner}: {why}; no lead to hand it to. Back in todo"
                                 + (" until tomorrow (UTC) or until someone writes here." if short == "turn budget"
                                    else " until someone writes here."))

    def _to_todo(self, a: Attempt, task: dict | None, always: bool = False) -> None:
        if not task or (not always and self.cats.category(task) != "in_progress"):
            return
        try:
            todo = next((s["id"] for s in self.mesh.statuses(task["project_id"])
                         if s.get("category") == "todo"), None)
            if todo:
                self.mesh.move(a.task_id, todo)
        except MeshError as e:
            log.warning("%s: route #%s: move: %s", self.agent.name, a.task8, e)

    # ------------------------------------------------------------------ phases
    def heartbeat(self) -> None:
        mono = time.monotonic()
        for a in self.store.by_state(self.agent.name, ("running",)):
            if not a.checkout.get("token") or mono - self._last_beat.get(a.id, 0) < self.cfg.heartbeat_seconds:
                continue
            self._last_beat[a.id] = mono
            self._guard(a, "heartbeat", self._beat)

    def _beat(self, a: Attempt) -> None:
        try:
            self._renew_gate(a)
        except Exception as e:                     # never cost the checkout heartbeat
            log.warning("%s: renew gate #%s: %s", self.agent.name, a.task8, e)
        co = Checkout(a.task_id, a.checkout["token"], a.checkout.get("generation"), "",
                      a.checkout.get("session_id", a.id), a.checkout.get("request_id", ""))
        try:
            self.mesh.heartbeat(co, self.cfg.checkout_ttl_minutes)
        except MeshError as e:
            if e.status in (403, 404, 409):
                # The agent checked the task out itself (same identity, new token) or
                # someone took it over. Either way our token is dead: stop renewing it.
                closed = self._card_closed(a)
                if closed is None:
                    return                         # unknown: keep the token, ask again next beat
                if closed and a.id not in self._closed_seen:
                    # closed once: an agent that closed its own card may still be writing;
                    # keep the token so the next beat confirms before stopping it
                    self._closed_seen.add(a.id)
                    return
                self.store.event(a.id, "checkout_lost", str(e))
                self.store.update(a, checkout={})
                if closed:
                    self.store.event(a.id, "kill", "task closed while the turn ran")
                    self._kill(a)
            else:
                raise
        else:
            # Mesh can accept the checkout heartbeat on a card it already closed while the
            # server run lease is gone (renew: writer_lease_mismatch); read the status too,
            # and stop on the second consecutive closed observation as above.
            if self._card_closed(a):
                if a.id not in self._closed_seen:
                    self._closed_seen.add(a.id)
                    return
                self.store.event(a.id, "checkout_lost", "heartbeat accepted on a closed card")
                self.store.update(a, checkout={})
                self.store.event(a.id, "kill", "task closed while the turn ran")
                self._kill(a)
            else:
                self._closed_seen.discard(a.id)

    def _card_closed(self, a: Attempt) -> bool | None:
        """True/False once Mesh answered whether the card is done/cancelled; None when it
        could not be read (unknown is not closed)."""
        try:
            task = self._task(a.task_id)
            return bool(task) and self.cats.category(task) in ("done", "cancelled")
        except MeshError:
            return None

    def settle_finished(self) -> None:
        for a in self.store.by_state(self.agent.name, ACTIVE):
            if not a.pid:
                created = parse_time(a.created_at)
                if created and now() - created > dt.timedelta(minutes=5):
                    # crashed between reservation and spawn in an earlier run: nothing ran
                    self._guard(a, "abort", lambda x: self._finish(x, "aborted", "no process was started"))
                continue
            self._guard(a, "settle", lambda x: self._turn_over(x) and self.settle(x))

    def deliver_mentions(self) -> None:
        """An @-address to this agent on a task it does not own becomes a request card
        assigned to it: a comment there wakes only the task's own assignee, so the ask
        reached nobody. A mention on its own task needs nothing (its waits wake on any
        comment); a mention inside a sentence is a reference, not an ask.

        At-least-once from the server's unseen flag; at-most-once card per comment from
        a local mark written before the row is marked seen. Unseen rows older than the
        moment this was switched on are history and only marked seen."""
        if not self.cfg.mention_requests or not self.agent_id:
            return
        mono = time.monotonic()
        if mono - self._mentions_at < MENTION_POLL_SECONDS:
            return
        self._mentions_at = mono
        ws = (self.me or {}).get("workspace_id") or ""
        if not ws:
            return
        floor = self.store.mark(self.agent.name, "_mentions", "floor")
        if not floor or not floor[0]:
            self.store.mark(self.agent.name, "_mentions", "floor", iso(now() - MENTION_LOOKBACK))
            floor = self.store.mark(self.agent.name, "_mentions", "floor")
        try:
            rows = self.mesh.mentions(ws)
        except MeshError as e:
            log.warning("%s: mentions: %s", self.agent.name, e)
            return
        made = 0
        for m in sorted(rows, key=lambda r: str(r.get("extracted_at") or "")):
            cid = str(m.get("comment_id") or "")
            if not cid:
                continue
            key = f"_mention:{cid}"
            try:
                done = self.store.mark(self.agent.name, key, "state")
                if not (done and done[0]) and str(m.get("extracted_at") or "") >= floor[0]:
                    if made >= MENTION_CARDS_PER_POLL:
                        break                  # the rest next poll; the server keeps them unseen
                    if self._mention(m):
                        made += 1
                    self.store.mark(self.agent.name, key, "state", "handled")
                self.mesh.mention_seen(cid)
            except Exception as e:             # one bad row never blocks the others
                fails = self._mention_fails.get(cid, 0) + 1
                self._mention_fails[cid] = fails
                log.warning("%s: mention %s on #%s (%s/%s): %s", self.agent.name, cid[:8],
                            str(m.get("task_id"))[:8], fails, MENTION_MAX_FAILS, e)
                if fails >= MENTION_MAX_FAILS:
                    self.store.mark(self.agent.name, key, "state", "gave-up")

    def _mention(self, m: dict) -> bool:
        """File a request card for an address on someone else's task. True if filed."""
        body = str(m.get("comment_body") or "")
        slug = str(m.get("mentioned_slug") or self.agent.name).lstrip("@")
        author = str(m.get("author_id") or "")
        if author == self.agent_id or body.startswith("[runner") or not addressed(body, slug):
            return False
        tid = str(m.get("task_id") or "")
        task = self._task(tid) if tid else None
        if not task or task.get("assignee_id") == self.agent_id or not task.get("project_id"):
            return False
        # Already part of the conversation on that task (this agent commented there in the
        # last day): the reply reaches it as an answer to its own ask, not as a new ask.
        # Without this, "@A done" on a third party's task would bounce cards between agents.
        recent = now() - dt.timedelta(hours=24)
        for c in self._comments(tid):
            at = parse_time(c.get("created_at"))
            cauthor = c.get("author_id") or (c.get("author") or {}).get("id")
            if cauthor == self.agent_id and at and at > recent:
                return False
        todo = next((s["id"] for s in self.mesh.statuses(task["project_id"]) if s.get("category") == "todo"), None)
        excerpt = body.strip()[:1500]
        who = m.get("author_name") or "someone"
        title_line = next((l for l in body.splitlines() if addressed(l, slug)), body)[:80]
        fields = {"title": f"[mention] {who}: {title_line.strip()} (#{tid[:8]})",
                  "description": (f"[runner mention-request:{m.get('comment_id')}]\n"
                                  f"{who} addressed you on task #{tid[:8]} «{task.get('title', '')}» ({tid}), "
                                  f"which is not yours.\n\nComment (verbatim):\n> "
                                  + excerpt.replace("\n", "\n> ") +
                                  f"\n\nDo what was asked and answer with a comment ON TASK #{tid[:8]} "
                                  f"(that comment reaches its owner). Do not take #{tid[:8]} unless the comment "
                                  "hands it to you. Then move this card to done."),
                  "assignee_id": self.agent_id, "assignee_type": "agent", "priority": "high",
                  "labels": list(MENTION_LABELS)}
        if todo:
            fields["status_id"] = todo
        created = self.mesh.create_task(task["project_id"], fields)
        log.info("%s: mention by %s on #%s → request #%s", self.agent.name, who, tid[:8],
                 str(created.get("id", ""))[:8])
        return True

    def _cap_observation(self, task_id: str) -> Attempt | None:
        mark = self.store.mark(self.agent.name, task_id, "cap_observation")
        a = self.store.get(mark[0]) if mark and mark[0] else None
        return a if a and a.state == "failed" and a.note.startswith(DAILY_CAP_NOTES) else None

    def _check_cap_event(self, a: Attempt) -> None:
        observed = self._cap_observation(a.task_id)
        if not observed or observed.id != a.id or a.wait.get("cap_wake"):
            return
        task = self._task(a.task_id)
        if not self._still_ours(task, a.mode) or self.store.open_for_task(self.agent.name, a.task_id):
            return
        wait = dict(a.wait)
        if wait.get("kind") not in ("pipeline", "mr", "card"):
            return
        override = self._cap_instruction_override(task, a)
        if override:
            comment_id, author_id, body = override
            wait.pop("cap_override_consumed", None)
            wait.update(cap_wake="project lead explicitly changed instructions",
                        cap_override={"attempt_id": a.id, "comment_id": comment_id, "author_id": author_id,
                                      "body": body})
            self.store.update(a, wait=wait)
            self.store.event(a.id, "cap_override", comment_id)
            return
        # Only the declared event can release the cap: no age re-check, comments,
        # cycles or observation errors. Ordinary static and admission gates still apply.
        check = copy.deepcopy(wait)
        check.update(started=iso(), rechecks=0)
        check.pop("ci_observation", None)
        event = wake_check(self.mesh, self.cats, self.gitlab, task, check, self.agent_id, [], now(),
                           max_wait=dt.timedelta(days=36500), first_recheck=dt.timedelta(days=36500),
                           delivery_reader=lambda key: self._ci_delivered(a.task_id, key))
        met = check.pop("met", False)
        check.pop("settled", None)
        check.pop("recheck_due", None)
        signature = json.dumps([check.get("kind"), check.get("target"), check.get("seen"), event],
                               sort_keys=True)
        if not met and check.get("seen") != wait.get("seen"):
            check.pop("cap_event_signature", None)   # a retry ran again before settling
        if met and event and signature != wait.get("cap_event_signature"):
            check.update(cap_wake=event, cap_event_signature=signature)
            if check.get("ci_observation"):
                check["ci_delivery"] = check.pop("ci_observation")
            self.store.event(a.id, "cap_event", event)
        # Do not move updated_at on every poll: it is also the daily-cap clock.
        check["started"] = wait.get("started", "")
        if check != wait:
            self.store.update(a, wait=check)

    def _cap_instruction_override(self, task: dict, a: Attempt) -> tuple[str, str, str] | None:
        """Only an addressed, fresh instruction from the configured project lead.

        Comment identity and authorship come from Mesh, never from its text or
        metadata. Persisting the receipt with this observation fences replay.
        """
        raw = self.cfg.project_leads.get(task.get("project_id", ""), "")
        if not raw:
            return None
        # The mark belongs to this exact observation generation and never moves
        # when a pipeline's seen state updates the attempt's updated_at.
        mark = self.store.mark(self.agent.name, a.task_id, "cap_observation")
        since = parse_time(mark[1]) if mark and mark[0] == a.id else None
        if since is None:
            return None
        candidates = []
        for c in reversed(self._comments(a.task_id)):
            if (a.wait.get("cap_override_consumed")
                    and c.get("id") == (a.wait.get("cap_override") or {}).get("comment_id")):
                continue
            metadata = c.get("metadata")
            value = metadata.get("runner_cap_override") if isinstance(metadata, dict) else None
            if not isinstance(value, dict) or value != {"attempt_id": a.id, "action": "instructions_changed"}:
                continue
            at = parse_time(c.get("created_at"))
            author = c.get("author_id") or (c.get("author") or {}).get("id")
            try:
                valid_id = str(uuid.UUID(c.get("id"))) == c.get("id")
            except (ValueError, TypeError, AttributeError):
                valid_id = False
            body = c.get("body")
            if (valid_id and author and author != self.agent_id and isinstance(body, str) and body.strip()
                    and len(body) <= 16384
                    and at is not None and since < at <= now()
                    and c.get("author_type") != "system" and not c.get("auto_summary")):
                candidates.append((c["id"], author, body))
        if not candidates:
            return None
        # Fresh directory authorization is needed only for an addressed candidate,
        # never for ordinary observation polling or unrelated comments.
        try:
            agents = self.mesh.workspace_agents(self.me.get("workspace_id", ""))
        except MeshError:
            return None             # no override; the original target can still complete
        matches = [x for x in agents if x.get("id") == raw]
        if not matches:
            matches = [x for x in agents if str(x.get("name") or "").casefold() == raw.casefold()]
        lead = matches[0] if len(matches) == 1 else None
        lead_id = (lead or {}).get("id")
        if not lead_id or lead_id == self.agent_id:
            return None
        return next((c for c in candidates if c[1] == lead_id), None)

    def check_wakes(self) -> None:
        mono = time.monotonic()
        for a in self.store.by_state(self.agent.name, ("failed",)):
            if not a.note.startswith(DAILY_CAP_NOTES):
                continue
            if mono - self._last_wake_check.get(a.id, 0) < self.cfg.wake_poll_seconds:
                continue
            self._last_wake_check[a.id] = mono
            self._guard(a, "cap-event", self._check_cap_event)
        waiting = self.store.by_state(self.agent.name, ("waiting",))
        for key in [k for k, j in self._merge_jobs.items()
                    if j["result"] is not None and k not in {w.id for w in waiting}]:
            del self._merge_jobs[key]              # a finished run of a wait that is gone
        for a in waiting:
            if mono - self._last_wake_check.get(a.id, 0) < self.cfg.wake_poll_seconds:
                continue
            self._last_wake_check[a.id] = mono
            self._guard(a, "wake", self._wake_one)
            current = self.store.get(a.id)
            if current and current.state == "waiting":
                self._guard(current, "workspace-suspend", self._suspend_waiting)
        # Storage maintenance must not depend on admission: pause, memory,
        # capacity, auth or checkout holds can bypass _resume_one entirely.
        # The existing helper reads ownership only after its opt-in hold timer.
        for a in self.store.by_state(self.agent.name, ("ready",)):
            if mono - self._last_wake_check.get(a.id, 0) < self.cfg.wake_poll_seconds:
                continue
            self._last_wake_check[a.id] = mono
            self._guard(a, "ready-workspace-suspend", self._suspend_held_ready)

    def _suspend_waiting(self, a: Attempt, *, requested: bool = False) -> None:
        own = self.agent.workspace_wait_release_minutes
        threshold = self.cfg.workspace_wait_release_minutes if own is None else own
        started = parse_time(a.wait.get("started")) or parse_time(a.updated_at)
        if not requested and (threshold <= 0 or not started or now() - started < dt.timedelta(minutes=threshold)):
            return
        if not requested and not self._wait_frees_workspace(a.wait, threshold):
            return
        # POSIX does not reuse a pid while a process group with that id exists. A live process
        # with this pid and a start time other than the stored stamp therefore means the
        # attempt's original group is gone. Without a stamp the answer stays conservative.
        proc = self.procs.get(a.id)
        if proc is not None:
            proc.poll()
        if group_alive(a.pid, a.stamp):
            raise WorkspaceError("checkpoint: writer group still alive; lease retained")
        rows = self.pool._checkpoint_rows(a.id)
        if rows and all(r["phase"] == "released" for r in rows) and not self.pool.leased_to(a.id):
            return
        self.pool.suspend(a.id, {"session_id": a.session_id, "harness": a.harness,
                               "cwd": str(self.agent.profile_dir), "workspaces": a.workspaces,
                               "wait": a.wait}, writer_stopped=True)
        if self.pool._checkpoint_rows(a.id):
            self.store.event(a.id, "workspace_suspended", "verified durable snapshot; leases returned")

    @staticmethod
    def _wait_frees_workspace(wait: dict, threshold_minutes: float) -> bool:
        """Only waits that do not need the working copy soon give the slot back.

        pipeline waits, and mr waits that watch CI (``ci_change``), resume right after CI and
        edit the same tree, so they keep it. A ``merge`` wait is driven by the runner's own gate
        (run from ``merge_command_cwd``, never in the task tree) and an ``mr`` wait on the
        ``merged`` contract only watches the MR state, so both give the slot back; an ``mr`` wait
        with no readable contract keeps it (uncertainty is never permission to release).
        A time wait that ends within the threshold would just re-lease at once;
        one with a missing or unreadable deadline keeps the slot too.
        """
        kind = wait.get("kind")
        if kind == "pipeline":
            return False
        if kind == "mr":
            contract = wait.get("mr_state")
            # the same contract typed_event accepts; anything else is uncertainty, kept
            if (wait.get("ci_change") or not isinstance(contract, dict)
                    or contract.get("predicate") != "merged" or contract.get("state") != "opened"):
                return False
        if wait.get("kind") == "time":
            until = parse_time(wait.get("until"))
            # An unknown remainder is uncertainty, never permission to release.
            if until is None or until - now() < dt.timedelta(minutes=threshold_minutes):
                return False
        return True

    def _suspend_held_ready(self, a: Attempt) -> None:
        """Apply the existing opt-in release policy to an admission-held ready attempt.

        Keep readiness and the original WAIT intact. The hold clock belongs to
        this exact attempt and survives restart; checkout bookkeeping must not
        restart it on every refused admission.
        """
        own = self.agent.workspace_wait_release_minutes
        threshold = self.cfg.workspace_wait_release_minutes if own is None else own
        if threshold <= 0 or a.state != "ready" or not self.pool.leased_to(a.id):
            return
        key = "workspace_ready_hold"
        saved = self.store.mark(a.agent, a.task_id, key)
        try:
            value = json.loads(saved[0]) if saved else {}
        except (ValueError, TypeError):
            value = {}
        started = parse_time(value.get("since")) if isinstance(value, dict) and value.get("attempt") == a.id else None
        if started is None:
            self.store.mark(a.agent, a.task_id, key, json.dumps({"attempt": a.id, "since": iso()}))
            return
        if now() - started < dt.timedelta(minutes=threshold):
            return
        # A hold never grants ownership. Re-read immediately before capture;
        # unknown ownership retains the source rather than releasing it.
        if not self._still_ours(self._task(a.task_id), a.mode):
            return
        self._suspend_waiting(a, requested=True)

    def _workspace_request(self, request: dict) -> str:
        a = self.store.get(request['attempt'])
        if (a is None or a.agent != self.agent.name or a.task_id != request['task']
                or a.state != 'waiting'):
            return 'rejected_identity_or_state'
        if workspace_requests.generation(a) != request['generation']:
            return 'rejected_wait_generation'
        try:
            rows = self.pool._checkpoint_rows(a.id)
            leases = self.pool.leased_to(a.id)
            if rows and all(row['phase'] == 'released' for row in rows) and not leases:
                return 'suspended'  # crash after release, before receipt (even past TTL)
            if request['expires'] <= int(time.time()):
                return 'expired'
            if not leases:
                return 'rejected_no_lease'
            # A request releases storage only, never the task's hold or checkout.
            task = self._task(a.task_id)
            if not self._still_ours(task, a.mode):
                return 'rejected_ownership'
            if request['expires'] <= int(time.time()):
                return 'expired'
            self._suspend_waiting(a, requested=True)
            return 'suspended' if not self.pool.leased_to(a.id) else 'rejected_no_release'
        except Exception:
            # Keep receipts allowlisted: exception strings may contain paths or
            # external response bodies. Existing checkpoint journal is the proof.
            log.warning('workspace request failed for attempt %s; inspect checkpoint journal', a.id)
            return 'failed_preserved'

    def _wake_queue(self, a: Attempt, task: dict) -> None:
        # Queue demand never falls through to age/recheck/error-budget wakes.
        if not self.cfg.queue_wait_freshness_v1 or a.mode != "work":
            return
        current_time = time.time()
        try:
            req = queue_wait.request(a.wait.get("queue_request"))
        except (ValueError, TypeError, KeyError):
            return
        if req["task_id"] != a.task_id:
            return
        pending = queue_wait.pending(self.store, a, req, self.agent_id)
        if pending is None or not queue_wait.stamp(current_time):
            return
        if current_time < pending["clock_watermark"]:
            return
        labels = task.get("labels")
        if (task.get("id") == a.task_id and self._still_ours(task, a.mode)
                and task.get("assignee_id") == self.agent_id
                and isinstance(labels, list) and all(isinstance(label, str) for label in labels)
                and "fleet-starvation-wake" not in labels):
            # Explicit public withdrawal, not evidence of queue recovery. Unknown
            # task/label shapes and a human/not-before hold cannot grant continuation.
            if (task.get("human_gate") is not False or task.get("completion_signal")
                    or task.get("checked_out_by") not in (None, "", self.agent_id)
                    or task.get("delegation_level") == "supervised"
                    or any(label.startswith("park:") or label in
                           ("no-runner", "awaiting-pavel", "rescue-exhausted") for label in labels)):
                return
            # Our event horizon is cleanup metadata, not an external start gate.
            # _resume_one removes it through the existing guarded on_end path.
            if task.get("start_after") and not own_schedule(task["start_after"], a.wait):
                not_before = parse_time(task["start_after"])
                if not_before is None or not_before.timestamp() > current_time:
                    return
            pending["state"], pending["clock_watermark"] = "cancelled", current_time
            event = "The owner withdrew this queue request by removing fleet-starvation-wake; continue the task under its current instructions. Queue recovery was not asserted."
            if self.store.update_with_marks(a, {
                    queue_wait.key(req): json.dumps(pending),
                    queue_wait.consumed_key(self.agent_id, req["contract"]): "withdrawn"},
                    expect="waiting", expect_wait=a.wait,
                    state="ready", wake_text=prompts.wake_prompt(task, event)):
                self.emit("wake", a, event="queue request explicitly withdrawn")
            return
        # Receipt reads are local and cheap. Source polling also detects new
        # demand without a receipt, but never retries Mesh each Runner tick.
        try:
            receipt = queue_wait.read_json(self.cfg.state_dir / "queue-wait-receipts.json")
        except (OSError, ValueError, TypeError):
            receipt = None
        checked = pending["source_checked_at"]
        if checked and current_time < checked + queue_wait.SOURCE_INTERVAL:
            return
        pending["clock_watermark"] = pending["source_checked_at"] = current_time
        # Write the attempt before I/O, including errors/429, so restart cannot
        # reset the bounded retry cadence. No mark writes on intervening ticks.
        self.store.mark(a.agent, a.task_id, queue_wait.key(req), json.dumps(pending))
        try:
            source = self._queue_source(task, current_time)
        except MeshError:
            return
        if source != req["contract"]:
            # A new machine demand supersedes this identity without waking an LLM.
            if source and source["observed_at"] > req["contract"]["observed_at"]:
                pending["state"] = "cancelled"
                replacement = queue_wait.new_request(a.task_id, source)
                wait = {k: v for k, v in a.wait.items() if k != "queue_delivery"}
                wait["queue_request"] = replacement
                mark = queue_wait.intent(a, replacement, self.agent_id, current_time)
                mark["source_checked_at"] = current_time
                self.store.update_with_marks(a, {queue_wait.key(req): json.dumps(pending),
                                               queue_wait.key(replacement): json.dumps(mark)},
                    expect="waiting", expect_wait=a.wait, wait=wait)
            return
        finished = time.time()
        if (not queue_wait.stamp(finished) or finished < current_time
                or not queue_wait.available(
                    receipt, req, finished, queue_wait.published_manifest(self.store))):
            self._first_move(a, task, req, pending, finished)
            return
        pending["state"], pending["clock_watermark"] = "complete", finished
        event = "fresh queue availability verified for the exact requested developers and projects"
        # Ready state IS the durable wake destination. Complete and effect share
        # a transaction, so restart cannot replay the effect-completion gap.
        def still_fresh():
            checked = time.time()
            return checked >= finished and queue_wait.available(
                receipt, req, checked, queue_wait.published_manifest(self.store))
        if self.store.update_with_marks(a, {queue_wait.key(req): json.dumps(pending), "met_wake": a.id,
                    queue_wait.consumed_key(self.agent_id, req["contract"]): "fulfilled",
                queue_wait.delivered_key(self.agent_id, req["contract"]): "fulfilled",
                queue_wait.FLOOR_KEY: queue_wait.raised_floor(
                    self.store, a.agent, a.task_id, req["contract"])},
                expect="waiting", expect_wait=a.wait,
                validate=still_fresh,
                state="ready", wake_text=prompts.wake_prompt(task, event)):
            self.emit("wake", a, event=event)

    def _first_move(self, a: Attempt, task: dict, req: dict, pending: dict, current_time: float) -> None:
        """A new refill request is addressed to the lead, who must create the work the queue
        waits for: availability can never arrive before the lead's first turn. Exactly one
        owner turn per source identity, committed with its delivery mark; this wake is not
        fulfilment, the request stays pending for the actual todo/start."""
        owner = self.agent_id
        if queue_wait.delivered(self.store, a, owner, req["contract"]):
            return
        start_after = task.get("start_after")
        if start_after and not own_schedule(start_after, a.wait):
            not_before = parse_time(start_after)
            if not_before is None or not_before.timestamp() > current_time:
                return
        event = ("a new queue refill request addressed to you has not been acted on yet: create or "
                 "assign the work for the requested developers and projects now, then wait for it. "
                 "This turn is not a fulfilment, the request stays open until that work is actually available.")
        # READY + wake text is the outbox; the delivered mark rides in the wait and is
        # committed by the spawn, so a refused gate/cap/crash never consumes the first move.
        wait = dict(a.wait, queue_delivery={"key": queue_wait.delivered_key(owner, req["contract"]),
                                            "value": "delivered",
                                            "floor": queue_wait.raised_floor(
                                                self.store, a.agent, a.task_id, req["contract"])})
        if self.store.update_with_marks(a, {queue_wait.key(req): json.dumps(pending)},
                expect="waiting", expect_wait=a.wait, wait=wait,
                state="ready", wake_text=prompts.wake_prompt(task, event)):
            self.emit("wake", a, event="queue refill first move")

    def _wake_one(self, a: Attempt) -> None:
        task = self._task(a.task_id)
        if not self._still_ours(task, a.mode):
            queue_wait.cancel(self.store, a)
            self._finish(a, "completed", "task left the agent while waiting")
            return
        if (self.cfg.queue_wait_freshness_v1 and a.mode == "work" and a.wait.get("kind") == "time"):
            try:
                source = self._queue_source(task, time.time())
            except MeshError:
                return
            if source and not queue_wait.consumed(self.store, a, self.agent_id, source):
                # The source changes the predicate, not the owner's existing
                # not-before time. Do not unschedule it via on_end or fall into
                # a legacy timer/comment wake while that gate is still future.
                checked = time.time()
                for value in (a.wait.get("until"), task.get("start_after")):
                    if value:
                        until = parse_time(value)
                        if until is None or until.timestamp() > checked:
                            return
                req = queue_wait.new_request(a.task_id, source)
                wait = dict(a.wait, kind="queue", until="", queue_request=req)
                mark = queue_wait.intent(a, req, self.agent_id, time.time())
                # Adopt an already parked typed refill without spending a turn.
                self.effects.on_end(a, wait, "queue WAIT adopted", close_request=False)
                self.store.update_with_marks(a, {queue_wait.key(req): json.dumps(mark)},
                    expect="waiting", expect_wait=a.wait, wait=wait)
                return
        if a.wait.get("kind") == "queue":
            self._wake_queue(a, task)
            return
        # Observers mutate nested seen state. Keep both the live attempt and
        # its CAS baseline isolated from the working observation/effects copy.
        expected_wait = copy.deepcopy(a.wait)
        wait = copy.deepcopy(expected_wait)
        wait.pop("ci_observation", None)
        wait.pop("ci_action_observation", None)
        self._classify_person_ask(wait)
        event = self._wait_cycle(a, wait) or (self._merge_event(a, wait) if wait.get("kind") == "merge" else wake_check(
            self.mesh, self.cats, self.gitlab, task, wait, self.agent_id,
            self.mesh.comments(a.task_id), now(),
            max_wait=dt.timedelta(hours=self.cfg.max_wait_hours),
            first_recheck=dt.timedelta(hours=self.cfg.wait_first_recheck_hours),
            delivery_reader=lambda key: self._ci_delivered(a.task_id, key),
            allow_action_required=True, comment_wake=self.cfg.comment_wake, agent_name=self.agent.name,
            comment_wake_always_from=self.cfg.comment_wake_always_from,
            decision_wake=self.agent.decision_wake or self.cfg.decision_wake, merge_gate=self.merge_enabled))
        negative = wait.pop("negative_observation", None)   # telemetry only, never persisted
        met = wait.pop("met", False)
        if met:
            # the awaited thing happened: whatever the agent declares next is the next step of
            # the work, not churn (counted by daily_step_budget, not daily_wait_budget)
            self.store.mark(self.agent.name, a.task_id, "met_wake", a.id)   # bound to this attempt
        if wait.pop("recheck_due", False):
            # the agent re-declares the same wait after a re-check: that is not churn, it does
            # not count against the daily wait budget, and later re-checks come less often
            self.store.mark(self.agent.name, a.task_id, "recheck_wake",
                            f"{a.id}:{int(wait.get('rechecks', 0) or 0) + 1}")   # bound to this attempt
        if event:
            self.effects.on_end(a, wait, f"woken: {event[:120]}", close_request=met)
            delivery = wait.pop("ci_observation", {})
            marks = {delivery["key"]: delivery["value"]} if met and delivery else {}
            action = wait.pop("ci_action_observation", {})
            if action:
                # READY + wake text is the pending outbox. A gate/cap refusal
                # must not consume it: acknowledge only with a real spawn.
                wait["ci_action_delivery"] = action
            if self.store.update_with_marks(a, marks, expect="waiting", expect_wait=expected_wait,
                                            state="ready", wait=wait,
                                            wake_text=prompts.wake_prompt(task, event)):
                self.emit("wake", a, event=event[:200])
                log.info("%s: #%s woke: %s", self.agent.name, a.task8, event[:120])
        else:
            if not self.effects.adopt(a, task, wait, self.me):
                self.effects.renew(a, wait)
            if wait != a.wait and not self.store.update_with_marks(
                    a, {}, expect="waiting", expect_wait=expected_wait, wait=wait):
                return                           # state or wait moved under us: the observation is stale
            if negative:
                self._record_suppressed_wake(a, negative)

    def _classify_person_ask(self, wait: dict) -> None:
        """Mark a decision wait whose handle is no workspace agent (a person). Waits parked before
        the marker existed carry none. A roster failure leaves the wait unmarked: unknown is
        never taken for a person."""
        if wait.get("kind") != "decision" or wait.get("recipient_id") or "person_ask" in wait:
            return
        try:
            if not self.effects._resolve_agent(str(wait.get("handle") or ""), self.me):
                wait["person_ask"] = True
        except MeshError:
            pass

    def _record_suppressed_wake(self, a: Attempt, negative: dict) -> None:
        """One ``wake_suppressed`` event per (attempt, wait generation, legacy re-check due
        moment): the age re-check was due, the observer read its target to the end and found
        nothing, so no turn was spent. Reuses the observation already made: no provider call.
        Ids, enums, hashes and times only. A failure here costs the measurement, never the wake."""
        try:
            # The observation was made on a copy: only the stored, still-waiting attempt of
            # the SAME wait generation may carry its receipt (a stale read must not).
            stored = self.store.get(a.id)
            if (not stored or stored.state != "waiting"
                    or wait_generation(stored.wait or {}) != negative["generation"]):
                return
            key = "wake_suppressed:" + hashlib.sha256(
                f"{a.id}:{negative['generation']}:{negative['legacy_due_at']}".encode()).hexdigest()[:32]
            if self.store.claim(self.agent.name, a.task_id, key, negative["legacy_due_at"]):
                self.emit("wake_suppressed", a, observer=negative["observer"],
                          wait_kind=negative["wait_kind"], generation=negative["generation"],
                          contract=negative["contract"], legacy_due_at=negative["legacy_due_at"],
                          interval_s=negative["interval_s"])
        except Exception as e:                   # noqa: BLE001 - telemetry must not block waking
            log.debug("wake_suppressed receipt: %s", e)

    # ------------------------------------------------------------ merge waits
    def _merge_event(self, a: Attempt, wait: dict) -> str:
        """Wake text for a ``merge`` wait, '' while it holds. Mutates ``wait["merge"]``.

        The runner runs the configured merge gate on the agent's behalf and judges the outcome by
        a fresh read of the merge request, never by the command's text: merged (wake with the
        merge commit), closed, a head that is not the reviewed one, a gate that refuses or fails
        (wake with its tail) wake the agent; a gate that says it is queued behind a pipeline
        (``⏳ WAIT pipeline:<project>#<id>`` as its last line) keeps the agent asleep until that
        pipeline or the merge request's head pipeline settles, or ``merge_rerun_minutes`` pass."""
        m = wait.setdefault("merge", {})
        target = str(wait.get("target") or "")
        since = parse_time(wait.get("since")) or now()
        c = foreign_comment(self.mesh.comments(a.task_id), self.agent_id, since)
        if c:
            who = c.get("author_name") or (c.get("author") or {}).get("name") or "someone"
            return f"{who} commented: {(c.get('body') or '')[:300]}"
        if self.gitlab is None or not m.get("sha") or "!" not in target:
            return ""                          # no observer here: the wait stands, nothing is guessed
        job = self._merge_jobs.get(a.id)
        if job and job["result"] is None:
            return ""                          # the gate is running
        try:
            mr = self.gitlab.get_mr(target)
            state = mr.get("state") if isinstance(mr, dict) else None
            if state not in ("opened", "locked", "merged", "closed"):
                raise ValueError("MR state unavailable")
        except Exception as e:                 # noqa: BLE001 - transient or misconfigured: count, then tell
            m["errors"] = int(m.get("errors", 0)) + 1
            if m["errors"] >= 5:
                return f"cannot read MR {target} ({type(e).__name__}); check the merge request yourself"
            return ""
        m["errors"] = 0
        project, iid = target.rsplit("!", 1)
        result = None
        if job:
            del self._merge_jobs[a.id]
            if job["target"] == target and job["sha"] == m["sha"]:
                result = job["result"]
        if state == "merged":
            wait["met"] = True
            at = mr.get("merge_commit_sha") or mr.get("squash_commit_sha") or mr.get("sha") or "unknown"
            return f"MR !{iid} ({project}) merged at {at}: deploy / verify as the task requires"
        if state == "closed":
            return f"MR {target} was closed without merge" + self._merge_tail(result)
        if state == "locked":
            return ""                          # GitLab is merging it right now
        head = str(mr.get("sha") or "")
        if head != m["sha"]:
            return (f"MR {target}: the head moved to {head[:12] or 'an unknown commit'} (you waited on "
                    f"{m['sha'][:12]}); the review covers the old head, a new review is needed")
        if result is not None:
            return self._merge_result(wait, m, mr, result)
        return self._merge_start(a, wait, m, mr)

    @staticmethod
    def _merge_tail(result: tuple[int, str] | None) -> str:
        return f"\nLast merge gate output (exit {result[0]}):\n{result[1]}" if result and result[1] else ""

    def _merge_obs(self, mr: dict, pipeline: str | None, prev: dict | None = None) -> dict:
        """What a re-run is measured against: the head pipeline of the MR and the pipeline the gate
        said it is queued behind, each reduced to ``active`` or its final status. A read that fails
        keeps the previous value: an unreadable pipeline is not a change."""
        head = mr.get("head_pipeline") or {}
        status = head.get("status")
        obs = {"mr": f"{head.get('id')}:{status if status in PIPELINE_DONE else 'active'}"}
        if pipeline:
            try:
                status = self.gitlab.get_pipeline(pipeline).get("status")
                obs["p"] = f"{pipeline}:{status if status in PIPELINE_DONE else 'active'}"
            except Exception:                  # noqa: BLE001
                obs["p"] = (prev or {}).get("p", f"{pipeline}:?")
        return obs

    def _merge_result(self, wait: dict, m: dict, mr: dict, result: tuple[int, str]) -> str:
        rc, tail = result
        lines = [ln for ln in tail.splitlines() if ln.strip()]
        last = lines[-1] if lines else ""
        m["history"] = (list(m.get("history") or []) + [f"{iso()} exit {rc}: {last.strip()[:160]}"])[-MERGE_HISTORY:]
        queued = MERGE_QUEUED.match(last)
        if queued:
            m["pipeline"] = queued["target"]
            m["obs"] = self._merge_obs(mr, m["pipeline"], m.get("obs"))
            return ""                          # queued behind other work: nothing for a model to do
        state = "is still open" if rc == 0 else f"failed (exit {rc})"
        return f"the merge gate for MR {wait['target']} {state} and did not merge it.{self._merge_tail(result)}"

    def _merge_start(self, a: Attempt, wait: dict, m: dict, mr: dict) -> str:
        """Start the next gate run when one is due ('' = not now, or a wake text)."""
        runs = int(m.get("runs", 0) or 0)
        last = parse_time(m.get("last_run"))
        if runs:
            obs = self._merge_obs(mr, m.get("pipeline"), m.get("obs"))
            if obs == m.get("obs") and last and now() - last < dt.timedelta(minutes=self.cfg.merge_rerun_minutes):
                return ""
            started = parse_time(m.get("first_run"))
            if started and now() - started >= dt.timedelta(hours=self.cfg.merge_queue_hours):
                history = "\n".join(m.get("history") or [])
                return (f"the merge gate ran {runs} times for MR {wait['target']} and it is still queued after "
                        f"{self.cfg.merge_queue_hours:g} h. Re-arm with ⏳ WAIT merge: (not mr:, which never runs "
                        f"the gate) once the cause is dealt with. Recent runs:\n{history}")
        try:
            env = self._fresh_env()
        except Exception as e:                 # noqa: BLE001 - the lane's identity could not be built
            m["env_errors"] = int(m.get("env_errors", 0)) + 1
            if m["env_errors"] >= MERGE_ENV_RETRIES:
                return f"cannot run the merge gate for MR {wait['target']}: {str(e)[:200]}"
            return ""
        m.pop("env_errors", None)
        m["runs"], m["last_run"] = runs + 1, iso()
        m.setdefault("first_run", m["last_run"])
        m["obs"] = self._merge_obs(mr, m.get("pipeline"), m.get("obs"))
        argv = self._merge_argv(wait["target"], m["sha"])
        job = {"result": None, "target": wait["target"], "sha": m["sha"]}

        def work() -> None:
            try:
                job["result"] = self._run_merge_command(argv, env)
            except Exception as e:             # noqa: BLE001 - a result is always recorded
                job["result"] = (-1, f"merge command crashed: {type(e).__name__}")

        job["thread"] = threading.Thread(target=work, name=f"merge-{a.task8}", daemon=True)
        self._merge_jobs[a.id] = job
        job["thread"].start()
        log.info("%s: #%s merge gate run %s for %s", self.agent.name, a.task8, m["runs"], wait["target"])
        return ""

    def _merge_argv(self, target: str, sha: str) -> list[str]:
        project, iid = target.rsplit("!", 1)
        values = {"{target}": target, "{project}": project, "{iid}": iid, "{sha}": sha}
        argv = []
        for part in self.cfg.merge_command:
            for key, value in values.items():
                part = part.replace(key, value)
            argv.append(part)
        return argv

    def _run_merge_command(self, argv: list[str], env: dict) -> tuple[int, str]:
        """(exit code, last lines of stdout+stderr in order); -1 = could not run or timed out.
        No shell, no stdin, own process group killed whole on timeout."""
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, bufsize=0, env=env,
                                    cwd=self.cfg.merge_command_cwd, start_new_session=True)
        except (OSError, ValueError) as e:
            return -1, f"could not run the merge command: {type(e).__name__}: {getattr(e, 'strerror', '') or e}"[:300]
        tail: collections.deque = collections.deque(maxlen=MERGE_TAIL_LINES)
        reader = threading.Thread(target=_drain_tail, args=(proc.stdout, tail), daemon=True,
                                  name="merge-output")
        reader.start()
        minutes = self.cfg.merge_timeout_minutes
        try:
            proc.wait(timeout=minutes * 60)
        except subprocess.TimeoutExpired:
            interrupt_group(proc.pid, signal.SIGKILL)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            reader.join(5)
            if not reader.is_alive():
                proc.stdout.close()
            return -1, _clip(tail) + f"\n[the merge command was killed after {minutes:g} min]"
        reader.join(5)             # a descendant that left the group may hold the pipe: take what we have
        if not reader.is_alive():
            proc.stdout.close()
        return proc.returncode, _clip(tail)

    def _wait_cycle(self, a: Attempt, wait: dict) -> str:
        """A ``card`` wait whose target card is itself waiting on this agent (a decision from
        it, or this card closing) never ends: the other side waits for a word this side can
        only give by working, and every card behind the pair stands with it. The card-waiter
        is woken once per cycle to do its part; the decision-waiter wakes on its comment."""
        if wait.get("kind") != "card" or not wait.get("target") or wait.get("cycle_told"):
            return ""
        me = {str(h).casefold() for h in (self.agent.name, (self.me or {}).get("name"), (self.me or {}).get("slug")) if h}
        for other in self.store.waiting_on_task(str(wait["target"])):
            w = other.wait or {}
            if other.agent == self.agent.name and other.task_id == a.task_id:
                continue
            if ((w.get("kind") == "decision" and str(w.get("handle") or "").casefold() in me)
                    or (w.get("kind") == "card" and str(w.get("target") or "") == a.task8)):
                wait["cycle_told"] = True
                return (f"wait cycle: #{wait['target']} ({other.agent}) is itself waiting on you "
                        f"({str(w.get('line') or w.get('kind'))[:160]}). Your wait for it to close can never "
                        f"end. Do your part of the work now, and if you then need its answer, wait with "
                        f"⏳ WAIT decision:@{other.agent.lower()} instead of card:#{wait['target']}.")
        return ""

    def resume_ready(self, room: int) -> int:
        if self.paused() or time.monotonic() < self._starts_paused_until:
            return room                       # a limit error: do not burn resumes either
        if self.memgate.check():
            return room
        for a in self.store.by_state(self.agent.name, ("ready",)):
            if room <= 0:
                break
            if time.monotonic() < max(self._quarantine.get((a.id, "resume"), 0), self._resume_hold.get(a.id, 0)):
                continue
            before = self.store.active_count(self.agent.name)
            self._guard(a, "resume", self._resume_one)
            if self.store.active_count(self.agent.name) > before:
                room -= 1
        return room

    def _resume_one(self, a: Attempt) -> None:
        task = self._task(a.task_id)
        if not self._still_ours(task, a.mode):
            self._finish(a, "completed", "task left the agent before resuming")
            return
        scope = self.store.mark(self.agent.name, a.task_id, RECEIPT_KEY)
        prior_scope = recorded_scope(scope[0] if scope else None, a.id)
        was_metadata = prior_scope == "metadata-only"
        if prior_scope and self.agent.policy.human_gate and task.get("human_gate"):
            self._release_gate(a, "human-held")
            self._release_checkout(a)
            self._suspend_held_ready(a)
            self._resume_hold[a.id] = time.monotonic() + GATE_HOLD_SECONDS
            return
        if self.pool.disk_hold():
            # Existing leases do not grant permission to start below the floor.
            # Only finish already verified releases; never capture an unjournaled
            # repository here. Verification retains its separate scratch budget.
            try:
                journal = self.pool._checkpoint_rows(a.id)
                verified = {r["repo"] for r in journal if r["phase"] in ("verified", "releasing")}
                leased = {r["pool"].split("/", 1)[1] for r in self.pool.rows()
                          if r["attempt"] == a.id and r["state"] == "leased"}
                if verified and leased <= verified:
                    if group_alive(a.pid, a.stamp):
                        raise WorkspaceError("checkpoint: writer group still alive")
                    self.pool.suspend(a.id, {"session_id": a.session_id, "harness": a.harness,
                        "cwd": str(self.agent.profile_dir), "workspaces": a.workspaces,
                        "wait": a.wait}, writer_stopped=True)
            finally:
                self._disk_hold_attempt(a)
            return
        if a.wait and self.effects.pending_cleanup(a.wait):
            wait = dict(a.wait)                  # an earlier wake could not undo its side effects
            self.effects.on_end(a, wait, "resuming", close_request=False)
            self.store.update(a, wait=wait)
        if a.mode == "review" and self._unstartable_hold(task, self._comments(a.task_id)):
            self._suspend_held_ready(a)
            self._resume_hold[a.id] = time.monotonic() + GATE_HOLD_SECONDS
            return                               # the gate keeps refusing this review: noticed, not retried
        reason, gate = self._gate("reserve", a.task_id, a.mode, a)
        if not reason:
            if not a.checkout.get("token") and not self._acquire(a):
                self._turn_end(a.task_id, a.mode, a, gate, "not-started")
                self._resume_hold[a.id] = time.monotonic() + GATE_HOLD_SECONDS
                return
            a = self.store.get(a.id) or a
            reason, gate = self._gate("admit", a.task_id, a.mode, a, gate)
        if reason:
            self._turn_end(a.task_id, a.mode, a, gate, "held")
            if a.checkout.get("token"):            # nobody heartbeats a held checkout
                self._release_checkout(a)
            self._resume_hold[a.id] = time.monotonic() + GATE_HOLD_SECONDS
            self._suspend_held_ready(a)
            self._log_hold("resume", a.task8, reason)
            if a.mode == "review":
                self._review_gate_refused(task, reason)      # counted like a refused feed: one notice, then left alone
            return
        if a.mode == "review":
            self.store.mark(self.agent.name, a.task_id, "review_gate_hold", "")
        self._clear_hold_log(a.task8, "resume")
        self._remember_gate(a, gate)
        restored = []
        try:
            env = self._fresh_env()
            blocked = self._auth_blocked(a, env)
            if blocked:
                self._auth_hold_attempt(a, blocked)
                return
            if prior_scope:
                task = self._task(a.task_id)  # the checkout may have changed the request scope
                if not self._still_ours(task, a.mode):
                    self._finish(a, "completed", "task left the agent before resuming")
                    return
                if self.agent.policy.human_gate and task.get("human_gate"):
                    self._release_gate(a, "human-held")
                    self._release_checkout(a)
                    self._suspend_held_ready(a)
                    self._resume_hold[a.id] = time.monotonic() + GATE_HOLD_SECONDS
                    return
            if a.turns - self.store.auth_turns(a.id) >= self.cfg.max_turns:
                # An auth failure after real work can exhaust the existing cap.
                # Clearing auth restores admission, never another product budget.
                if self.cfg.turn_cap_continue_on_progress and a.mode == "work":
                    self._turn_cap(a, task)
                else:
                    self._give_up(a, task, f"stopped after {a.turns} turns on this task")
                return
            journal = self.pool._checkpoint_rows(a.id)
            if journal and any(r["phase"] != "active" for r in journal):
                # Finish an interrupted release even below the disk floor: verified
                # checkpoints make returning source slots safe and may free space.
                # restore() checks the floor before fresh leases or replay writes.
                journal_repos = {r["repo"] for r in journal}
                leased_repos = {r["pool"].split("/", 1)[1] for r in self.pool.rows()
                                if r["attempt"] == a.id and r["state"] == "leased"}
                if (any(r["phase"] in ("verified", "releasing") for r in journal)
                        or leased_repos - journal_repos):
                    if group_alive(a.pid, a.stamp):
                        raise WorkspaceError("checkpoint: writer group still alive")
                    self.pool.suspend(a.id, {"session_id": a.session_id, "harness": a.harness,
                        "cwd": str(self.agent.profile_dir), "workspaces": a.workspaces,
                        "wait": a.wait}, writer_stopped=True)
                journal = self.pool._checkpoint_rows(a.id)
                # The attempt's inventory was fixed when it was admitted and journaled; a
                # config that grew repos since then applies to new attempts only. Anything
                # ambiguous (extra/unknown repo, snapshot disagreeing with the journal,
                # a lease outside the journal) stays fail-closed.
                expected_repos = {r["repo"] for r in journal}
                configured = {r.name for r in self.agent.repos}
                if (len(expected_repos) != len(journal) or not expected_repos
                        or expected_repos - configured
                        or sorted(a.workspaces) != sorted(r["source"] for r in journal)):
                    raise InventoryError("checkpoint: incomplete repository inventory; leases retained")
                held_now = {r["pool"].split("/", 1)[1] for r in self.pool.rows()
                            if r["attempt"] == a.id and r["state"] == "leased"}
                if any(self.pool.full(name) for name in expected_repos - held_now):
                    # restore() would lease fresh slots and fail on a full pool: three failures
                    # quarantine the resume. Hold it like a start does, without an error.
                    self._release_gate(a, "workspace-admission-held")
                    self._release_checkout(a)
                    self._resume_hold[a.id] = time.monotonic() + GATE_HOLD_SECONDS
                    self._log_hold("resume", a.task8, "no free worktree", episode="resume workspace")
                    return
                paths, restored = self.pool.restore(a.id, expected_repos=expected_repos)
                # Native sessions cannot promise cwd migration, even when a slot path happens
                # to be reused. Start fresh and explicitly carry the old context into prompt.
                self.store.update(a, workspaces=paths, session_id="", pid=0, stamp="")
                self.store.db.execute("UPDATE workspace_checkpoints SET phase='active' "
                                      "WHERE owner=? AND attempt=?", (self.pool.owner, a.id))
            elif not a.workspaces and not journal:
                owned = [r for r in self.pool.rows() if r["attempt"] == a.id]
                if not (was_metadata and metadata_only(task, a.mode) and not owned):
                    # Fence the code transition before the first lease. A crash or
                    # label toggle cannot turn partially leased work back into metadata.
                    self.store.mark(self.agent.name, a.task_id, RECEIPT_KEY,
                                    json.dumps({"attempt": a.id, "scope": "workspace"}))
                    by_repo = {r["pool"].split("/", 1)[1]: r for r in owned}
                    configured = {r.name for r in self.agent.repos}
                    if (len(by_repo) != len(owned) or set(by_repo) - configured
                            or any(r["state"] != "leased" for r in owned)):
                        raise WorkspaceError("workspace transition: existing leases require operator review")
                    if (self.pool.disk_hold() or any(self.pool.full(r.name) for r in self.agent.repos
                                                    if r.name not in by_repo)):
                        self._release_gate(a, "workspace-admission-held")
                        self._release_checkout(a)
                        self.store.update(a, pid=0, stamp="")
                        self._resume_hold[a.id] = time.monotonic() + GATE_HOLD_SECONDS
                        return
                    paths = [by_repo[r.name]["path"] if r.name in by_repo
                             else str(self.pool.lease(a.id, r.name)) for r in self.agent.repos]
                    self.store.update(a, workspaces=paths, pid=0, stamp="",
                                      wake_text=a.wake_text + "\n[runner] Normal repository admission "
                                      "completed. Use only these leased workspaces for code: "
                                      + json.dumps(paths))
        except (WorkspaceError, MeshError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
            self._release_gate(a, "workspace-restore-held")
            self._release_checkout(a)
            delay = GATE_HOLD_SECONDS
            if isinstance(exc, InventoryError):
                # A permanent refusal only changes with the config or the journal: back off
                # exponentially per (attempt, config) and say why, instead of a 2 min loop.
                key = (a.id, tuple(sorted(r.name for r in self.agent.repos)))
                n = self._inventory_failures[key] = self._inventory_failures.get(key, 0) + 1
                delay = min(GATE_HOLD_SECONDS * 2 ** (n - 1), TASK_HOLD_SECONDS)
                log.warning("%s: attempt %s refused resume (%s) workspaces=%d config=%s; retry in %ds",
                            self.agent.name, a.id[:8], exc, len(a.workspaces), key[1], delay)
            self._resume_hold[a.id] = time.monotonic() + delay
            raise
        self._clear_hold_log(a.task8)                 # resumed: every hold episode of this task is over
        def first():
            text = self._first_prompt(task, a.mode, a.workspaces)
            if restored:
                context = [{"previous_session": m["metadata"].get("session_id", ""),
                            "previous_workspaces": m["metadata"].get("workspaces", []),
                            "head": m["fingerprint"]["head"], "wait": m["metadata"].get("wait", {})}
                           for m in restored]
                text += "\n\n[runner] Restored verified WAIT checkpoint into these leased workspaces. " \
                        "Continue the existing work; inspect staged/unstaged/untracked files before edits. " \
                        "Prior session context: " + json.dumps(context) + "\n" + a.wake_text
            return text
   # noqa: E731
        try:
            if not a.session_id:
                started = self._spawn(a, first(), resume=False, expect=("ready",), env=env)
            else:
                text = (a.wake_text or prompts.wake_prompt(task, "resumed")) + self._news_since_last_turn(a)
                started = self._spawn(a, text, resume=True, expect=("ready",), first_prompt=first, env=env)
            if started:
                # A passing start gate alone does not end the hold: fresh human,
                # auth and workspace admission can still refuse the same attempt.
                self.store.mark(a.agent, a.task_id, "workspace_ready_hold", "")
        except BaseException:
            cur = self.store.get(a.id)
            if not (cur and cur.pid):              # the turn did not start: nothing will settle it
                self._release_gate(a, "not-started")
            raise

    def _news_since_last_turn(self, a: Attempt) -> str:
        since = parse_time(a.turn_started_at)
        if since is None:
            return ""
        try:
            return prompts.new_comments_block(foreign_comments(self.mesh.comments(a.task_id), self.agent_id, since))
        except MeshError:
            return ""

    def _new_comment_since(self, task_id: str, at: dt.datetime) -> bool:
        try:
            return foreign_comment(self.mesh.comments(task_id), self.agent_id, at) is not None
        except MeshError:
            return False

    def _failed_recently(self, task_id: str, failed_at: dt.datetime) -> bool:
        """A failed task is not retried until someone else writes on it or the
        cooldown passes: retrying at once only repeats the same failure."""
        if now() - failed_at >= dt.timedelta(hours=self.cfg.failed_cooldown_hours):
            return False
        try:
            return foreign_comment(self.mesh.comments(task_id), self.agent_id, failed_at) is None
        except MeshError:
            return True

    def _first_prompt(self, task: dict, mode: str, paths: list[str], changed: dict | None = None) -> str:
        comments = self._comments(task["id"])
        if mode == "review":
            protocol = prompts.load_preamble(self.agent.review_prompt_file) if self.agent.review_prompt_file else ""
            changed = changed or {}
            news = ""
            since = parse_time(changed.get("since"))
            if since:
                news = prompts.new_comments_block(foreign_comments(comments, self.agent_id, since))
            return prompts.review_prompt(task, self.agent.name, paths, comments, self.preamble, protocol,
                                         changed=changed.get("event", ""), news=news,
                                         unreviewed_hours=changed.get("unreviewed_hours"))
        return prompts.start_prompt(task, self.agent.name, paths, comments, self.preamble,
                                    merge=self.merge_enabled)

    def _own_hold_hours(self) -> float:
        own = self.agent.review_own_hold_hours
        return self.cfg.review_own_hold_hours if own is None else own

    def _verdict_re(self, own: bool = False) -> re.Pattern | None:
        key = "review_own_verdict_pattern" if own else "review_verdict_pattern"
        raw = getattr(self.agent, key)
        raw = getattr(self.cfg, key) if raw is None else raw
        return re.compile(raw, re.I | re.M) if raw else None

    def _verdict_event(self, comments: list[dict], since: dt.datetime, heads) -> str:
        """The newest review verdict written at or after ``since`` by anyone (the author
        may run the reviewer itself and post the verdict as its own comment, then it is
        the anchor of ``since``): after SHIP the author merges, after DO-NOT-SHIP it fixes.
        A verdict naming a commit counts only on the head of a linked merge request when
        those heads are known (``heads()``: shas, empty = no linked MR, None = cannot tell now)."""
        theirs, mine = self._verdict_re(), self._verdict_re(own=True)
        for c in reversed(comments):
            at = parse_time(c.get("created_at"))
            if at is None or at < since:
                break
            body = c.get("body") or ""
            if body.startswith("[runner"):
                continue
            author = c.get("author_id") or (c.get("author") or {}).get("id")
            pattern = mine if author == self.agent_id else theirs
            if pattern is None:
                continue
            found = [m for m in pattern.finditer(body)
                     if not ENUMERATED_VERDICTS.search(body[body.rfind("\n", 0, m.start()) + 1:].split("\n", 1)[0])]
            if not found:
                continue
            m = found[-1]
            groups = m.groupdict()
            sha = (groups.get("sha") or "").lower()
            if sha:
                known = heads()
                if known is None:
                    continue                    # cannot tell the head now: next pass
                if known and not any(h.startswith(sha) or sha.startswith(h) for h in known):
                    continue                    # a verdict on an older commit
            who = c.get("author_name") or (c.get("author") or {}).get("name") or "someone"
            line = body[m.start():].splitlines()[0].strip() if body[m.start():] else m.group(0)
            return f"review verdict {groups.get('verdict') or m.group(0)} by {who}: {line[:200]}"
        return ""

    def _own_work(self, task_id: str, comments: list[dict]) -> tuple[Attempt, dt.datetime, dict | None] | None:
        """The agent's newest work attempt on this task that did work of its own:
        (attempt, end of its last turn, the agent's last comment in it). A task that
        left the agent while it was parked, or an attempt that never ran a turn, is
        not the agent's work. The end is the agent's own last comment in the attempt
        (Mesh clock, the same clock as everyone else's comments); without one, the
        end of its last turn — never when the runner noticed the attempt closed."""
        best = None
        for a in self.store.by_state(self.agent.name, ("completed",)):
            if a.task_id != task_id or a.mode != "work" or not a.turns or a.note.startswith(LEFT_WHILE_IDLE):
                continue
            closed = parse_time(a.updated_at)
            if closed is None:
                continue
            end = min(t for t in (closed, parse_time(a.leader_done_at)) if t)
            begun = parse_time(a.created_at) or end
            last = None
            for c in comments:
                at = parse_time(c.get("created_at"))
                author = c.get("author_id") or (c.get("author") or {}).get("id")
                if (author == self.agent_id and at and begun - CLOCK_SLACK <= at <= closed + CLOCK_SLACK
                        and not (c.get("body") or "").startswith("[runner")):   # the runner writes as the agent
                    last = c
            if last is not None:
                end = parse_time(last["created_at"])
            if best is None or end > best[1]:
                best = (a, end, last)
        return best

    @staticmethod
    def _heads(targets: list[tuple[str, str]], gl: "_GitLabReads | None") -> list[str] | None:
        """Heads of the linked merge requests; empty = none known; None = GitLab is down
        right now (a verdict on a commit waits for it)."""
        out = []
        for kind, target in targets if gl else []:
            try:
                sha = str(gl.get_mr(target).get("sha") or "").lower()
            except _GitLabDown:
                return None
            except _TargetUnreadable:
                continue
            if sha:
                out.append(sha[:40])
        return out

    def _ship_since(self, task: dict, comments: list[dict], since: dt.datetime) -> bool:
        """A SHIP verdict on the current head was posted after ``since``."""
        def heads():
            try:
                links = self.mesh.vcs_links(task["id"])
            except MeshError:
                return None
            gl = _GitLabReads(self) if self.gitlab else None
            return self._heads([("mr", t) for t in gitlab_mr_targets(links, self.gitlab.base_url)] if gl else [], gl)
        return self._verdict_event(comments, since, heads).startswith("review verdict SHIP ")

    def _stale_ci_baseline(self, a, wait: dict) -> str:
        """Text for a typed CI wait whose baseline differs from the current result that the agent was
        already told about: the delivery is durably suppressed, so that wait would sleep for good.
        '' = fine (baseline is current, the result is new to the agent, the predicate would not
        wake on it anyway, or it cannot be read/validated: the normal evaluator owns those)."""
        contract = wait["ci_change"]
        try:
            sha, pid, status = ci_observation(self.gitlab, wait["kind"], wait["target"])
            if (sha, pid, status) == (contract["sha"], contract["pipeline"], contract["status"]):
                return ""
            if status not in ("success", "failed", "canceled", "cancelled", "skipped", "manual"):
                return ""
            if contract.get("predicate") == "success" and status != "success":
                # until=success never wakes on a non-success result; the normal evaluator
                # owns that case (owner notice), so do not bounce it here
                return ""
            if self._ci_delivered(a.task_id, ci_delivery_key(wait["target"], pid)) != f"{sha}:{pid}:{status}":
                return ""
            state = ("blocked on manual jobs (they start only by your action)" if status == "manual"
                     else f"finished: {status}")
            return (f"{wait['kind']} {wait['target']} already {state} (pipeline {pid}, {str(sha)[:8]}) "
                    f"and you were told. Your baseline after={contract['sha']}:{contract['pipeline']}:"
                    f"{contract['status']} is stale, so this wait would never wake. Act on the result "
                    f"(fix, retry, merge, ROUTE), or wait for a NEW observation with "
                    f"after={sha}:{pid}:{status}")
        except Exception as exc:
            log.warning("%s: #%s stale CI baseline check failed, baseline retained: %r",
                        self.agent.name, a.task8, exc)
            return ""

    def _canceled_pipeline_wait(self, a: Attempt, outcome: Outcome) -> tuple[str, bool]:
        """(bounce text, dead_again) for a typed ``pipeline`` wait (until=change or until=success)
        on a pipeline in a terminal canceled/skipped state with no queued or running job: no retry
        is coming, so nothing will change it. The agent is told once per (target, status), with
        the newer pipeline of the same ref when there is one. The same dead wait declared again
        after that is kept as an ordinary wait (``dead_again``: the other sent-back checks must not
        bounce it either) and leaves a ``dead_pipeline_rewait`` mark for the hourly report.
        ('', False) = not this case, or unreadable: the wait stands."""
        ci = outcome.extra.get("ci_change")
        if outcome.kind != "pipeline" or not ci or self.gitlab is None:
            return "", False
        target = outcome.target
        try:
            row = self.gitlab.get_pipeline(target)
            sha, pid, status = ci_observation(self.gitlab, "pipeline", target, row=row)
            if status not in ("canceled", "cancelled", "skipped") or self.gitlab.pipeline_has_active_jobs(target):
                return "", False
            key = f"{target}:{status}"
            # one durable mark per (task, target, status), never truncated; claim() is atomic
            if not self.store.claim(self.agent.name, a.task_id, f"canceled_bounce:{key}", "1"):
                self.store.mark(self.agent.name, a.task_id, "dead_pipeline_rewait", key)
                log.info("%s: #%s declared the dead wait on %s again after a bounce; kept as a wait",
                         self.agent.name, a.task8, target)
                return "", True
        except Exception as exc:                # noqa: BLE001 - unreadable: the wait stands
            log.warning("%s: #%s canceled pipeline wait check failed, wait kept: %r", self.agent.name,
                        a.task8, exc)
            return "", False
        project = target.rsplit("#", 1)[0]
        text = (f"pipeline {pid} is {status}; nothing will change it (terminal state at {sha[:8]}, no queued or "
                "running job). Retry it, push, or wait on a newer pipeline")
        try:
            newer = self.gitlab.newer_pipeline(target, row)
        except Exception as exc:                # noqa: BLE001 - the hint is optional
            log.info("%s: #%s newer pipeline lookup failed: %r", self.agent.name, a.task8, exc)
            newer = None
        nsha, nid = (str((newer or {}).get("sha") or ""), (newer or {}).get("id"))
        if newer and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", nsha) and type(nid) is int:
            text = (f"pipeline {pid} is {status}; nothing will change it. Retry it, push, or wait on the newer "
                    f"pipeline {nid} ({str(newer.get('status') or 'unknown')}, {nsha[:8]}) for the same ref: "
                    f"`⏳ WAIT pipeline:{project}#{nid} until=change after={nsha}:{nid}:{newer.get('status')}`")
        elif newer and type(nid) is int:
            text = (f"pipeline {pid} is {status}; nothing will change it. Retry it, push, or wait on the newer "
                    f"pipeline {nid} ({str(newer.get('status') or 'unknown')}) for the same ref")
        else:
            text += " (none exists yet for this ref)."
        return text, False

    def _unattended_ci_wait(self, a: Attempt, outcome: Outcome) -> str:
        """Text for a typed CI/MR wait whose condition nobody will bring about:
        - ``pipeline ... until=success`` on a pipeline already ``manual`` (its next stage
          starts only by hand, usually after a review), ``failed`` or ``canceled``;
        - a plain ``pipeline`` wait (no until/after) on a pipeline still ``manual`` at the same
          commit after that manual state was already delivered to the agent (see
          ``_manual_state_delivered``): it would only wake on the same news again;
        - ``pipeline ... until=change`` on a pipeline that has finished (any final state,
          ``manual`` included) at the declared baseline: only the agent's own action moves it;
        - ``mr ... until=merged`` or ``until=change`` on the task's own open MR whose head
          pipeline is ``success`` or ``manual`` and which carries no review label
          (``review_label_prefix``): nobody will merge it, nothing will change it;
        - ``mr ... until=change`` on an MR whose head pipeline is ``failed``/``canceled`` (any MR),
          or ``manual`` (the task's own MR, review label or not): a new head or a retry is the
          agent's or the owner's action, not an event that will arrive by itself;
        - ``mr ... until=merged`` on an open MR (any MR) that conflicts with its target or must be
          rebased (``mr_merge_blocker``), or whose head pipeline is ``failed``/``canceled``/``skipped``
          after that red result was already delivered to the agent (``_red_head_delivered``): it
          will not merge as is. A red head not yet delivered is accepted once, so that the
          evaluator's owner notice reaches the agent.
        The MR is the task's own when it is linked to the task, names the task id in its title,
        description or source branch, or was authored by this agent's GitLab user. A result that
        differs from the declared baseline and is new to the agent is left to wake the wait on the
        next tick.
        '' = accept: anything else, an until=change whose new observation will wake the wait
        anyway, an MR not owned by this task, or a target that cannot be read (the wait stands)."""
        if outcome.kind not in ("pipeline", "mr") or self.gitlab is None:
            return ""
        ci, merged = outcome.extra.get("ci_change"), outcome.extra.get("mr_state")
        target = outcome.target
        if outcome.kind == "pipeline" and not ci:
            return self._delivered_manual_plain_wait(a, target)
        try:
            if outcome.kind == "pipeline":
                row = self.gitlab.get_pipeline(target)
                sha, pid, status = ci_observation(self.gitlab, "pipeline", target, row=row)
                if ci.get("predicate") != "success":
                    if status not in ("success", "failed", "canceled", "cancelled", "skipped", "manual"):
                        return ""
                    if self._result_new_to_agent(a, target, ci, sha, pid, status):
                        return ""                   # the evaluator wakes on it
                    return self._dead_change_text("pipeline", target, sha, pid, status)
                if status == "manual":
                    # its manual jobs start only after a review (by the merge gate, when there is one)
                    return manual_pipeline_text(target, sha, pid, row, self.merge_enabled, sent_back=True)
                if status in ("failed", "canceled", "cancelled"):
                    return (f"pipeline {target} already finished: {status} (pipeline {pid}, {sha[:8]}), so "
                            "until=success will not arrive on its own. Fix the cause and push, or retry the "
                            "failed job once, then wait on the new result.")
                return ""
            prefix = self.cfg.review_label_prefix
            is_change = bool(ci and ci.get("predicate") == "change")
            if not (merged or is_change):
                return ""
            row = self.gitlab.get_mr(target)
            state = row.get("state") if isinstance(row, dict) else None
            if state in ("merged", "closed", "locked"):
                return ""                       # the evaluator owns these
            if state != "opened":
                raise ValueError("MR state unavailable")
            cause = mr_merge_blocker(row) if merged else ""
            if cause:
                # a conflict does not resolve itself, whoever owns the MR: only a new head does
                return self._blocked_merge_text(target, row, cause)
            reviewed = False
            if prefix:                          # the label check can be turned off; the dead-change checks cannot
                labels = row.get("labels")
                if not isinstance(labels, list):
                    raise ValueError("MR labels unavailable")
                names = [x.get("name") if isinstance(x, dict) else x for x in labels]
                if not all(isinstance(n, str) for n in names):
                    raise ValueError("MR label unreadable")   # unknown shape: the wait stays parked
                reviewed = any(n.startswith(prefix) for n in names)
            sha, pid, status, generation = ci_observation_gen(self.gitlab, "mr", target, row=row)
            if status not in ("success", "manual", "failed", "canceled", "cancelled", "skipped"):
                return ""
            if ci and self._result_new_to_agent(a, target, ci, sha, pid, status):
                return ""                       # a result new to the agent: the evaluator wakes on it
            if status in ("failed", "canceled", "cancelled", "skipped"):
                if is_change:
                    return self._dead_change_text("MR", target, sha, pid, status)
                # until=merged: a red head not yet told is accepted once, so the evaluator's owner
                # notice reaches the agent; the same red head declared again after it would park for good
                if (status in red_merge_statuses(self.merge_enabled)
                        and self._red_head_delivered(a, target, sha, pid, status, generation)):
                    return self._blocked_merge_text(target, row, "red", pid, status)
                return ""
            if not self._owns_mr(a, row, target):
                return ""                       # someone else's MR: its owner may still review it
            if reviewed or not prefix:
                if status == "manual" and is_change:
                    return self._dead_change_text("MR", target, sha, pid, status)
                return ""
            until = "merged" if merged else "change"
            return (f"MR {target} has no review label ({prefix}...) and its head pipeline {pid} is {status} "
                    f"({sha[:8]}), so nobody will merge it and until={until} will not arrive on its own. "
                    "Run the review, then merge with the project's merge tool.")
        except Exception as exc:                # noqa: BLE001 - unreadable: the wait stands
            log.warning("%s: #%s %s wait check failed, wait kept: %r", self.agent.name, a.task8,
                        outcome.kind, exc)
            return ""

    def _red_head_delivered(self, a: Attempt, target: str, sha: str, pid: int, status: str,
                            generation: str) -> bool:
        """The agent was already told that MR ``target``'s head ``sha`` (pipeline ``pid``) finished
        ``status``: by the owner notice of a wait for its merge, by a delivered ``until=change``
        observation, or by a plain wait on the same MR that woke on that result."""
        reader = lambda key: self._ci_delivered(a.task_id, key)   # noqa: E731
        if owner_action_delivered(target, sha, pid, status, reader, generation):
            return True
        if reader(ci_delivery_key(target, pid)) == f"{sha}:{pid}:{status}":
            return True
        prev = a.wait or {}
        return (prev.get("kind") == "mr" and prev.get("target") == target and not typed_wait(prev)
                and (prev.get("seen") or {}).get("mr_pipeline") == f"{pid}:{status}")

    def _blocked_merge_text(self, target: str, row: dict, cause: str, pid: int = 0, status: str = "") -> str:
        """Send-back text for a wait for the merge of an open MR that will not merge as is."""
        sha = row.get("sha") if isinstance(row.get("sha"), str) else ""
        head = f" head {sha[:8]}" if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha) else ""
        if cause == "red":
            what = f":{head} pipeline {pid} finished: {status} and you were already told;"
        elif cause == "conflict":
            what = f"{head} has a merge conflict with its target branch;"
        else:
            what = f"{head} must be rebased onto its target branch before it can merge;"
        return (f"MR {target}{what} it will not merge as is, so this wait for its merge cannot complete on "
                "its own and was sent back. " + blocked_mr_next_step(target, cause, self.merge_enabled))

    def _delivered_manual_plain_wait(self, a: Attempt, target: str) -> str:
        """Send-back text for a plain ``pipeline`` wait (the default predicate: wake when it
        settles) declared on a pipeline that is still blocked on manual jobs at the same commit
        after that manual state was already delivered. '' = accept: any other status (running,
        pending, a manual stage someone started), a manual state not yet delivered (the wait's
        own seed tells it once), or a target that cannot be read."""
        try:
            row = self.gitlab.get_pipeline(target)
            sha, pid, status, generation = ci_observation_gen(self.gitlab, "pipeline", target, row=row)
            if status != "manual" or not self._manual_state_delivered(a, target, sha, pid, generation):
                return ""
            return manual_pipeline_text(target, sha, pid, row, self.merge_enabled, sent_back=True)
        except Exception as exc:                # noqa: BLE001 - unreadable: the wait stands
            log.info("%s: #%s pipeline wait check failed, wait kept: %r", self.agent.name, a.task8, exc)
            return ""

    def _manual_state_delivered(self, a: Attempt, target: str, sha: str, pid: int, generation: str) -> bool:
        """The agent was already told that pipeline ``target`` (``pid`` at ``sha``) is blocked on
        manual jobs: by an owner notice of an ``until=success`` wait, by a delivered ``until=change``
        observation, or by a plain wait on the same pipeline that woke on that manual state."""
        if owner_action_delivered(target, sha, pid, "manual",
                                  lambda key: self._ci_delivered(a.task_id, key), generation):
            return True
        if self._ci_delivered(a.task_id, ci_delivery_key(target, pid)) == f"{sha}:{pid}:manual":
            return True
        prev = a.wait or {}
        return (prev.get("kind") == "pipeline" and prev.get("target") == target and not typed_wait(prev)
                and (prev.get("seen") or {}).get("pipeline") == "manual")

    def _result_new_to_agent(self, a: Attempt, target: str, ci: dict, sha: str, pid: int, status: str) -> bool:
        """The observation differs from the declared baseline and was never delivered to the agent."""
        if (sha, pid, status) == (ci["sha"], ci["pipeline"], ci["status"]):
            return False
        return self._ci_delivered(a.task_id, ci_delivery_key(target, pid)) != f"{sha}:{pid}:{status}"

    @staticmethod
    def _dead_change_text(kind: str, target: str, sha: str, pid: int, status: str) -> str:
        """Bounce text for an until=change wait on a head that only the agent's own action can move."""
        new = "`after=<full-head-sha>:<pipeline-id>:running`"
        if status == "manual":
            return (f"{kind} {target}: pipeline {pid} ({sha[:8]}) is blocked on manual jobs, so until=change will "
                    "not arrive on its own: nothing changes without your action. Run your merge wrapper or "
                    f"start the job, then wait on the new state ({new}, or until=success).")
        if status in ("failed", "canceled", "cancelled"):
            return (f"{kind} {target}: pipeline {pid} ({sha[:8]}) already finished: {status}, so until=change "
                    "will not arrive on its own: nothing changes without a push or a retry. Fix the cause "
                    f"and push, or retry the failed job once, then wait on the new state ({new}); if the fix "
                    "belongs to another card, wait on that card (`⏳ WAIT card:#<8hex>`).")
        return (f"{kind} {target}: pipeline {pid} ({sha[:8]}) already finished: {status}, so until=change "
                "will not arrive on its own: nothing changes without your action. Act on the result "
                f"(merge, hand over), or push and wait on the new state ({new}).")

    def _owns_mr(self, a: Attempt, row: dict, target: str) -> bool:
        """The MR belongs to this task: linked to it, naming its id in the title, description
        or source branch, or authored by this agent's GitLab user (``gitlab_username``): an agent
        that opened the MR itself often neither links it nor names the task in it."""
        if target in gitlab_mr_targets(self.mesh.vcs_links(a.task_id), self.gitlab.base_url):
            return True
        mine = (self.agent.gitlab_username or "").casefold()
        author = row.get("author")
        username = author.get("username") if isinstance(author, dict) else None
        if mine and isinstance(username, str) and username.casefold() == mine:
            return True
        text = " ".join(str(row.get(k) or "") for k in ("title", "description", "source_branch"))
        return re.search(rf"(?<![0-9a-f]){re.escape(a.task8)}(?![0-9a-f])", text, re.IGNORECASE) is not None

    def _forget_spent_ci_delivery(self, a: Attempt, outcome: Outcome) -> None:
        """A retried pipeline keeps its id and returns to a state the agent was already told about
        (failed, retried, failed again). The durable delivery of the earlier result would swallow the
        second one: when the accepted wait's baseline is the current observation and the stored
        delivery is a different result, that delivery is spent and is dropped."""
        ci = outcome.extra.get("ci_change")
        if outcome.kind not in ("pipeline", "mr") or not ci or self.gitlab is None:
            return
        try:
            sha, pid, status = ci_observation(self.gitlab, outcome.kind, outcome.target)
            if (sha, pid, status) != (ci["sha"], ci["pipeline"], ci["status"]):
                return
            key = ci_delivery_key(outcome.target, pid)
            delivered = self._ci_delivered(a.task_id, key)
            if delivered and delivered != f"{sha}:{pid}:{status}":
                self.store.mark(self.agent.name, a.task_id, key, "")
                log.info("%s: #%s spent CI delivery dropped (%s, now %s)", self.agent.name, a.task8,
                         delivered.rsplit(":", 1)[-1], status)
        except Exception as exc:                # noqa: BLE001 - unreadable: keep the delivery
            log.warning("%s: #%s CI delivery reset failed, kept: %r", self.agent.name, a.task8, exc)

    def _repeated_decision_told(self, a: Attempt, task: dict, outcome: Outcome, comments: list[dict]) -> str:
        """Text for a ``decision:@agent`` wait that repeats, verbatim, the question its addressee
        already answered on this task since it was asked ('' = accept). A person's handle, a task
        under a human gate and an unreadable roster all accept: the usual routing decides."""
        if outcome.kind != "decision" or not outcome.handle or task.get("human_gate"):
            return ""
        try:
            return self.effects.answered_repeat(a, outcome.handle, outcome.note, comments, self.me)
        except Exception as exc:                 # noqa: BLE001 - a failed check never blocks the ask
            log.warning("%s: #%s repeated decision check failed, wait kept: %r", self.agent.name, a.task8, exc)
            return ""

    def _card_wait_told(self, a: Attempt, outcome: Outcome) -> str:
        """Text for a ``card`` wait on X when X itself waits on a timer, a measurement window, a
        person or a review (a human gate): by the 21.10 dependency scheme that does not hold
        development. '' = accept: X runs, waits on a pipeline/mr/card (code is coming), is an
        epic/umbrella, is closed, or is unknown to this store."""
        if outcome.kind != "card" or not outcome.target:
            return ""
        try:
            target = [x for x in self.store.open_on_task(str(outcome.target)) if x.task_id != a.task_id]
            if not target:
                return ""
            idle = ""
            for x in target:
                w = x.wait or {}
                kind = w.get("kind")
                if x.state == "waiting" and kind in ("time", "decision", "blocking"):
                    idle = idle or ("the timer or the measurement window" if kind == "time" else "a person")
                elif x.state == "ready":
                    t = self._task(x.task_id)
                    if not (t and t.get("human_gate")):
                        return ""
                    idle = idle or "a person (human gate)"
                else:
                    return ""                    # running, or waiting for code (pipeline/mr/card/queue)
            t = self._task(target[0].task_id)
            labels = {str(l.get("name") if isinstance(l, dict) else l).casefold() for l in (t or {}).get("labels") or []}
            if not t or labels & {"epic", "umbrella"}:
                return ""
            return (f"#{outcome.target} itself waits for {idle}; under the 21.10 dependency scheme that does not "
                    "hold development. Do your part now, or name the code in it that you need and wait "
                    "for its merge (`⏳ WAIT mr:<group>/<project>!<iid>`).")
        except Exception as exc:                 # noqa: BLE001 - an unreadable target: the wait stands
            log.warning("%s: #%s card wait check failed, wait kept: %r", self.agent.name, a.task8, exc)
            return ""

    def _ci_delivered(self, task_id: str, key: str) -> str:
        mark = self.store.mark(self.agent.name, task_id, key)
        return Delivered(mark[0], mark[1]) if mark else ""

    def _review_event(self, task: dict, since: dt.datetime, last: dict | None, comments: list[dict]) -> str:
        """What happened on this task after ``since`` that the agent has not seen
        ('' = nothing): a review verdict, someone else's comment, a change on its linked merge requests
        (merged, closed, a new head, a pipeline finished), or the wait the agent
        declared in its last comment being over."""
        if not hasattr(self, "_review_ci_delivery"):
            self._review_ci_delivery = {}
            self._review_ci_observation = {}
        self._review_ci_delivery.pop(task["id"], None)
        lines = declaration_lines((last or {}).get("body") or "")
        declared = self._parse(lines[-1], parse_time(last["created_at"]) or since, task["id"][:8]) if lines else None
        if declared and declared.kind == "blocking":
            # a question to a human: only a person's answer (a comment) or the gate it raised
            # being lifted ends it. The gate is not set on this path (the card went to review
            # in the same turn), so "no gate" says nothing; only a seen set -> cleared does.
            c = foreign_comment(comments, self.agent_id, since)
            if c:
                who = c.get("author_name") or (c.get("author") or {}).get("name") or "someone"
                return f"{who} commented: {(c.get('body') or '')[:300]}"
            if task.get("human_gate"):
                self.store.mark(self.agent.name, task["id"], "gate_seen", "1")
                return ""
            seen = self.store.mark(self.agent.name, task["id"], "gate_seen")
            seen_at = parse_time(seen[1]) if seen else None
            return "the human gate on your question was lifted" if seen_at and seen_at > since else ""
        try:
            links = self.mesh.vcs_links(task["id"])
        except MeshError:
            links = []
        gl = _GitLabReads(self) if self.gitlab else None
        targets = [("mr", t) for t in gitlab_mr_targets(links, self.gitlab.base_url)] if gl else []
        event = self._verdict_event(comments, since, lambda: self._heads(targets, gl))
        if event:
            return event
        c = foreign_comment(comments, self.agent_id, since)
        if c:
            who = c.get("author_name") or (c.get("author") or {}).get("name") or "someone"
            return f"{who} commented: {(c.get('body') or '')[:300]}"
        if declared and typed_wait(declared.extra):
            self._review_ci_observation[task["id"]] = "unknown"
            try:
                observation = {}
                event = typed_event(gl, declared.kind, declared.target, declared.extra,
                                    lambda key: self._ci_delivered(task["id"], key), observation)
                self._review_ci_observation[task["id"]] = "changed" if event else "unchanged"
                if event:
                    self._review_ci_delivery[task["id"]] = observation
                return event
            except Exception:
                log.info("%s: #%s typed VCS wait UNKNOWN; baseline retained", self.agent.name, task["id"][:8])
                return ""
        event = vcs_event(links, since, SELF_ECHO)
        if event:
            return event
        if declared and declared.kind in ("pipeline", "mr"):
            if gl and (declared.kind, declared.target) not in targets:
                targets.insert(0, (declared.kind, declared.target))
        elif declared and declared.is_wait:
            wait = {"kind": declared.kind, "target": declared.target, "handle": declared.handle,
                    "until": declared.until.isoformat() if declared.until else "", "since": iso(since),
                    "started": iso(since)}
            event = wake_check(self.mesh, self.cats, None, task, wait, self.agent_id, [], now(),
                               max_wait=dt.timedelta(days=3650))
            if event:
                return f"your {declared.line.strip()[:120]} is over: {event}"
        for kind, target in targets:
            try:
                event = changed_since(gl, kind, target, since, SELF_ECHO)
            except _GitLabDown:
                break
            except Exception as e:        # unreadable: no event, the hold stands
                log.info("%s: #%s %s %s unreadable: %s", self.agent.name, task["id"][:8], kind, target, e)
                continue
            if event:
                return event
        return ""

    @staticmethod
    def _qa_infra_refusal(reason: str) -> bool:
        # Finite machine tags only: unknown classes are not reviewer/capacity verdicts.
        # A route sibling's missing pool/controller evidence can recover off-card.
        return (reason.removeprefix("route: ") == "qa_no_selection:sibling_evidence_unknown"
                or bool(re.match(r"^(?:route: )?qa_(?:artifact_unresolved|fail_closed):", reason)))

    def _unstartable_hold(self, task: dict, comments: list[dict]) -> str:
        """A refused review sleeps until a comment or its bounded retry.
        Infrastructure recovery can happen without changing the card; reviewer absence cannot."""
        stuck = self.store.mark(self.agent.name, task["id"], "review_unstartable")
        if stuck and stuck[0] and self.cfg.review_gate_hold_minutes <= 0:
            self.store.mark(self.agent.name, task["id"], "review_unstartable", "")      # the feature was turned off
            stuck = None
        if not (stuck and stuck[0]):
            return ""
        infra = self._qa_infra_refusal(stuck[0])
        label = "QA infrastructure unavailable" if infra else "no reviewer could start it"
        held_since = parse_time(stuck[1]) or now()
        retry = dt.timedelta(minutes=self.cfg.review_unstartable_retry_minutes)
        tid = task["id"]
        if foreign_comment(comments, self.agent_id, held_since) is None:
            if retry <= dt.timedelta(0) or now() - held_since < retry:
                if retry > dt.timedelta(0):          # a cached hold must not outlive the retry moment
                    left = (held_since + retry - now()).total_seconds()
                    self._hold_deadline[tid] = time.monotonic() + max(left, 0)
                return f"review: {label}; waits for a new comment or a retry"
            sig = self.store.mark(self.agent.name, tid, "review_unstartable_sig")
            # Probe infrastructure again after the retry even if card metadata did not change.
            # The ordinary reviewer-absence hold still needs an observable card change.
            if not infra and sig and sig[0] and sig[0] == self._review_state_sig(task):
                # the retry moment came but nothing on the card moved: the gate would say the same
                self._hold_deadline[tid] = time.monotonic() + retry.total_seconds()
                return "review: no reviewer could start it; waits for a new comment or a change on the card"
            # a retry on a changed card: the streak stays, so a refusal is one hold, not a new grace window
            streak = self.store.mark(self.agent.name, tid, "review_gate_hold")
            if streak and streak[0]:
                self.store.mark(self.agent.name, tid, "review_gate_hold", streak[0])
        else:
            self.store.mark(self.agent.name, tid, "review_gate_hold", "")
            self.store.mark(self.agent.name, tid, "review_gate_hold_n", "")
        self.store.mark(self.agent.name, tid, "review_unstartable", "")
        self.store.mark(self.agent.name, tid, "review_unstartable_sig", "")
        return ""

    @staticmethod
    def _review_state_sig(task: dict) -> str:
        """What a retry of a refused review can depend on, read off the card: its status, assignee and labels."""
        labels = sorted(str(l.get("name") if isinstance(l, dict) else l) for l in task.get("labels") or [])
        return json.dumps([task.get("status_id") or task.get("status") or "", task.get("assignee_id") or "", labels])

    def _review_hold(self, task: dict) -> str:
        """The queue feeder's review rules: a card this agent just sent to review waits 10 min;
        the agent's own work waits for someone else to review it, until something it
        has not seen happens (a comment, a verdict, its MR merged or its pipeline done,
        its own wait over): then it is fed once with what changed. Past
        ``review_own_hold_hours`` with nothing, it is fed once to route it on. At no age
        is the author given the accept protocol on its own work unless someone else
        commented or a SHIP verdict came since. Once fed it is not fed again for a day
        unless someone else wrote on it."""
        self._review_why.pop(task["id"], None)
        comments = self._comments(task["id"])
        standing = self._unstartable_hold(task, comments)
        if standing:
            return standing
        updated = parse_time(task.get("updated_at"))
        last = comments[-1] if comments else {}
        last_author = last.get("author_id") or (last.get("author") or {}).get("id")
        if updated and now() - updated < REVIEW_MIN_AGE and last_author == self.agent_id:
            return "review: just sent by this agent"
        fed = self.store.mark(self.agent.name, task["id"], "review_fed")
        fed_at = parse_time(fed[1]) if fed else None
        hours = self._own_hold_hours()
        own = self._own_work(task["id"], comments)
        own_lines = declaration_lines((own[2] or {}).get("body") or "") if own else []
        own_typed = bool(own_lines and typed_wait(self._parse(own_lines[-1], own[1]).extra))
        if own and (own_typed or not (fed_at and fed_at > own[1])):
            event_since = max(own[1], fed_at) if own_typed and fed_at else own[1]
            event = self._review_event(task, event_since, own[2], comments)
            if event:
                # the next step is the author's (merge, acceptance, a fix, a new request): fed
                # once with what changed; after that the refeed rule below decides. Only a
                # reviewer's word (someone else's comment, a SHIP verdict) lets it accept.
                reviewed = (event.startswith("review verdict SHIP ")
                            or foreign_comment(comments, self.agent_id, own[1]) is not None)
                self._review_why[task["id"]] = {"event": event, "since": iso(own[1])}
                if getattr(self, "_review_ci_delivery", {}).get(task["id"]):
                    self._review_why[task["id"]]["ci_delivery"] = self._review_ci_delivery[task["id"]]
                if not reviewed:
                    self._review_why[task["id"]]["unreviewed_hours"] = self._hours_since(own[1])
                return ""
            if own_typed and getattr(self, "_review_ci_observation", {}).get(task["id"]) == "unknown":
                return "review: typed VCS wait UNKNOWN; baseline retained"
            if now() - own[1] < dt.timedelta(hours=hours):
                return "review: the agent's own work; waits for someone else to review it"
            if self._own_wait_pending(task, own[2], own[1]):
                # the card went to review without the author (its MR merged mid-turn) and the
                # author's last word is a WAIT that is not over: that wait stands. Feeding it as
                # a QA launch of its own work is refused by the gate (author = reviewer) every
                # 10 min; _review_event above already wakes it when the wait ends.
                return "review: the agent's own declared wait is not over"
            # nobody looked at it for the whole window: the author routes it, not accepts it
            self._unreviewed(task, own[1])
            return ""
        if fed_at and now() - fed_at < self._refeed_max() and not foreign_comment(comments, self.agent_id, fed_at):
            return "review: already reviewed; waits for a new comment"
        if (own and not foreign_comment(comments, self.agent_id, own[1])
                and not self._ship_since(task, comments, own[1])):
            self._unreviewed(task, own[1])         # fed again later: still its own, still unreviewed
        return ""

    def _refeed_max(self) -> dt.timedelta:
        hours = getattr(self.cfg, "review_refeed_hours", 0) or 0
        return dt.timedelta(hours=hours) if hours > 0 else REVIEW_REFEED_MAX

    def _own_wait_pending(self, task: dict, last: dict | None, worked: dt.datetime) -> bool:
        """The agent's last comment of its work turn ends in a ⏳ WAIT (pipeline, mr, card,
        decision or time) and is still within its bound. The caller has already seen ``_review_event`` return nothing, so the wait is not over."""
        lines = declaration_lines((last or {}).get("body") or "")
        if not lines:
            return False
        try:
            declared = self._parse(lines[-1], parse_time(last["created_at"]) or now(), task["id"][:8])
        except Exception:
            return False
        if not (declared and declared.is_wait):
            return False
        if typed_wait(declared.extra):
            return True                  # only its explicit predicate or a real external event ends it
        if declared.kind == "time":
            return True                  # _review_event wakes the author when the moment passes
        # _review_event cannot see a pipeline/mr that was already final when the wait was declared,
        # nor anything without a GitLab client: those holds end after a day, never stand for ever
        return now() - worked < self._refeed_max()

    def _review_gate_refused(self, task: dict, reason: str) -> None:
        """The start gate refused a review feed (reviewer absence or unavailable QA infrastructure).
        A card whose QA cannot start is not retried every few minutes for ever:
        refusals are timed from the first one; past ``review_gate_hold_minutes`` the card gets one
        notice and is left alone until someone else writes on it (assigning a reviewer does)."""
        minutes = self.cfg.review_gate_hold_minutes
        if minutes <= 0:
            return
        infra = self._qa_infra_refusal(reason)
        if not infra and not re.search(self.cfg.review_gate_hold_pattern, reason):
            # a quota or capacity hold is not "no reviewer can take it" and breaks the streak
            self.store.mark(self.agent.name, task["id"], "review_gate_hold", "")
            return
        retry = self.cfg.review_unstartable_retry_minutes
        held = self._gate_hold.get(task["id"])
        if retry > 0 and held:
            # a no-reviewer gate hold must not outlast the review's own retry moment
            self._gate_hold[task["id"]] = (held[0], min(held[1], time.monotonic() + retry * 60), *held[2:])
        first = self.store.mark(self.agent.name, task["id"], "review_gate_hold")
        last = parse_time(first[1]) if first else None
        since = parse_time(first[0]) if first and first[0] else None
        if since is None or last is None or now() - last > REVIEW_REFUSAL_GAP:
            self.store.mark(self.agent.name, task["id"], "review_gate_hold", iso())
            self.store.mark(self.agent.name, task["id"], "review_gate_hold_n", "1")
            return
        self.store.mark(self.agent.name, task["id"], "review_gate_hold", first[0])     # refreshes the last-refusal time
        n = self.store.mark(self.agent.name, task["id"], "review_gate_hold_n")
        refusals = (int(n[0]) if n and n[0].isdigit() else 1) + 1
        self.store.mark(self.agent.name, task["id"], "review_gate_hold_n", str(refusals))
        today = now().date().isoformat()
        told = self.store.mark(TASK_WIDE, task["id"], "review_unstartable_told")
        if not (told and told[0] == today and refusals >= REVIEW_REFUSALS_AFTER_NOTICE):
            # the card has said its piece today: a comment buys two more refusals, not a whole window
            if now() - since < dt.timedelta(minutes=minutes):
                return
        if self.store.claim(TASK_WIDE, task["id"], "review_unstartable_told", today):
            retry = self.cfg.review_unstartable_retry_minutes
            try:
                explanation = ("QA infrastructure could not verify this review" if infra
                               else "No reviewer could be started for this review")
                action = ("Restore artifact/provenance or routing availability; independence remains required. " if infra
                          else "It needs a reviewer other than its author; ")
                cadence = (f"at most every {int(retry)} min" + ("" if infra else ", and only when the card changed,")
                           if retry > 0 else "only")
                self.mesh.add_comment(task["id"], f"[runner] {explanation} in {int(minutes)} min ({reason[:160]}). "
                                      + action + "The runner tries again " + cadence
                                      + " or as soon as someone comments on the card.")
            except MeshError as e:
                log.warning("%s: comment on #%s failed: %s", self.agent.name, task["id"][:8], e)
                self.store.mark(TASK_WIDE, task["id"], "review_unstartable_told", "")   # unclaimed
                return                    # no hold without the notice: the next refusal tries again
        self.store.mark(self.agent.name, task["id"], "review_unstartable", reason[:200])
        self.store.mark(self.agent.name, task["id"], "review_unstartable_sig", self._review_state_sig(task))

    @staticmethod
    def _hours_since(since: dt.datetime) -> int:
        return int((now() - since).total_seconds() // 3600)

    def _unreviewed(self, task: dict, since: dt.datetime) -> None:
        self._review_why[task["id"]] = {"event": "", "since": iso(since),
                                        "unreviewed_hours": self._hours_since(since)}

    def _family_hold(self, task: dict) -> str:
        """Epics and parents: an umbrella whose open children all sit parked is not
        fed; a parent is fed again only when a child changed status or someone wrote."""
        if not int(task.get("subtask_count") or 0):
            return ""
        try:
            kids = [k for k in self.mesh.subtasks(task["id"]) if self.cats.category(k) in OPEN_CHILD]
        except MeshError:
            return ""                         # the feeder fails open here too
        if not kids:
            return ""
        if all(k.get("delegation_level") == "supervised" or k.get("human_gate")
               or self.cats.category(k) == "backlog" for k in kids):
            return "umbrella: all open children are parked, gated or in backlog"
        print_ = ",".join(sorted(f"{k['id'][:8]}:{k.get('status_id')}" for k in kids))
        prev = self.store.mark(self.agent.name, task["id"], "children")
        if prev and prev[0] == print_:
            since = parse_time(prev[1])
            if since and not foreign_comment(self._comments(task["id"]), self.agent_id, since):
                return f"parent waits on {len(kids)} open children; none changed since the last feed"
        return ""

    def _hook_env(self, task_id: str, mode: str, phase: str, attempt: Attempt | None = None,
                  gate: dict | None = None) -> dict:
        """Hooks get the allowlisted environment (no runner or lane secrets), what the
        task and turn are, the Mesh checkout (writer identity) once there is one, and
        everything the gate said so far as MESH_RUNNER_GATE_<KEY>."""
        env = {k: v for k, v in os.environ.items() if k in ENV_PASSTHROUGH or k.startswith("LC_")}
        env.update(MESH_RUNNER_AGENT=self.agent.name, MESH_RUNNER_AGENT_ID=self.agent_id,
                   MESH_RUNNER_TASK=task_id, MESH_RUNNER_MODE=mode, MESH_RUNNER_PHASE=phase,
                   MESH_RUNNER_HARNESS=attempt.harness if attempt else self.agent.harness,
                   MESH_RUNNER_ATTEMPT=attempt.id if attempt else "")
        co = (attempt.checkout if attempt else None) or {}
        if co.get("request_id"):
            env.update(MESH_RUNNER_CHECKOUT_REQUEST=str(co["request_id"]),
                       MESH_RUNNER_CHECKOUT_GENERATION=str(co.get("generation") or ""),
                       MESH_RUNNER_CHECKOUT_SESSION=str(co.get("session_id") or ""))
        for k, v in (gate or {}).items():
            env[f"MESH_RUNNER_GATE_{k}"] = v
        return env

    def _run_hook(self, argv: list[str], env: dict, timeout: int) -> tuple[int, str, str]:
        """(rc, stdout, stderr); rc -1 = could not run. Own process group, killed whole
        on timeout; no stdin."""
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, env=env, start_new_session=True)
        except (OSError, ValueError) as e:
            return -1, "", f"{type(e).__name__}: {e}"
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            interrupt_group(proc.pid, signal.SIGKILL)
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:      # a descendant that left the group holds the pipe
                pass
            return -1, "", "timed out"
        return proc.returncode, out, err

    @staticmethod
    def _kv(text: str) -> dict:
        out = {}
        for line in text.splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                if re.fullmatch(r"[A-Z][A-Z0-9_]{0,40}", k.strip()):
                    out[k.strip()] = v.strip()
        return out

    def _gate(self, phase: str, task_id: str, mode: str, attempt: Attempt | None = None,
              gate: dict | None = None) -> tuple[str, dict]:
        """One phase of the operator's start gate: `reserve` (before a session starts or
        resumes), `admit` (the Mesh checkout is held: the writer is known), `renew`
        (heartbeat of a running turn). ('', merged outputs) = go; (reason, gate) = hold."""
        gate = dict(gate or {})
        self._gate_task_only = False
        if not self.agent.start_gate_command:
            return "", gate
        if phase == "reserve" and self._agent_gate_hold:
            reason, until, source = self._agent_gate_hold
            if source in self._gate_hold and time.monotonic() < until:
                return reason, gate
        rc, out, err = self._run_hook(self.agent.start_gate_command,
                                      self._hook_env(task_id, mode, phase, attempt, gate), timeout=120)
        if rc == 0:
            gate.update(self._kv(out))
            if phase == "admit":
                self._clear_agent_gate_hold()
            return "", gate
        self._gate_task_only = rc == GATE_HOLD_TASK
        if rc in (GATE_HOLD, GATE_HOLD_TASK):
            reason = gate_hold_reason(out)
            gate.update(self._kv(out))
            if rc == GATE_HOLD and phase in ("reserve", "admit"):
                key = ("agent", reason)
                failures = min(self._gate_failures.get(key, 0) + 1, 4)
                self._gate_failures = {key: failures}
                until = time.monotonic() + min(GATE_HOLD_SECONDS * 2 ** (failures - 1), TASK_HOLD_SECONDS)
                self._agent_gate_hold = (reason, until, task_id)
                self._gate_hold[task_id] = (reason, until)
            return reason, gate
        log.warning("%s: start gate %s #%s rc=%s: %s", self.agent.name, phase, task_id[:8], rc,
                    (err or out).strip()[:200])
        return f"start gate failed ({phase}, rc {rc})", gate

    def _turn_end(self, task_id: str, mode: str, attempt: Attempt | None, gate: dict, outcome: str) -> None:
        """Tell the operator's hook a turn is over (release what the gate reserved)."""
        if not self.agent.turn_end_command or not gate:
            return
        env = self._hook_env(task_id, mode, "end", attempt, gate)
        env["MESH_RUNNER_OUTCOME"] = outcome
        rc, out, err = self._run_hook(self.agent.turn_end_command, env, timeout=60)
        if (rc == 0 and outcome == "turn-end" and self._agent_gate_hold
                and self._agent_gate_hold[0] == GATE_PUBLIC_REASONS["capacity"]):
            # A local writer released real capacity: ask the gate again, never
            # treat the event itself as authorization to start another writer.
            self._clear_agent_gate_hold()
        if rc != 0:
            log.warning("%s: turn-end hook #%s rc=%s: %s", self.agent.name, task_id[:8], rc,
                        (err or out).strip()[:200])

    def _remember_gate(self, a: Attempt, gate: dict) -> None:
        stale = self.store.mark(self.agent.name, a.task_id, "gate")
        if stale and stale[0]:
            self._release_gate(a, "superseded")      # never overwrite a live reservation
        if gate:
            self.store.mark(self.agent.name, a.task_id, "gate", json.dumps(gate))

    def _gate_of(self, a: Attempt) -> dict:
        got = self.store.mark(self.agent.name, a.task_id, "gate")
        try:
            return json.loads(got[0]) if got and got[0] else {}
        except ValueError:
            return {}

    def _log_hold(self, kind: str, task8: str, reason: str, episode: str = "") -> None:
        """Log a hold once per episode (re-logged every HOLD_RELOG_SECONDS), not on every
        GATE_HOLD_SECONDS re-check: a card held for an hour is one event, not thirty. An episode
        ends when the task gets past the check that held it (`_clear_hold_log`), so the next
        hold of the same task logs at once, even with the same reason."""
        key = (task8, episode or kind)
        now = time.monotonic()
        last_reason, last_at = self._hold_log_at.get(key, ("", -HOLD_RELOG_SECONDS))
        # a changed reason logs at once, also a return to an earlier one (A -> B -> A)
        if reason == last_reason and now - last_at < HOLD_RELOG_SECONDS:
            return
        self._hold_log_at[key] = (reason, now)
        for k in [k for k, (_, t) in self._hold_log_at.items() if now - t > 2 * HOLD_RELOG_SECONDS]:
            del self._hold_log_at[k]
        log.info("%s: #%s %s held: %s", self.agent.name, task8, kind, reason)

    def _clear_hold_log(self, task8: str, *episodes: str) -> None:
        """The task got past a hold check: forget that hold's log entries (all of the task's
        when no episode is named), so a later hold of the same task is a new episode."""
        for k in [k for k in self._hold_log_at if k[0] == task8 and (not episodes or k[1] in episodes)]:
            del self._hold_log_at[k]

    def _release_gate(self, a: Attempt, outcome: str) -> None:
        """Release what the gate holds for this task, once (the mark is cleared first)."""
        gate = self._gate_of(a)
        if not gate:
            return
        self.store.mark(self.agent.name, a.task_id, "gate", "")
        self._turn_end(a.task_id, a.mode, a, gate, outcome)

    def _renew_gate(self, a: Attempt) -> None:
        gate = self._gate_of(a)
        if gate:
            _, merged = self._gate("renew", a.task_id, a.mode, a, gate)
            if merged != gate:
                self.store.mark(self.agent.name, a.task_id, "gate", json.dumps(merged))

    def _clear_agent_gate_hold(self) -> None:
        if self._agent_gate_hold:
            self._gate_hold.pop(self._agent_gate_hold[2], None)
        self._agent_gate_hold = None
        self._gate_failures.clear()

    def _gate_event(self, task: dict) -> None:
        """An update of the refused task lifts its cached gate for a fresh decision."""
        held = self._gate_hold.get(task["id"])
        if held and len(held) >= 3 and held[2] != (task.get("updated_at") or ""):
            self._gate_hold.pop(task["id"], None)
            if self._agent_gate_hold and self._agent_gate_hold[2] == task["id"]:
                self._clear_agent_gate_hold()

    def hold_reason(self, task: dict, mode: str, log_change: bool = True) -> str:
        """Every reason not to start this task now ('' = start). Shared by the
        loop and by `plan`, so the dry run says exactly what the loop does. A new
        reason is logged once (gate holds log themselves), so audits see why a task
        sits: dependencies, a park, an umbrella, a review cooldown."""
        reason = self._hold_reason(task, mode, reconcile=log_change)
        if log_change:
            tid = task["id"]
            if not reason:
                self._hold_logged.pop(tid, None)
            elif self._hold_logged.get(tid) != reason:
                self._hold_logged[tid] = reason
                if not reason.startswith("gate: "):
                    log.info("%s: #%s held: %s", self.agent.name, tid[:8], reason[:200])
        return reason

    def _hold_reason(self, task: dict, mode: str, reconcile: bool = False) -> str:
        if reason := self.pool.disk_hold():
            return reason
        if mode == "review" and task.get("human_gate"):
            # seen while it holds the task: the author's own question is answered when it is
            # lifted (_review_event), and nothing else reads the gate of a held review card
            seen = self.store.mark(self.agent.name, task["id"], "gate_seen")
            if not seen or now() - (parse_time(seen[1]) or now()) > dt.timedelta(minutes=10):
                self.store.mark(self.agent.name, task["id"], "gate_seen", "1")
        reason = static_skip_reason(task, self.agent.policy, now())
        if (reason.startswith("start_after ") and reconcile
                and self.effects.reconcile_orphan_schedule(task)):
            # the schedule was left by a wait that has ended (ours or another lane's): gone now
            task = dict(task, start_after=None)
            reason = static_skip_reason(task, self.agent.policy, now())
        if reason:
            return reason
        self._gate_event(task)
        if self._agent_gate_hold:
            reason, until, source = self._agent_gate_hold
            if source in self._gate_hold and time.monotonic() < until:
                return f"gate: {reason}"
        held = self._gate_hold.get(task["id"])
        if held and time.monotonic() < held[1] and (len(held) < 3 or held[2] == (task.get("updated_at") or "")):
            return f"gate: {held[0]}"
        if mode == "work" and self.cfg.requeue_on_cap:
            waiting = self._cap_wait(task["id"])
            if waiting and not self._inputs_changed(task, waiting):
                return f"{WAITING_FOR_INPUT}{waiting.get('kind') or 'cap'}"
        observed = self._cap_observation(task["id"])
        event_ready = bool(observed and observed.wait.get("cap_wake"))
        if observed and not event_ready:
            if observed.wait.get("cap_override_consumed"):
                return ("observed WAIT; instruction reserved for attempt "
                        f"{observed.wait['cap_override_consumed']}; ambiguous spawn outcome requires writer verification")
            return "daily turn budget spent; observed WAIT unresolved; waits for its event or addressed project lead instructions"
        failed = [(parse_time(a.updated_at), a.note or "") for a in self.store.by_state(self.agent.name, ("failed",))
                  if a.task_id == task["id"]]
        failed = [f for f in failed if f[0]]
        budget_until = self._budget_backoff.get(task["id"])
        if budget_until is not None and (self._task_backoff.get(task["id"]) != budget_until
                                         or time.monotonic() >= budget_until):
            self._budget_backoff.pop(task["id"], None)        # expired or replaced by another backoff
            budget_until = None
        if budget_until is not None and failed:
            at, note = max(failed, key=lambda f: f[0])
            # a lead that could not take a spent-budget task kept it until tomorrow: a new
            # comment releases that the same way as the no-lead hold below
            if note.startswith(DAILY_CAP_NOTES) and self._new_comment_since(task["id"], at):
                self._task_backoff.pop(task["id"], None)
                self._budget_backoff.pop(task["id"], None)
        if time.monotonic() < self._task_backoff.get(task["id"], 0):
            if task["id"] in self._budget_backoff and event_ready:
                pass                         # the event lifts only this budget backoff
            elif task["id"] in self._budget_backoff:          # still its own deadline (checked above)
                return "daily turn budget spent; waits for tomorrow (UTC) or a new comment"
            elif not self._backoff_lifted(task):
                return "backing off after a failed start"
        elif task["id"] in self._backoff_inputs:
            self._backoff_inputs.pop(task["id"], None)         # over: its record goes with it
            self._backoff_checked.pop(task["id"], None)
        if failed:
            at, note = max(failed, key=lambda f: f[0])
            if note.startswith(DAILY_CAP_NOTES):
                # the per-day budget is spent: a restart today only runs one turn into the same
                # cap (no lead to take it). It waits for the next UTC day or a new comment; after
                # either, no cooldown applies and the other holds below still decide.
                if not event_ready and at.date() == now().date() and not self._new_comment_since(task["id"], at):
                    return "daily turn budget spent; waits for tomorrow (UTC) or a new comment"
            elif self._failed_recently(task["id"], at):
                return "failed recently; waits for a new comment"
        cached = self._hold_cache.get(task["id"])
        if cached and cached[0] == (task.get("updated_at") or ""):
            # A blocker closing, a child moving or a merge request changing does not touch
            # this task's updated_at: dependency and own-work verdicts are re-read after
            # 2 min, other review/epic verdicts after 10.
            ttl = 120 if cached[2].startswith(("blocked by", "review: the agent's own work",
                                                "dependencies unreadable")) else 600
            if (time.monotonic() - cached[1] < ttl
                    and time.monotonic() < self._hold_deadline.get(task["id"], float("inf"))):
                return cached[2]
        self._hold_deadline.pop(task["id"], None)
        self._hold_cache.pop(task["id"], None)   # re-read now: an empty verdict must not leave a stale hold
        if mode == "review":
            verdict = self._review_hold(task)
        else:
            try:
                blockers = blocking_tasks(self.mesh, self.cats, task["id"])
            except MeshError as e:
                verdict = f"dependencies unreadable ({e.status})"   # cached below: do not ask each pass
                self._hold_cache[task["id"]] = (task.get("updated_at") or "", time.monotonic(), verdict)
                return verdict
            verdict = self._blocked_verdict(blockers) if blockers else self._family_hold(task)
        if verdict:
            self._hold_cache[task["id"]] = (task.get("updated_at") or "", time.monotonic(), verdict)
        return verdict

    def _blocked_verdict(self, blockers: list[dict]) -> str:
        """``blocked by …``; a blocker in backlog comes first and is named as such:
        nobody feeds backlog, so this task would otherwise wait without a word."""
        def in_backlog(b: dict) -> bool:
            try:
                return self.cats.category(b) == "backlog"
            except MeshError:
                return False
        parked = [b for b in blockers if in_backlog(b)]
        ids = ", ".join(b["id"][:8] for b in parked + [b for b in blockers if b not in parked])
        return (BACKLOG_BLOCKER if parked else "blocked by ") + ids

    def candidates(self) -> list[dict]:
        held = {a.task_id for a in self.store.by_state(self.agent.name, ACTIVE + ("waiting", "ready"))}
        out = []
        if self.agent.feed_review:
            out += [dict(t, _mode="review") for t in order(self.mesh.my_tasks("review")) if t.get("id") not in held]
        work = []
        for cat in ("in_progress", "todo"):
            work += [t for t in self.mesh.my_tasks(cat) if t.get("id") not in held]
        out += [dict(t, _mode="work") for t in self._work_order(work)]
        for task in out:
            self._gate_event(task)
        return out

    def _work_order(self, work: list[dict]) -> list[dict]:
        """Priority order, with decision requests addressed to this agent first (an answer frees
        the asking card) and tasks that wait for new input after a cap last (they hold no slot and
        must not stand in front of ready work)."""
        ordered = order(work)
        waiting = set(self.store.marked(self.agent.name, CAP_WAIT)) if self.cfg.requeue_on_cap else set()

        def rank(t: dict) -> int:
            if t.get("id") in waiting:
                return 2
            return 0 if DECISION_REQUEST_LABEL in (t.get("labels") or []) else 1
        return sorted(ordered, key=rank)             # stable: priority order kept within a rank

    def start_new(self, room: int) -> int:
        self._pool_hold = ""                   # only what this tick measured
        if room <= 0:
            return room
        if self.paused() or time.monotonic() < self._starts_paused_until:
            self._break_review_streaks()
            return room
        if self.memgate.check():
            self._break_review_streaks()
            return room                        # host short of memory: no new sessions
        if self.pool.disk_hold():
            self._pool_full()
            self._break_review_streaks()
            return room
        reviews = 0
        for task in self.candidates():
            if room <= 0 or time.monotonic() < self._starts_paused_until:
                self._break_review_streaks()
                break                          # a start just paused the agent (workspace/identity)
            if self._pool_full() and not metadata_only(task, task.get("_mode", "work")):
                self._break_review_streaks()
                continue                       # a later typed decision may need no Git slot
            review = task.get("_mode") == "review"
            if review and reviews >= self.cfg.review_starts_per_tick:
                continue
            self._start_held = False
            self._gate_asked = False
            try:
                started = self._start_one(task)
                if review and (started or self._gate_asked):
                    # the per-tick review slot is spent by a review the gate was asked about, not by
                    # one held locally: a held review first in the list would otherwise take the only
                    # slot every tick, and the reviews after it would never even be tried
                    reviews += 1
                if started:
                    room -= 1
                elif self._start_held:
                    break                          # the gate holds (pool full, …): do not ask per task
            except Exception as e:
                if review and self._gate_asked:
                    reviews += 1                   # it reached the gate: the slot is spent
                log.warning("%s: start #%s failed: %s", self.agent.name, task["id"][:8], e)
                self._backoff_on_input(task, time.monotonic() + self.cfg.abort_backoff_minutes * 60)
        return room

    def _break_review_streaks(self) -> None:
        """A hold of the whole runner (quota pause, memory, full worktree pool) means the start gate was
        not asked this tick: refusals on either side of it are not consecutive, so the streaks start over."""
        if self.cfg.review_gate_hold_minutes > 0:
            self.store.clear_marks(self.agent.name, "review_gate_hold")

    def _pool_full(self) -> bool:
        """Every worktree of a repo is leased: open tasks of this agent hold them, and waiting
        ones do not count against the cap. A start now would check a task out only to abort it
        and ask an operator to clean slots that are in use; hold quietly until one frees."""
        full = [r.name for r in self.agent.repos if self.pool.full(r.name)]
        hold = self.pool.disk_hold()
        if full and not hold:
            held = len(self.store.by_state(self.agent.name, ACTIVE + ("waiting", "ready")))
            hold = f"worktree pool full: {', '.join(full)} ({held} open tasks hold them)"
        if bool(hold) != self._pool_hold_logged:        # log the edges, not every tick
            self._pool_hold_logged = bool(hold)
            log.info("%s: starts %s", self.agent.name, f"held: {hold}" if hold else "resume: a worktree is free")
        self._pool_hold = hold
        return bool(hold)

    def _start_one(self, task: dict) -> bool:
        backoff = time.monotonic() + self.cfg.abort_backoff_minutes * 60
        mode = task.get("_mode", "work")
        if self.hold_reason(task, mode):
            return False
        cap = self.cfg.daily_start_cap
        # review feeds have their own refeed rule (_review_hold); the cap is for work starts
        # a decision request is reopened per question of its requester: its starts are answers, not a loop
        if (cap and mode != "review" and DECISION_REQUEST_LABEL not in (task.get("labels") or [])
                and self.store.starts_today(self.agent.name, task["id"]) >= cap):
            # a capped task that got past hold_reason has new input: one more start, then the cap again
            if not (self.cfg.requeue_on_cap and self._cap_wait(task["id"])):
                # the fingerprint first, the comment check second: a comment between them is seen by
                # the check, one after them changes the fingerprint and wakes the task
                fp = self._fingerprint_or_none(task) if self.cfg.requeue_on_cap else None
                if fp is not None and self._unseen_input(task):
                    log.info("%s: #%s start cap not applied: a comment arrived after the last session",
                             self.agent.name, task["id"][:8])
                else:
                    self._start_cap(task, cap, fp)
                    return False
        self._gate_task_only = False
        self._gate_asked = True
        reason, gate = self._gate("reserve", task["id"], mode)
        if reason:
            self._turn_end(task["id"], mode, None, gate, "held")
            # a hold of this one task (a human gate on it) must not stop the agent's other starts;
            # it stands until the task changes (an answer lifts the gate) or 10 min pass
            ttl = TASK_HOLD_SECONDS if self._gate_task_only else GATE_HOLD_SECONDS
            until = (self._agent_gate_hold[1] if not self._gate_task_only and self._agent_gate_hold
                     else time.monotonic() + ttl)
            self._gate_hold[task["id"]] = (reason, until, task.get("updated_at") or "")
            self._start_held = not self._gate_task_only
            self._log_hold("start", task["id"][:8], reason)
            if mode == "review":
                self._review_gate_refused(task, reason)
            return False
        self._clear_hold_log(task["id"][:8], "start")
        try:
            a = self.store.create(self.agent.name, task["id"], self.agent.harness, task.get("title", ""), mode=mode)
        except sqlite3.IntegrityError:
            self._turn_end(task["id"], mode, None, gate, "not-started")
            return False
        self._remember_gate(a, gate)
        if self.cfg.requeue_on_cap and self._cap_wait(task["id"]):
            self.store.mark(self.agent.name, task["id"], CAP_WAIT, "")      # the new input is being worked
        try:
            started = self._start_attempt(task, mode, a, backoff)
        except BaseException:
            cur = self.store.get(a.id)
            if not (cur and cur.pid):
                self._release_gate(a, "not-started")
            raise
        if not started:
            self._release_gate(a, "not-started")
        return started

    def _start_attempt(self, task: dict, mode: str, a: Attempt, backoff: float) -> bool:
        if not self._acquire(a):
            self.store.update(a, state="aborted", note="checkout refused")
            self._task_backoff[task["id"]] = backoff
            return False
        try:
            # The pool fetches with the agent's own identity. Reject a known auth
            # hold before taking worktrees or changing product status, including
            # tasks that have never run (and therefore have no progress to park).
            env = self._fresh_env()
            blocked = self._auth_blocked(a, env)
            if blocked:
                self._finish(a, "aborted", f"authentication held for profile {blocked}; no turn started")
                self._task_backoff[task["id"]] = time.monotonic() + GATE_HOLD_SECONDS
                self._start_held = True
                return False
            # Labels can change between listing and checkout. Only a fresh,
            # still-owned typed request may skip repository admission.
            task = self._task(a.task_id)
            if not self._still_ours(task, mode):
                self._finish(a, "completed", "task left the agent before starting")
                return False
            if self.agent.policy.human_gate and task.get("human_gate"):
                self._finish(a, "aborted", "waiting for a human decision")
                return False
            no_workspace = metadata_only(task, mode)
            if not no_workspace and any(self.pool.full(r.name) for r in self.agent.repos):
                self._finish(a, "aborted", "workspace admission held after scope refresh")
                self._task_backoff[a.task_id] = time.monotonic() + GATE_HOLD_SECONDS
                return False
            if no_workspace:
                self.store.mark(self.agent.name, a.task_id, RECEIPT_KEY,
                                json.dumps({"attempt": a.id, "scope": "metadata-only"}))
            paths = [] if no_workspace else [str(self.pool.lease(a.id, r.name)) for r in self.agent.repos]
        except MeshError:
            self._finish(a, "aborted", "task unreadable after checkout; no turn started")
            self._task_backoff[a.task_id] = backoff
            return False
        except WorkspaceError as e:
            self._finish(a, "aborted", f"workspace: {e}")
            if not str(e).startswith("no free worktree"):
                # fetch/auth/disk: an operator problem of the agent, not of this task —
                # stop starting anything for a while, say it in status, write nothing on tasks
                self._starts_paused_until = backoff
                self._start_error = f"workspace: {str(e)[:240]}"
                log.warning("%s: starts paused %d min: %s", self.agent.name, self.cfg.abort_backoff_minutes, e)
                return False
            self._task_backoff[task["id"]] = backoff
            if not self.store.recent(self.agent.name, task["id"], "aborted-notified", 86400):
                self._comment(task["id"], f"cannot start: {e}. An operator must clean or free worktrees "
                                          "(`mesh-runner slots`).")
                self.store.event(a.id, "notified", "pool full")
                self.store.update(a, state="aborted-notified")
            return False
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:   # env command refused/missing/hung
            self._finish(a, "aborted", str(e)[:300])
            self._starts_paused_until = backoff
            self._start_error = str(e)[:240]
            log.warning("%s: starts paused %d min: %s", self.agent.name, self.cfg.abort_backoff_minutes, e)
            return False
        self._start_error = ""
        self.store.update(a, workspaces=paths)
        a = self.store.get(a.id) or a
        reason, gate = self._gate("admit", a.task_id, mode, a, self._gate_of(a))
        if gate:
            self.store.mark(self.agent.name, a.task_id, "gate", json.dumps(gate))
        if reason:
            self._gate_hold[task["id"]] = (reason, time.monotonic() + GATE_HOLD_SECONDS)
            self._start_held = True
            self._log_hold("start at admit", a.task8, reason)
            self._finish(a, "aborted", f"held: {reason}")
            if mode == "review":
                self._review_gate_refused(task, reason)
            return False
        self._clear_hold_log(a.task8)                 # started: every hold episode of this task is over
        # a `model:<name>` label picks the model for every turn of this task
        model = next((str(l)[6:].strip() for l in task.get("labels") or [] if str(l).startswith("model:")), "")
        if model and not MODEL_NAME.fullmatch(model):
            log.warning("%s: #%s label model:%s ignored: not a model name", self.agent.name, a.task8, model[:40])
            model = ""
        if model or self.store.mark(self.agent.name, task["id"], "model"):
            self.store.mark(self.agent.name, task["id"], "model", model)
        # an `effort:<level>` label sets the reasoning effort the same way
        effort = next((str(l)[7:].strip().lower() for l in task.get("labels") or []
                       if str(l).startswith("effort:")), "")
        if effort and effort not in EFFORTS:
            log.warning("%s: #%s label effort:%s ignored: not one of %s", self.agent.name, a.task8,
                        effort[:20], ",".join(EFFORTS))
            effort = ""
        if effort or self.store.mark(self.agent.name, task["id"], "effort"):
            self.store.mark(self.agent.name, task["id"], "effort", effort)
        if mode != "review":
            self._mark_in_progress(task)
        changed = self._review_why.pop(task["id"], None) if mode == "review" else None   # dict or None
        if changed and changed.get("ci_delivery"):
            self.store.update(a, wait={**(a.wait or {}), "ci_delivery": changed["ci_delivery"]})
        observed = self._cap_observation(task["id"])
        prompt = self._first_prompt(task, mode, paths, changed)
        before_spawn = None
        if observed and observed.wait.get("cap_wake"):
            wait = dict(observed.wait)
            event = wait.pop("cap_wake")
            override = wait.get("cap_override") if not wait.get("cap_override_consumed") else None
            if override:
                # Reserve this exact instruction for the new attempt before
                # spawning. A crash cannot replay it into another attempt.
                consumed = dict(observed.wait)
                consumed.pop("cap_wake", None)
                consumed["cap_override_consumed"] = a.id
                def before_spawn():
                    self.store.update(observed, wait=consumed)
                    self.store.event(observed.id, "cap_override_reserved", a.id)
                prompt += ("\n\nProject lead changed instructions (this does not mean the awaited target finished)."
                           f"\nComment {override['comment_id']} by {override['author_id']}:\n{override['body']}")
            else:
                prompt += "\n\n" + prompts.wake_prompt(task, event)
            self.store.update(a, wait={**(a.wait or {}), **wait})
        if not self._spawn(a, prompt, resume=False, expect=("starting",),
                           **({"before_spawn": before_spawn} if before_spawn else {})):
            # A sibling could have established the hold after our preflight.
            # Nothing ran, so return these fresh leases instead of filling the pool.
            self._finish(a, "aborted", a.note)
            self._task_backoff[task["id"]] = time.monotonic() + GATE_HOLD_SECONDS
            return False
        if observed:
            self.store.mark(self.agent.name, task["id"], "cap_observation", "")
        # marks only once the session really started: a failed start must not hold the task
        if mode == "review":
            self.store.mark(self.agent.name, task["id"], "review_fed", a.id)
            self.store.mark(self.agent.name, task["id"], "review_gate_hold", "")
            self.store.mark(self.agent.name, task["id"], "review_unstartable", "")
            self.store.mark(self.agent.name, task["id"], "review_unstartable_sig", "")
            self.store.mark(self.agent.name, task["id"], "review_gate_hold_n", "")
        elif int(task.get("subtask_count") or 0):
            self._remember_children(task)
        self._hold_cache.pop(task["id"], None)
        return True

    def _remember_children(self, task: dict) -> None:
        try:
            kids = [k for k in self.mesh.subtasks(task["id"]) if self.cats.category(k) in OPEN_CHILD]
            self.store.mark(self.agent.name, task["id"], "children",
                            ",".join(sorted(f"{k['id'][:8]}:{k.get('status_id')}" for k in kids)))
        except MeshError:
            pass

    def _mark_in_progress(self, task: dict) -> None:
        if self.cats.category(task) != "todo":
            return
        try:
            target = next((s for s in self.mesh.statuses(task["project_id"]) if s.get("category") == "in_progress"),
                          None)
            if target:
                self.mesh.move(task["id"], target["id"])
        except MeshError as e:
            log.warning("%s: move #%s: %s", self.agent.name, task["id"][:8], e)

    def recheck_slots(self) -> None:
        if time.monotonic() - self._last_recheck < 600:
            return
        self._last_recheck = time.monotonic()
        open_ids = {a.id for a in self.store.by_state(self.agent.name, ACTIVE + ("waiting", "ready"))}
        try:
            self.pool.audit_admin()             # holds and logs free slots whose admin link went bad
        except Exception as e:
            log.warning("%s: worktree admin audit: %s", self.agent.name, e)
        try:
            for path in self.pool.recheck_held(open_ids):
                log.info("%s: worktree %s is clean again, returned to the pool", self.agent.name, path)
        except Exception as e:
            log.warning("%s: worktree recheck: %s", self.agent.name, e)
        try:
            self.free_dead_slots()
        except Exception as e:
            log.warning("%s: dead worktree pass: %s", self.agent.name, e)

    def _dead_slot_reason(self, slot: dict) -> str:
        """Why a held slot's work can no longer resume in it; '' when it may still matter or cannot
        be told. A held slot is never leased again (leases take free rows only, and a WAIT restores
        into a fresh lease), so only its attempt's own task decides whether anyone will want it."""
        a = self.store.get(slot["attempt"]) if slot["attempt"] else None
        if a is None or a.agent != self.agent.name or a.state not in FINISHED_ATTEMPT:
            return ""
        try:
            task = self._task(a.task_id)
            if task is None:
                return "task no longer exists"
            category = self.cats.category(task)
        except MeshError:
            return ""                                   # unreadable now: decide on a later pass
        if category in ("done", "cancelled"):
            return f"task is {category}"
        assignee = task.get("assignee_id") or (task.get("assignee") or {}).get("id")
        if assignee != self.agent_id:
            return "task is no longer assigned to this agent"
        # still the agent's: an open attempt on the task (waiting, ready, running) is the one that
        # resumes it, and nothing here may disturb its context
        if self.store.open_for_task(self.agent.name, a.task_id):
            return ""
        hours = self.cfg.dead_slot_hours
        held_since = parse_time(slot["updated_at"])
        if hours > 0 and held_since and now() - held_since >= dt.timedelta(hours=hours):
            newer = [b for b in self.store.on_task(a.task_id)
                     if b.agent == self.agent.name and b.id != a.id and b.created_at > a.created_at]
            if not newer:
                return f"held for over {hours:g}h with no newer attempt on the task"
        return ""

    def free_dead_slots(self) -> list[str]:
        """Free held slots left dirty by finished attempts whose work cannot resume there, after
        saving the work on a never-pushed ref. Each slot is looked at once an hour, a few per pass;
        a slot whose work cannot be saved stays held."""
        freed, budget, at = [], DEAD_SLOT_PER_TICK, time.monotonic()
        for slot in self.pool.rows():
            if budget <= 0:
                break
            path = slot["path"]
            if slot["state"] != "held" or slot["note"] != DIRTY_HOLD_NOTE or not slot["attempt"]:
                self._dead_slot_seen.pop(path, None)
                continue
            if at - self._dead_slot_seen.get(path, -DEAD_SLOT_RECHECK) < DEAD_SLOT_RECHECK:
                continue
            self._dead_slot_seen[path] = at
            budget -= 1
            why = self._dead_slot_reason(slot)
            if not why:
                continue
            try:
                ref = self.pool.archive_and_free(path, expect_attempt=slot["attempt"])
            except WorkspaceError as e:
                log.warning("%s: worktree %s (attempt %s, task #%s) stays held: %s; %s", self.agent.name, path,
                            slot["attempt"][:8], (self.store.get(slot["attempt"]).task_id or "")[:8], why, e)
                continue
            freed.append(path)
            log.info("%s: worktree %s returned to the pool (%s): attempt %s, task #%s, uncommitted work saved as %s",
                     self.agent.name, path, why, slot["attempt"][:8],
                     self.store.get(slot["attempt"]).task_id[:8], ref or "(nothing to save)")
        return freed

    # ------------------------------------------------------------------ tick
    def refresh_me(self) -> None:
        try:
            fresh = self.mesh.me()
            self.me = fresh.get("agent", fresh)
            self.effects.agent_id = self.agent_id
        except MeshError:
            if not self.me:
                raise

    def keep_chat(self) -> None:
        """The agent's standing chat session (a bridge types into it), once a minute."""
        if self.chat is None or time.monotonic() < self._chat_due:
            return
        self._chat_due = time.monotonic() + CHAT_CHECK_SECONDS

        def env_of():
            env = self._fresh_env()
            if self._routed_harness != self.agent.harness:
                return env, None              # the chat runs the configured harness: no routing compare now
            return env, env_digest({**env, "MESH_RUNNER_PROFILE": str(getattr(self.adapter, "profile", ""))})
        try:
            outcome = self.chat.check(env_of)
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            self._chat_error = f"chat session check failed: {type(e).__name__}: {str(e)[:160]}"
            log.warning("%s: %s", self.agent.name, self._chat_error)
            return
        self._chat_error = self.chat.error
        self._chat_hold = ("restart pending since " + time.strftime("%H:%M", time.localtime(self.chat.held_since))
                           if self.chat.held_since else "")

    def tick(self) -> dict:
        self.refresh_me()
        self.heartbeat()
        self.settle_finished()
        self.reap_interactive()
        self.check_wakes()
        workspace_requests.consume(self.cfg.state_dir, self.agent.name, self._workspace_request)
        self.deliver_mentions()
        room = self.cap() - self.store.active_count(self.agent.name)
        room = self.resume_ready(room)
        self.start_new(room)
        self.recheck_slots()
        attempts = self.store.by_state(self.agent.name, ACTIVE + ("waiting", "ready"))
        for a in attempts:
            self.effects.taken(a.task_id)
        self.effects.publish(attempts)
        self.publish_skips()
        self.keep_chat()                           # last: it may wait on the env command and tmux
        return self.status()

    def publish_skips(self) -> None:
        """Show on each task the agent does not run why it waits (feeder parity):
        the hold reason, or its place in the queue when only capacity holds it."""
        if not self.cfg.mesh_effects or time.monotonic() < self._skips_due:
            return
        self._skips_due = time.monotonic() + SKIP_EVERY.total_seconds()
        try:
            tasks = self.candidates()
        except MeshError as e:
            log.warning("%s: skip states: %s", self.agent.name, e)
            return
        self._queue_seen = (len(tasks), iso())     # board services read how much work waits
        paused = self.paused()
        states, keep, queued = {}, set(), 0
        # bounded: the first SKIP_EVALUATE in feed order are evaluated (each may read
        # dependencies/comments); the rest keep whatever they last showed
        for i, t in enumerate(tasks):
            if i >= SKIP_EVALUATE:
                keep.add(t["id"])
                continue
            reason = "paused" if paused else self.hold_reason(t, t.get("_mode", "work"))
            if not reason:
                queued += 1
                states[t["id"]] = {"kind": "queued", "lane": self.agent.name, "ref": f"position:{queued}"}
                continue
            s = skip_state(reason, self.agent.name)
            if s:
                states[t["id"]] = s
            elif reason.startswith("dependencies unreadable"):
                keep.add(t["id"])               # a read error: do not clear what it showed
        self.effects.publish_skips(states, keep)

    def plan(self) -> dict:
        """Dry run: what the next tick would do with each of the agent's tasks.
        Reads only; never checks out, leases or spawns."""
        self.refresh_me()
        held = {a.task_id: a.state for a in self.store.by_state(self.agent.name, ACTIVE + ("waiting", "ready"))}
        active = self.store.active_count(self.agent.name)
        rows = []
        tasks = ([dict(t, _mode="review") for t in self.mesh.my_tasks("review")] if self.agent.feed_review else [])
        for cat in ("in_progress", "todo"):
            tasks += [dict(t, _mode="work") for t in self.mesh.my_tasks(cat)]
        for t in tasks:
            reason = held.get(t["id"]) and f"attempt {held[t['id']]}"
            reason = reason or self.hold_reason(t, t["_mode"], log_change=False)
            rows.append({"task": t["id"][:8], "mode": t["_mode"], "priority": t.get("priority"),
                         "title": (t.get("title") or "")[:70],
                         "decision": f"skip: {reason}" if reason else "eligible"})
        eligible = sum(r["decision"] == "eligible" for r in rows)
        return {"agent": self.agent.name, "agent_id": self.agent_id, "cap": self.cap(), "active": active,
                "auth_holds": auth.status(self.store),
                "disk_hold": self.pool.disk_hold(),
                "memory_hold": self.memgate.describe() if self.memgate.check() else "",
                "would_start": min(eligible, max(0, self.cap() - active)), "tasks": rows}

    def status(self) -> dict:
        rows = self.store.by_state(self.agent.name, ACTIVE + ("waiting", "ready"))
        out = {"agent": self.agent.name, "cap": self.cap(), "paused": self.paused(),
               "auth_holds": auth.status(self.store),
               "attempts": [{"task": a.task8, "task_id": a.task_id, "state": a.state, "title": a.title[:60],
                             "session": a.session_id, "wait": a.wait.get("kind", ""), "harness": a.harness,
                             "turns": a.turns, "since": a.updated_at,
                             **({"wait_observation": a.wait.get("observation_state", "unknown")}
                                if typed_wait(a.wait) else {})} for a in rows]}
        if self._start_error and time.monotonic() < self._starts_paused_until:
            out["error"] = f"starts paused: {self._start_error}"   # alerting reads this field
        if self._queue_seen is not None:
            out["queue"], out["queue_at"] = self._queue_seen   # tasks it holds and does not run
        if self.memgate.holding:
            out["memory_hold"] = self.memgate.describe()
        if self._pool_hold:
            out["pool_hold"] = self._pool_hold
        if self._chat_error:
            out["chat_error"] = self._chat_error
        if self._chat_hold:
            out["chat_hold"] = self._chat_hold
        return out


STALL_TICKS = 10
STALL_MIN_SECONDS = 900.0              # a first fetch of a big repo may legitimately take minutes
STOP_GRACE_SECONDS = 60.0


class Runner:
    def __init__(self, cfg: RunnerConfig, lock: bool = True):
        self.cfg = cfg
        os.umask(0o077)                    # stream logs and the registry hold task content and tokens
        cfg.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = None
        if lock:
            self._lock = open(cfg.state_dir / "runner.lock", "w")
            try:
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AlreadyRunning(f"another runner owns {cfg.state_dir}") from None
        self.store = Store(cfg.state_dir / "runner.sqlite")
        gitlab = GitLab(cfg.gitlab_url, cfg.gitlab_token) if cfg.gitlab_url else None
        # one sqlite connection per agent: each agent ticks in its own thread (run_forever)
        self.agents = [AgentRunner(cfg, a, Store(cfg.state_dir / "runner.sqlite"), gitlab=gitlab)
                       for a in cfg.agents]
        if self.agents:                    # one host, one memory gate for every agent thread
            for ar in self.agents[1:]:
                ar.memgate = self.agents[0].memgate
        self._latest: dict[str, tuple[dict, float]] = {}
        self._ignored_next = 0.0
        self.ignored_freed_bytes = 0
        self.checkpoint_gc_freed_bytes = 0
        self.checkpoint_bytes: int | None = None     # None until the first measurement
        self._started = time.time()
        self._latest_lock = threading.Lock()
        self._wake_gen = 0                 # bumped by a Mesh event: agent loops poll now, not at poll_seconds

    def wake(self) -> None:
        self._wake_gen += 1

    def _sleep(self, stop: threading.Event, seconds: float, gen: int) -> None:
        """Wait out the poll interval; a stop or a Mesh event (``wake``) ends it early."""
        end = time.monotonic() + seconds
        while time.monotonic() < end and self._wake_gen == gen:
            if stop.wait(min(0.5, max(0.0, end - time.monotonic()))):
                return

    def tick(self) -> list[dict]:
        out = []
        for ar in self.agents:
            try:
                out.append(ar.tick())
            except Exception as e:     # one agent's failure must not stop the others
                log.exception("%s: tick failed: %s", ar.agent.name, e)
                out.append({"agent": ar.agent.name, "error": str(e)[:300]})
        self.write_status(out)
        return out

    def write_status(self, out: list[dict]) -> None:
        """status.json for operators; optional Prometheus textfile for alerting."""
        stamp = time.time()
        if self.cfg.queue_wait_freshness_v1:
            try:
                queue_wait.publish(self.store, self.cfg.state_dir / "queue-wait-requests.json",
                                   self.cfg.state_dir / "queue-wait-receipts.json")
            except (OSError, ValueError, TypeError, KeyError):
                log.warning("queue WAIT manifest unavailable; demand retained")
        try:
            tmp = self.cfg.state_dir / "status.json.tmp"
            from .pool_ops import summary
            pool = summary(self.store, self.cfg.pool_root)
            status = {"at": iso(), "unix": stamp, "agents": out, "pool": pool}
            if getattr(self, "checkpoint_bytes", None) is not None:
                status["checkpoints"] = {"bytes": self.checkpoint_bytes,
                                         "budget_bytes": int(self.cfg.checkpoint_budget_gb * 1024 ** 3)}
            tmp.write_text(json.dumps(status, ensure_ascii=False, indent=1))
            tmp.replace(self.cfg.state_dir / "status.json")
        except OSError as e:
            log.warning("status file: %s", e)
        if not self.cfg.metrics_file:
            return
        oldest = min((float(r.get("tick_at", stamp)) for r in out), default=stamp)   # the slowest agent's last tick
        lines = ["# TYPE mesh_runner_last_tick_seconds gauge", f"mesh_runner_last_tick_seconds {oldest:.0f}",
                 "# TYPE mesh_runner_pool_ignored_freed_bytes_estimate_total counter",
                 f"mesh_runner_pool_ignored_freed_bytes_estimate_total {getattr(self, 'ignored_freed_bytes', 0)}",
                 "# TYPE mesh_runner_checkpoint_gc_freed_bytes_estimate_total counter",
                 f"mesh_runner_checkpoint_gc_freed_bytes_estimate_total {getattr(self, 'checkpoint_gc_freed_bytes', 0)}",
                 "# TYPE mesh_runner_attempts gauge", "# TYPE mesh_runner_agent_error gauge",
                 "# TYPE mesh_runner_agent_paused gauge"]
        if getattr(self, "checkpoint_bytes", None) is not None:
            lines += ["# TYPE mesh_runner_checkpoint_bytes gauge", f"mesh_runner_checkpoint_bytes {self.checkpoint_bytes}"]
        for st in out:
            name = st.get("agent", "?")
            lines.append(f'mesh_runner_agent_error{{agent="{name}"}} {1 if "error" in st else 0}')
            lines.append(f'mesh_runner_agent_paused{{agent="{name}"}} {1 if st.get("paused") else 0}')
            counts: dict[str, int] = {}
            for a in st.get("attempts", []):
                counts[a["state"]] = counts.get(a["state"], 0) + 1
            for state in ("running", "starting", "waiting", "ready", "unknown"):
                lines.append(f'mesh_runner_attempts{{agent="{name}",state="{state}"}} {counts.get(state, 0)}')
        try:
            tmp = Path(str(self.cfg.metrics_file) + ".tmp")
            tmp.write_text("\n".join(lines) + "\n")
            os.chmod(tmp, 0o644)
            tmp.replace(self.cfg.metrics_file)
        except OSError as e:
            log.warning("metrics file: %s", e)

    def _prune_ignored_if_due(self) -> None:
        """Start the periodic regenerable-dir cleanup off the main loop (it must never delay
        write_status); one pass at a time, all pools share a single deadline."""
        cfg = self.cfg
        if not (cfg.ignored_cleanup or getattr(cfg, "checkpoint_gc", False)) or time.monotonic() < self._ignored_next:
            return
        worker = getattr(self, "_ignored_worker", None)
        if worker is not None and worker.is_alive():
            return
        self._ignored_next = time.monotonic() + max(1.0, cfg.ignored_cleanup_interval_minutes) * 60
        self._ignored_worker = threading.Thread(target=self._prune_ignored_pass, name="ignored-cleanup", daemon=True)
        self._ignored_worker.start()

    def _prune_ignored_pass(self) -> None:
        cfg = self.cfg
        from . import pool_ops
        deadline = time.monotonic() + 120          # one budget for the whole pass, not per agent
        for ar in self.agents if getattr(cfg, "checkpoint_gc", False) else ():
            if time.monotonic() >= deadline:
                break
            try:
                res = pool_ops.prune_checkpoints(ar.pool, grace_hours=cfg.checkpoint_gc_grace_hours, apply=True,
                                                 deadline=deadline)
                self.checkpoint_gc_freed_bytes = getattr(self, "checkpoint_gc_freed_bytes", 0) + res["freed_bytes"]
            except Exception as e:
                log.warning("%s: checkpoint gc failed: %s", ar.agent.name, e)
        for ar in self.agents if cfg.ignored_cleanup else ():
            if time.monotonic() >= deadline:
                break
            try:
                res = pool_ops.prune_ignored(ar.pool, idle_hours=cfg.ignored_cleanup_idle_hours, apply=True,
                                             deadline=deadline)
                self.ignored_freed_bytes = getattr(self, "ignored_freed_bytes", 0) + res["freed_bytes"]
            except Exception as e:
                log.warning("%s: ignored cleanup failed: %s", ar.agent.name, e)
        # Old-format waiting snapshots shed their caches in the live runner (the CLI cannot: the runner
        # owns the state directory). One snapshot at a time, its own budget, after prune and cleanup, so a long
        # snapshot never delays them; no new snapshot is started once the budget is spent.
        compact_deadline = time.monotonic() + 600
        for ar in self.agents if getattr(cfg, "checkpoint_gc", False) else ():
            if time.monotonic() >= compact_deadline:
                break
            try:
                res = pool_ops.compact_checkpoints(ar.pool, apply=True, deadline=compact_deadline)
                self.checkpoint_gc_freed_bytes = getattr(self, "checkpoint_gc_freed_bytes", 0) + res["freed_bytes"]
                if res.get("errors"):
                    log.warning("%s: checkpoint compact: %d error(s), see earlier lines", ar.agent.name, res["errors"])
            except Exception as e:
                log.warning("%s: checkpoint compact failed: %s", ar.agent.name, e)
        if getattr(cfg, "checkpoint_gc", False):
            self._measure_checkpoints()

    def _measure_checkpoints(self) -> None:
        """Total WAIT-snapshot size for status.json, the gauge and the alert. A budget overrun is
        reported, not enforced by deleting: a snapshot is the only copy of a waiting card's
        uncommitted work."""
        from . import pool_ops
        try:
            self.checkpoint_bytes = pool_ops.checkpoint_bytes(self.cfg.pool_root)
        except (WorkspaceError, OSError, subprocess.SubprocessError) as e:
            log.warning("checkpoint size: %s", e)
            return
        budget = int(self.cfg.checkpoint_budget_gb * 1024 ** 3)
        if budget and self.checkpoint_bytes > budget:
            log.warning("checkpoints use %.1f GiB, over the %.1f GiB budget", self.checkpoint_bytes / 1024 ** 3,
                        self.cfg.checkpoint_budget_gb)

    def _tick_agent(self, ar: AgentRunner) -> dict:
        try:
            out = ar.tick()
        except Exception as e:         # one agent's failure must not stop the others
            log.exception("%s: tick failed: %s", ar.agent.name, e)
            out = {"agent": ar.agent.name, "error": str(e)[:300]}
        with self._latest_lock:
            self._latest[ar.agent.name] = (out, time.time())
        return out

    def _agent_loop(self, ar: AgentRunner, stop: threading.Event) -> None:
        """Each agent on its own cadence: a slow Mesh read or a long settle for one
        agent no longer delays every other agent's wakes and starts."""
        try:
            while not stop.is_set():
                started = time.monotonic()
                gen = self._wake_gen
                self._tick_agent(ar)
                self._sleep(stop, max(1.0, self.cfg.poll_seconds - (time.monotonic() - started)), gen)
        except BaseException as e:       # a dead loop must show at once, not as a stale healthy row
            log.critical("%s: agent loop died: %r", ar.agent.name, e)
            with self._latest_lock:
                self._latest[ar.agent.name] = ({"agent": ar.agent.name, "error": f"agent loop died: {e!r}"[:300]},
                                               time.time())

    def snapshot(self, now_unix: float | None = None) -> list[dict]:
        """Latest result per agent; an agent whose loop has not finished a tick for
        STALL_TICKS polls (and at least STALL_MIN_SECONDS) is reported as an error, so a
        wedged thread shows in status and metrics instead of a frozen healthy row."""
        now_unix = time.time() if now_unix is None else now_unix
        limit = max(STALL_MIN_SECONDS, STALL_TICKS * self.cfg.poll_seconds)
        out = []
        with self._latest_lock:
            latest = dict(self._latest)
        for ar in self.agents:
            row, at = latest.get(ar.agent.name, ({"agent": ar.agent.name}, self._started))
            row = dict(row, tick_at=round(at))
            if now_unix - at > limit:
                row["error"] = f"no tick finished for {int(now_unix - at)}s"
            out.append(row)
        return out

    def _start_announce(self, stop: threading.Event) -> None:
        """Lead every configured agent in Mesh (`[runner] announce_controllers`): the runner is the
        controller of its lanes, which the My Runner screen and the chat picker read."""
        if not self.cfg.announce_controllers:
            return
        from .chat_announce import announce_loop
        from .chat_protocol import load_runner_id
        threading.Thread(target=announce_loop, name="controller-announce", daemon=True, args=(
            {ar.agent.name: ar.mesh for ar in self.agents}, load_runner_id(self.cfg.state_dir), stop)).start()

    def _start_chat(self, stop: threading.Event) -> None:
        """Agent Chat for the configured agents, in a thread of its own (off unless `[chat] enabled`)."""
        cfg = self.cfg
        if not cfg.chat_enabled:
            return
        from .chat_host import ChatHost
        from .chat_protocol import load_runner_id, registered_client
        ids = {}
        for ar in self.agents:
            if ar.agent.name in cfg.chat_agents:
                try:
                    ids[str(ar.mesh.me()["id"])] = ar.agent.name
                except (MeshError, KeyError, TypeError) as e:
                    log.warning("chat: %s has no Mesh identity yet: %s", ar.agent.name, type(e).__name__)
        if not ids:
            log.warning("chat enabled but no configured agent resolved")
            return
        runner_id = load_runner_id(cfg.state_dir)
        if not cfg.announce_controllers:       # otherwise _start_announce already leads every agent
            from .chat_announce import announce_loop
            threading.Thread(target=announce_loop, name="chat-announce", daemon=True, args=(
                {ar.agent.name: ar.mesh for ar in self.agents if ar.agent.name in ids.values()}, runner_id,
                stop)).start()
        client = registered_client(cfg.mesh_url, cfg.chat_credential, cfg.state_dir, runner_id,
                                   "mesh-runner@" + socket.gethostname())
        host = ChatHost(self, client, ids, cfg.chat_max_per_agent,
                        cfg.chat_host_cap, cfg.chat_idle_minutes, cfg.chat_workspace)
        threading.Thread(target=host.loop, args=(stop,), name="chat", daemon=True).start()
        log.info("chat serving %s", ", ".join(sorted(ids.values())))

    def _start_extension(self, stop: threading.Event):
        """The Mesh extension endpoint and the extension token's self-rotation (`[extension]`)."""
        cfg = self.cfg
        service = None
        if cfg.extension_enabled:
            from .extension_service import ExtensionService
            service = ExtensionService(cfg.state_dir, self.wake, cfg.extension_workspace_id,
                                       expected_secret=cfg.extension_secret,
                                       trust_first_install=cfg.extension_trust_first_install, allow_rekey=cfg.extension_allow_rekey,
                                       tolerance=cfg.extension_tolerance_seconds)
            service.serve(cfg.extension_host, cfg.extension_port)
            log.info("extension endpoint on %s:%s", cfg.extension_host, cfg.extension_port)
        token = cfg.extension_token
        if token is not None and token.file and cfg.extension_rotate_days > 0 and self.agents:
            client = self.agents[0].mesh

            def rotate_loop() -> None:
                while not stop.wait(cfg.extension_rotate_days * 86400):
                    try:
                        token.rotate(client)
                        log.info("extension token rotated")
                    except (MeshError, RuntimeError, OSError) as e:
                        log.warning("extension token rotation failed: %s", type(e).__name__)
            threading.Thread(target=rotate_loop, name="extension-token", daemon=True).start()
        return service

    def run_forever(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        self._started = time.time()
        threads = [threading.Thread(target=self._agent_loop, args=(ar, stop), name=f"agent-{ar.agent.name}",
                                    daemon=True) for ar in self.agents]
        for t in threads:
            t.start()
        self._start_announce(stop)
        self._start_chat(stop)
        service = self._start_extension(stop)
        try:
            while not stop.is_set():
                self._prune_ignored_if_due()
                self.write_status(self.snapshot())
                stop.wait(self.cfg.poll_seconds)
        finally:
            # let every agent finish its tick: a thread cut between spawn and the
            # store update would leave a session the next start does not know about
            stop.set()
            if service:
                service.shutdown()
            deadline = time.monotonic() + STOP_GRACE_SECONDS
            for t in threads:
                t.join(timeout=max(0.1, deadline - time.monotonic()))
            self.write_status(self.snapshot())
