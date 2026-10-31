"""Runner configuration (TOML).

Secrets are referenced by file path or environment variable name, never
written inline: ``key_file`` / ``key_env`` for the Mesh agent key and
``token_file`` / ``token_env`` for the GitLab token.

    [runner]
    mesh_url = "https://mesh.example.com"
    state_dir = "~/.local/state/mesh-runner"
    review_gate_hold_minutes = 30  # a review the start gate keeps refusing: one notice, then no retries
    review_own_hold_hours = 1     # the agent's own work in review waits this long for someone else
    # review_verdict_pattern = '' # regex of a review verdict line that feeds the author ('' = off)
    # review_own_verdict_pattern = ''   # the same for the author's own comments
    # review_label_prefix = "verified-by:"   # MR label recording a review verdict ('' = off)
    # turn_cap_continue_on_progress = false  # at max_turns a progressing attempt restarts fresh on the same agent
    # turn_cap_progress_min = 3        # step/next outcomes among the last 10 that count as progress
    # loop_streak_limit = 6            # consecutive turns without progress before a task is handed up as a LOOP (0 = off)
    # requeue_on_cap = false           # a capped task returns to todo and restarts on new input, not a clock
    # cap_wake_next_day = false        # a capped task of an agent without a lead restarts on the next UTC day
    # soft_person_asks_to_lead = false # a question to a person that no hard rule matches goes to the lead
    # unanswerable_ask_to_lead = true  # a question to an agent that cannot answer goes to the lead
    # decision_live_task_card_wait = true  # a question about the addressee's own live task waits on that task
    # decision_identical_no_reopen = true  # the same question again is attached to its request, never reopened
    # decision_default_proceeds = true     # a question carrying the asker's default proceeds; the addressee may object
    # hard_ask_patterns = [...]        # replaces the keyword list (see asks.py); hard_ask_labels likewise
    # unanswerable_users = ["example"]    # test or service users nobody answers for
    # merge_command = ["merge-gate", "{iid}", "{project}", "--expected-sha", "{sha}"]   # enables `WAIT merge:`
    # reviewer_select_command = ["python3", "select-reviewer.py", "{project_id}", "{author}"]   # enables `reviewer-merge`
    # merge_command_cwd = "~/tools"    # working directory of merge_command
    # merge_timeout_minutes = 15       # one run of merge_command
    # merge_rerun_minutes = 5          # re-run a queued merge at least this often even if nothing changed
    # merge_max_runs = 30              # deprecated, ignored: a queued run always ends in a wake or another poll
    # merge_follow_head = false        # a moved head (server rebase) runs the gate on it instead of waking the author;
    #                                  # the gate alone decides whether the review carries over (fail-closed)
    # merge_queue_hours = 24           # a wait that stays queued this long wakes the agent too

    [catalog]                     # optional: apply the runtime catalog and report on it (off by default)
    enabled = false
    manifest = "~/runtime/accounts.json"   # what this host can execute: controllers, accounts, modes, pools
    # interval_seconds = 120
    # observations = "~/runtime/quota-observations.json"    # quota-observations/v1; absent = pools unknown
    # harness_limits = "~/runtime/harness-limits.json"      # harness-limits/v1: usage limits lanes printed
    # thresholds = "~/runtime/thresholds.json"

      [[catalog.workspace]]
      id = "<workspace uuid>"
      reporter_key_file = "~/.config/mesh-runner/reporter.key"   # an agent with the reporter grant here
      # home = "..."              # state.json, pools.json, desired-etag.json (default state_dir/catalog/<id>)
      # catalog_dir = "..."       # applied.json, desired.json (default: home)
      # manifest = "..."          # this workspace's own manifest (default [catalog] manifest)

    [gitlab]                      # optional: enables pipeline/mr waits
    base_url = "https://gitlab.example.com"
    token_file = "~/.config/mesh-runner/gitlab.token"

    [[agents]]
    name = "builder"
    key_file = "~/.config/mesh-runner/builder.key"
    harness = "claude"            # claude | codex
    profile_dir = "~/agents/builder"   # session cwd: the agent's rules, skills, MCP config
    args = ["--model", "sonnet"]
    max_parallel = 0              # 0: use the agent's max_concurrent_tasks in Mesh
    # review_own_hold_hours = 2   # per agent; overrides [runner]
    # pool = "dev"                # agents with the same pool take each other's capped tasks (requeue_on_cap)

      [[agents.repos]]
      name = "app"
      base = "~/src/app"          # an existing clone; worktrees are added next to the pool root
"""
from __future__ import annotations

import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import asks
from .selection import Policy
from .workspace import RepoSpec


def _default_state_dir() -> str:
    """An install that already keeps state in the legacy home directory keeps using it."""
    legacy = Path("~/.mesh-runner").expanduser()
    return str(legacy) if legacy.is_dir() else "~/.local/state/mesh-runner"


def _idle_hours(value, name: str = "ignored_cleanup_idle_hours") -> float:
    """Idle threshold in hours: a real number (not a string or bool), finite and >= 0 (a negative
    one would put the cutoff in the future)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a number, got {value!r}")
    hours = float(value)
    if not math.isfinite(hours) or hours < 0:
        raise ConfigError(f"{name} must be >= 0, got {value!r}")
    return hours


class ConfigError(Exception):
    pass


# "Verify: SHIP @1a2b3c4", "Verdict: DO-NOT-SHIP on 1a2b3c4d", "review: SHIP". The verdict
# token is case-sensitive ("do not ship before CI" is prose, not a verdict); a line naming
# both verdicts ("reply SHIP or DO-NOT-SHIP") is a request and never counts.
_SHA = r"(?:[^\n]*?(?:@|\bon\s+)\s*(?P<sha>[0-9a-f]{7,40})\b)?"
DEFAULT_VERDICT_PATTERN = (r"^\W*(?:verify|verdict|review)\b[^\n]*?\b(?P<verdict>(?-i:SHIP|DO-NOT-SHIP))\b"
                           + _SHA)
# The author's own comment counts only as a plain verdict line: the author also writes
# requests and plans that mention a verdict.
DEFAULT_OWN_VERDICT_PATTERN = r"^\W*(?:verify|verdict)\b\W*(?P<verdict>(?-i:SHIP|DO-NOT-SHIP))\b" + _SHA


def _pattern(value, where: str) -> str:
    if value is None:
        return value
    try:
        re.compile(str(value), re.I | re.M)
    except re.error as e:
        raise ConfigError(f"{where}: verdict pattern: {e}") from None
    return str(value)


def _path(value: str | None) -> Path | None:
    return Path(os.path.expandvars(os.path.expanduser(value))) if value else None


def _basic_auth(r: dict) -> str:
    if not (r.get("basic_auth_file") or r.get("basic_auth_env")):
        return ""
    value = read_secret(r.get("basic_auth_file"), r.get("basic_auth_env"), "mesh basic auth")
    if ":" not in value:
        raise ConfigError("mesh basic auth: expected user:password")
    return value


def read_secret(file: str | None, env: str | None, what: str) -> str:
    if file:
        p = _path(file)
        mode = p.stat().st_mode & 0o077
        if mode:
            raise ConfigError(f"{what}: {p} is readable by group/others; chmod 600")
        value = p.read_text().strip()
    elif env:
        value = os.environ.get(env, "").strip()
    else:
        raise ConfigError(f"{what}: set *_file or *_env")
    if not value:
        raise ConfigError(f"{what}: empty")
    return value


def read_env_file(path: Path | None) -> dict[str, str]:
    """KEY=VALUE lines (optionally prefixed by ``export``); comments ignored."""
    out: dict[str, str] = {}
    if not path:
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip("'\"")
    return out


@dataclass
class AgentConfig:
    name: str
    key: str = field(repr=False)
    harness: str
    profile_dir: Path
    repos: list[RepoSpec]
    args: list[str] = field(default_factory=list)
    codex_profile: str = ""
    binary: str = ""
    max_parallel: int = 0
    workspace_wait_release_minutes: float | None = None  # per-agent canary; None inherits runner
    env: dict[str, str] = field(default_factory=dict, repr=False)
    env_file: Path | None = None      # re-read before every spawn: account switches apply at once
    inline_env: dict[str, str] = field(default_factory=dict, repr=False)
    env_command: list[str] = field(default_factory=list)   # prints KEY=VALUE lines; run before every spawn
    on_limit_command: list[str] = field(default_factory=list)  # run on a provider limit; exit 0 = switched
    on_auth_command: list[str] = field(default_factory=list)  # exact auth hold; exit 0 = routed, never clears hold
    start_gate_command: list[str] = field(default_factory=list)  # before a start/resume: 0 go, 75 hold
    turn_end_command: list[str] = field(default_factory=list)    # after every turn, with the gate's output
    alt: dict = field(default_factory=dict)    # other harnesses the agent may be routed to: {"codex": {"profile", "args", "binary"}}
    chat: dict = field(default_factory=dict)   # standing interactive session: session, command, idle_command, max_age_hours
    launch_mode: str = ""              # "" inherits RunnerConfig.launch_mode; "interactive" or "headless"
    decision_wake: str = ""            # "" inherits RunnerConfig.decision_wake; "any" or "answer"
    preamble_file: str = ""
    paused: bool = False
    lead: str = ""                     # agent name that gets the task when a turn budget runs out
    # agents with the same non-empty ``pool`` are interchangeable for a task that hit a cap
    # (requeue_on_cap). Unset: another agent with the same repos and the same lead is a peer.
    pool: str = ""
    review_prompt_file: str = ""       # replaces the default review protocol text
    feed_review: bool = True           # feed the agent's own tasks in review
    review_own_hold_hours: float | None = None   # overrides RunnerConfig.review_own_hold_hours
    review_verdict_pattern: str | None = None    # overrides RunnerConfig.review_verdict_pattern
    review_own_verdict_pattern: str | None = None   # overrides RunnerConfig.review_own_verdict_pattern
    identity: tuple[str, str] | None = None
    # The lane's GitLab username: an MR it authored counts as the task's own MR in wait checks.
    # None = the agent name lower-cased; '' = off (only links and the task id decide).
    gitlab_username: str | None = None
    policy: Policy = field(default_factory=Policy)

    def __post_init__(self) -> None:
        if self.gitlab_username is None:
            self.gitlab_username = self.name.lower()
        # Built in code with only `env`: those values are the inline ones.
        if self.env and not self.inline_env and not self.env_file and not self.env_command:
            self.inline_env = dict(self.env)


@dataclass
class CatalogWorkspace:
    workspace_id: str
    reporter_key: str = field(repr=False)
    home: Path = Path()
    catalog_dir: Path = Path()
    manifest: Path | None = None


@dataclass
class CatalogConfig:
    """`[catalog]`: the runner is the catalog controller of these workspaces (catalog.py)."""
    enabled: bool = False
    controller_ref: str = "mesh-runner"
    interval_seconds: float = 120.0
    manifest: Path | None = None
    observations: Path | None = None
    harness_limits: Path | None = None
    thresholds: Path | None = None
    workspaces: tuple = ()


def _catalog(raw, state_dir: Path) -> CatalogConfig:
    from . import RUNNER_NAME
    c = raw or {}
    if not isinstance(c, dict):
        raise ConfigError("[catalog] must be a table")
    if "execution_observations" in c:
        # this package ships no execution publisher: the key is only parsed so that no value can look accepted
        observe = c["execution_observations"]
        if not isinstance(observe, bool):
            raise ConfigError(f"[catalog] execution_observations must be false or absent, got {observe!r}")
        if observe:
            raise ConfigError("[catalog] execution_observations = true is not supported: this runner build has no "
                              "execution publisher")
    if c.get("enabled", False) is not True:
        return CatalogConfig()
    if c.get("controller_ref", RUNNER_NAME) != RUNNER_NAME:
        raise ConfigError(f"[catalog] reports under the runner's own name {RUNNER_NAME!r}; controller_ref cannot differ")
    if not c.get("manifest"):
        raise ConfigError("[catalog] manifest (the host manifest path) is required")
    spaces, seen = [], set()
    for w in c.get("workspace") or []:
        ws = str(w.get("id") or "")
        if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", ws) or ws in seen:
            raise ConfigError(f"[[catalog.workspace]] id must be a unique workspace UUID, got {ws!r}")
        seen.add(ws)
        home = _path(w.get("home")) or state_dir / "catalog" / ws
        spaces.append(CatalogWorkspace(
            workspace_id=ws,
            reporter_key=read_secret(w.get("reporter_key_file"), w.get("reporter_key_env"),
                                     f"catalog workspace {ws[:8]} reporter key"),
            home=home, catalog_dir=_path(w.get("catalog_dir")) or home, manifest=_path(w.get("manifest"))))
    if not spaces:
        raise ConfigError("[catalog] enabled needs at least one [[catalog.workspace]]")
    return CatalogConfig(
        enabled=True, controller_ref=RUNNER_NAME,
        interval_seconds=_positive(c.get("interval_seconds", 120), "[catalog] interval_seconds"),
        manifest=_path(c["manifest"]), observations=_path(c.get("observations")),
        harness_limits=_path(c.get("harness_limits")), thresholds=_path(c.get("thresholds")),
        workspaces=tuple(spaces))


@dataclass
class RunnerConfig:
    mesh_url: str
    state_dir: Path
    agents: list[AgentConfig]
    poll_seconds: int = 20
    wake_poll_seconds: int = 120
    heartbeat_seconds: int = 300
    checkout_ttl_minutes: int = 120
    max_no_outcome: int = 1            # immediate corrections (same session) after an unreadable outcome
    # Consecutive turns without a readable outcome on one task (any lane; a readable outcome resets the
    # count) after which it is routed to the lead. The second miss in a row puts the task back in todo
    # with the same lane until new input arrives (``requeue_on_cap`` inputs, no timer; needs
    # ``requeue_on_cap``, else it is handed up as before). A task already routed up for this reason is
    # only held by the receiving lead, never handed further, until a readable outcome.
    no_outcome_route_after: int = 3
    daily_start_cap: int = 0           # starts of one task by one agent per UTC day; past it -> lead (0 = off).
                                       # With requeue_on_cap a start an event woke does not count (docs/requeue-on-event.md)
    mention_requests: bool = False     # an @-address on someone else's task becomes a request card
    max_compactions: int = 0           # context compactions in one attempt; past it -> lead (0 = off)
    max_failures: int = 3
    provider_outage_giveup_minutes: int = 360   # continuous provider outage before a task goes up (0 = never)
    provider_outage_backoff_seconds: int = 120  # first retry delay; doubles per failed turn
    provider_outage_backoff_cap_seconds: int = 7200
    provider_outage_lanes: int = 3     # lanes failing within 15 min that pause every start (0 = off)
    max_startup_failures: int = 2      # failed turns that never created a session before the task goes up
    failed_cooldown_hours: float = 0.5   # a failed task is retried this soon even without a new comment
    max_turns: int = 30
    # At max_turns an attempt that still made progress (>= turn_cap_progress_min of its last 10 turn
    # outcomes were step/next) ends and the same agent starts the task again in a fresh session instead
    # of handing it to the lead. Needs daily_start_cap > 0: the restart counts as a start, so the loop
    # is bounded by it. False: today's behaviour (RUNNER GAVE UP -> lead).
    turn_cap_continue_on_progress: bool = False
    turn_cap_progress_min: int = 3
    max_turn_minutes: int = 180
    straggler_grace_minutes: int = 30
    # Turn progress watchdog (docs/turn-watchdog.md); minutes, 0 = off. A turn is ended early when
    # the harness shows no first output / no progress for this long, instead of at max_turn_minutes.
    turn_first_output_minutes: float = 0.0       # from the turn's start until the harness has replied
    turn_idle_minutes: float = 0.0               # no progress while nothing is running
    turn_external_wait_minutes: float = 0.0      # no progress while the harness runs a tool / background task
    # every wait is re-checked by its agent: first after wait_first_recheck_hours (a wrong or
    # unexplained deferral is caught fast), then every max_wait_hours while it stands. These
    # re-checks do not count against daily_wait_budget.
    wait_first_recheck_hours: float = 2.0
    min_free_gb: float = 0.0           # worktree admission floor in GiB; 0 = off
    # a held slot of a finished attempt is freed (its uncommitted work saved on a never-pushed ref)
    # when the task is closed or no longer the agent's, or after this many hours without a newer
    # attempt on the task; 0 = only the first two reasons
    dead_slot_hours: float = 24.0
    workspace_wait_release_minutes: float = 0.0  # 0 disables durable WAIT suspension
    ignored_cleanup: bool = False      # opt-in; sizes are du estimates, not Data.free gain. drop regenerable ignored dirs (node_modules, target...) in idle free slots
    ignored_cleanup_idle_hours: float = 24.0     # idle = since the slot became free (slots.updated_at)
    ignored_cleanup_interval_minutes: float = 30.0
    checkpoint_gc: bool = False        # opt-in; drop checkpoints of ended attempts and superseded generations
    checkpoint_gc_grace_hours: float = 6.0       # since the attempt ended / the generation was replaced
    checkpoint_budget_gb: float = 4.0            # total size of all WAIT snapshots; over it = warning + gauge, never a deletion
    # Directory names a WAIT snapshot leaves out (when nothing under them is tracked): build caches.
    checkpoint_exclude_names: tuple = (".gocache", "node_modules", "target", ".venv", "__pycache__", "var")
    queue_wait_freshness_v1: bool = False  # machine refill WAIT only; shared state_dir protocol files
    max_wait_hours: float = 6.0
    # Which comments by other agents end a pipeline/MR/time/decision/card/merge wait early. "all":
    # every foreign comment. "addressed": only an @-mention of the waiting agent, a person's comment,
    # the addressee (or an explicit answer) of a decision wait, or an author listed in
    # comment_wake_always_from (unless the comment opens with an FYI marker, which asks nothing);
    # the rest is read when the wait ends for its own reason (the awaited card closing, the MR
    # merging or closing, the pipeline finishing, the deadline).
    comment_wake: str = "all"
    comment_wake_always_from: list[str] = field(default_factory=list)
    # What ends a decision wait that has a request card for its addressee. "any": every comment the
    # comment_wake mode lets through, so the addressee's progress notes on the waiting card wake it
    # (each one a full model turn). "answer": only an answer: the request card got a comment from
    # its addressee or a person or was closed, or a comment on the waiting card is a person's, an
    # @-mention of the waiting agent, an answer marker, quotes the request card id, or comes from
    # comment_wake_always_from. The rest stays in the thread, read at the next wake.
    decision_wake: str = "any"
    # A decision wait asked of the same agent from the same source task within this many hours of
    # the request's filing reuses that request (the question is added as a comment, a closed one
    # reopened) instead of filing another; a verbatim repeat of a question the agent already
    # answered is sent back. 0 = every new question files a new request.
    decision_reuse_hours: float = 24.0
    # A decision wait whose question names exactly one other task (#<8hex> or its uuid) that the
    # addressee itself holds and is working on (todo, in_progress or review) files no request: the
    # addressee could only answer "not ready yet". It becomes a card wait on that task, woken when
    # the task closes or ships, or when the addressee comments on it. False: a request is filed.
    decision_live_task_card_wait: bool = True
    # The same question (case, whitespace, trailing punctuation and trailing references to the
    # asking task ignored) from the same asker and source to the same agent is attached to the
    # request it already has: no new card, no reopen, no repeat comment. A changed question is
    # asked on that request as before. False: only a byte-identical question is attached.
    decision_identical_no_reopen: bool = True
    # A decision wait to an agent whose question states the asker's own proposed default
    # ("default:", "proposed:", "if no objection", ...) and no hard rule (asks.py) matches files no
    # request: the question is posted on the asking task for the addressee to object, and the asker
    # continues with its default at once. False: a request is filed.
    decision_default_proceeds: bool = True
    # Obsolete: read so existing configs still load. Time WAITs need a sourced external
    # timestamp (outcome.TIME_WAIT_GUIDANCE); relative intervals are rejected outright.
    time_wait_reason_after_minutes: float = 60.0
    short_time_wait_series: int = 3
    time_wait_ignore_names: list[str] = field(default_factory=list)
    quota_pause_minutes: int = 30
    abort_backoff_minutes: int = 15
    gitlab_url: str = ""
    gitlab_token: str = field(default="", repr=False)
    github_orgs: tuple = ()                 # `org` or `org/repo` prefixes whose mr:/merge:/pipeline: targets are on GitHub
    github_token: str = field(default="", repr=False)
    github_api_url: str = "https://api.github.com"
    # Agent Chat (protocol v1): off unless `[chat] enabled = true`. Interactive sessions only.
    announce_controllers: bool = False      # lead every configured agent in Mesh (agent_controllers), chat or not
    chat_enabled: bool = False
    chat_agents: tuple = ()                 # runner agent names whose chats this runner serves
    chat_credential: str = field(default="", repr=False)   # runner credential for the Agent Chat protocol
    chat_max_per_agent: int = 3             # max_chat_sessions per agent
    chat_host_cap: int = 6                  # live sessions on this host, chats and cards together
    chat_idle_minutes: float = 10.0
    chat_workspace: str = "none"            # none | read | write (a slot of the card pool)
    # Mesh MCP of a chat session: stdio command (binary + args) run with the chat's session token
    # (MESH_CHAT_TOKEN_FILE), never the agent key; empty = the session gets no Mesh MCP from the runner
    chat_mcp_command: tuple = ()
    chat_mcp_url: str = ""                  # MESH_API_URL for it; empty = [runner] mesh_url
    metrics_file: Path | None = None   # Prometheus textfile (node_exporter textfile collector)
    mesh_effects: bool = True          # runtime state, schedules, edges, decision requests
    decision_request_metadata_only_recipients: list[str] = field(default_factory=list)  # resolved agent UUIDs; new requests only
    session_reports: bool = True      # spend of every turn reported to Mesh (agents/me/sessions/report)
    completion_comment: bool = True    # time, turns and cost on the task when an attempt completes
    review_starts_per_tick: int = 1    # a backlog of old review tasks must not crowd out work
    # a review card the agent itself just worked on is not fed back to it for this long, unless
    # something it has not seen happens (a comment, its MR or pipeline changed, its wait is over);
    # then it is fed once to route it on, never to accept it (0 = at once)
    review_own_hold_hours: float = 1.0
    # a review feed the start gate keeps refusing (no reviewer can take it) for this long gets one
    # notice on the card and is not retried until someone writes on it (0 = retry forever)
    review_gate_hold_minutes: float = 30.0
    review_unstartable_retry_minutes: float = 60.0   # a review no reviewer could start is tried again this soon, if the card changed
    review_refeed_hours: float = 4.0   # an already reviewed card without news is fed again after this
    review_gate_hold_pattern: str = r"qa_(refused|no_selection)"   # gate reasons that mean "no reviewer can take it"
    # a review verdict on the task (by anyone, the author included: it may run the reviewer
    # itself) also feeds the author's own work back to it; groups `verdict` and optional `sha`
    review_verdict_pattern: str = DEFAULT_VERDICT_PATTERN
    review_own_verdict_pattern: str = DEFAULT_OWN_VERDICT_PATTERN   # the same, in the agent's own comment
    # a merge request label starting with this records a review verdict. A wait on the task's own
    # open MR with a green or manual head and no such label is sent back: nobody will merge it
    # ('' = off)
    review_label_prefix: str = "verified-by:"
    project_leads: dict[str, str] = field(default_factory=dict)   # project id -> lead (name or agent id)
    # Loop guard: a task is handed up (banner LOOP) after this many consecutive turns without
    # progress. A turn has no progress when it is a NEXT that left the worktree as it was and wrote
    # nothing new, a WAIT the runner sent back and the agent declared again, a wait the agent
    # already declared in the current window (alternating between two waits), or the same WAIT
    # declared again in a turn the agent started with its own NEXT. An accepted standing wait
    # re-declared after a wake (a runner re-check included) leaves the streak as it is. A change in
    # the worktree or the card status, a met wake, a wait not yet seen in the window, or a comment
    # of the agent's own that is news (40+ characters once numbers, times and ids are removed, and
    # not seen in the window) resets it. 0 = off.
    loop_streak_limit: int = 6
    # The per-task, per-UTC-day turn counters below stop a task by count, whether or not it
    # progresses (a release QA card with 12 waits stopped at 8). With loop_streak_limit on they are a
    # backstop only: a rollback path and a bound against a loop the streak cannot see (an agent that
    # alternates between two waits, or writes a different comment each turn). Set them high
    # (e.g. 100000) to leave the streak as the only loop guard; they stay config-driven so the fleet
    # can roll back to the counters alone with loop_streak_limit = 0.
    daily_next_budget: int = 6
    daily_wait_budget: int = 8
    # waits declared right after the previous wait was met (CI finished, a card closed, the
    # answer came): normal progress, bounded only against an endless CI retry loop
    daily_step_budget: int = 24
    memory_hold_free_pct: float = 0.0       # host memory gate; 0 = off
    memory_release_free_pct: float = 0.0
    memory_hold_swap_mb: float = 0.0
    memory_release_swap_mb: float = 0.0
    # host load gate: no new or resumed sessions while the 5-minute load average per CPU is over
    # this (0 = off); released below load_release_per_cpu (0 = three quarters of the hold value)
    load_hold_per_cpu: float = 4.0
    load_release_per_cpu: float = 0.0
    tmux_binary: str = "tmux"               # standing chat sessions and interactive turns
    # how turns run: "interactive" (the harness's interactive program in tmux, fed by a driver;
    # what a subscription covers) or "headless" (claude -p / codex exec, billed per token)
    launch_mode: str = "interactive"
    interactive_idle_minutes: float = 60.0  # a warm session idle this long is closed
    interactive_max_sessions: int = 20      # live interactive sessions on this host, all agents
    # an interactive turn's prompt waits this long while the session's background agents run
    # (Claude Code resumes the main thread itself when one finishes); 0 = paste at once
    background_wait_minutes: float = 45.0
    catalog: CatalogConfig = field(default_factory=CatalogConfig)
    mesh_basic_auth: str = field(default="", repr=False)   # "user:pass" for a Mesh behind HTTP basic auth
    # Mesh extension: the service endpoint (/healthz, /mesh/install, /mesh/events) and the extension
    # actor token that replaces basic auth + agent key. Off unless `[extension]` says so.
    extension_enabled: bool = False
    extension_host: str = "127.0.0.1"
    extension_port: int = 8787
    extension_workspace_id: str = ""            # the one workspace whose install is accepted
    extension_secret: str = field(default="", repr=False)   # pre-shared install secret (file or env)
    extension_trust_first_install: bool = False
    extension_allow_rekey: bool = False
    extension_tolerance_seconds: int = 300
    extension_token: object = field(default=None, repr=False)   # ExtensionToken: file or env name, never a value
    extension_rotate_days: float = 0.0          # self-rotate the token this often; 0 = never
    tgpeek_editor: object = None                # EditorConfig of [tgpeek_editor]; None = the editor-job routes are off
    tgpeek_editor_control_secret_file: str = ""  # [tgpeek_editor_control] secret_file; empty = operator editor lifecycle off
    config_path: str = ""                       # the file this config was read from (the source of an operator reconfigure)
    # A task that hits START CAP, TURN CAP or COMPACTION CAP goes back to todo with the same assignee
    # (or to a free peer of its pool) and restarts on new input, never on a clock. False: the task goes
    # to the lead, or waits for the next UTC day without one (RUNNER GAVE UP is not a cap and always
    # goes to the lead).
    requeue_on_cap: bool = False
    # A capped task of an agent with no lead (the top of the chain) has nobody to hand it to and no input
    # that would wake it: it restarts in the first tick of the next UTC day after its cap (the restart is
    # a start, so daily_start_cap still bounds the loop). False: it waits for new input only.
    cap_wake_next_day: bool = False
    # A question to a person that no hard rule (asks.py) matches is put to the project lead as an agent
    # decision request instead of raising a human gate. False: every such question raises the gate.
    soft_person_asks_to_lead: bool = False
    # A `WAIT decision:@x` whose addressee cannot answer (not run by this runner, paused, not resolvable,
    # a configured test or service user) goes to the project lead instead of waiting for ever.
    unanswerable_ask_to_lead: bool = True
    # argv (no shell) that reads a question on stdin: exit 10 = the account registry already answers it
    # (a login ask about a live session), stdout = the reason shown to the lane; anything else lets the
    # question through. Runs on the final text of every lane, Codex included. () = off.
    login_ask_check: tuple = ()
    hard_ask_patterns: tuple = asks.DEFAULT_HARD_PATTERNS     # keyword fragments of a hard question
    hard_ask_labels: tuple = asks.DEFAULT_HARD_LABELS         # task labels that make a question hard
    # slugs of Mesh users that are test or service accounts: nobody answers them (the roster
    # does not mark them)
    unanswerable_users: tuple = ()
    # `⏳ WAIT merge:<group>/<project>!<iid> after=<sha>`: the runner runs this argv (no shell) with the
    # lane's session environment until the merge request is merged or something needs the agent.
    # Placeholders: {target} {project} {iid} {sha}. Empty = merge waits are rejected.
    merge_command: tuple = ()
    merge_command_cwd: Path | None = None
    reviewer_select_command: tuple = ()      # router adapter for `reviewer-merge`: JSON {lane, reason, username}
    merge_timeout_minutes: float = 15.0
    merge_rerun_minutes: float = 5.0
    merge_max_runs: int = 30
    merge_queue_hours: float = 24.0
    merge_follow_head: bool = False
    source: Path | None = field(default=None, compare=False, repr=False)   # file this was loaded from

    @property
    def pool_root(self) -> Path:
        return self.state_dir / "worktrees"


LAUNCH_MODES = ("interactive", "headless")


def _launch_mode(value, where: str, allow_empty: bool = False) -> str:
    v = str(value or "").strip()
    if (allow_empty and not v) or v in LAUNCH_MODES:
        return v
    raise ConfigError(f"{where}: launch_mode must be one of {', '.join(LAUNCH_MODES)}")


def _decision_wake(value, where: str, allow_empty: bool = False) -> str:
    # only an absent value inherits; any other non-string or unknown string is a config error
    if allow_empty and (value is None or (isinstance(value, str) and not value.strip())):
        return ""
    v = value.strip() if isinstance(value, str) else None
    if v in ("any", "answer"):
        return v
    raise ConfigError(f'{where}: decision_wake must be "any" or "answer"')


def _string_list(value, name: str, default: tuple) -> tuple:
    if value is None:
        return default
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{name} must be a list of strings")
    return tuple(value)


def _github_hosted(section: dict) -> tuple:
    """`[github] orgs` (whole orgs, or `org/repo`) plus `repos` (exactly `org/repo`, for an org that
    also lives on GitLab): the prefixes whose mr:/merge:/pipeline: targets are read from GitHub."""
    repos = _string_list(section.get("repos"), "[github] repos", ())
    for repo in repos:
        if not re.fullmatch(r"[^/\s]+/[^/\s]+", repo):
            raise ConfigError(f"[github] repos: {repo!r} must be org/repo")
    return tuple(_string_list(section.get("orgs"), "[github] orgs", ())) + repos


def _ask_patterns(value) -> tuple:
    patterns = _string_list(value, "hard_ask_patterns", asks.DEFAULT_HARD_PATTERNS)
    try:
        asks.validate_patterns(patterns)
    except re.error as e:
        raise ConfigError(f"hard_ask_patterns: {e}") from None
    return patterns


MERGE_PLACEHOLDERS = ("target", "project", "iid", "sha")


def _merge_command(value) -> tuple:
    if value is None:
        return ()
    if (not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value)):
        raise ConfigError("merge_command must be a list of non-empty strings")
    for part in value:
        for name in re.findall(r"\{([^{}]*)\}", part):
            if name not in MERGE_PLACEHOLDERS:
                raise ConfigError(f"merge_command: unknown placeholder {{{name}}}; "
                                  f"use {', '.join('{' + n + '}' for n in MERGE_PLACEHOLDERS)}")
    return tuple(value)


def _reviewer_select_command(value) -> tuple:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
        raise ConfigError("reviewer_select_command must be a list of non-empty strings")
    for part in value:
        for name in re.findall(r"\{([^{}]*)\}", part):
            if name not in ("project_id", "author"):
                raise ConfigError(f"reviewer_select_command: unknown placeholder {{{name}}}; use {{project_id}}, {{author}}")
    return tuple(value)


def _loop_streak_limit(value) -> int:
    """Consecutive turns without progress before a task is handed up as a loop: an integer >= 0
    (0 = off)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"loop_streak_limit must be an integer, got {value!r}")
    if value < 0:
        raise ConfigError(f"loop_streak_limit must be >= 0, got {value!r}")
    return value


def _positive(value, name: str, integer: bool = False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a number, got {value!r}")
    if not math.isfinite(value) or value <= 0 or (integer and int(value) != value):
        raise ConfigError(f"{name} must be a positive {'integer' if integer else 'number'}, got {value!r}")
    return int(value) if integer else float(value)


def _exclude_names(value) -> tuple:
    if value is None:
        return (".gocache", "node_modules", "target", ".venv", "__pycache__", "var")
    if not isinstance(value, list) or not all(isinstance(v, str) and v and "/" not in v and v not in (".", "..", ".git")
                                              for v in value):
        raise ConfigError("checkpoint_exclude_names must be a list of plain directory names")
    return tuple(value)


def _chat_workspace(value) -> str:
    if value not in ("none", "read", "write"):
        raise ConfigError('[chat] chat_workspace must be "none", "read" or "write"')
    return value


def _gitlab_username(value, agent: str) -> str | None:
    """``gitlab_username`` of an agent: absent = None (the name lower-cased), a string as given."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"agent {agent}: gitlab_username must be a string")
    return value.strip()


def load(path: str | Path) -> RunnerConfig:
    raw = tomllib.loads(Path(path).read_text())
    r = raw.get("runner") or {}
    import math
    floor = float(r.get("min_free_gb", 0.0))
    if not math.isfinite(floor) or floor < 0:
        raise ConfigError("min_free_gb must be finite and nonnegative")
    dead_hours = float(r.get("dead_slot_hours", 24.0))
    if not math.isfinite(dead_hours) or dead_hours < 0:
        raise ConfigError("dead_slot_hours must be finite and nonnegative")
    if not r.get("mesh_url"):
        raise ConfigError("[runner] mesh_url is required")
    recipients = r.get("decision_request_metadata_only_recipients", [])
    if (not isinstance(recipients, list) or any(not isinstance(v, str) or not re.fullmatch(
            r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", v) for v in recipients)):
        raise ConfigError("decision_request_metadata_only_recipients must be a list of canonical agent UUIDs")
    comment_wake = r.get("comment_wake", "all")
    if comment_wake not in ("all", "addressed"):
        raise ConfigError('comment_wake must be "all" or "addressed"')
    always_from = r.get("comment_wake_always_from", [])
    if not isinstance(always_from, list) or any(not isinstance(v, str) or not v.strip() for v in always_from):
        raise ConfigError("comment_wake_always_from must be a list of author names")
    gl = raw.get("gitlab") or {}
    chat = raw.get("chat") or {}
    ext = raw.get("extension") or {}
    ext_token = None
    if ext.get("token_file") or ext.get("token_env"):
        if r.get("basic_auth_file") or r.get("basic_auth_env"):
            raise ConfigError("[extension] token replaces basic auth: set only one of them")
        from .extension_service import ExtensionToken
        ext_token = ExtensionToken(_path(ext.get("token_file")), str(ext.get("token_env") or ""))
        if ext_token.file:
            mode = ext_token.file.stat().st_mode & 0o077
            if mode:
                raise ConfigError(f"extension token: {ext_token.file} is readable by group/others; chmod 600")
    from .editor_job import parse_config as _editor_config
    tgpeek_editor = _editor_config(raw.get("tgpeek_editor") or {}, _path(r.get("state_dir") or _default_state_dir()), ConfigError)
    control = raw.get("tgpeek_editor_control") or {}
    control_secret_file = str(control.get("secret_file") or "")
    if control_secret_file and not os.path.isabs(control_secret_file):
        raise ConfigError("[tgpeek_editor_control] secret_file must be an absolute path")
    ext_ws = str(ext.get("workspace_id") or "")
    if ext.get("enabled", False) is True and not re.fullmatch(
            r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", ext_ws):
        raise ConfigError("[extension] workspace_id (the workspace UUID this runner serves) is required")
    ext_secret = ""
    if ext.get("enabled", False) is True:
        if ext.get("signing_secret_file") or ext.get("signing_secret_env"):
            ext_secret = read_secret(ext.get("signing_secret_file"), ext.get("signing_secret_env"),
                                     "extension signing secret")
        elif ext.get("trust_first_install", False) is not True:
            raise ConfigError("[extension] needs signing_secret_file/signing_secret_env "
                              "(or trust_first_install = true on a private link)")
    agents = []
    for a in raw.get("agents") or []:
        harness = a.get("harness", "claude")
        if harness not in ("claude", "codex"):
            raise ConfigError(f"agent {a.get('name')}: harness must be claude or codex")
        repos = []
        for x in a.get("repos") or []:
            overrides = x.get("fetch_url_overrides", {})
            if not isinstance(overrides, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                                      for k, v in overrides.items()):
                raise ConfigError("fetch_url_overrides must be a table of URL strings")
            repos.append(RepoSpec(name=x["name"], base=_path(x["base"]), remote=x.get("remote", "origin"),
                                  branch=x.get("branch", "main"), max_slots=int(x.get("max_slots", 4)),
                                  fetch_url_overrides=tuple(overrides.items())))
        ident = a.get("identity")
        agents.append(AgentConfig(
            name=a["name"],
            key=("" if ext_token is not None and not (a.get("key_file") or a.get("key_env"))
                 else read_secret(a.get("key_file"), a.get("key_env"), f"agent {a['name']} key")),
            harness=harness,
            profile_dir=_path(a["profile_dir"]),
            repos=repos,
            args=list(a.get("args") or []),
            codex_profile=a.get("codex_profile", ""),
            binary=a.get("binary", ""),
            max_parallel=int(a.get("max_parallel", 0)),
            workspace_wait_release_minutes=(float(a["workspace_wait_release_minutes"])
                if a.get("workspace_wait_release_minutes") is not None else None),
            env={**read_env_file(_path(a.get("env_file"))), **{k: str(v) for k, v in (a.get("env") or {}).items()}},
            inline_env={k: str(v) for k, v in (a.get("env") or {}).items()},
            env_command=list(a.get("env_command") or []),
            on_limit_command=list(a.get("on_limit_command") or []),
            on_auth_command=list(a.get("on_auth_command") or []),
            start_gate_command=list(a.get("start_gate_command") or []),
            turn_end_command=list(a.get("turn_end_command") or []),
            chat=dict(a.get("chat") or {}),
            launch_mode=_launch_mode(a.get("launch_mode", ""), f"agent {a['name']}", allow_empty=True),
            decision_wake=_decision_wake(a.get("decision_wake", ""), f"agent {a['name']}", allow_empty=True),
            alt={str(k): dict(v) for k, v in (a.get("alt") or {}).items()},
            env_file=_path(a.get("env_file")),
            preamble_file=a.get("preamble_file", ""),
            paused=bool(a.get("paused", False)),
            lead=a.get("lead", ""),
            pool=str(a.get("pool", "")),
            review_prompt_file=a.get("review_prompt_file", ""),
            feed_review=bool(a.get("feed_review", True)),
            review_own_hold_hours=(float(a["review_own_hold_hours"]) if a.get("review_own_hold_hours") is not None
                                   else None),
            review_verdict_pattern=_pattern(a.get("review_verdict_pattern"), f"agent {a['name']}"),
            review_own_verdict_pattern=_pattern(a.get("review_own_verdict_pattern"), f"agent {a['name']}"),
            identity=tuple(ident) if ident else None,
            gitlab_username=_gitlab_username(a.get("gitlab_username"), a["name"]),
            policy=Policy(
                skip_labels=tuple(a.get("skip_labels", ("no-runner",))),
                skip_label_prefixes=tuple(a.get("skip_label_prefixes", ("park:",))),
                require_label=a.get("require_label", ""),
                project_ids=tuple(a.get("project_ids", ())),
                human_gate=bool(a.get("human_gate_flag_holds", True)),
            ),
        ))
    names = [a.name for a in agents]
    if len(set(names)) != len(names):
        raise ConfigError("agent names must be unique")
    return RunnerConfig(
        mesh_url=r["mesh_url"],
        state_dir=_path(r.get("state_dir") or _default_state_dir()),
        agents=agents,
        poll_seconds=int(r.get("poll_seconds", 20)),
        wake_poll_seconds=int(r.get("wake_poll_seconds", 120)),
        heartbeat_seconds=int(r.get("heartbeat_seconds", 300)),
        checkout_ttl_minutes=int(r.get("checkout_ttl_minutes", 120)),
        max_no_outcome=int(r.get("max_no_outcome", 1)),
        no_outcome_route_after=max(int(r.get("no_outcome_route_after", 3)), 1),
        daily_start_cap=int(r.get("daily_start_cap", 0)),
        mention_requests=bool(r.get("mention_requests", False)),
        comment_wake=comment_wake,
        comment_wake_always_from=[v.strip() for v in always_from],
        decision_wake=_decision_wake(r.get("decision_wake", "any"), "[runner]"),
        decision_reuse_hours=_idle_hours(r.get("decision_reuse_hours", 24.0), "decision_reuse_hours"),
        decision_live_task_card_wait=r.get("decision_live_task_card_wait", True) is not False,
        decision_identical_no_reopen=r.get("decision_identical_no_reopen", True) is not False,
        decision_default_proceeds=r.get("decision_default_proceeds", True) is not False,
        max_compactions=int(r.get("max_compactions", 0)),
        max_failures=int(r.get("max_failures", 3)),
        provider_outage_giveup_minutes=int(r.get("provider_outage_giveup_minutes", 360)),
        provider_outage_backoff_seconds=int(r.get("provider_outage_backoff_seconds", 120)),
        provider_outage_backoff_cap_seconds=int(r.get("provider_outage_backoff_cap_seconds", 7200)),
        provider_outage_lanes=int(r.get("provider_outage_lanes", 3)),
        max_startup_failures=int(r.get("max_startup_failures", 2)),
        failed_cooldown_hours=float(r.get("failed_cooldown_hours", 0.5)),
        max_turns=int(r.get("max_turns", 30)),
        turn_cap_continue_on_progress=r.get("turn_cap_continue_on_progress", False) is True,
        turn_cap_progress_min=max(1, int(r.get("turn_cap_progress_min", 3))),
        max_turn_minutes=int(r.get("max_turn_minutes", 180)),
        straggler_grace_minutes=int(r.get("straggler_grace_minutes", 30)),
        turn_first_output_minutes=_idle_hours(r.get("turn_first_output_minutes", 0.0), "turn_first_output_minutes"),
        turn_idle_minutes=_idle_hours(r.get("turn_idle_minutes", 0.0), "turn_idle_minutes"),
        turn_external_wait_minutes=_idle_hours(r.get("turn_external_wait_minutes", 0.0),
                                               "turn_external_wait_minutes"),
        wait_first_recheck_hours=float(r.get("wait_first_recheck_hours", 2.0)),
        min_free_gb=float(r.get("min_free_gb", 0.0)),
        dead_slot_hours=dead_hours,
        workspace_wait_release_minutes=float(r.get("workspace_wait_release_minutes", 0.0)),
        ignored_cleanup=r.get("ignored_cleanup", False) is True,
        ignored_cleanup_idle_hours=_idle_hours(r.get("ignored_cleanup_idle_hours", 24.0)),
        ignored_cleanup_interval_minutes=float(r.get("ignored_cleanup_interval_minutes", 30.0)),
        checkpoint_gc=r.get("checkpoint_gc", False) is True,
        checkpoint_gc_grace_hours=_idle_hours(r.get("checkpoint_gc_grace_hours", 6.0), "checkpoint_gc_grace_hours"),
        checkpoint_budget_gb=_idle_hours(r.get("checkpoint_budget_gb", 4.0), "checkpoint_budget_gb"),
        checkpoint_exclude_names=_exclude_names(r.get("checkpoint_exclude_names")),
        queue_wait_freshness_v1=r.get("queue_wait_freshness_v1") is True,
        max_wait_hours=float(r.get("max_wait_hours", 6.0)),
        time_wait_reason_after_minutes=float(r.get("time_wait_reason_after_minutes", 60.0)),
        short_time_wait_series=int(r.get("short_time_wait_series", 3)),
        time_wait_ignore_names=[str(n) for n in (r.get("time_wait_ignore_names") or [])],
        quota_pause_minutes=int(r.get("quota_pause_minutes", 30)),
        abort_backoff_minutes=int(r.get("abort_backoff_minutes", 15)),
        metrics_file=_path(r.get("metrics_file")),
        mesh_effects=bool(r.get("mesh_effects", True)),
        decision_request_metadata_only_recipients=list(recipients),
        completion_comment=bool(r.get("completion_comment", True)),
        session_reports=bool(r.get("session_reports", True)),
        review_starts_per_tick=int(r.get("review_starts_per_tick", 1)),
        review_own_hold_hours=float(r.get("review_own_hold_hours", 1.0)),
        review_gate_hold_minutes=float(r.get("review_gate_hold_minutes", 30.0)),
        review_unstartable_retry_minutes=float(r.get("review_unstartable_retry_minutes", 60.0)),
        review_refeed_hours=float(r.get("review_refeed_hours", 4.0)),
        review_gate_hold_pattern=_pattern(r.get("review_gate_hold_pattern", r"qa_(refused|no_selection)"), "[runner]"),
        review_verdict_pattern=_pattern(r.get("review_verdict_pattern", DEFAULT_VERDICT_PATTERN), "[runner]"),
        review_own_verdict_pattern=_pattern(r.get("review_own_verdict_pattern", DEFAULT_OWN_VERDICT_PATTERN),
                                            "[runner]"),
        review_label_prefix=str(r.get("review_label_prefix", "verified-by:")),
        loop_streak_limit=_loop_streak_limit(r.get("loop_streak_limit", 6)),
        daily_next_budget=int(r.get("daily_next_budget", 6)),
        project_leads={str(k): str(v) for k, v in (raw.get("project_leads") or {}).items()},
        daily_wait_budget=int(r.get("daily_wait_budget", 8)),
        daily_step_budget=int(r.get("daily_step_budget", 24)),
        memory_hold_free_pct=float(r.get("memory_hold_free_pct", 0)),
        tmux_binary=str(r.get("tmux_binary", "tmux")),
        launch_mode=_launch_mode(r.get("launch_mode", "interactive"), "[runner]"),
        interactive_idle_minutes=float(r.get("interactive_idle_minutes", 60.0)),
        interactive_max_sessions=int(r.get("interactive_max_sessions", 20)),
        background_wait_minutes=_idle_hours(r.get("background_wait_minutes", 45.0), "background_wait_minutes"),
        mesh_basic_auth=_basic_auth(r),
        catalog=_catalog(raw.get("catalog"), _path(r.get("state_dir") or _default_state_dir())),
        extension_enabled=ext.get("enabled", False) is True,
        extension_host=str(ext.get("host", "127.0.0.1")),
        extension_port=_positive(ext.get("port", 8787), "[extension] port", integer=True),
        extension_workspace_id=ext_ws,
        extension_secret=ext_secret,
        extension_trust_first_install=ext.get("trust_first_install", False) is True,
        extension_allow_rekey=ext.get("allow_rekey", False) is True,
        extension_tolerance_seconds=_positive(ext.get("tolerance_seconds", 300), "tolerance_seconds", integer=True),
        extension_token=ext_token,
        extension_rotate_days=float(ext.get("rotate_days", 0.0)),
        tgpeek_editor=tgpeek_editor,
        tgpeek_editor_control_secret_file=control_secret_file,
        config_path=str(Path(path).resolve()),
        requeue_on_cap=r.get("requeue_on_cap", False) is True,
        cap_wake_next_day=r.get("cap_wake_next_day", False) is True,
        soft_person_asks_to_lead=r.get("soft_person_asks_to_lead", False) is True,
        unanswerable_ask_to_lead=r.get("unanswerable_ask_to_lead", True) is not False,
        login_ask_check=tuple(_string_list(r.get("login_ask_check"), "login_ask_check", ())),
        hard_ask_patterns=_ask_patterns(r.get("hard_ask_patterns")),
        hard_ask_labels=_string_list(r.get("hard_ask_labels"), "hard_ask_labels", asks.DEFAULT_HARD_LABELS),
        unanswerable_users=tuple(s.casefold() for s in _string_list(r.get("unanswerable_users"),
                                                                    "unanswerable_users", ())),
        merge_command=_merge_command(r.get("merge_command")),
        merge_command_cwd=_path(r.get("merge_command_cwd")),
        reviewer_select_command=_reviewer_select_command(r.get("reviewer_select_command")),
        merge_timeout_minutes=_positive(r.get("merge_timeout_minutes", 15.0), "merge_timeout_minutes"),
        merge_rerun_minutes=_positive(r.get("merge_rerun_minutes", 5.0), "merge_rerun_minutes"),
        merge_max_runs=_positive(r.get("merge_max_runs", 30), "merge_max_runs", integer=True),
        merge_queue_hours=_positive(r.get("merge_queue_hours", 24.0), "merge_queue_hours"),
        merge_follow_head=bool(r.get("merge_follow_head", False)),
        memory_release_free_pct=float(r.get("memory_release_free_pct", 0)),
        memory_hold_swap_mb=float(r.get("memory_hold_swap_mb", 0)),
        memory_release_swap_mb=float(r.get("memory_release_swap_mb", 0)),
        load_hold_per_cpu=_idle_hours(r.get("load_hold_per_cpu", 4.0), "load_hold_per_cpu"),
        load_release_per_cpu=_idle_hours(r.get("load_release_per_cpu", 0.0), "load_release_per_cpu"),
        announce_controllers=r.get("announce_controllers", False) is True,
        chat_enabled=chat.get("enabled", False) is True,
        chat_agents=tuple(_string_list(chat.get("agents"), "[chat] agents", ())),
        chat_credential=read_secret(chat.get("credential_file"), chat.get("credential_env"), "chat credential")
        if chat.get("enabled", False) is True else "",
        chat_max_per_agent=_positive(chat.get("max_chat_sessions", 3), "max_chat_sessions", integer=True),
        chat_host_cap=_positive(chat.get("host_cap", 6), "[chat] host_cap", integer=True),
        chat_idle_minutes=_positive(chat.get("session_idle_minutes", 10.0), "session_idle_minutes"),
        chat_workspace=_chat_workspace(chat.get("chat_workspace", "none")),
        chat_mcp_command=_string_list(chat.get("mcp_command"), "[chat] mcp_command", ()),
        chat_mcp_url=str(chat.get("mcp_url") or ""),
        gitlab_url=gl.get("base_url", ""),
        gitlab_token=read_secret(gl.get("token_file"), gl.get("token_env"), "gitlab token")
        if gl.get("base_url") else "",
        github_orgs=_github_hosted(raw.get("github") or {}),
        github_token=read_secret((raw.get("github") or {}).get("token_file"),
                                 (raw.get("github") or {}).get("token_env"), "github token")
        if (raw.get("github") or {}).get("token_file") or (raw.get("github") or {}).get("token_env") else "",
        github_api_url=str((raw.get("github") or {}).get("api_url") or "https://api.github.com"),
        source=Path(path).resolve(),
    )
