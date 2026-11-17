# Architecture

## Model

| Object | Meaning |
|---|---|
| Agent | A Mesh agent identity (agent key). Its rules live in `profile_dir`; the runner never copies them. |
| Cap | The agent's `max_concurrent_tasks` in Mesh (or `max_parallel` locally). Counts sessions that are working now. |
| Attempt | One task executed by one agent: native session id, worktrees, checkout while running, wait condition. |
| Slot | A worktree in the warm pool, leased to one attempt at a time. |

Attempt states: `starting → running → waiting ⇄ ready → running … → completed | failed | aborted`.
`starting` and `running` occupy the cap; `waiting` and `ready` do not.

The allowed changes are one table, `lifecycle.TRANSITIONS`, which the store checks on every state write.
Every write goes through one object, `lifecycle.AttemptStateMachine` (`AgentRunner.life`), with one method
per target state (`to_running`, `to_waiting`, `to_ready`, …); it decides nothing and passes the write to
`Store.update` / `Store.update_with_marks`. `tests/test_state_writes.py` fails when any other module passes
`state=` to the store.

One open attempt per (agent, task) is enforced by a unique index; the Mesh checkout is
the cross-controller fence while a turn runs.

## Turn lifecycle

1. **Start.** Select the next eligible task (priority, then age; skip gated, scheduled,
   parked, unmet `blocks` dependencies, recently failed). Check it out in Mesh, lease a
   worktree per configured repository, move it to in-progress, spawn the harness in the
   agent's profile directory with the worktrees added, prompt on stdin.
2. **Run.** The harness (or, in the interactive mode, its driver) writes its structured
   event stream to a per-turn file. The
   process runs in its own process group; the runner heartbeats the checkout.
3. **Settle.** When the whole process group is gone: read the native session id and
   final message, then the agent's newest comment written during the turn.
   - task left the agent's working statuses → `completed`, worktrees returned;
   - `⏳ WAIT …` / `❓ Blocking` → `waiting`, checkout released, session and worktrees kept;
   - `↻ NEXT` → `ready`;
   - nothing → ask once in the same session, then `failed` (not retried until someone
     else writes on the task or the cooldown passes).
4. **Wake.** Waiting attempts are checked every two minutes: the condition itself
   (pipeline/MR state via GitLab, the other task closed, time, gate lifted) or any new
   comment from someone other than the agent. Woken attempts become `ready` and are
   resumed in the **same session** with a short message saying what changed.

## Launch modes

Turns run in `interactive` mode unless an agent sets its own `launch_mode` (e.g. to try a mode
on one agent, or to roll one back); `[runner] launch_mode` is a retired option (README "Retired options").

- **`interactive`** (default): the harness runs as its interactive program in a tmux
  window, one window per attempt in a session `mr-<agent>`. The turn's process is a small
  driver (`tui_driver.py`): it finds the attempt's window or starts it (`claude
  --session-id|--resume`, `codex [resume <thread>]`), pastes the prompt, waits for the
  harness's end-of-turn hook (Claude Code `Stop` from a per-window `--settings` file,
  Codex `notify`), copies the turn from the session transcript into the turn log in the
  headless format, and exits. Parsing, outcomes and waits are therefore unchanged. The
  window outlives the turn: the next turn of the task goes into a warm session. The
  agent's tick closes windows whose attempt is closed, that sat idle longer than
  `interactive_idle_minutes`, or whose attempt waits on a person, a decision or a timer
  beyond that; `interactive_max_sessions` caps live windows on the host (the one idle
  longest is closed). A screen that shows a limit or login banner without an end-of-turn
  hook ends the turn with that text, so the quota and authentication rules apply.
  A Claude Code session whose background agents still run (listed under its prompt box)
  has a free prompt box, but a prompt pasted then only gets a "still waiting" turn; when
  an agent finishes, Claude Code resumes the main thread by itself. The driver therefore
  holds the prompt until no background agent is listed and the session is idle with a
  settled transcript, at most 45 minutes, then
  pastes it anyway. The hold reports as an external wait: the first-output limit
  (`turn_first_output_minutes`) is not judged during it and counts from its end.
  The same holds at the end of a turn: a Claude Stop that lists running `background_tasks`
  (a background Bash, a Monitor, a background agent) while the turn called no Mesh turn
  command does not end the turn. Claude Code starts a turn by itself when the task finishes
  (its UserPromptSubmit carries the `<task-notification>` under a prompt id of its own);
  that turn continues ours, and its Stop ends it. A continuation that again stops on a running
  background task holds the turn once more: the wait, `background_wait_minutes`, counts from the
  last such Stop, so a session that runs its checks one after another is not cut off while it
  works (the turn's own limits still bound it). Settling the first Stop would count "no outcome" and paste a correction into
  a session about to resume. `python -m mesh_runner.no_outcome_classes` sorts past
  `no_outcome` events by what the transcripts show.
- **`headless`**: `claude -p --output-format stream-json`, `codex exec --json`; one
  process per turn, no terminal. A Claude turn's stream carries the provider's
  `rate_limit_event` (`status`, `resetsAt`, `rateLimitType`, `unifiedWindows`). A turn
  that ends after `status: rejected` takes the provider-limit path (`on_limit_command`)
  whatever its closing text says; the command gets `MESH_RUNNER_LIMIT_SOURCE=rate_limit_event`,
  `MESH_RUNNER_LIMIT_WINDOW` and `MESH_RUNNER_LIMIT_RESETS_AT`, so the account comes back at
  the provider's reset time, not by reading a screen. The windows of every such turn are
  written to `<state_dir>/rate-limits/<agent>.json` (schema `lane-rate-limit/v1`: the
  launch's account label, status, reset, `is_using_overage`, utilization per window) for the
  host's quota observations.

Both modes draw on the same subscription windows: a headless turn logged in with a
subscription is not billed per request (`isUsingOverage: false` in its `rate_limit_event`).
`status.json` names each agent's `launch_mode` and the harness `pid` of its active attempts,
so a host monitor can tell a configured headless lane from a stray headless process; each
turn's `launch` event carries the `launch_mode` too, so a turn's limit evidence is attributed
to a headless process without an interactive window.

Each agent ticks in its own thread, and one tick can take many minutes: checkpointing and
restoring the worktrees of waiting attempts, start gates, launches. An agent row's `tick_at` is
the time the row was computed, not the end of the tick: inside a long tick the lane republishes
its row at most every 30 s from progress beats (each Mesh request; each file, and each chunk of a
large one, copied, hashed or compared; each directory walked or removed; and every 10 s of a git
command, start gate, subprocess or repository-lock wait), and such a row also carries
`tick_running_since` and `tick_step` (the phase: `wakes`, `resume`, `start`, ...). `tick_done_at`
is when the last tick returned. A row with neither a finished tick nor a beat for
`max(900 s, 10 polls)` is reported as `error: no tick finished for Ns`, as before. A tick longer
than 120 s is logged as `<agent>: tick took Ns (<phase> Ns, ...)`.

### API mode (a turn under an API key)

A Claude turn may run on a provider API key instead of the subscription login: the profile
`<account>/api` of the host manifest (`[api] manifest`, else `[catalog] manifest`). `api_mode.py`
admits it only when every condition holds, in this order:

1. **Route.** The profile is the lane's explicit primary (`api_primary_profile`), or the lane's
   env command names it (`MESH_RUNNER_API_PROFILE=<account>/api`, a ref, never a key) as a reserve
   and the account's provider is in the lane's `api_reserve_providers`.
2. **Consent.** The lane has `api_optin = true`. Without consent, or for a profile the lane does
   not permit, the API route is ignored (event `api_route_ignored`) and the turn runs on the
   subscription as before; withdrawing consent therefore returns the lane to its subscription.
3. **Headless.** The lane's `launch_mode` is `headless` and the routed harness is Claude: the
   interactive program asks before it uses an environment key, so it never receives one. Otherwise
   the start is held (`api_refused: api_requires_headless`).
4. **Key.** `credential_ref` of the account names a carrier in `[api.credentials]`: a private file
   of the runner's user (mode 600, no symlink) with exactly one `ANTHROPIC_API_KEY=` line, an
   Anthropic API key (an OAuth token is refused), optionally pinned by `sha256_8`. The value goes
   into the turn process's environment only (other logins' tokens and `ANTHROPIC_BASE_URL` are
   dropped from it): never argv, the turn log, a mark, an event or a comment.
5. **Budget.** Every spend pool of the account's `api` mode has `[api.pools."<pool>"]` `cap_usd`
   and `run_usd`; a pool without a cap is never spent. Before the process starts the turn reserves
   `run_usd` in the ledger (`[api] ledger`, default `<state_dir>/api-spend.sqlite`, mode 600) and
   the CLI gets `--max-budget-usd <run_usd>`. Settled spend plus open reservations plus the new
   one over `cap_usd` refuses the start (`api_refused: budget_exhausted`, the attempt's note names
   it). When the turn settles, the pool is charged the turn's `total_cost_usd`; a turn without a
   result is charged the whole reservation, a process that never started
   has its reservation removed, so the same turn may reserve again.

The `system/init` event of the turn names the credential the CLI used (`apiKeySource`; `none` is
the subscription login); the `api_spend` event records it with the amount charged.
`mesh-runner api-budget --config runner.toml` prints each pool's cap, spend, commitment and open
reservations.

## Restart safety

Event streams go to files, not pipes; processes are detached into their own groups. A
restarted runner re-attaches by pid and log path: while the process group lives, the slot
stays occupied; once it is gone, the turn is settled from its event file. An attempt whose
process never started is aborted and its task given back. A turn cut off without a result
counts as a turn without an outcome (asked once in the same session, then back in todo with the same lane until new input
arrives; handed to the lead from the third turn in a row without a readable outcome).

## Worktree pool

`<state_dir>/worktrees/<repo>/slot-<n>`, added with `git worktree add --detach` from the
operator's base clone. A slot is handed out only when clean and fully pushed; on release
it is detached (freeing any branch) and returned. Ignored directories survive reuse.
Per-worktree git identity (`extensions.worktreeConfig`) keeps other worktrees untouched.

Several agents' pools may share one base clone, and every pool names its slots `slot-<n>`.
Git derives a worktree's admin id (`<common dir>/worktrees/<id>`) from the directory name,
so a new slot is added under `<agent>-slot-<n>` and then moved to `slot-<n>`: the admin id
stays agent-unique while the directory keeps its name. Before any git command runs in a
slot, its `.git` file must lead to an admin dir under the base clone's `worktrees/` whose
`commondir` and `gitdir` point back to that clone and that slot. A slot that fails (admin
dir missing, or reused by another worktree after it disappeared) is held with
`worktree admin link requires operator inspection` and is never leased, snapshotted,
restored into, cleaned or evicted; the periodic slot pass also checks free slots and logs
one warning per slot with the admin dir and listing timestamps.
Every slot's admin entry is locked (`git worktree lock --reason mesh-runner:<agent>`): a
`git worktree prune` run by a process that cannot see the slot's path (another mount view, a
sandbox) skips locked entries instead of deleting the admin dir of a live slot. New slots are
locked when made, existing ones by the periodic slot pass; eviction unlocks before
`worktree remove` and locks again if removal fails. When an admin dir is removed anyway, the
slot is recovered without an operator: a held slot whose work nobody resumes (no attempt, or a
finished one whose task has no open attempt) has its content committed onto a never-pushed
`refs/mesh-runner/archive/orphan-*` ref through a scratch index in the base clone (git never
runs in the slot), its directory moved into `<archive>/orphan-admin/<agent>/` on the same
volume and its row dropped; a leased slot is rebuilt in place during suspend (same content
laid back on the matched commit, its branch checked out when unambiguous), so its attempt
checkpoints and resumes. A pool whose starts are held names its held slots and their notes
in `pool_hold`.
Legacy `git worktree add` appended a decimal collision counter to the slot basename (`slot-1` -> `slot-11`, `slot-12`); `WorkspacePool.issue` accepts that unprefixed spelling only after the reciprocal `commondir`/`gitdir` proof above, the lease check and the same owner/repo/slot tail. A foreign repo, foreign back-pointer, other owner's tree, shared physical target or a non-numeric suffix is refused. No migration is needed: nothing on disk is renamed, and the runner release carrying this check is the whole recovery (rollback = the previous release).

## What the runner does not do

Review routing, account selection, notifications and team policies are the operator's:
they belong in the agent's profile and the optional preamble. The runner's prompts only
state the task, the worktrees and the outcome contract.
