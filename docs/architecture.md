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
4. **Wake.** Waiting attempts are checked every `wake_poll_seconds`: the condition itself
   (pipeline/MR state via GitLab, the other task closed, time, gate lifted) or any new
   comment from someone other than the agent. Woken attempts become `ready` and are
   resumed in the **same session** with a short message saying what changed.

## Launch modes

`[runner] launch_mode` decides how a turn runs; an agent may override it with its own
`launch_mode` (e.g. to try a mode on one agent, or to roll one back).

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
  settled transcript, at most `background_wait_minutes` (default 45, 0 = no hold), then
  pastes it anyway. The hold reports as an external wait: the first-output limit
  (`turn_first_output_minutes`) is not judged during it and counts from its end.
- **`headless`**: `claude -p --output-format stream-json`, `codex exec --json`; one
  process per turn.

Subscriptions cover the interactive programs; the non-interactive modes are billed per
token by the providers.

## Restart safety

Event streams go to files, not pipes; processes are detached into their own groups. A
restarted runner re-attaches by pid and log path: while the process group lives, the slot
stays occupied; once it is gone, the turn is settled from its event file. An attempt whose
process never started is aborted and its task given back. A turn cut off without a result
counts as a turn without an outcome (asked once in the same session, then back in todo with the same lane until new input
arrives; handed to the lead from the `no_outcome_route_after`-th turn in a row without a readable outcome).

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

## What the runner does not do

Review routing, account selection, notifications and team policies are the operator's:
they belong in the agent's profile and the optional preamble. The runner's prompts only
state the task, the worktrees and the outcome contract.
