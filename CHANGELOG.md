# Changelog

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
