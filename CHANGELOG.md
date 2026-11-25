# Changelog

## 0.2.129 — 2026-11-25

- **A standing chat stays up while the lane's cards go to a harness it cannot run (agy, kimi,
  zcode).** The chat's env read now passes `MESH_RUNNER_STANDING_CHAT_HARNESSES` (the chat's
  harnesses, home first, e.g. `claude,codex`) to the env command, so the operator route may answer
  with an env for one of them; card and card-less reads never pass it, and an inherited value is
  dropped. The raw `MESH_RUNNER_HARNESS` of the answer is checked against that list before routing:
  anything else keeps the chat held (`no chat command for the routed harness`) instead of starting
  a chat command with another harness's env. A command that ignores the variable behaves as before.

## 0.2.128 — 2026-11-25

- **An agy lane may carry named non-Mesh MCP servers: `mcp_allow`.** `mcp_allow = ["playwright"]` in
  `[[agents]]` (harness `agy`) or `[agents.alt.agy]` lists exact server names of the lane's `.mcp.json`;
  their stdio launch fields (`command`, `args`, `env`, `cwd`) are written with the Mesh servers into
  `<profile>/.agents/mcp_config.json` (0600, atomic). Without it the file stays Mesh-only. An allowed
  server never comes from the old workspace file, a disabled or remote (`url`) one is not carried, and
  it never stands in for the Mesh server (`agy-mesh-mcp-missing` still refuses the start). A read-only
  turn denies `mcp(<name>/*)` for each allowed server
  except one that is a Mesh server of the workspace. A pattern, an empty name or the key on another
  harness is a config error.

## 0.2.127 — 2026-11-24

- **A launch whose prompt never left is told apart from a start.** `start`/`resume` in `events.jsonl`
  and the `launch` event carry `delivery`, the launch's prompt token; `prompt_unsent_reconciled` and
  `prompt_quota_unbound_reconciled` in `events.jsonl` carry the same `delivery`, `launched_turn` (the
  launch's turn; the row's own `turn` is the restored count) and `delivered: false`, so a metric can drop
  the start they cancel. The relaunch after such an end reads `restart_reason: prompt_unsent` instead of
  `session_missing`; a resumed retry reads it instead of an empty reason.

## 0.2.126 — 2026-11-24

- **A pause token can say why it exists, and an overdue one is flagged.** A token whose content is a
  JSON object shows its `reason`, `card`, `by`, `created_at`, `expires_at` and `until_event` in
  `status.json` `pause_token_details`. A token past its `expires_at` stays and keeps the lane paused,
  but reads `"expired": true` and is counted in `mesh_runner_agent_pause_tokens_expired{agent}`.
  Empty tokens pause exactly as before. See `docs/operator-pause.md`.

## 0.2.125 — 2026-11-24

- **An interactive Claude session answers one trust question per untrusted `--add-dir` repository.**
  Claude Code 2.1.330 asks "Adding a new directory … Yes, add it" for every added directory whose
  repository was never trusted. The start-up limit now counts menus (one per `--add-dir` plus three),
  not keys, so a lane with many repositories becomes ready; a screen not yet redrawn after a key gets
  no second key, so an Enter never lands on the next menu's preselected "No, exit". A start that
  still fails names the menu left open (its directory, repository and options) instead of a cut
  screen tail.
- **A turn whose prompt was never sent lifts the lane's capacity hold.** A confirmed release on the
  unsent end of a turn (`prompt_unsent_reconciled`) now clears the lane-wide capacity hold like an
  ordinary turn end, so the next card of the same identity asks the start gate at once instead of
  waiting out the hold's backoff (2-10 min) behind a slot that was already free.

## 0.2.124 — 2026-11-24

- **A turn whose prompt was never sent gives its capacity reservation back.** The unsent end of a
  turn (native process gone, attempt back to `ready`) released the start gate's reservation only
  for a quota refusal; otherwise the consumed server slot outlived the turn. Enough of them filled a
  lane's identity cap, every admit was refused `identity_cap_reached` and the lane stood. Each tick
  now also releases a gate still held by a ready or waiting attempt with no process (log line
  `attempt without a process held a gate reservation; releasing it`), which frees slots stranded
  before this change; an unconfirmed release stays pending and is retried.

## 0.2.123 — 2026-11-22

- **A git killed on its timeout no longer jams the slot with its `index.lock`.** When the Runner
  kills a git after the 600 s timeout, an `index.lock`/`HEAD.lock` that was absent right before that
  git started and is open in no live process (`lsof`, or `/proc` without it) is exchanged with an
  empty directory barrier. After checking the moved inode and holder again, it is retained as
  `<lock>.killed-<utc>-<ns>` and the barrier is removed; a concurrently substituted lock is returned
  while the barrier keeps a second writer out. The log records both paths and rollback `mv`.
  An existing lock, missing index/HEAD target or unsupported atomic exchange is left alone.
  If returning a candidate fails, the barrier and file remain and an ERROR gives manual recovery
  instructions for use after stopping writers.

## 0.2.122 — 2026-11-22

- **A session carried over from another account no longer fails its resume on that account's limit.**
  A new window resumed on a session draws its history, which may end with the old account's
  limit notice; before the paste the interactive driver no longer reads it as this account's
  limit (an offer to continue there is dismissed). This account's own limit still ends the turn
  once it meets the prompt.

## 0.2.121 — 2026-11-22

- Add the agy harness for Google Antigravity CLI in print mode, with resumable conversations, explicit session permissions, Mesh MCP filtering and admission commands before every start.
- Retry an egress refusal for the affected lane without treating it as a quota or pausing other lanes.

## 0.2.120 — 2026-11-22

- Hold a provider-limited task until its quota resets when no switch target is available, preserving its checkpoint and allowing other tasks to run.
- Dismiss an expired provider limit notice before sending the next prompt.

## 0.2.119 — 2026-11-22

- Recognize the active terminal busy region without treating historical output as a current busy signal.

## 0.2.118 — 2026-11-22

- Require independent source review for pinned external merge requests when a task merge request is already merged.

## 0.2.117 — 2026-11-22

- Recheck cached start-gate refusals when explicitly declared installed hook or helper bytes change, while preserving full gate and checkout admission.

## 0.2.116 — 2026-11-21

- **Follow card waits that name only a short id.** The wait-root radar resolves the
  target with the waiting lane's access and the existing read budget and cache,
  so readable targets receive their usual classification instead of `unknown-target`.

## 0.2.115 — 2026-11-21

- Preserve preconfigured repository pools when additive workspace admission fails, and report a bounded credential-masked failure reason in its receipt and log. Mesh-managed admission pins the permitted host inventory specification, including its slot limit, rather than the legacy repository entry.

## 0.2.114 — 2026-11-21

- **What changed.** A Codex card turn's Mesh MCP server now sees the runner carrier
  (`MESH_RUNNER_TASK`, `MESH_RUNNER_LANE`, `MESH_RUNNER_CHECKOUT_*`). Codex starts a stdio MCP server
  with an allowlisted environment, so its memory reads reached Mesh without a task or session id.
  The Codex adapter now names the carrier in `mcp_servers.<name>.env_vars` (names only, values stay in
  the environment) for each Mesh MCP server defined in the config file Codex loads for the launch's
  `CODEX_HOME` and profile; an unreadable config leaves the launch unchanged. Cardless turns (answer
  desk, chat) carry no checkout and stay without ids.

## 0.2.113 — 2026-11-21

- **A returning card starts without replaying its snapshots twice.** Adopting the previous run's
  working-copy snapshots replayed each of them into scratch under the process-wide checkpoint lock, and
  the restore right after replayed them again. The start step `task` of such a card took up to 147 s
  (a lane with 13 repositories: 91 s) and held every other lane's capture and restore meanwhile.
  Adoption now checks the stored bytes without a replay: the tree against its recorded inventory and
  every git index against the sha256 the capture now records, or, for a snapshot taken before, against
  the index's own checksum. Only such an older snapshot whose index git wrote without a checksum
  (`index.skipHash`) is still proven by a replay. A damaged snapshot is still refused before anything
  is transferred or leased; it holds only its card, which starts once the snapshot is whole.

## 0.2.112 — 2026-11-20

- **A lane's team-directory entry follows the harness and model its turns actually ran on.** After
  each settled turn on the lane's own route the Runner compares the harness and the model the harness
  itself recorded (Claude's stream init, Codex's turn_context) with the lane's Mesh profile and reports
  `agent_type`/`model` through the agent heartbeat when they differ. A lane switched to another
  harness, or moved to a Codex profile by a quota fallback, no longer announces its old model family to
  review routing and reviewers. Configured defaults are never reported: a turn without an observed
  model reports only a family change and clears the old model. A card routed to its own harness by a
  label leaves the lane's entry alone.

## 0.2.111 — 2026-11-20

- **A lane's standing chat is checked between the steps of a long tick too.** A chat session that died
  during a long start (twelve repositories) or a long tick stayed down until the tick ended; the check
  keeps its once-a-minute pace and its failure never fails the start.
- **A lease no longer fetches a base fetched successfully within the last 60 s.** A start of a lane with
  twelve repositories fetched each over the network, and the next start in the same tick did it again;
  the held-slot recheck still fetches every time.
- **A start longer than 60 s is logged with its steps** (`<lane>: #<card> start took Ns (reserve, env,
  preflight, checkout, task, workspaces, admit, end)`), so a slow `start` phase names the step that took it.

## 0.2.110 — 2026-11-20

- **runner.log no longer fills with `MallocStackLogging: can't turn off malloc stack logging`.**
  On macOS, after the first critical memory pressure event, every child the Runner forked printed
  that line into runner.log before its streams were redirected (97% of the file, until the next
  restart). The Runner now turns malloc stack logging on and straight off once at start, which keeps
  the fork hook silent; the startup line `fork notice: armed: …` shows it took effect.

## 0.2.109 — 2026-11-20

- **Wait advice for another card's code names its merge request, never the card.** When a fix or a
  dependency is another card's code, the runner's texts (a refused time wait on a red head, the notice
  on someone else's merge request, a sent-back `until=change` wait, a sent-back `until=deployed` card
  wait) now advise `wait_for on=mr … until=merged` or a stack on its branch, and with no merge request
  yet `wait_for on=card target_condition=artifact_published`, then the stack. A plain card wait held
  the waiter until the owner closed the card, long after the code merged.

## 0.2.108 — 2026-11-20

- **Restores no longer refuse an empty submodule after Git refreshes only its index metadata.** Staged changes still block replacement.
- **A merge-only card closed without the merge is no longer handed the same head again.** The
  author's repeated `wait_for on=merge` used to find that closed card by its stable key, "hand" the
  merge to it and wake at once, on every re-arm, with no new attempt by the lead. Now the re-arm wakes
  once with the cause quoted from the closed card (its assignee's last comment) and says how a new
  attempt starts: move the card back to todo, then declare the wait again. A closure seen while
  waiting quotes the cause too.
- **A merge gate that gets 404 for an open merge request is classified `merger-no-access`.** The
  runner reads the merge request as open, so the 404 means the gate's GitLab identity cannot see the
  project, not a vanished MR. The wake says so and points to an access card; on a merge-only card the
  runner notes it there and the card's instructions say not to close it.

## 0.2.107 — 2026-11-20

- **A turn that ends while another card's start restores its working copies gives its reservation
  back during that start.** The release pass ran between the units of the start loop, and one start
  of a lane with many repositories restores or leases every working copy inside a single unit (up to
  14 minutes on the fleet host), so an ended turn kept its capacity slot until that start finished.
  The workspace pool now calls the runner's release pass after each repository it leases or restores,
  outside the repository's lock; a failing pass is logged and never fails the lease or the restore.

## 0.2.106 — 2026-11-19

- **A session woken on a settled pipeline, merge request or merge gets the result in the wake prompt.**
  The runner reads it once and puts it under «The runner already read the result». For a pipeline that is
  the sha, the ref, the link and the job counts. A failed pipeline adds the failed jobs (stage, failure
  reason, job id), the jobs that failed but were allowed to, and the last 25 lines of the first failed
  job's log, with terminal codes and section markers removed. A downstream pipeline adds its status. A
  merge request adds its state, head and link; a merged one adds the merge commit, the target branch and
  the pipeline on the merge commit. Measured on claude session logs over 7 days (all lanes): after such a
  wake the first turn made 4.3 to 6.8 glab, git and Mesh calls, mostly re-reading the merge request, the
  job list and the failed job's log. A read that fails leaves the wake as before, with no detail.
- **A turn that ends during a long tick gives its gate reservation back in that tick.** The settle step
  ran once per tick, at its start, so a turn that ended while a busy lane spent minutes starting or
  resuming other cards kept its capacity slot until the next tick, and other lanes were refused that
  slot meanwhile. Now the reservation of every ended turn goes back, and its leader is reaped, between
  the steps of the tick and between the units of the start, resume, wake and skip-state loops; the
  settle step returns the reservations of all ended turns before it settles the first one. The turn
  itself is still settled by the next tick's settle step, so its card is never resumed in the tick its
  turn ended in (one the watchdog cut off included). A turn whose prompt delivery is not proven keeps
  its reservation until the settle step decides.
- **A process group whose only members are zombies counts as gone.** The turn's leader is the runner's
  own child, so after it exits it stays a zombie until reaped, and `killpg(pgid, 0)` cannot tell such a
  group from a live one (Linux answers success, macOS answers `EPERM`). The liveness check now reads the
  members' states from the kernel (`sysctl` on macOS, `/proc` on Linux); an unreadable state still
  counts as alive.

## 0.2.105 — 2026-11-19

- **A lane's row in `status.json` is complete right after the runner starts.** Until a lane's first
  tick finished or beat (30 s after it began), `status.json` showed it as a placeholder with only
  `agent` and `tick_at`, no `cap`, `paused` or `attempts`, and every reader that checks a lane took
  every lane for an unknown state for about 30 s after each restart. A lane now publishes its row in
  its first tick as soon as its Mesh identity (and so its cap) is read, and a new process holds its
  first `status.json` until every lane has a row of its own or the error of its tick, at most 30 s;
  the previous process's file stands meanwhile. A lane still silent then keeps its placeholder and
  the runner logs `first status without a row of its own`.
- **A settle that may not clear a released working copy is held on that exact path, like resume.**
  A `PermissionError` while settling a turn (a root-owned `__pycache__` left in the slot) went to the
  step's blind quarantine, 15 minutes growing to 4 hours, with no hold event, so the alert never fired
  and a repaired path still waited out the pause. `settle` and `prompt-delivery` now take the same
  `permission_hold:<step>` mark and `workspace_permission_hold` event as `resume`: no retry while the
  path's owner, mode and ACL are unchanged, and one retry on the next tick (`permission_path_changed`)
  once they change.
- **The first prompt carries the card description.** Start and review prompts include the description
  (cut at 6000 characters, with a marker) and the count of open dependencies, so the session no longer
  spends its first call reading the card; a row without a description is read by id first.
- **The no-outcome correction is short.** It points to the outcome contract in the session's first
  prompt instead of repeating it.
- **A new attempt names why it started.** The launch event and log line carry `reason=new_attempt`
  with the previous attempt's state and note when the card had earlier work.
- **A compacted session gets the full outcome contract again.** The short correction points to the
  contract in the first prompt; after a context compaction that prompt may be gone, so the correction
  carries the whole contract instead.
- **The answer desk asks once more after a harness failure.** When the addressee's session ends on a
  harness error, the desk queues the question once more before the asker files a request card; an
  addressee that ends its turns without an answer, or a turn that runs out of its time, is not retried.

## 0.2.104 — 2026-11-19

- **A review held by `qa_no_selection:no_available_route` is retried on schedule without a card
  change.** A route or quota refusal clears off the card (a routing rule change, a restored pool, a
  quota reset), so the runner now treats it like other QA infrastructure holds: one notice, then the
  start gate is asked again every `review_unstartable_retry_minutes`. Before, such reviews waited
  for a comment or an edit of the card. Reviewer absence (`qa_refused:*`, other `qa_no_selection`
  reasons) still waits for a card change.
- **A card can name the code it merges after, and the merge gate waits for it.** A `merge-after:`
  line in the card description (another card, `group/project!iid` or its GitLab URL) holds the merge
  wait's gate until every predecessor is merged, also when the runner declares the wait itself on a
  SHIP verdict; before, a SHIP handed back to the author ran the gate in the same tick and could merge
  ahead of the code it builds on. The author's own `wait_for on=mr until=merged` on another merge
  request is carried into the runner's merge wait the same way, also without a baseline and when that
  request cannot be read; when Mesh cannot say which wait the author holds, nothing is declared. An
  order that can no longer be met (card cancelled, merge request closed, a line naming nothing
  readable or nothing at all) wakes the author; anything unreadable holds. See README § Merge waits.
- **A merge wait on a card with an empty description no longer fails.** Mesh leaves an empty
  description out of the card it returns; the gate took that for an unreadable card and woke the
  author after three checks instead of merging.

## 0.2.103 — 2026-11-19

- **A refused snapshot of a card's working copies is a declared hold, not a silent retry.** A slot
  whose populated submodule keeps its repository inside its own path (`.git` a directory, e.g. a
  clone made there by hand) could not be snapshotted, and every settle, prompt delivery and
  workspace suspend of the card was refused with the same `populated submodule unsupported` line,
  the slot staying leased, while only `runner.log` said so. The refusal now names the layout
  (`populated submodule unsupported (embedded .git directory): <path>`; absorbing the submodule's
  repository, `git submodule absorbgitdirs`, repairs it). Every refused snapshot of a card counts in
  a streak (count, call site `wait|ownership|resume|start`, held slots, cause) that survives a
  restart, is published in `status.json` as `suspend_refusals` for alerting, and ends with the first
  snapshot that goes through or when the slot is given back. The card gets one comment per attempt
  that its snapshot keeps failing and its lease is retained, now also when the refusal comes from
  settle or prompt delivery.

## 0.2.102 — 2026-11-19

- **A resumed Claude session no longer reports its whole running cost again on every turn.**
  Claude's `total_cost_usd` and `modelUsage` are totals of the session, so each turn's spend
  report (and the completion comment's notional cost) now carries their increment over the
  session's previous turn; the first turn of a session is taken as it is. Turn tokens come from
  the `modelUsage` increment (subagent calls included) when the harness reports it. The totals are
  kept in the state store, so a restart between turns keeps the same report. The API-mode pool
  ledger still settles on the turn's raw `total_cost_usd`.

## 0.2.101 — 2026-11-19

- **A start that a full slot table moves to an overflow route is no longer logged as a capacity
  hold.** The start attempt whose admit is refused and fed again at once on an overflow route used
  to end with `aborted` / `held: runtime capacity unavailable` in `events.jsonl`, even when the
  overflow feed started the card seconds later; resumes never did. The superseded attempt now gets
  its single event once the overflow feed has answered: `overflow: … started on its overflow route`
  when it started, the overflow attempt's own end when that one was refused again, and the old
  capacity hold only when no open profile was found. Counting capacity holds in the event log now
  counts cards that really stay held.
- **The spend report can carry the turn's id, times, provider, account, billing mode and cache split.**
  With `[spend_report] turn_fields = true` each turn's report also sends `turn_id`, `started_at`,
  `ended_at`, `provider`, `account_ref` (`<harness>/<account>`, the name of the account's quota pools),
  `billing_mode` and `cache_read` / `cache_write` / `cache_write_1h`; a cache figure the turn did not
  report is named in `unknown`, never sent as 0. Off by default: turn it on only against a Mesh that
  accepts these fields. `[spend_report.account_refs]` names the account of a default login directory.
  See `docs/spend-report.md`.

## 0.2.100 — 2026-11-19

- **The checkpoint size gauge measures a store kept behind a link.** `pool_root/checkpoints` may be a
  symlink onto the slot volume, so that snapshots are APFS clones instead of byte copies on another
  volume; `du` counted such a link as zero, and `mesh_runner_checkpoint_bytes`, `status.json`
  `checkpoints.bytes` and the budget warning read 0. The size is now taken from the link's target.
  Tests pin that a store linked onto the slot volume clones every delta file and restores exactly,
  and that a store on another volume copies them.
- **A stale submodule is set aside even when the slot and its git directory sit on different
  volumes.** A restore into a pooled slot moved the leftover submodule worktree next to its gitdir;
  with the slot on one volume and the base clone's git directory on another that rename failed with
  `Cross-device link`, and every start of the lane paused again and again. The worktree is now parked
  on its own volume in `.runner-stale-submodules-<slot>` beside the real slot directory (the gitdir
  stays in `runner-stale-submodules` by the modules directory), so both moves remain atomic renames.
  A set-aside that still fails is a refused restore of that card (`restore_refusals` in
  `status.json`), not a pause of the lane. Parked pairs are kept for the operator; nothing purges them.

## 0.2.99 — 2026-11-19

- **A card's turn prompt lists the open evidence edges waiting on it.** A card that another card
  waits on with `artifact_published`, `access_ready` or `contract_ready` used to learn nothing about
  it: only an attestation on the producer's side releases such an edge while the producer is open,
  so a published result could sit unattested for hours. The first prompt and every resumed work turn
  now name each open incoming evidence edge — the waiting card, the condition, the exact criterion,
  the dependency id — with the one call that releases it (`update_dependency … evidence_criterion=…
  evidence_url=… attest=true`). Released, attested and `task_done`/typed edges are not shown; with
  no such edge the prompt is unchanged. An unreadable edge set drops the block with one warning in
  the runner log and never fails the turn.

## 0.2.98 — 2026-11-19

- **Checkpoint cleanup now removes captured read-only directory trees.** It grants owner rwx through
  each already-open directory descriptor before unlinking children, without following symlinks, so a
  read-only stand no longer leaves its lease suspended on a permission error.
- **A time wait on a card whose merge request is still open is refused.** Its CI result and its
  merge are the events, not a clock: the correction names the `wait_for on=mr`, `on=pipeline` or
  `on=merge` command that fits the head pipeline's state (a failed or manual head gets its action
  first). Repeating the timer ends in the usual correction hold. A time wait already sleeping is
  checked the same way at most every 10 minutes, and its session is resumed with that hint; only the
  runner's own schedule is removed. The open state is confirmed with GitLab (or GitHub): when the
  links or the forge cannot be read, the timer is accepted and the reason is logged.
- **A fetched submodule tag on a published commit no longer blocks a card's restore, and a
  restore refused again and again is reported.** A stale submodule gitdir in the target slot is
  set aside when its tags only name commits a remote already has, or tags the incoming checkpoint or
  the last fetch brings; a tag on a commit no remote has, or an annotated tag made locally, is still
  refused. Replayed submodule gitdirs now pack their refs like a clone, so the next card restored
  into the same slot is not refused on the Runner's own loose tags. A refusal caused by the target
  slot (stale submodule work, an active git writer, a symlinked gitdir or an unsafe submodule
  pointer) holds only that card, on the start and the resume path; its streak (count, slot, cause)
  survives a restart and is published in `status.json` as `restore_refusals` for alerting, and it
  ends when the card gets its working copies.
- **An unsent prompt held in `unknown` because its scope changed now ends by the quiet rule, and the
  same workspaces in another order are no longer a changed scope.** A proven-unsent prompt whose
  lane's workspaces or harness changed while it waited went to `unknown`
  (`prompt_retry_scope_changed`), and the quiet rule skipped every retryable intent, so only an
  operator token ended it: such attempts stood for days with their cards. It now ends like any other
  quiet `unknown` (turn process gone, evidence unchanged for the quiet minutes): no replay, the card
  starts in a fresh session, a comment says why, and `runner.log` logs the return to the queue. The
  narrow restored-scope recovery still runs first, and an operator token still wins. The scope check
  compares the leased workspaces as a set: the same paths in another order send the exact prompt.

## 0.2.97 — 2026-11-19

- **A task hold can wait on a wake file instead of being re-asked every two minutes.** A start
  gate that exits 76 may print `HOLD_WAKE_FILE=<absolute path>` and `HOLD_WAKE_SIG=<signature>`;
  the hold then ends on the first tick at which that file reads differently (or the task is
  updated), with a two-hour guard, rather than on the gate cadence. A review waiting for its route
  was re-asked 437 times in a day before. Gates that print no pair behave as before. See the
  `start_gate_command` section of the README.

## 0.2.96 — 2026-11-18

- **A checkpoint fault of one card no longer stops the whole lane, and no longer repeats forever.**
  A slot returned to the pool drops its restore marker, so a restore released before launch (an
  admission refusal) can be replayed into that slot again instead of being refused as "newer work"
  on every start. The checkpoint pass keeps the snapshots an unspent ownership-loss note still names
  (up to 14 days), so a card that comes back can adopt them. When those snapshots are gone in whole or in part,
  the start continues on the card's previous paths and says so once on the card. An inventory fault
  or a refused restore holds only that card (task backoff, one comment per distinct fault, retried after the backoff); the lane-wide start
  pause stays for fetch, authentication and disk errors.

## 0.2.95 — 2026-11-18

- **A turn whose harness reports no price is reported to Mesh at the model's list tariff.**
  Interactive Claude, Codex, Kimi and ZCode turns sent only token counts, so the cost of most cards
  read «—». The spend report now prices such a turn from its tokens (input, output, cache read, cache
  write by 5m/1h lifetime; Codex cache hits inside its input) with a built-in table of provider list
  prices for claude-opus-5-5, claude-sonnet-5-5 and gpt-6.1-sol/6-luna/6-astra; `[prices."<model>"]`
  in the runner config adds or replaces a tariff (with its `source`), `[prices] builtin = false` drops
  the table. A model without a tariff, or a cache write whose lifetime is unknown, stays unknown. The
  interactive Claude driver now keeps the 5m/1h cache-write split in the turn's usage. A provider's
  own `total_cost_usd` still wins.

## 0.2.94 — 2026-11-18

- **The API spend pool is charged each turn's own cost, not the session's running total.** A resumed
  harness session reports `total_cost_usd` for the whole session so far; the API ledger charged that
  total on every turn, so a pool ran out 1.5–3 times sooner than the provider bills. Each API turn is
  now charged its total minus the total the previous turn of the same session reported (kept across
  runner restarts); a first turn or a new session is charged in full, and a turn that reports no cost
  still takes its whole reservation. Ledger rows charged this way carry the source
  `total_cost_usd_delta`; rows already written stay as they are.

## 0.2.93 — 2026-11-18

- **A card wait sent back because its target waits for a person or a timer no longer leaves its edge
  behind.** The runner never armed that wait, so no wake ended it: the next outcome only superseded it
  in Mesh, and the blocks edge Mesh made for it stayed. After the session ended the card was held as
  blocked by the target, a gate it never declared. The sent-back wait is now resolved as `cancelled`
  with the reason, and the edge is removed with the owner-checked delete when the receipt says this
  wait owns it, the same as a sent-back `until=deployed` wait. A failed delete is logged and does not
  hold the send-back.

## 0.2.92 — 2026-11-18

- **A question whose addressee's transport gate holds every turn now goes to its request card instead of
  waiting on the answer desk without end.** The desk stamps each request it cannot start because of the
  lane's transport gate with the moment its hold began (the later of the lane's hold as the desk first saw
  it and the question's own arrival); 20 minutes later (`FALLBACK_SECONDS`, the same
  answering time a delivered turn gets) the asker files the `[decision]` card with the gate's reason, and a
  routed or mention question is refused or carded the same way. The clock measures one continuous hold:
  the gate letting a turn through, or a memory, load or pause wait in between, restarts it for every
  question of the lane. The hold is logged once per reason
  (`answer desk: transport gate holds …`), shown as `transport_hold` in the lane's `answer_desk` status
  block, and the gate hook is asked again only after 60 s instead of on every 2-second desk pass. Memory,
  load and host-slot waits still spend none of the 20 minutes.

## 0.2.91 — 2026-11-18

- **A provider's refusal of a Claude turn's content is no longer retried as a harness error.** A turn
  whose last assistant message carries the typed `stop_reason: refusal` used to be resumed in the same
  session until the failure cap (each resume refused the same way), then handed up as an opaque
  `harness error [sha256:…]`. The runner now records the refusal (`provider_refusal` in the attempt's
  events and in `events.jsonl`, with the harness and `stop_details.category`) and never resumes that
  session: when the lane also runs the other harness of the Claude/Codex pair, this card alone
  continues there in a new session (once per card; other cards keep the lane's route); otherwise the
  card goes up at once with `provider refusal: <category>`. The refusal is read before the turn cap
  and the refused turn that moves the card is outside the turn budget, so a refusal on the last turn
  still moves the card or names its category. Neither the provider's text nor the prompt
  is published. The env command of a card routed to its own harness (this route or the
  `runner-harness:<h>` label) now gets `MESH_RUNNER_TASK_HARNESS=<h>`, so it can build that harness's
  environment.

## 0.2.90 — 2026-11-18

- Preserve a returning task's branch, commit and uncommitted work when its previous worktree slot has been reused. A live branch holder blocks the return without launching another writer or retaining an extra lease; releasing the holder lets the same attempt retry.

## 0.2.89 — 2026-11-18

- **Retry review admission after infrastructure holds with diagnostic details.** Missing pool evidence remains an infrastructure refusal when the gate appends its cause and next actor, so the runner probes again after the retry interval even if the card has not changed.

## 0.2.88 — 2026-11-17

- A reviewer's SHIP written below a blank line (a hand-off note: one line naming the new owner, a blank line, then the verdict) now gets the runner's merge wait: the verdict line is read from the verdict word, not from the start of the pattern match (17.11: 20 of 49 SHIP verdicts had that shape and none got the runner's wait). The enumerated-verdict check reads the same line.
- A card returned to its author is looked at between the steps of the author's tick and between the cards of its long loops (`ShipDue`): a review turn end on this runner and Mesh `task.assigned`/`task.updated` events make it due, so the merge wait no longer waits for the start phase of the next tick (busy lanes tick 135-274 s).

## 0.2.87 — 2026-11-17

- **A headless kimi turn ended by the provider's account limit before it bound a session is a quota
  failure, not an unknown delivery.** Kimi's print mode names its session only after a completed turn, so a turn
  refused on the usage window (at once on a resume, or after it had already worked on a fresh session) left no
  session id, and recovery read it as `headless-native-session-mismatch`: the card was held in `unknown` with an
  operator alert, and the lane was never moved to another account. Now the CLI's own terminal failure, typed by
  the adapter as the account's limit, keeps the exact pending prompt (not acknowledged, pending wakes undelivered),
  runs the on-limit command and requires fresh account capacity for the next start. The pending request continues
  in a new session, including when the account changes within Kimi; its previous session id is never sent to the
  fresh account (event `prompt_quota_unbound_reconciled`).
  Any other failure without a session, an authentication refusal, limit words that only appear in the output
  of the turn's commands, or a limit line beside another terminal failure the turn names (a typed stream error,
  an earlier failure line) still hold the card as before. Recovery is limited to Kimi; other harnesses without
  a session keep their existing unknown-delivery behaviour.

## 0.2.86 — 2026-11-17

- **A decision request whose close Mesh refuses for an unmerged linked merge request is no longer
  retried every pass.** When the request card carries an open merge request, Mesh answers the move to
  done with `422 done_evidence_required`; the runner used to repeat that write and log a warning on
  every pass (hundreds of lines a day per card). The refusal is now held like a human-gate refusal:
  one warning, then a retry only when the card changes (the merge request merged or unlinked) or
  every 6 hours.

## 0.2.85 — 2026-11-17

- **After a reviewer's SHIP the merge gate starts in the same tick that declares the merge wait.** The
  runner used to declare the author's `wait_for on=merge` in one tick and start the gate on the next wake
  check, one tick later (about 20 s of the 77 s measured live from SHIP to the first gate run). The first
  wake check of that wait now runs right after the declaration; later checks keep `wake_poll_seconds`.

## 0.2.84 — 2026-11-17

- **Answer desks select the recipient's lane in the asking card's workspace.** Each lane keeps its own scoped credentials; supported headless lanes can answer without a work card, and host contention does not consume the answer deadline before the first delivered turn.
- **Decision fallback reuses explicit answers and existing work.** Exact recipient answers and explicitly linked incidents prevent duplicate requests; orders naming a merge request or branch reach the recipient's matching active card when the target is unambiguous.
- Restore a locally compressed Codex rollout before resuming its thread, preserving the original session instead of starting a new one after idle-session retention.

## 0.2.83 — 2026-11-17

- **A card refused at admit for a full slot table is fed again at once when the start gate asked
  for overflow.** A gate that turns a server slot refusal (`acquire_refused:pool_exhausted`) into
  an overflow request now prints `TASK_ROUTE_OVERFLOW=requested`; the Runner then runs the card's
  next reserve in the same call, so the card moves to the next profile of its chain within the
  tick instead of waiting for the next one. Once per call, never a loop; a refusal without the
  key keeps the ordinary hold. Start and resume both apply.
- **Answer comments now complete answer-desk requests.** Fresh recipient answers are recorded in Mesh even without `answer_decision`. Addressed questions use the desk, notifications and program-generated mentions stay silent, and explicit operations for metadata-only recipients receive workspace-required access requests under ordinary permissions.
- **Fix managed partner reservation release after Runner retires a checkout.** A stopped attempt can release its exact durable pending reservation using its last published fence generation; admission and renewal still require the live checkout. Confirmed releases now acknowledge the Runner end-hook protocol.

## 0.2.82 — 2026-11-16

- **A card wait on another card ends when that card's merge request is merged.** A plain
  `wait_for on=card` used to sleep until somebody closed the target, often hours after its code was in.
  Now the Runner wakes it as soon as every merge request linked to the target is merged or closed and at
  least one is merged (Mesh link status, confirmed by GitLab); a target already merged when the wait is
  declared releases it on the first check. Mesh's link status alone never releases it: without a GitLab
  observer, or for a merge request this GitLab does not hold, the wait stays until the target closes. Each merged set is told once, so a re-declared wait on the same
  target sleeps until closing. A target without a merge request still ends the wait by closing. Typed
  conditions (`target_condition=deployed|accepted|shipped`) and question waits are unchanged.
- **The Runner declares the merge wait itself after a reviewer's SHIP.** When a review verdict of
  someone else names the head of the card's one open, non-draft GitLab merge request, the Runner declares
  `wait_for on=merge` for the author through its own checkout and parks a sessionless attempt on it: the
  merge gate runs on the next wake check, and the author's session starts only on the gate's answer
  (merged, refused, closed, head moved), with that answer as its event. This holds whether the card stays
  in review or the reviewer hands it back to the author (todo): there the author's earlier session goes on
  when it can. The author's own verdict, a verdict naming no commit, several open merge requests, a draft,
  a disabled merge gate, a refused declaration or a comment of someone else after the verdict keep the
  ordinary path (the author is started with the verdict), once per verdict.
- **A review start held for its official route is re-asked after 2 minutes, not 10.** The router writes
  that route without touching the card, so the task hold used to sit its full ten minutes after the route
  was ready.

## 0.2.81 — 2026-11-16

- **A repository added to a lane now reaches the lane's waiting attempts on their resume.** Until now an
  attempt kept the repositories it was admitted with: a repository added to `[[agents.repos]]` (or
  granted in Mesh repository access) later reached new attempts only, every waiting one asked an
  operator for `admit-workspace-repo`, and one that had released its worktrees into a checkpoint
  could not be helped at all (`rejected_checkpointed`) and asked again after the restore. On resume,
  after a checkpoint restore too, the runner now leases each missing configured repository through
  that command's own journal and tells the turn `[runner] Repository <name> was added to this task`.
  The restore still brings back exactly the journaled set and refuses an inconsistent one as before;
  an ambiguous inventory adds nothing, a full pool skips the repository for that turn. Event
  `repo_admitted_on_resume`; see `docs/repo-preflight.md`.

## 0.2.80 — 2026-11-16

- **A repository-access refusal now reaches its contact even when the contact just spoke on the card.**
  The mention path drops an `@agent` line on someone else's card when that agent commented there in
  the last 24 hours (it reads as a reply, not an ask). The refused lane's `@<contact> repository
  access for lane …` handoff was dropped the same way, so a lane whose card had just been answered
  by its access contact stood silent until an alert. A comment of exactly that shape (opening
  `@<contact> repository access for lane` line, `repo_preflight=` facts with an access reason and
  `next_actor` = the addressee) whose Mesh author is the card's assignee (the only lane whose runner
  refuses that card) is now filed as a request card regardless of the conversation; the runner logs
  `is a repository access refusal: filed whatever the conversation`. The same text from anyone else,
  and plain replies, keep the 24-hour rule.

## 0.2.79 — 2026-11-16

- **A turn the Runner cut at its own limit after the session took the prompt no longer goes to `unknown`.**
  A turn stopped at `max_turn_minutes` (or by the turn watchdog) leaves an idle-looking window; the next
  start on the host closed it to make room, its hook events were dropped, and recovery read
  `cursor-mismatch`: the card was held 15 minutes and the operator got an alert. When the driver's
  journal shows the session took this prompt (`accepted`), the end of such a turn is known: the
  attempt ends at once the way the quiet rule ends a held one (prompt archived in a mark, never
  replayed; window closed; the card starts in a new attempt), with one card comment naming the
  limit and event `prompt_ended_by_own_cut`, without the hold and the alert. A cut turn whose
  journal never reached `accepted` still goes to `unknown`.

## 0.2.78 — 2026-11-16

- **A repository with submodules is captured in the slim form and proven in place.** Before, any
  configured submodule sent the capture to the full form: the whole worktree copied into the
  checkpoint and, since the proof in place refused it, the whole checkout laid out again in scratch
  (thousands of file events per suspension, a clean submodule included). Now the
  superproject is stored as a delta over HEAD and each populated submodule as a delta over its own
  HEAD; the proof models the submodule gitdirs and hands over to the scratch replay only when the
  source's submodule gitdir reads differently (`not decided (submodule-config)`, `submodule-info`,
  `submodule-state`, `submodule-bundle`). Such a manifest is version 3, which an earlier runner
  refuses rather than replays; see `docs/wait-workspace-checkpoints.md`.

## 0.2.77 — 2026-11-16

- **A review that keeps ending without a verdict goes to the lead instead of standing until UTC
  midnight.** Review attempts the infrastructure stopped before their first round (a quota or limit
  switch, a harness error, a spawn failure, a prompt never proven delivered) no longer count toward
  `daily_start_cap`. At the cap the runner writes a `REVIEW CAP` note on the card (the attempts and
  how each ended, the merge request head, the reviewer) and files a request card for the next lead
  up the agent's chain, where before the card was held silently with `daily start cap reached`. A
  comment on the card after the hand-up from anyone but the reviewer, a new reviewer, or the new
  operator command `mesh-runner rearm-review --agent --task --reason [--apply]` starts the count
  again; each reset is kept with its author and reason. Two hand-ups in a row without a verdict go
  one level further up; a verdict ends the run. README: "Review attempts without a verdict".

## 0.2.76 — 2026-11-16

- **A routine WAIT capture no longer checks the whole tree out to verify it.** At capture a slim
  snapshot is now proven in place: HEAD's blobs are read from the object store and hashed against
  the recorded fingerprint, the replay's layout is predicted, and Git's view (staged and unstaged
  diff, status) is computed from a fresh repository with the stored index over the source tree, as
  the replay computes it; every Git read of the proof (config and attributes included) runs in
  that repository, never with the source's own settings, and reads config as the replay's
  commands read it (`GIT_CONFIG`, which redirects only `git config`, is not passed to it). Before, every capture wrote and removed
  the full checkout under `.verify-*` in the checkpoint directory (tens of thousands of files per
  suspension of a large repository), a major source of file-system events on the host. What the proof does not model
  (submodules, checkout conversions, `core.symlinks=false`, any config key not on the proof's
  allow list, a relative path Git resolves against the work tree (`core.attributesFile`,
  `core.excludesFile`, `init.templateDir`, `HOME` and the other variables Git finds user-wide
  files through), a conditional `includeIf` in the config,
  an fsmonitor or a hook the replay runs, which would otherwise start in the source, a changed
  `.gitattributes`, a deleted symlink, any mismatch)
  still goes through the scratch replay, which stays the authority; re-verification, compaction
  and restore keep the replay.
- **A truncated, corrupt or missing checkpoint bundle is refused on every verification path.** The whole
  pack is read with `git index-pack --strict` before the proof or the replay; before, a bundle
  whose commits the object store already held passed unread. A capture whose bundle fails falls
  back to the full form with `slim_failure_reason: bundle-invalid` (stage `verify-pack`). See
  `docs/wait-workspace-checkpoints.md`.

## 0.2.75 — 2026-11-16

- **A Claude turn held for its background check is no longer cut off while the session keeps
  working.** The hold that waits for a background task counted `background_wait_minutes` (45) from
  the turn's first Stop. A session that ran its checks one after another (each notification turn
  starting the next check and ending on text again) was ended mid-work after 45 minutes and counted
  as "no outcome"; the correction pasted then got a turn command within seconds. The wait now counts
  from the last Stop the session went on to; `turn_idle_minutes` and `max_turn_minutes` still bound
  the turn. The driver's stderr names each further hold ("stopped again with N background task(s)")
  and a hold that runs out ("did not go on within N min").

## 0.2.74 — 2026-11-16

- **Only the MR's own task is told to push a fix.** A task that waits for the merge of another
  card's merge request is no longer woken with the owner notice of a red head, a merge conflict or
  failed named jobs ("the owner must push a fix", "merge the target branch into the MR branch",
  "fix and push or retry it"). Told to every waiter,
  that notice made a second session of the same lane push the same fix into the owner's branch while
  the owner pushed its own. Such a waiter now hears once per head that the MR belongs to another
  card, with a ready `wait_for on=card` for it and no push instruction; a merge or success wait it
  declares again on that MR stands instead of being sent back, and an `until=changed` wait on its red
  head is sent back with the same owner text instead of "fix the cause and push"; a new red result
  that wakes such a wait carries the owner text as well. The task's own MR (linked, named, or authored by the
  lane and linked nowhere else) keeps every notice and send-back as before.

## 0.2.73 — 2026-11-16

- **Preserve terminal Codex errors in interactive turns.** A provider refusal or API failure that ends
  a Codex turn before any model output now reaches the runner as a failed turn, including after
  delivery recovery, instead of triggering a misleading missing-outcome correction. An error after
  model work and an interrupted turn still end as a turn without a final message, so an outcome the
  turn already recorded is read first. Failed turns retain their reported token usage.
- **A lane's status row stays fresh during a long tick.** `tick_at` used to be written only when
  a lane's whole tick returned, so a lane busy checkpointing many worktrees or starting several
  sessions published a row ten to fifteen minutes old and status readers took it for an unknown
  state. The lane now republishes its row at most every 30 s from progress beats inside the tick
  (Mesh requests; files and chunks of large files copied, hashed or compared; directories walked
  or removed; every 10 s of a git command, start gate, subprocess or repository-lock wait); the
  row adds `tick_running_since`, `tick_step` and `tick_done_at`. A wedged lane is still reported
  after 900 s without progress, and a tick over 120 s is logged with its slowest phases
  (`docs/architecture.md`).
- **A comment a program wrote under an agent's key no longer hides addresses to that agent.** An
  `@agent` line on someone else's task is not filed as a request when the agent itself commented
  there in the last day (a reply to its own ask). Comments labelled as automation (a `[runner]` note,
  or `metadata.source` other than `api`, `ui` or `mcp`, such as a report script posting under the
  agent's key) no longer count as the agent taking part, so a reply under a scripted digest becomes a
  request. A dropped address now leaves a log line naming the agent's own comment.

## 0.2.72 — 2026-11-15

- Manual waits resume when a paired Telegram user's reply is delivered by the lane's
  own Mesh service identity. Ordinary own comments remain ignored; the delivery
  marker does not answer a decision or lift a human gate.

## 0.2.71 — 2026-11-15

- **A card whose agent ends its escalation chain no longer dies after `RUNNER GAVE UP`.** When the
  session ended without a readable outcome three turns in a row and nobody is above the agent (no
  lead, or the project's lead is the agent itself), the card used to wait in todo for a comment
  nobody was due to write. It now starts again at once in a fresh session, told that the previous
  one gave up, once per card and UTC day; a second give-up the same day waits for a comment or the
  next UTC day. A lane with a lead still hands the card to the lead.

## 0.2.70 — 2026-11-15

- **A question a desk session asks another agent with `ask_agent` is delivered.** Mesh opens such
  a request and delivers it to nobody, and no lane filed its request card, because a desk turn
  holds no card: the question sat open and unseen while its asker took it as asked. After every
  desk turn the answer desk now reads the asking card for the questions the session asked during
  it and puts each on its addressee's desk queue (the answer lands on the asking card, where its
  owner reads it). A question that cannot go there (the addressee is not a lane of this runner,
  has no desk, is headless or paused) or that the addressee's desk then fails is refused visibly:
  a comment on the asking card addressed to its owner names the request and the route (ask from
  that card with `ask_agent`, which files the request card); a refusal Mesh does not take is
  written again. A routed question no desk takes (the addressee's desk is switched off, or it is
  still queued after 20 minutes) is refused the same way. The card is read back from its newest
  comment to the turn's start, however long its history, and the read is owed from the turn's
  start: one that fails, or that a runner restart cut off, is done again until it succeeds or the
  card is gone, the pause doubling up to an hour. The
  log line of a request answered in the reminder turn no longer carries the first turn's «still
  open» reason.

## 0.2.69 — 2026-11-15

- **A turn whose end is known no longer goes to `unknown`.** A turn the Runner itself stopped for a
  lost lock (reassigned, released or expired) used to read an empty native stream (the stop closes the
  window and drops its hook events), report `cursor-mismatch`, hold the card 15 min and alert the
  operator that "the session did not confirm it took the prompt". It now ends as a lock-lost turn
  (`failed`, `checkout lost: …`, event `prompt_ended_by_lock_loss`); its prompt is archived in the
  mark `lock_lost_intent:<attempt>`, never consumed or replayed, and pending wakes stay undelivered.
  A headless Claude turn with several `result` records of its own session (a background task's
  notification adds one) is acknowledged; a `result` of any other session is still a mismatch. An
  `unknown` whose driver journal is at `accepted` now says the session took the prompt and the end
  of its turn could not be read, instead of "did not confirm".
- **API mode: a lane turn runs under an API key within its spend pool's budget.** A Claude
  turn whose route is the profile `<account>/api` (the lane's `api_primary_profile`, or
  `MESH_RUNNER_API_PROFILE` from its env command when the account's provider is in
  `api_reserve_providers`) runs on the key of the account's `credential_ref` only when the lane
  has `api_optin = true` (otherwise the subscription, as before), the launch is headless and the
  pool has a cap in `[api.pools]`. The key reaches only the turn process's environment; each turn
  reserves `run_usd` before it starts (`--max-budget-usd`), is refused with `budget_exhausted`
  over `cap_usd`, and is charged its `total_cost_usd`. `mesh-runner api-budget` shows the pools
  (`docs/architecture.md`, "API mode").

## 0.2.68 — 2026-11-15

- **A chat turn or an answer-desk question that finds the host full waits for a slot instead of
  failing.** With every slot of `interactive_max_sessions` busy (or the memory floor reached) for the
  whole start wait, the interactive driver gave up and the chat turn failed for good, while the answer
  desk spent its question turn, then its reminder, failed the request and the asker filed a request
  card. Nothing had been started or pasted, so the turn is now driven again: a chat turn stays
  streaming in Mesh (its lease renewed) and runs exactly once when a slot frees; Stop while it waits
  ends it cancelled and gives its chat slot back at once, other chats running on. The answer desk keeps
  such a request queued and unspent and asks again on a later pass; a request carded meanwhile stays
  carded and its waiting turn is stopped. The desk's own writes of a request (its claim, the turn count,
  the requeue, the result) and the asker's card fallback now check and write under one lock, so a
  request carded at any moment is neither brought back to the queue nor reminded next to its card. The driver marks the wait in its output
  (`mesh_runner.host_wait`: `waiting`, `room`, `timeout`); card turns end as before. The cap and the
  eviction of idle windows are unchanged. The log says `the turn waits for a free slot` and
  `no free session slot on the host; stays queued`.

## 0.2.67 — 2026-11-15

- **A window just started is never closed as the one idle longest.** The window's metadata got its
  start and last-turn times only after the host start lock was released, so for a moment it read as
  idle since the epoch; a parallel start that needed room and saw no live busy claim on it closed that
  window, and its own start failed. The times are now written together with the busy claim, before
  the lock lets another start count the window.
- **A turn-cap continuation goes on in the card's own working copies, a dirty one included.** An
  attempt that reached `max_turns` while still making progress ends and the card restarts in a fresh
  session; its slots were released, a slot with uncommitted work (an unfinished merge, staged or
  unstaged edits, ignored state) was held under the old attempt, and the fresh session got a clean
  slot on the remote branch while the card's work sat in a slot it could no longer write to (and that
  the dead-slot pass archived after a day). The continuation now records the attempt's paths; the
  next start of the same card leases them again, and a slot held only for uncommitted changes is
  leased to it as it is, nothing in the working tree touched. A full pool no longer holds such a start.
  A card closed in between is not started and its slot is archived by the dead-slot pass as before;
  no other card ever gets the slot. The log says `turn-cap continuation goes on in its own held
  worktree(s)`.
- **Editor reconfiguration waits for killed descendants to be reaped.** A cancelled job's process group gets a bounded cleanup window before the configuration swap; any remaining group still refuses the swap.

## 0.2.66 — 2026-11-15

- **The start ramp after a host hold now sees a wait root that wakes between two radar passes,
  and no longer keeps a slot for a root whose card waits for a person.** A lane's ready attempt on a
  root the radar last saw waiting (on a pipeline, a merge request, another card) claims the next
  slot with the waiting cards behind it at once, instead of after the next pass up to three minutes
  later, and is held for the ramp interval rather than the 120 s gate hold when the slot is spaced.
  A ready attempt whose card carries `human_gate` claims nothing: the start gate would refuse its
  resume, and the slot went to nobody for up to a minute.
- **Headless lanes switch accounts on the provider's `rate_limit_event`.** A `claude -p` turn that
  ends after `status: rejected` takes the provider-limit path whatever its closing text says; the
  on-limit command gets `MESH_RUNNER_LIMIT_SOURCE`, `MESH_RUNNER_LIMIT_WINDOW` and
  `MESH_RUNNER_LIMIT_RESETS_AT`. Each turn's windows (`unifiedWindows`) go to
  `<state_dir>/rate-limits/<agent>.json` (`lane-rate-limit/v1`) for the host's quota observations,
  `status.json` names each agent's `launch_mode` and its active harness pids, and each turn's
  `launch` event names its `launch_mode`. The docs now state
  that both modes draw on the same subscription windows
  (`docs/architecture.md`, "Launch modes").
- **An `@lane` comment on a card waiting on a gmail, receipt, lifecycle or recovery registration wakes it.**
  These waits ended only on their registration's event, and a request card is filed only for a mention on
  someone else's card: a lead's questions on the owner's own card slept for days behind a gmail wait. A
  comment whose line opens with an address of the lane (its runner name, Mesh agent name or slug) now wakes
  the session after that wait's own event is checked; a mention inside a sentence, the lane's own note and a
  runner note do not. The registration is not touched: an event that lands meanwhile stays pending, and the
  wake text tells the session to declare the same wait again, which binds to it. Stall, queue and native
  waits keep their own predicate only.
- **An interactive Claude turn that ends on text while its background check still runs is no longer
  counted as "no outcome".** Claude Code reports the tasks still running in its Stop hook
  (`background_tasks`) and goes on by itself when one finishes; the driver now holds such a turn
  (up to `background_wait_minutes`, 45) and ends it with the Stop of the turn that the task's
  notification started, so the turn command given there is read and no correction prompt is pasted
  into a session that is about to resume. A turn that already called a Mesh turn command is not held.
- **`python -m mesh_runner.no_outcome_classes`** sorts the window's Claude `no_outcome` events by
  what the session transcript shows (text with a background task running, text without a turn
  command, command accepted or refused, no activity, no transcript) per class and per lane. It
  opens the runner database read-only.

## 0.2.65 — 2026-11-14

- **`[runner] completion_comment` is an option again (default `true`).** Set it to `false` on a runner
  whose board people outside the team read: the completion line names the harness, the session and the
  token counts. An older file that sets it no longer logs "no longer an option"; the value now applies.

## 0.2.64 — 2026-11-14

- **A question delivered on the addressee's card reaches a session that wakes for it.**
  The `comment_wake = "addressed"` filter matched an @-mention against the lane name only. A lane that
  runs a Mesh agent under another name (lane `ops-vc`, Mesh agent `ops`) slept through
  `@ops, … asks …`, the comment the runner writes when it delivers an `ask_agent` question on the
  addressee's card. An @-mention of the lane name, the Mesh agent
  name or its slug now wakes every filtered wait (card, server-observed card, mr, pipeline, time,
  merge, decision). Delivery itself reads the named card's wait first: a card that waits on another
  card is context, not the card to act on (the question takes the answer desk / request card path),
  and a card whose wait a comment does not end (`registered`, `human`) gets a request card. A
  delivered question still unanswered after 30 minutes, while the addressee still holds the wait it
  held at delivery, is logged as a WARNING and kept on the wait as `delivered.unheard`.

## 0.2.63 — 2026-11-14

- **A turn command recorded after its turn was settled as "no outcome" is now that turn's outcome.**
  An interactive session can go on after the runner settled its turn (a background command it started
  finishes) and call `wait_for`; Mesh accepts it and releases the checkout. Before, that command belonged
  to no turn: the correction turn resumed on the released lease, every command of it got 409, the second
  "no outcome" sent the card back to todo and the declared wait (a merge gate) never ran. The runner now
  reads the turn outcomes from the start of the turn counted as "no outcome", before it issues the
  correction and again when the correction settles: a command found there is acted on like any outcome
  read at a settle, the lease Mesh released is forgotten and no correction runs; one the runner cannot act
  on keeps the one correction, naming what was recorded, and frees the dead lease. Events
  `late_typed_outcome` and `late_outcome_refused` record it.

## 0.2.62 — 2026-11-14

- **A card waiting on another card is no longer woken by every push and review of that card.** With
  `next_action_prep` on, the prep wake counted every new next-action generation, and Mesh's
  generation also hashes the blocker's HEAD and review verdict: a waiting card got one empty turn
  per push and per SHIP on its blocker. The wake and the prep start
  now count only the projection's content (stages, requirements, what they gate and release, the
  next action); a new generation with the same content moves the mark without a turn. A push or
  verdict that releases or holds an edge still wakes once. A card that must follow every push of its
  blocker (a rebase onto a stacked branch) waits on the blocker's merge request with
  `wait_for on=mr until=changed`. No new option.

## 0.2.61 — 2026-11-14

- **The runner's own notes are labelled automation.** Every comment the runner writes in its own
  name (body opening with `[runner]`, e.g. the completion line) now carries
  `metadata.source = "mesh-runner"`. Mesh treats such a comment as a driver note, so a completion
  line that lands on a card another lane has just closed (a reviewer closing the owner's card) no
  longer opens an urgent follow-up card. Text the runner relays for others is not labelled, and
  `informational` is never set, so attention readers still see runner notes.

## 0.2.60 — 2026-11-14

- **A `until=deployed` card wait sent back at its declaration no longer leaves its edge behind.** The
  runner never armed that wait, so no wake ended it: the wait stayed open in Mesh and its blocks edge
  on the target stayed too. On a closed target such an edge is never released, so the card kept
  reading as waiting on it. The sent-back wait is now resolved as `cancelled` with the reason, and
  the edge Mesh made for it is removed with the owner-checked delete, only when the receipt says this
  wait owns it. A failed delete is logged and does not hold the send-back.
- **After a host hold the start ramp gives its slots to wait roots first, across lanes.** Before,
  the one start per interval went to whichever lane asked first, so a card with several others
  waiting on it could be passed by every card nobody waits on and its whole team stood still. Each
  lane now tells the ramp which roots it would start or resume and how many cards wait behind each;
  a lighter start is refused until the heaviest root starts. A root its lane cannot start keeps a
  free slot for at most three intervals per ramp. No new option; `runner.log` shows
  `host ramp: next start kept for wait root …` and `host ramp: start … goes first`. README, host gates.
- **A merge the author's GitLab identity may not do goes to the card's lead.** When the merge gate
  passes every check but ends with `⛔ MERGE RIGHTS <project>!<iid>` (a Developer on a protected
  branch), the runner files one merge-only card for the nearest lead it runs (`lead`, then
  `project_leads`), whose runner merges under the lead's identity; the author keeps sleeping on the
  merge request instead of being woken for a merge it cannot do. See `docs/reviewer-merge.md`
  § Merge rights.

## 0.2.59 — 2026-11-14

- **The periodic checkpoint/cleanup pass no longer reads through a lane's database connection.** The pass
  ran in its own thread but used each lane's connection; while one of its statements was open, the lane's
  next write failed at once with `database is locked` (SQLite does not wait out the busy timeout for a
  connection that is already reading), and the failed journal write in the step's error handler turned
  that into `tick failed` for the whole lane, most often in the first minute after a restart. The pass now
  opens its own connection and closes it when done, and a journal write that fails in a step's error
  handler is logged as a warning: the step's failure stays the step's failure and the lane's other cards
  still tick.

## 0.2.58 — 2026-11-14

- **A question to an agent whose lanes all serve other workspaces is refused instead of filed.**
  An agent the Runner drives only in another workspace cannot read the card, so a request card
  addressed to it was never answered. Both `ask_agent` and the `decision:@x` marker now resume the
  asker with the reason, the agents that do answer in that workspace, and the hint to use
  `ask_human`. No request card is filed and the typed wait is cancelled. A lead that has no lane in the
  workspace is not chosen for the redirect either. Lanes are matched by agent id and workspace, not
  by config name. When a lane's identity is not yet read, the old name rule applies.
- **A card wait `until=deployed` that can never end is sent back once.** With the new optional
  `deploy_inventory` (path to the deploy inventory JSON, default unset = no check), a
  `wait_for on=card target_condition=deployed` on a card whose open or merged merge request is in a
  repository the inventory lists without a live-commit source (`type: none`) is returned to the
  agent with the reason and the wait to use instead (the card itself, or its merge request until
  merged): no deploy is ever recorded there, so Mesh would never release the edge and the card stayed
  waiting for good. The same wait declared again is accepted. See README, *Waits and wakes*.

## 0.2.57 — 2026-11-14

- **A server-observed card wait woken by a comment, a wait cycle or failed receipt reads now ends in
  Mesh as `cancelled`, not `invalid`/`dependency_removed`.** The runner removed the wait's blocks edge
  before it reported the wake, and for a server-observed wait Mesh takes that removal as an event of
  the target: it ended the wait as `invalid` first, and the runner's `cancelled` came back 409 (a
  `resolving Mesh wait … failed` warning and a `mesh_wait_resolve_failed` event on every such wake).
  The edge now stays until the wake is recorded and the wait is resolved with its cause; the resume
  removes it afterwards, and only once the resolve of that very wait is recorded as final. A resolve
  that failed is sent again first (a missing or unreadable resolve record is rebuilt for the wait), and
  while it is not final the resume waits for the next tick, at most until the resolve's three tries
  are spent. The target stays
  held throughout; the runner-observed card wait and the met path are unchanged.

## 0.2.56 — 2026-11-14

- **A host hold now holds every session start until the host has settled.** Both host gates
  (memory and load) are read again, uncached, right before a window opens, for new and resumed
  sessions alike, so a start admitted before a hold no longer opens into it. A hold lifts only after
  its release condition has held for `host_release_settle_seconds` (default 120); a reading that
  fails meanwhile keeps the hold and starts that wait again. Then for
  `host_ramp_seconds` (default 300) the host starts at most one session every
  `host_ramp_start_interval_seconds` (default 20). A session that took its prompt, worked and then
  failed is now reported as `prompt-taken-session-failed` ("the session took the prompt, worked and
  then failed") instead of "did not confirm it took the prompt"; it is still closed after the quiet
  period without sending the prompt again. See the host load gate in the README.

## 0.2.55 — 2026-11-14

- **A server-observed card wait (target in another workspace) wakes on an addressed comment and on a
  wait cycle.** Mesh resolves such a wait's receipt only on the target's events, and the runner read
  nothing else: an @-mention of the lane on the waiting card, a person's question there, a question
  another agent delivered there with `ask_agent` and the mutual-wait detector never woke it, while the
  same comment on a same-workspace card wait woke it in the next tick. While the receipt is pending the
  runner now also reads the waiting card's own comments (the same `comment_wake` rules) and its own wait
  store for a cycle. The receipt stays the only reader of the target: no target or edge read, no
  "target closed" and no `met` without a resolved receipt; a comment wake ends the Mesh wait as
  `cancelled`.

## 0.2.54 — 2026-11-14

- **A card wait held by an open typed edge still wakes on an addressed comment and on a wait cycle.**
  A `wait_for on=card` with `artifact_published`, `shipped`, `deployed`, `accepted`, `access_ready` or
  `contract_ready` (or an edge set the runner cannot read) used to skip every check while the edge
  was open: an @-mention of the lane, a question another agent delivered on its card and the
  mutual-wait detector were never read, so two lanes could wait on each other until someone released
  the edge by hand. The open edge now holds only the target's own event (closed, shipped); a comment
  that passes `comment_wake` and a wait cycle wake the same attempt as on any card wait.

## 0.2.53 — 2026-11-14

- **A re-declared card wait no longer wakes its own card through the next-action prep stage.**
  Mesh folds every edge id into the next-action generation. Since 0.2.51 a typed card wait owns its
  edge and the wake removes it, so re-declaring the same wait made a new edge, a new generation and
  another "continue the prep stage once" wake every 1-2 minutes with nothing new, until the turn cap
  handed the card up. The prep mark now also keeps the projection's content without edge ids: a
  generation that differs only in edge ids moves the mark without a wake. A change of the blocker
  under the same edges, prep becoming ready, a gate shift or another target or condition still wakes
  once. A mark written by 0.2.51 compares generations only and gains the content at its next wake.

## 0.2.52 — 2026-11-14

- **An 8-hex in an MR's text that Mesh refuses to resolve no longer turns the merge-wait check into a
  kept wait.** When the runner looks for the other card an MR is linked to, it reads each 8-hex the
  MR's title, description or source branch names as a short card id. Mesh answers 403 (`workspace
  access denied`) for an id that names no card the agent can read, such as a commit fragment in a
  release description; that answer used to abort the whole check, so the wait was kept for the
  wrong reason, and the agent's own unreviewed MR naming such a commit was no longer sent back.
  A 403 is now skipped like a 404, and the ownership check stops at the first other card the MR is
  linked to, so later ids are not read at all.
- **The `sent back` log line of a card wait keeps the addressee under a human gate.** When the target card waited on an agent's answer under a human gate, `runner.log` cut the line at the first `;` and showed only `waits for a person (human gate`, so it did not say whom the decision went to. The line now reads `waits for a person (human gate; decision → @lead)` in full. The wake text was not affected.
- **A card wait on a target that itself waits on a timer or a person is sent back once, not on
  every declaration.** The runner remembers the target's state it already told the dependent
  about; the same wait declared again while that target has not changed is accepted, so the
  dependent is no longer woken turn after turn with no new event. A new wait or gate on the
  target sends it back once more.
- **Checkpoint GC removes the scratch a killed runner left in the checkpoint root.** A runner stopped
  in the middle of a verify, compaction or bundle proof left `.verify-*`, `.compact-src-*` or
  `.preflight-*` directories that no pass ever removed. Such a directory created
  before the current runner process started is now removed by the next GC pass while no checkpoint
  work runs, logged as `checkpoint gc: removed orphan scratch <name> bytes=<n>`; the scratch of the
  running process is never touched.

## 0.2.51 — 2026-11-14

- **A lane tick no longer fails with "not enough values to unpack".** The status loop read every
  lane's recent logins through that lane's own database connection while the lane's thread was
  ticking on it; two threads reading one connection get rows reset under them, and the whole lane
  tick failed. The status loop now reads through the runner's own connection, so each connection is
  used by exactly one thread.
- **A card wait on a card that waits for an agent's answer is accepted.** The runner sent back a `wait_for on=card` on X whenever X waited on any `decision`, calling it "a person", although a question X put to an agent (`ask_agent`, a question redirected to the lead, a marker question to an agent) is answered by an agent and X runs on. When X had asked the waiting agent itself, the agent answered, got its own wait sent back as "waits for a person" before X even woke, and spent a turn on a false integration blocker. Only a question to a person (`ask_human`, a decision addressed to a person) or a human gate on X still sends the wait back. The wake text and the `sent back` log line now name what X waits for (`ask_human → @owner`, `decision → @lead`, `human gate; decision → @lead`, `time until …`) instead of "a timer or a person". An unreadable agent roster keeps the wait.

## 0.2.50 — 2026-11-14

- **A wait for the merge of another card's MR is no longer sent back for a missing review label.** The MR an agent's own GitLab user authored counted as the waiting card's own even when Mesh links it to another card (one lane often holds both cards; that card has its own reviewer and merge gate), so `wait_for on=mr until=merged` from the dependent card was sent back with "no review label ... nobody will merge it". Authorship now counts only when no other card the MR names (title, description, source branch) or the lane holds open links that MR; a link to the waiting card or its id in the MR still makes it its own. Behind a merge gate a review verdict SHIP naming the MR head on its card now stands in for the `verified-by:` label, since that verdict is what the gate reads; without a merge gate the label rule is unchanged.
- **A lane's result writes carry the lock of the running attempt, and a turn that lost its lock
  stops.** At every turn start the Runner writes the attempt's checkout (token, session, generation)
  and the task's `input_revision` to `~/.mesh-runner/checkout-fence/<agent_id>/<task_id>.json`; the
  agent's Mesh MCP sends it with every comment, artifact, finish and move on that card, and the
  Runner sends it with its own notes there. When Mesh refuses a fenced write for a lost lock, or
  refuses the heartbeat of a card that is not closed (another holder, the agent's own checkout under
  the same identity, a reassignment, an expired lock), the turn is stopped at once and the attempt
  ends `failed` with the reason in its note; nothing more is written under it. A closing write
  refused because the task changed (`input_revision_changed`) sends the next turn to read the task
  again and is not counted as a turn without an outcome. `[runner] checkout_fence = false` turns all
  of it off (today's behaviour) without a release. See `docs/checkout-fence.md`.
- **A question that names a live card of its addressee is delivered on that card.** When an
  `ask_agent` question names (`#<8hex>`) a card assigned to the addressee that is in progress or holds a
  declared wait (`manual` included), the asking lane posts it as an `@addressee` comment on that card,
  with the request id to answer through `answer_decision`, instead of putting it on the answer desk or
  filing a `[decision]` request card. The comment wakes that card; the Mesh request stays open until
  answered. A question that names a card of the addressee that is not live still skips the desk and gets
  a request card; questions that name no card of the addressee behave as before. Every named card is
  checked, not only the first few. A named card that cannot be read (no answer, 429, 5xx) chooses no
  route: it is read again on the next passes, and if it is still unread after about ten minutes the
  question gets a request card, never the answer desk. `runner.log` line:
  `question to <lane> delivered on its card #<id>`.

## 0.2.49 — 2026-11-14

- **A typed card wait removes the blocks edge Mesh made for it when the wait ends.** `wait_for on=card` makes Mesh create the edge to the target and name the owning wait on the receipt (`dependency_id`, `dependency_generation`, `dependency_owned`). The runner used to take that edge for an operator's and only observe it, so it stayed in the graph after the wake, and a later wait the other way round (consumer and producer waiting on each other in turn) was refused as a dependency cycle (422). The runner now binds an edge the receipt says this wait owns and, when the wait ends, removes it with the owner-checked delete (`expected_generation` and `expected_owner_wait_id`). A 409 (an operator or a newer wait of the same agent took or changed the edge) or 403 leaves the edge in place and is not asked again; a 404 or 410 means the edge is already gone; a transient failure keeps it for the next cleanup. An edge with no owner or another wait's is never removed. A typed card wait parked by an older runner reads its receipt once when it ends and gets the same cleanup.

## 0.2.48 — 2026-11-14

- **Kimi usage is counted per turn.** 0.2.47 read the whole session's journal at card end, so an attempt that continued an earlier attempt's session (a card returned to its lane) counted that earlier usage again, and the Kimi home came from the lane's current environment rather than the turn's. Before a Kimi turn starts the adapter now notes, in a `.wire` file next to the turn log, which Kimi home and session it runs in and how far the session's journal (`$KIMI_CODE_HOME/sessions/<workspace>/<session>/agents/main/wire.jsonl`) already runs, and reads the `usage.record` events of that one turn into `input_tokens`, `output_tokens`, `cache_read_input_tokens` and `cache_creation_input_tokens`. Each runner turn reads only its own Kimi turn; a turn refused before its prompt, a journal replaced or rewritten since the turn started, a session id not in the CLI's shape, or a log written before this version stays without usage. The whole-session fallback is removed.

## 0.2.47 — 2026-11-14

- **Kimi cards report their usage.** Kimi's print mode writes no usage to stdout; the completion line now reads the session's own wire log (`usage.record` events) once per card when the turns carried none, so it no longer reads `0.0k tok` for Kimi cards.

## 0.2.46 — 2026-11-14

- **The operator env command sees the card it starts.** For a card start or resume it receives `MESH_RUNNER_TASK` and `MESH_RUNNER_LANE` (a chat or card-less read gets neither), so an operator route can run one card on its own account, for example when the lane's profile has no free slot. Nothing changes for an env command that ignores them.
- **ZCode cards report their usage.** The ZCode adapter adds `input_tokens`, `output_tokens`, `cache_read_input_tokens` and `cache_creation_input_tokens` next to the provider's own camelCase keys, so the completion line no longer reads `0.0k tok` for ZCode cards.

## 0.2.45 — 2026-11-14

- **A standing chat session follows the routed harness.** `[agents.chat] commands = { <harness> =
  [...] }` gives the session a command per harness: a gone session routed to another harness than
  its configured command's now starts with that harness's command instead of waiting silently, and
  switches back (while idle) when the route returns. A chat that is still held (gone, or live and
  routed to a harness it has no command for) is logged once as a
  WARNING, reported as `chat_hold_since` in the status and exported as
  `mesh_runner_chat_hold_seconds{agent}`.

## 0.2.44 — 2026-11-13

- **A `required_jobs` wait wakes when a named job is stuck behind a manual job.** GitLab leaves a job
  `created` while the job it needs is `manual`; such a wait read as running and never woke, so an
  owner slept on a pipeline only a person could move. When the pipeline is `manual`, no job of it is
  moving and every unfinished named job is `created`, the named jobs now count as `manual`: an
  `until=green` wait wakes its owner once with `action required: … job(s) X manual, job(s) Y blocked
  behind a manual job`, an `until=changed` wait wakes on that state, and the same wait declared again
  after the notice is sent back. A named job that is `created` while any job of the pipeline runs,
  queues or is scheduled keeps the wait asleep as before.

## 0.2.43 — 2026-11-13

- **A review attempt that decided nothing no longer counts as a review.** A review whose last word
  was no verdict and that ended as a failure (a harness error, an unknown delivery closed by the
  quiet rule or an operator token, turns without an outcome) used to hold the card as "already
  reviewed" for `review_refeed_hours` (4 h). `continue_next` is not a verdict: a NEXT followed by a
  turn that died is no review either. The card is now fed to the reviewer again on the next pass (a
  failed attempt still waits out `failed_cooldown_hours`; the no-outcome hold still waits for someone
  else's comment), at most `daily_start_cap` review starts a day. `runner.log` says
  `review fed again: attempt <id> ended without a verdict`. A review whose last outcome decided holds
  as before. The `readable_outcome` event now carries the outcome kind.

## 0.2.42 — 2026-11-13

- **An author sending its own card to review again is settled, not refused.** When the card is
  already in review and still assigned to its author, `finish_task to=review` moves nothing; the
  runner used to refuse that turn ("does not settle a turn") and the author asked its lead for a
  route. The attempt now ends, the card stays in review and the review router assigns the
  reviewer. A reviewer's `to=review` on someone else's card is still sent back for correction.
- **A re-armed recovery wait no longer wakes at once on an earlier card with the same marker.** A
  candidate already finished more than five minutes before the registration was armed is evidence of
  a previous event (a recurring alert, say), so it no longer binds and a binding to it is rejected; a
  card still open at arm time or finished inside the create/arm race window binds as before. See
  `docs/recovery-wait.md`.

## 0.2.41 — 2026-11-13

- **Two more harnesses: `kimi` (Kimi Code CLI) and `zcode` (ZCode agent CLI).** A lane may run on
  either (`harness = "kimi" | "zcode"`) or be routed to it (`[agents.alt.kimi]`, `[agents.alt.zcode]`
  with `binary` and `args`). Both run in their print modes in every launch mode: their TUIs draw on
  the same subscription, so there is no per-token cost to avoid. Sessions resume by id; a provider
  limit reaches the existing account switch; Kimi cannot confine one turn read-only, so a
  metadata-only decision turn routed to it is held instead of running with write tools. The config
  now refuses an unknown `harness` or `alt` key. See `docs/harnesses-kimi-zcode.md`.
- **The reviewer merge handoff accepts every `same-family: <why>` selection.** The reviewer
  router now prefers another model family without requiring it: when no other family can review, it
  assigns the author's own family (`same-family: quota`, `other-unavailable`, `ci-only`). The handoff
  used to accept only `cross-family` and `same-family: quota` and refused the rest; the author is
  still never the reviewer.
- **Operator pauses hold merges.** Paused lanes continue observing merge waits but start no new merge gate, including when a pause arrives during environment preparation. Token changes take effect without restarting the Runner.
- **A closed decision request held by a `blocks` edge is no longer reopened.** A request card that
  still carried an unmet `blocks` edge (its addressee's wait from an ended session) was reopened for
  the asker's next question and then sat behind "held: blocked by" while the asker waited on it; such
  a request is now left closed and a new one is filed (store event `decision_request_held`). A
  failed dependency read leaves the question for a later pass; a malformed edge set keeps the old reuse.

## 0.2.40 — 2026-11-13

- **An amount with a currency word is a money question.** "20 долларов", "50 евро", "30
  dollars", "15 баксов", "5к" or "10 тыс", and "закинуть"/"скинуть" with an amount a few words
  on, now make an ask hard, so a spending question with a default "yes" is never applied
  unanswered. "Евро" counts only as a whole word: "европейский регион" stays soft.
- Before reopening a decision request for a new question, remove its previous card-wait dependency on the source only when Mesh's durable receipt proves ownership. Compare both the recorded generation and owner wait when deleting; preserve manual, changed and legacy dependencies. Repeated answered questions still attach without reopening.
- **A money transfer asked with a verb is hard too.** The transfer stem only matched the noun
  ("перевод средств"); "перевести средства", "переведу деньги" and other verb forms now make an
  ask hard, while moving a task or a document stays soft.
- **Breaking: a turn ends only through a Mesh turn command; text marker lines are no longer read.** From
  this release the runner settles a turn only from the typed outcome Mesh recorded for it (`wait_for`,
  `continue_next`, `ask_agent`, `ask_human`, `finish_task`, `hand_off`). A `⏳ WAIT …`, `↻ NEXT` or
  `❓ Blocking @…` line written in a comment or in the final message is prose: a turn that recorded no
  command ends without an outcome and gets the usual correction prompt. The same goes for the places that
  used to read the agent's comments: a review card follows the wait the author still holds in Mesh, a
  correction hold is lifted by the agent's own `continue_next` with a note, and a newer card wait replaces
  an older one only through `wait_for`. The prompts no longer offer marker lines as a fallback, and the
  `outcome_source` event counts only `typed` and `none`. Lanes need a Mesh MCP with the turn commands
  (evc-mesh-mcp with all six commands, including `ask_human` and `wait_for on=registered|manual|merge`); a
  lane without them gets "no outcome" on every turn. Three shortcuts that worked only on a marker-declared
  question to an agent are removed with the parser: turning a question about the addressee's own live task
  into a card wait, proceeding on a question that carried the asker's proposed default, and sending back a
  marker-line repeat of an answered question (a typed `ask_agent` arrives with the request Mesh already
  opened, and Mesh refuses an answered one as `already_answered`); an answered request is still never
  reopened for the same question. A typed `ask_agent` that hits the turn cap
  now still gets its request card filed, and a time wait refused again after its correction is a
  correction hold, as the marker form was.
- Session reports mark missing token counts and cost as unknown instead of zero, including turns without usage. Observed zero remains known. Interactive transcript aggregation preserves missing counters; Codex and interactive Claude costs remain unknown when their harness provides no price.

## 0.2.39 — 2026-11-13

- **A decision request that answered a question is no longer reopened when the same question comes
  again.** Every request remembers the questions answered on it, not only its last one; a verbatim
  or normalized repeat of any of them is attached without a move or a comment, also after the
  re-check hands it back, and a marker-line repeat is sent back naming the request as the answer
  reference. A changed question, or a request closed without an answer (cancelled), is asked
  again as before. The turn contract offers `wait_for on=card target_condition=artifact_published
  criterion=<named result>` and says an answered `ask_agent` question is refused
  (`already_answered`); an `artifact_published` wait that Mesh releases because the card closed
  without the result (`released_by=closure_without_evidence`) wakes with the card's status, who
  closed it and the missing result instead of "met".
- **A wait that can no longer complete by itself no longer parks for good.** `mr ... until=success`
  on a merge request whose red head was already told is now sent back at declaration, as
  `until=merged` already was. A typed MR `until=merged` or `until=success` wait that already stands
  (declared before such a rule existed) is judged by the same rules once per wait and runner
  process, after its observer found nothing new, so an upgrade re-checks every standing wait;
  pipeline waits, `until=change`, a `manual` head and the no-review-label rule are left alone there, because
  someone else may still complete them. A wait whose every check fails to read (timeout,
  transport) wakes its owner with the cause after 3 failures in a row spanning at least 6 hours;
  one successful check resets the count, which survives a restart.
- **A card wait is no longer sent back when its target waits for a published external event or
  when the wait itself is for acceptance.** A wait `until=accepted|deployed|shipped` names a release
  gate already and is accepted whatever the target waits for. A target whose `on=time` wait carries
  an event and a source (a scheduled external event, not a measurement window) holds a plain card
  wait too. A target on a bare timer or on a person is still sent back for a plain wait.

## 0.2.38 — 2026-11-12

- **The runner's writes to Mesh are safe to repeat.** The per-turn spend report
  (`POST /agents/me/sessions/report`) and every runner comment carry an `Idempotency-Key` derived from what
  the write is (its kind, the attempt, the turn), never from its text. A write whose answer was lost
  (timeout, reset) is sent again with the same key, and Mesh answers the repeat without writing again; a
  write rebuilt after a restart with other text is refused by Mesh (`idempotency_key_reused`) and counted as
  already written. A runner note without a named key is keyed by its text and the UTC hour.
  `[runner] idempotency_keys = false` sends no key and repeats no write (rollback without a release).
  See [repeat-safe writes](docs/idempotent-writes.md).
- **A card WAIT wakes when its target closes even if the card has other blocking links.** The guard
  that keeps a WAIT from observing a retired target counted every blocking link of the card, so a WAIT
  whose target sat beside another open blocker (or beside satisfied ones) was held silently, with no log
  line, until every other blocker closed too. A fully known `task_done` link to the current target now
  admits the ordinary observer; the owner is woken by its own target and re-declares on what still
  blocks. Links not naming the target, typed links and unknown or paginated link sets still hold the WAIT.

## 0.2.37 — 2026-11-12

- **A card wait whose uniqueness check outlasts one wake is pinned on a later attempt instead of
  never.** On first sight of a waited card the runner asks every other agent of the process whether
  the same short id reads as a different card. A large sweep could exceed the
  wake read budget, so it failed as `kind=timeout stage=GET /tasks/by-short-id/:id`, the wait
  was never pinned and every attempt started the sweep again. What each agent answered is now kept for 10 minutes per short id; the next
  attempt asks only the agents that have not answered, so the sweep finishes in two or three
  attempts. The card that decides a wake is still read fresh; faults are not remembered.
- **A top-up, a balance or an amount of money now makes an ask hard, and the default is read with
  the question.** "Top up the shared balance by $20?" with a default such as "yes, top it up" used
  to classify soft, so its default would have applied by itself at the deadline. The hard list now
  holds top-up, balance, credits and deposit stems and amounts in a currency (`$20`, `20$`,
  `1500 ₽`, `20 usd`), and `gate_class` reads the stated default together with the question, as
  Mesh does with the marker's `Default:` line. The shared fixture with the server is updated.
- **An ask that asks nothing stays hard.** A marker carrying only a default (and perhaps a
  deadline or a class tag), or an empty question with a stated default, now fails closed as
  "unsure: no question text" instead of reading the default as its question.

## 0.2.36 — 2026-11-12

- **Slots are locked against a foreign `git worktree prune`, and a slot whose admin dir was removed
  recovers without an operator.** A prune run where the slot's path was not visible deleted the admin
  dirs of live slots; the held rows then filled the pool and stopped the lane. Every slot is now locked
  (new ones when made, existing ones by the periodic slot pass; eviction unlocks first). A held orphan
  whose work nobody resumes is archived onto a `refs/mesh-runner/archive/orphan-*` ref, its directory
  moved to `<archive>/orphan-admin/<agent>/` and its row dropped; a leased orphan is rebuilt in place on
  suspend, so its attempt checkpoints and resumes. `pool_hold` now names the held slots and their notes.
  See `docs/architecture.md`.

## 0.2.35 — 2026-11-12

- **An answered decision request keeps its working copies for the next question.** A decision
  request is closed when answered and its asker reopens it with the next question; the return
  already continues the addressee's session, but the pool treated the closed request as gone and
  handed its free slots to the lane's next cards, so the return found other paths and started fresh
  (`path_changed`). The ownership-loss note now records that the card is a decision request, and the
  pool keeps such slots aside like those of any card that may come back. Cancelled requests and
  ordinary closed cards are unchanged; at the pool maximum the slots are still handed out, and a
  return that lost a path still starts fresh.

## 0.2.34 — 2026-11-12

- **A blocked pool returns by its reset time, and a long route hold is signalled.** A pool left at the threshold used to turn `unknown` as soon as its reading went stale (an expired collector token), so lanes stayed on their reserve with no end. Now a blocked pool with a known reset time stays blocked until that time whatever the readings do, and is reported `available` (source `reset_elapsed`) once it has passed, until a fresh reading says otherwise. The code fallback thresholds block and transfer at 99 % and readmit below 95 %. A lane held by one `route:` reason for 30 minutes raises one `operator_alert` (`route_hold:<lane>`). A resume re-reads the lane's route after the start gate, so a gate that restores the lane to its primary is followed by the launch.

## 0.2.33 — 2026-11-12

- **A sent-back Mesh wait resumes with a checkout.** `wait_for`, `ask_agent` and `ask_human` release the caller's Mesh checkout in the same step, but the runner kept the lease token on the attempt. When it then sent the wait back (a pipeline in a terminal canceled state, a stale baseline, any other bounce) and resumed the same session, `_resume_one` skipped the acquire because a token was present, so every turn command of the resumed session got 409 "the caller does not hold this card's checkout". The runner now drops the token as soon as it reads a typed outcome whose command released it (event `checkout_released_by_mesh`); the resume acquires a new lease for the same session.
- **New `[runner]` option `hold_labels` (default `["awaiting-owner"]`) names the labels that keep a card from a starvation or queue wake.** It replaces a built-in deployment-specific label; set it to the label your team uses for a card that waits for a person. Tests, docs and comments use neutral lane names and fixture card ids.

## 0.2.32 — 2026-11-12

- **Behind a merge gate, an MR that is only behind its target is no longer the author's to sync.** With `merge_command` set, a `⏳ WAIT mr:… until=merged` on an MR whose only blocker is `need_rebase` is accepted instead of sent back, and the evaluator no longer wakes the waiter with an "action required" notice for it: the gate syncs that branch itself right before merging. Where the next step is still shown, it tells the author to leave the branch alone and arm `⏳ WAIT merge:` on the current head. A conflict, and every case without a gate, keep the old text (merge the target into the branch, new head, new review).

## 0.2.31 — 2026-11-12

- Asks to write to, call or contact someone classify as hard in English too. A letter, an offer or a call was caught only in Russian; `call`, `letter`, `offer`, `proposal`, `outreach`, `reach out`, `write to`, `reply to`, `contact`, `dm` and their Russian counterparts now keep such an ask hard, so its stated default is never applied by the clock. Asking to write a test stays soft. The shared fixture `tests/fixtures/ask_class_cases.json` gains these cases.
- The shared runner can publish what each attempt really runs. The execution publisher is part of the package (`mesh_runner.execution_observations`) and starts with `[catalog] execution_observations = true` (default off; needs `[catalog] enabled`). There is one publisher per `[[catalog.workspace]]` behind one router: an attempt is reported in the workspace whose `desired.json` binds its agent. Each turn reports the launch route from the native argv (`resolved_route.source = runner_launch`) and, when it settles, the model the harness itself recorded. A 403 on a report means the runner integration is off for that project: the batch is split per project and agent, the refused scope is skipped for 10 minutes and the others go on; 404 and 409 still fence. Rollback: set the key to false (or drop it) and redeploy.

## 0.2.30 — 2026-11-12

- A new repository-access grant now starts a card that was refused for missing access on the next tick: the grant lifts exactly the backoff and resume holds the access refusal set, and a deadline set by another failure stays.
- The runner is installed from the release commit. `git archive` writes the exported commit and its parents into `SOURCE_COMMIT`; `ops/install.py --source` refuses a tree exported from a merge commit (`--allow-merge-commit` overrides) and `--status` shows `commit.sha`. `docs/release-process.md` installs the head of `release/<version>`, not the release merge.
- Turn guidance names the typed turn commands; a marker line is offered only as the fallback after a command was refused, quoted with the command and its error.
- `wait_for on=manual` is read: the typed passive wait on a `wake:manual` card settles as the manual marker did, with the same label check.

## 0.2.29 — 2026-11-12

- Wait-root radar: every 3 minutes, off the main loop, open card waits are followed to the card that waits on no other card. Each root gets a kind, a holder, the number of waiting cards behind it and the age of the oldest wait. Only a ready root of this runner's own lanes is acted on: its lane starts it before other work, the heaviest first. Nothing is written. `status.json` shows `roots`; `mesh-runner roots --config <toml> [--json]` prints the full picture.
- A provider refusal of an outdated harness version and an expired login are lane faults with one operator signal. The refusal holds the harness on the host, keeps the card's counters and turn budget, and signals once; the hold lifts when the CLI binary changes or lets one turn through every 6 hours. An authentication hold signals once per hold and every 6 hours while it lasts, naming the account. The signal is one `operator_alert` line in `events.jsonl` and one warning; `status.json` lists `auth_holds` for logins launched in the last 3 days and `harness_holds`.

## 0.2.28 — 2026-11-12

- End a turn whose prompt delivery stays unproven without an operator: once its processes are gone and its native evidence stays unchanged for 15 minutes, the prompt is archived (never replayed) and the card starts in a new attempt. The transition emits one operator alert and the card says what happens next. No session window starts below the configured memory floor, read right before the window opens, and a driver claims its window under the host start lock so another driver cannot close it as idle.

## 0.2.27 — 2026-11-12

- A blocked merge wait asks the owner to merge the target branch in and push normally, matching the merge gate, instead of asking for a rebase.
- Slim checkpoints keep the modes of an empty directory's parents, so such captures verify.
- Granted cross-workspace card waits wake from the source card's server-side receipt; revoking the grant wakes the owner without treating the condition as met. Requires the matching Mesh API.
- Changelog entries live in one file per change under changelog.d/; ops/release.py sets the version in all three files and collects the entries in the release commit, and the mr-policy check enforces it. See docs/release-process.md.

## 0.2.26 — 2026-11-12

- Route a lane again when its launch returns to a login that is on hold.
- A card whose previous lane still holds its reservation waits alone, with the holder named, instead of holding back the whole lane; an unconfirmed gate release is resent with backoff.
- asks classify reports the gate class Mesh holds; outward contact in someone's name is a hard ask.
- Repository-access refusals are addressed to the configured repo_access_contacts, once per fingerprint and addressee.
- Repository access is read from Mesh on every tick, separate from the host's [[repositories]] inventory; the last valid state survives outages and only a newer revision replaces it. See docs/repository-access.md.

## 0.2.25 — 2026-11-11

- An unparked review resumes its own compatible session, using the same ownership-return checks as work admission.

## 0.2.24 — 2026-11-11

- Honor park labels at the end of review turns and before review resume, keeping the existing session.

## 0.2.23 — 2026-11-11

- Add the default-off cmux receiver transport with exact native process binding and typed refusal without fallback input. Optional terminal patches and their upstream license notices are included under contrib/cmux.
- Reserve the names of pruned slots that suspended or returning work may need again.

## 0.2.22 — 2026-11-11

- Preserve a returning session when all its earlier working copies are leased again and additional repositories joined its scope.

## 0.2.21 — 2026-11-11

- Add chat_workspace = "lane" so chat turns use the lane's configured tools without acquiring a worktree. Thread-scoped Mesh credentials remain enforced; the default is "none".

## 0.2.20 — 2026-11-11

- Keep suspended slots aside while a pool can grow. Answer desks use the lane's configured tools; metadata-only recipients remain confined.

## 0.2.19 — 2026-11-11

- Recreate a returning card's pruned preferred slot at its previous path while the pool can grow.

## 0.2.18 — 2026-11-11

- Add optional cardless_question_lanes and an answer desk for same-workspace decision requests, with request-card fallback.

## 0.2.17 — 2026-11-11

- Keep a recently departed open card's preferred slots aside while a pool can grow.

## 0.2.16 — 2026-11-11

- Retire 61 default-only runner configuration keys with explicit warnings for ignored values. Document the remaining options.

## 0.2.15 — 2026-11-11

- Reserve preferred slots only for recently departed open cards.

## 0.2.14 — 2026-11-11

- Prefer free slots that are not named by recent ownership-loss notes.

## 0.2.13 — 2026-11-11

- Prefer a returning card's previous working-copy paths when restoring its session.

## 0.2.12 — 2026-11-11

- Route attempt-state writes through AttemptStateMachine while retaining transition validation.

## 0.2.11 — 2026-11-11

- Treat positive remaining-usage notices as warnings instead of terminal limit banners.

## 0.2.10 — 2026-11-11

- Recover refused interactive turns only from exact bound prompt and typed-error evidence.

## 0.2.9 — 2026-11-11

- Resume a reopened decision request in the addressee's kept session when its harness, profile and working copies still match.

## 0.2.8 — 2026-11-11

- Support separate supervised chat, editor and Gmail processes; improve chat attachment refresh, token-based pause migration and shared chat identity creation.

## 0.2.7 — 2026-11-11

- Add guarded operator resolution for unknown attempts when prompt-delivery evidence cannot be recovered, without replaying the pending prompt.

## Unreleased

## 0.2.6 — 2026-11-11

- **A question asked with `ask_agent` reaches its addressee again.** Since 0.2.3 the Runner treated a typed
  decision wait as already delivered by Mesh and filed no request card. Mesh only records the request: its comment
  carries `wakes=false` and no queue lists open requests, so the addressee was never started.
  The Runner now files the request card for a typed question as it does for a marker one,
  quoting the question from the `ask_agent` comment, and files it on the next pass for waits already parked without
  one. A question whose comment cannot be read is retried, never filed blind.

## 0.2.5 — 2026-11-11

- **An ended attempt whose prompt was provably delivered no longer blocks drains forever.** A Runner without the
  intent protocol that ran over this store during a restart closed attempts as completed and left their consumed
  `interactive_prompt_intent` behind; `launch_guard.pending()` counted them and config operations were undrainable.
  The once-a-minute terminal settle now also retires such an intent, only on the exact native proof the ordinary
  consumer uses: the driver's `prompt_ack` with the intent's token and sha256 in the intent's own log, the same turn
  and log, the delivery journal for that token at `accepted`, and no live process group. Compare-and-set on the row,
  event `prompt_intent_retired` (reason `proven-delivered`), no state change and no re-send; pending wake deliveries
  become their marks as on the ordinary ack. A sent-but-unacknowledged (ambiguous) intent keeps blocking.

## 0.2.4 — 2026-11-10

- **Every typed wait form is read.** The typed outcome reader builds the Outcome for each wait Mesh's `wait_for`
  and `ask_human` now record: `merge` waits, `registered` waits (ops-review, gmail, receipt, lifecycle, stall,
  recovery, queue, native), mr/pipeline baselines (`after_sha`, `after_pipeline_id`, `after_status`),
  `required_jobs` on mr waits, pipeline `until=changed`, GitHub Actions runs and the `human` wait. Each gives the
  Outcome its marker line parses to; a combination the runner cannot act on is an `unparsed` naming the row and
  the field. A typed question to a person is not raised again: Mesh already posted it and armed the gate.
- **The runner resolves the Mesh wait it observed.** When an attempt stops waiting on a wait recorded through a
  turn command, the runner calls `POST /tasks/:id/waits/:wait_id/resolve` (`met` when the event arrived,
  `cancelled` when it was superseded or the card left the lane, cause = the wake text cut to 200 characters).
  Best effort: one `mesh_wait_resolve_failed` event on failure, at most three sends, 404/409 final.
- **The contract asks for a Mesh turn command.** Every agent is told to end a turn with one of `wait_for`,
  `continue_next`, `ask_agent`, `ask_human`, `finish_task`, `hand_off`, with the MCP server's own parameter names;
  the marker lines remain a compact fallback and are still parsed. Correction prompts ask for the command first.
- **A fresh session knows where it stopped.** Its first prompt on a card the lane worked on carries the restart
  reason, the lane's last comment in full (up to 4,000 characters), the last declared outcome, linked merge
  requests and `git status --short` of each working copy.
- **The lane option `project_ids` is removed.** Mesh membership decides which projects a lane serves; a config
  that still lists it loads and logs one warning per lane.

## 0.2.3 — 2026-11-10

- **The installer recovers a job definition from before the launch guard.** `ops/install.py` rebuilt nothing when the
  launchd plist had been replaced by an old-style one (`python -m mesh_runner.cli run`, release chosen by PYTHONPATH).
  The installer now rewrites such a definition in the guarded form, and "already active" also requires that the live
  process publishes a receipt from the target release (`--activate <version>` restarts a service that runs another tree).
- **A Claude session survives a restore into another slot.** When a suspended attempt's old slot was leased to
  other work, the restore lands on another path and the session used to start fresh (`restart_reason=path_changed`).
  For an interactive Claude session the session is now kept
  (`restore_session` intent `resume`, reason `workspace_restore_moved`, plus a `workspace_moved` event): the moves
  (old path -> new path, chained across restores) are kept in the store per native session, and the session's
  launch installs a `PreToolUse` hook (`path_guard.py`) that refuses any tool call naming an old path, since that
  path may now hold another attempt's work. The first resumed turn after a move opens with the list of moves. A
  launch that cannot carry the hook (headless `claude -p`, Codex, an unwritable map) never continues such a
  session: it starts fresh with `restart_reason=path_changed`, as before. No new option.
- **A typed turn outcome is read first and taken as it is.** The outcome an agent records through a Mesh turn
  command (`wait_for`, `continue_next`, `ask_agent`) is now THE outcome of the turn, read before any marker line; a
  marker line in the same turn is only compared, and when it says another kind or target an `outcome_disagree`
  event records both while the typed one stands (before, the marker won and the wait Mesh already held, with the
  checkout it had released, was left unreconciled). The outcome is built straight from Mesh's rows
  (`outcome.from_typed`, a table of builders per wait kind: card, mr, pipeline, time, decision) instead of being
  rendered as a marker line and parsed back, so a time wait whose source is `comment:<uuid>`, an `event_ref` with
  spaces or a deadline with fractional seconds is no longer refused. `ask_agent` settles as a decision wait on the
  request Mesh filed (the runner files no second request); it used to end the turn as "no outcome". A row the
  runner cannot act on (a wait kind it does not have yet, such as `merge`, a superseded wait, a malformed field)
  gives a correction that names the row and what was not understood. `outcome.typed_line` is removed.
- **An unreadable typed outcome defers the settle.** When Mesh cannot be read for the turn's typed outcome (an HTTP
  error other than 404, a transport error), the attempt is left as it is and settled on the next tick
  (`outcome_read_deferred` event, once per turn) instead of asking the agent again for an outcome it may have
  recorded. A Mesh without the read (404) still settles on the marker line.
- **How turns end is counted.** Every settled turn emits `outcome_source` (`source` typed / comment / final_text /
  none, `kind`, `harness`, `both`, `disagree`), and `status.json` carries `outcome_sources`, the totals since start
  per source and harness. The marker line can be removed once nothing ends a turn with it. No new option.
- **Manual wait.** `⏳ WAIT manual:#<this-card-8hex> — <why>` on a card a lead labelled `wake:manual` (no producer or
  observable event exists): the session sleeps with no timer and no age re-check and resumes only on a comment by someone
  else or the owner's own `↻ NEXT`. Without the label, or naming another card, the line is refused as unreadable.

## 0.2.2 — 2026-11-10

- **A wrong recovery gate can be superseded, once, by its owner.** `mesh-runner recovery-wait supersede --reg-id <generation>
  --reason '<line>'` replaces exactly one registration generation (a stale, delivered or already superseded one is
  refused). It is authorized by Mesh, not by the operator: the receipt author or an id in the new `[runner]
  recovery_supersede_authors` must have written `supersede-recovery: <reg_id>` on the source card. The registration is
  disarmed and the attempt that declared it resumes once in its own session; the gate itself is never satisfied by this
 .
- **A bare base stays bare when linked worktrees get their own config.** Before `extensions.worktreeConfig` is first
  enabled, the pool moves `core.bare=true` and `core.worktree` into the base's `config.worktree`; every owned slot gets
  `core.bare=false`. A base already switched on is repaired the same way before leasing. Failed checkpoint list operations
  name the Git subcommand, exit code and a finite cause (`git-not-a-work-tree`, `git-other`).
- **A finished merge request no longer holds a card's start.** The repository preflight requires only projects with an open
  (not merged/closed) MR link; a project named only by finished links is reported as `historical` in the verdict and is
  never a refusal reason. Open links, any role, stay hard requirements.

## 0.2.1 — 2026-11-10

- **Eleven `[runner]` options are removed; what they switched on is unconditional.** `mention_requests`,
  `turn_cap_continue_on_progress`, `queue_wait_freshness_v1`, `recovery_wait_v1`, `lifecycle_wait_v1`, `stall_wait_v1`,
  `requeue_on_cap`, `cap_wake_next_day`, `soft_person_asks_to_lead`, `merge_follow_head` and `checkpoint_gc` were all
  `true` in production. The loader ignores keys it does not know, so a config file that still carries them loads
  unchanged; the lines can be deleted. Three options nothing read (`time_wait_reason_after_minutes`,
  `short_time_wait_series`, `quota_pause_minutes`) are gone the same way. The older hand-up path of a cap (the lead, or
  the agent keeps the task until the next UTC day) remains only as the fallback when a capped task cannot be put back in
  the queue.
- **A suspended attempt finds its slot again.** New work leases a slot that another attempt of the pool was suspended
  from only when no other free slot exists, so a restore lands on the path the session ran in and the session continues
  (a restore to another path was a fresh start, `restart_reason=path_changed`).
- **The attempt lifecycle is one table** (`lifecycle.TRANSITIONS`), asked by the store on every state write. A write on
  an ended attempt is refused and recorded; a change between live states that the table does not name is applied and
  recorded as `unlisted_transition`.
- **A turn outcome recorded through Mesh's turn commands** (`continue_next`, `wait_for`) is read when the turn wrote no
  marker line; the marker line stays the default form.
- **A decision request is not closed under the addressee's running turn**: the close waits for the card's checkout to
  end.
- Side services (Agent Chat, the session-metadata receiver, the extension endpoint, the controller announcement) start
  from `services.py`; behaviour unchanged.

## 0.2.0 — 2026-11-10

Versions 0.1.45-0.1.51 were not released; their changes are part of this one.

- **Install without a drain.** `ops/install.py --source <tree>` copies a release into the catalog, loads the live config
  with the new code, switches the launchd job and waits for the new process's receipt; without it the previous job
  definition goes back. `--activate <version>` is the rollback. The attempt registry, leases, pause tokens and the config
  are not touched.
- **The launch guard no longer blocks a new release or a config change on a pending row.** A target enters with UNKNOWN or
  unproven prompt-delivery rows still pending when the previously activated release on the same state declared the same
  `CAPABILITY` (the delivery-journal contract); the rows are reconciled by the ordinary tick. The two narrower exceptions
  (legacy bootstrap rows, additive repo delta) are covered by this and removed; `--previous-config` is accepted and unused.
- **Slots of a bare base clone stay work trees.** With `extensions.worktreeConfig` on, `core.bare` moves to the base's
  `config.worktree`; a base left in the broken form is repaired on the next lease.
- **A card given back by another agent continues in the lane's own session** (same harness, native root and paths, card not
  closed in between), with a start prompt that says another agent held it. Before, every such return was a fresh session.
- **A question to a person is not woken by its age.** It ends on the answer or on the lifted gate.

- An operation pause token (`pause.d`) that lands while a tick is already walking the queue now stops the later native starts and resumes: the pause is re-read between candidates and in `_spawn` before the launch probe and again before the one-time reservation is consumed. A denied launch returns the reservation, keeps the session and pending input, and sets no timer; the retry waits only for the token.

- A card that returns to a lane routed to its alternate harness after a status-only roundtrip now resumes that lane's own session, as it already did for lanes on their
  native harness. Before, every such return started a new session.

- Optional external start order for a lane's ordinary work (default off). Per agent:
  `queue_order_command` (argv), `queue_order_timeout_seconds` (3), `queue_order_cache_seconds` (60).
  Applied in `_work_order` to the ordinary class only, with 2+ cards; any failure keeps the runner's own order.

- Add the opt-in Gmail WAIT (`gmail_watch_v1`, `⏳ WAIT gmail:gmr_<12hex>`, `mesh-runner watch gmail register|status|cancel`): a read-only, header-only pull observer on Runner's own schedule that wakes the observer card exactly once per new matching inbound message, with a durable baseline/seen/receipt/outbox, idempotent comment delivery and no agent polling turns (docs/gmail-watch.md). Ships disabled.
- Add operation-scoped pause tokens (`<state_dir>/pause.d/<name>/<token>`, host-wide `pause.d/_all/<token>`): an operation takes and releases only its own token, so removing it can never lift a foreign `pause/<name>` marker, config `paused` or another operation's token. Leftover tokens fail closed and are listed in status (`pause_tokens`) and metrics (`mesh_runner_agent_pause_tokens`).
- Keep a `decision:@agent` question as a request when the addressee's live task itself waits on the asking card: converting it into a card wait formed a wait cycle whose notice asked for the same question again.
- Tell a wait cycle once per card and per question on the other side: the runner remembers every cycle it already told a card (the last 64, kept across restarts and reassignment), so the same card wait declared again, replayed, or met again after the other side returns to an earlier question is kept without a new turn; a question not told before wakes once.

## 0.1.44

- Recover proven unsent requests after verified workspace migration while preserving pending delivery and fresh admission.
- Recover wrapped native Claude quota menus before paste and preserve unsent requests through fresh capacity admission.
- Carry exact typed quota and legacy unsent requests into a fresh admitted harness without losing pending delivery or workspace context.
- Invalidate stale local capacity holds after observed route or pool changes.
- Retire only proven-unsent terminal intents during drain and expose exact target coverage to older guards; preserve ambiguous and active delivery fences.
- Preserve owner-edited typed WAIT edges, prepare explicitly allowed next actions, and retain routed ownership-return sessions.
- Apply required repository admission, configured queue ordering, and clean failed archive destinations.
- Optional external start order for a lane's ordinary work (default off). Per agent:
  `queue_order_command` (argv), `queue_order_timeout_seconds` (3), `queue_order_cache_seconds` (60).
  Applied in `_work_order` to the ordinary class only, with 2+ cards; any failure keeps the runner's own order.
- A card that returns to a lane routed to its alternate harness after a status-only roundtrip now resumes that lane's own session, as it already did for lanes on their
  native harness. Before, every such return started a new session.

## 0.1.43

- Admit a human-gated card to the owner of its live ask for one bounded turn per answer: when Mesh reports `human_gate_info.decision_pending` with this agent as `owner_agent_id` and assignee, the runner claims exactly one start per `decision_comment_id` (also past a future `start_after`); the gate itself is never lifted by the runner, and missing info or a foreign owner keeps the ordinary hold.
- Resolve a wait on a target in another workspace only within granted scope: two different full ids for one short ref are ambiguous (unknown, never a verdict), the single resolved id is pinned on the wait and later ticks and the resume check accept only that id; an unreadable target names the concrete action (ask the workspace owner for access or re-declare with the full id) and carries no target content.
- Legacy bootstrap proof: a same-cwd Codex rollout bound to the session of a different attempt is no longer a candidate for the legacy send, in the launch guard and in delivery recovery alike (`Store.other_sessions`), so the ordinary tick reconciles such rows instead of leaving them `unknown`; unbound, unreadable and same-key rollouts still keep the row unresolved.

## 0.1.42

- Classify a slim checkpoint fallback by stage, fixed reason and failing git subcommand (allowlists only) and log one `checkpoint slim fallback:` line per fallback; when the slot lives on another volume the capture keeps the lease and retries instead of copying the whole worktree.
- Send the headers and a small `bytes` body of a Mesh HTTPS request in one TLS write (MeshClient and the catalog/chat/meta-delivery/QA clients); file, iterable, chunked, GET and large bodies keep the stock `http.client` path.
- Run the opt-in launch probe for interactive Codex lanes too: the runner captures the exact native argv/env the tmux driver will exec (same override marks, effort and read-only sandbox as the launch) before `reserve`/`admit`; an unproven context no longer blocks an ordinary interactive start, a blocked quota still denies.
- Check the repository a code/review card needs before its start: the repository named by the card's typed MR links is compared with the lane's approved config, and a missing declaration, membership, approved clone, assigned checkout or a checkpoint conflict refuses the start with no lease and no model turn, naming the next actor and the supported action in one comment per distinct fact set (docs/repo-preflight.md).
- Add the opt-in Gmail WAIT (`gmail_watch_v1`, `⏳ WAIT gmail:gmr_<12hex>`, `mesh-runner watch gmail register|status|cancel`): a read-only, header-only pull observer on Runner's own schedule that wakes the observer card exactly once per new matching inbound message, with a durable baseline/seen/receipt/outbox, idempotent comment delivery and no agent polling turns (docs/gmail-watch.md). Ships disabled.
- Treat a typed Codex authentication refusal (`workspace routing discovery unauthorized (401)`, `refresh_token_reused` / "access token could not be refreshed", 401 on the token endpoint) as an auth failure in headless and interactive Codex: the profile gets one auth hold (`clear-auth-hold` after repair) instead of burned turns and a give-up; `discovery failed` / `timed out` stay a provider outage, and a Codex window that died in TUI bootstrap is a proven unsent delivery.
- Re-read the blocks edges inside the same transaction before waking a removed-edge card WAIT: a link restored between the read and the write, or an unreadable edge set, keeps the WAIT; the resumed owner is told to re-check blocking links.
- Let the launch guard of the target release admit a target change while delivery rows are pending only when every pending row is a proven legacy interactive Codex bootstrap row (`unknown` / `session-unbound`, no live pid, no session or finished turn, `delivery_recovery.unbound_bootstrap_proof` holds); any other pending row or in-flight intent still refuses, and the ordinary tick reconciles the rows after activation.

## 0.1.41

- Wake the same attempt once when the blocks edge behind its card WAIT is deleted; unreadable, paginated or malformed edge sets and any remaining unsatisfied blocker keep the WAIT.
- Connect account capacity to the actual Codex launch: opt-in `launch_probe_command` (and `launch_probe_version = 2` usage percents) observes the exact launch context before `reserve`/`admit` and inside the spawn supervisor; a non-available, unclean or identity-changed answer keeps the attempt ready with the same session and a local retry hold instead of parking the task or lane.
- Read independently enrolled Codex launch bindings and the shared launch cache in the catalog pool observer.
- Add the native-result path adoption interface (`mesh-runner native-result-config` status/adopt/rollback on the existing extension listener): disabled by default, startup-pinned operator policy plus a separate HMAC key, signed revocations, and a process-epoch loaded receipt; it grants no install, enrollment or channel authority.
- Admit a newly configured repository beside the restored `active` checkpoint journal of a waiting attempt instead of refusing it as `rejected_checkpointed`; any other journal phase, a row without its live lease, a duplicate, a row for the requested repository or an unjournaled lease stays refused.
- Keep a `decision:@agent` question as a request when the addressee's live task itself waits on the asking card: converting it into a card wait formed a wait cycle whose notice asked for the same question again.
- Tell a wait cycle once per card and per question on the other side: the runner remembers every cycle it already told a card (the last 64, kept across restarts and reassignment), so the same card wait declared again, replayed, or met again after the other side returns to an earlier question is kept without a new turn; a question not told before wakes once.
- Add operation-scoped pause tokens (`<state_dir>/pause.d/<name>/<token>`, host-wide `pause.d/_all/<token>`): an operation takes and releases only its own token, so removing it can never lift a foreign `pause/<name>` marker, config `paused` or another operation's token. Leftover tokens fail closed and are listed in status (`pause_tokens`) and metrics (`mesh_runner_agent_pause_tokens`).

## 0.1.40

- Let the ordinary card observer proceed when Mesh explicitly satisfies every supported formal dependency condition; retain conservative handling of unknown or unsatisfied conditions.
- Clone checkpoint files through the native APFS API without starting a process per file; retain a portable copy fallback.
- Report bounded slim-checkpoint failure reasons and preserve bytes, modes, staged changes and symlinks on restore.
- Restrict checkpoint prune and compaction to explicit manifest pins; reject changed, active, ambiguous and unsafe checkpoints.

## 0.1.39

- Add the narrow, policy-pinned `admit-git-index` admission (one closed operation, scoped receipt, per-delivery re-proof); see docs/git-index-admission.md.
- Clarify implementation comments and docstrings without changing behavior or fixture bytes.
- Add `requirements.lock` (hash-pinned, generated from the `receipt` extra) with a declaration/lock drift test and a CI scanner-coverage job.
- Use synthetic identifiers in the admission test fixtures; binding and negative controls are kept.

## 0.1.38

- Package bounded initial, review and resume comment context for every routed provider, with comment IDs, truncation markers and authoritative fetch hints.
- Preserve the 0.1.37 account-capacity admission and same-session quota recovery changes.

## 0.1.37

- Keep provider quota within account admission; reject quota-driven task schedules and preserve same-session recovery.
- Require fresh capacity confirmation for reserve and admit, then launch with the selected account environment.

## 0.1.36

- Enrolled reviewer-receipt source for the native result channel (merged to main after 0.1.35 was published) is now
  packaged; retained controls pin the exact A1-issued packet vector. Receipt delivery is data, never admission.

## 0.1.35

- A chat turn that fails before its native process starts now leaves a durable, secret-free diagnosis (runner log and
  a separate durable diagnostics file): stage, exception class, an allowlisted reason (env command/file failure, barrier refusal,
  workspace error, OS error) and the own-package file/function/line. No exception text, traceback, environment,
  arguments, prompt or full paths. Terminal error codes, guards and retry are unchanged.
- Provider-qualified `mr:` / `pipeline:` waits: `mr:github:org/repo!N`, `mr:gitlab:group/project!N`,
  `pipeline:github:org/repo#N` (`mr:github:` supports plain and `until=merged`; its `until=change|success` is refused, use `pipeline:github:…#run`). An explicit
  provider always wins over the `[github]` allowlist, so a GitHub repo no longer has to be listed (and thereby capture the
  same-path GitLab project); it is never read from the other provider, and a provider that is not configured fails closed.
  Observation/dedupe keys carry the provider. Bare targets keep the allowlist inference. `merge:` has no qualified form
  and is rejected with the supported spelling. `[github]` with only a token now builds the GitHub reader.
- `host_local_qa_assignment_v1`: the frozen brief/policy/schema bytes are no longer packaged. They stay private and are
  read only from the explicit local opt-in `bundle_dir`, verified against the unchanged sha256 pins before any registration,
  credential read or state change (fail closed); absent/empty/`enabled = false` still never reads them. The disable path
  (`qa-revoke`, `revoke`, `status`) works without the bundle. Only `conformance.json` ships.

## 0.1.34

- Add default-OFF `host_local_qa_assignment_v1` to the existing-session receiver: a separate opt-in section, scoped
  credential, server-side admission and routes (`/runner/host-local-qa/v1/...`), a strict sender envelope pinned to the
  frozen `schema.json`/`policy.json`/`brief.txt` bytes, an authoritative backend `/check` before the paste and again before
  the Enter (iTerm paste is split into stage/submit), and a host-local guard (`session-receiver qa-check`) for the operation
  owner. One lock, journal and injector with the metadata receiver; legacy routes, admission and ledger are unchanged.
  See `docs/host-local-qa-assignment.md`.

## 0.1.33

- Add opt-in `stall_wait_v1`: `⏳ WAIT stall:#<this-card-8hex>` on an operator-armed registration
  (`mesh-runner stall-wait arm|inspect|disarm`) sleeps until the first NEW own-author
  `[mesh-stall-control:<uuid>]` result comment on that card, with a durable baseline and exactly one
  wake of the same session. See `docs/stall-wait.md`.

## 0.1.32

- Add opt-in `recovery_wait_v1`: `⏳ WAIT recovery:#<this-card-8hex>` on an operator-armed registration
  of a FUTURE shared recovery card (marker + project + recovery owner + receipt author). The registration
  may precede or follow the card; Runner reconciles the actual Mesh cards, binds one, and delivers one
  wake when it is `done` and carries a `carrier-receipt: <ref>` comment by the registered author. Evidence
  only, never a security clearance. See `docs/recovery-wait.md`.

## 0.1.31

- Add opt-in `lifecycle_wait_v1`: one bounded wait on the first new, correlated native SessionStart
  lifecycle event (startup/resume/compact, plus hook failure) of the implementation-pinned profile (`lifecycle_wait.PROFILE`), with durable
  cursors, dedup and owner/generation-checked delivery. Disabled until an operator arms it.

## 0.1.30

- Add opt-in durable native-result delivery with enrolled source lineage, exact receiver ACK,
  restart-safe cursors, explicit terminal ownership and guarded receiver rebinding.
- Keep native-result observation disabled until an operator enrolls the source and receiver.

## 0.1.29

- Durable launch intent: the exact prompt and refs to mandatory events are journaled and persisted before any native
  process exists (journal first, then the DB intent). PID, birth and turn are registered before stdin; a CAS or commit
  refusal keeps the native behind the EOF gate. Queues and wake are consumed only on a matched acknowledgement.
- A native prompt accepted before the driver died is recovered from native hooks/transcript bound to session, turn and
  the exact prompt (exact text compare: paste wrapper, line endings and edges only). No new paste, no second writer;
  an unprovable state stays UNKNOWN (`prompt_delivery_unknown`) with a resolution owner. A fresh Codex session, which
  has no id before its first input, is bound to the journal at the exact native acceptance.
- Equivalent headless contract for both harnesses (`headless_acceptance`, `spawn_gate`), `mesh-runner recheck-delivery`.
- Upgrade/rollback guard `mesh-runner-guarded-launch` (`launch_guard`): the service must start through it; it refuses a
  target without the capability and any target/config/tree change while a pending intent or UNKNOWN row exists.
  README "Updating the runner" lists the supported launch path; an unguarded old binary is unsupported.

## 0.1.28

- `reviewer-merge --apply`: the merge-only card is created with the live Todo `status_id` read from the project's
  statuses (every status row is validated: id, known category; any malformed row refuses before the POST). The status
  list is read only on the create path, so a repeat apply or a repair of an existing card does not depend on it.
  New in this version: `docs/reviewer-merge.md` "Positive check after install" (dry-run, apply, same-id repeat).

## 0.1.27

- `archive_root` / `archive_require_mount`: slot archives go to an explicit root on a mounted volume (fail closed
  when the volume is not the mounted one, no fallback to the internal disk); source files move by same-device atomic
  rename into a durable quarantine with manifest and restore, no auto-purge. See README "External archive root".

## 0.1.26

- `session-receiver`: opt-in `transport = "iterm"` for a native producer already running in iTerm2: exact terminal session
  (`unique id` + tty) and producer pid/start/foreground group proved before each injection, bound with host/runner/session
  ids into the ledger pin and admission record; fail closed, one injector, tmux unchanged. See `docs/session-receiver.md`.

## 0.1.25

- `mesh-runner session-receiver`: the existing-session metadata receiver as a separate opt-in process (pinned producer,
  shared ledger and lock with the controller receiver, fail closed). See `docs/session-receiver.md`.
- `session-receiver revoke`: disable/rollback revokes only this receiver's own recorded admission (`DELETE /receivers/:id`),
  fail closed on unknown/mismatched identity, never reports a failed revoke as success; ledger and producer untouched.

## 0.1.24

- WAIT observation runs under one shared deadline (`wake_budget_seconds`, default 45); a failed read keeps
  the wait untouched and records `wake_read_failed` with kind, stage, elapsed and retries. See
  `docs/wait-observation-budget.md`.

## 0.1.23

- Repo admission compares the declared agent env, not the session env a turn applied, so a normal turn no longer causes a false `rejected_config`.
- `status.json` carries `receipt`: PID, release, source dir, `status_unix` of the same snapshot and
  `cfg_sha256` over the stable serialization of the in-memory `RunnerConfig` (resolved defaults; fields
  declared `repr=False` and secret-named fields excluded). A disk edit does not move it until the held
  cfg is replaced by a restart; a serialization failure writes `receipt.error` with no digest.
  Scope and reproduction: `docs/config-receipt.md`.

## 0.1.22

- Preserve reviewed WAIT intent and access recovery in one integrated path.
- Hold unlaunchable sessions before spawning them.
- Persist the auth-recovery wake so it survives a restart.
- Resume the native session on a proven same-path workspace restore.

## 0.1.21

- Add the opt-in `receipt_wait_v1` receipt WAIT (`⏳ WAIT receipt:#<card8>`): a durable operator-armed registration with per-journal cursors wakes the card once on the first new accepted correlated Telegram text receipt; `mesh-runner receipt-wait arm|inspect|disarm|rebaseline` (docs/receipt-wait.md).
- Accept Git numeric collision admin names for exactly owned legacy worktree slots while preserving ownership and reciprocal path checks.
- Reconcile newer corroborated owner card WAIT intent without waking a stale target or taking over its dependency.
- Preserve durable capability holds and release them only through authenticated exact-binding access recovery with fresh own probes; support the configured native permission-grant notifier.
