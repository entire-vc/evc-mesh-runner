# Mesh Agent Runner

Runs Claude Code and Codex agents on their [Mesh](https://github.com/entire-vc/evc-mesh) tasks.

- **Several sessions per agent.** One Mesh agent identity works on up to its own
  `max_concurrent_tasks` tasks at once — each task in its own native session
  (Claude session id / Codex thread id). No extra agents, no copies of its rules.
- **One worktree per task, from a warm pool.** Parallel writers never share a
  checkout. Clean worktrees are reused, so dependency installs and build caches
  survive; a worktree with uncommitted or unpushed work is kept, never wiped.
- **Waits resume the same session.** When an agent ends a turn with
  `⏳ WAIT pipeline:…`, `mr:…`, `card:…`, `decision:@…` or a time, its slot is
  freed and the session is kept. When the condition is met — or someone writes on
  the task — the runner resumes that same conversation instead of re-explaining
  the task to a fresh one.
- **Policy-free.** Team rules, review gates and prompts live in the agent's own
  profile directory (`CLAUDE.md`, skills, MCP config) and an optional preamble
  file. The runner only schedules, isolates and resumes.

Status: early (0.1). Python ≥ 3.11, standard library only.

## How it works

```
Mesh agent ── cap = max_concurrent_tasks
   └─ attempt (task) ── native session ── worktree(s) ── Mesh checkout while running
```

Each tick, per agent: settle finished turns → check wake conditions → resume
woken tasks, then start new ones while active sessions < cap → heartbeat
checkouts. A turn is over only when the harness process **and all its children**
have exited, so a background command keeps the slot busy instead of racing the
next writer. See [docs/architecture.md](docs/architecture.md).

## Outcome contract

The agent ends each turn with a comment on the task whose last line is one of:

```
⏳ WAIT pipeline:<group>/<project>#<id>     ⏳ WAIT mr:<group>/<project>!<iid>
⏳ WAIT card:#<8hex>                        ⏳ WAIT decision:@<handle>
⏳ WAIT merge:<group>/<project>!<iid> after=<sha>   (opt-in, see Merge waits)
⏳ WAIT 2h | 2026-10-20T06:00Z              ↻ NEXT
❓ Blocking @<human>: <question>
```

or moves the task to its review/done status. No outcome → the runner asks once
in the same session (`max_no_outcome`). A second turn in a row without a readable
outcome puts the task back in todo with the same lane until new input arrives (a comment,
a change to a linked merge request or pipeline, a closed dependency: the `requeue_on_cap`
inputs; no timer; without `requeue_on_cap` it is handed up as before). The
`no_outcome_route_after`-th turn in a row without a readable outcome on the task (default 3,
any lane; a readable outcome resets the count) hands it to the lead. A task that was handed up
for this reason is only ever kept by the lead that receives it, never handed further up.

A time wait whose reason names an event (a verdict, an answer, a person, a card) is answered
once, in the same session, with the forms that wait on the event itself; that answer does not
count as a missing outcome. The same form repeated afterwards is an ordinary miss. An absolute
`⏳ WAIT <moment>` whose moment has already passed (or is within a minute) means `↻ NEXT`.

Observable waits are polled without starting the agent while their target is healthy
and unchanged; time waits wake at their deadline. Age re-checks apply to unreadable
or unavailable targets and human gates, using `wait_first_recheck_hours` (2), then
`max_wait_hours` (6), without counting against the daily wait budget. A `❓ Blocking`
ask to a person retains its separate one-day re-check. A time wait longer than an
hour must give a reason (`⏳ WAIT 6h: <why>`), and one whose reason names a merge
request, a card or a pipeline is sent back once to wait on that event instead. A
`card:` wait whose card is itself waiting on this agent is woken as a cycle.

With `queue_wait_freshness_v1` enabled, a parked typed refill can adopt queue
WAIT without a turn. The newest machine timestamp selects the source; its typed
contract must be strictly valid and all sources at that timestamp must agree.
A newer integer source can supersede an older malformed contract without editing
history. A typed source missing its machine timestamp remains UNKNOWN. Adoption
preserves an existing future time deadline and the card's fresh `start_after`:
it waits until both have passed before removing the old schedule. Markerless
legacy cards are not inferred from their title or automatically migrated.

After observing a known CI result, declare a typed change wait rather than
waiting for that same result again:

```
⏳ WAIT mr:group/project!56 until=change after=<full-head-sha>:14845:failed
⏳ WAIT mr:group/project!56 until=success after=<full-head-sha>:14845:failed
```

The baseline is explicit machine metadata, never inferred from the explanation.
`change` accepts a different head/pipeline/status observation; `success` requires a
changed successful pipeline on the current head. A pending pipeline does not
satisfy `success`. `pipeline:group/project#14845` supports the same fields for
retries of that exact pipeline (the baseline ID must match). Missing/malformed CI
observations remain UNKNOWN and do not acknowledge a result. Ordinary WAIT syntax
keeps its existing behavior, including an immediate wake on an already failed CI.

`after=` may be left out: `⏳ WAIT pipeline:group/project#14845 until=success` (also
`until=change`, and `mr:group/project!56` with `until=success|change|merged`) takes the
CI/MR state read at the moment the wait is declared as its baseline. A target that is
already final (a pipeline in any terminal status, a merge request merged or closed), or that
cannot be read, leaves the plain wait; only a non-final target keeps a typed baseline.

An ordinary typed `pipeline:... until=success after=...` wait also gives one
action-required notice when that exact pipeline and SHA have finished with
`failed`, `canceled`/`cancelled`, or `skipped`, or are blocked in aggregate `manual`
status. Optional manual jobs in a running or successful pipeline do not
trigger it. This notice does not satisfy `success`, close the request, waive a
budget, retry a pipeline or start a manual job. It asks the owner to inspect the
outcome and take the next authorized step or wait on a new concrete action;
manual status requires arranging the required review or authorized action while
preserving project and human gates. Its durable receipt
is separate from completion and is acknowledged only with an actual writer
launch; a pending READY notice survives restart and admission refusals. The same
pipeline/SHA/status observation is not delivered again, but a new requested identity
can be. Later success still has its normal, separate wake. Missing/stale identity
never produces this notice or success; existing bounded UNKNOWN-error recovery
remains diagnostic only. Review and exhausted-budget observers do not use this
action path.

A typed wait that nobody will bring about is sent back at declaration instead of
being parked, and counts against the daily wait budget like any idle wait:
`pipeline:... until=success` on a pipeline that is already `manual`, `failed` or
`canceled`, and `mr:... until=merged` or `until=change` on the task's own open merge
request whose head pipeline is `success` or `manual` and which has no label starting
with `review_label_prefix` (`verified-by:`; `''` turns this check off). The merge
request is the task's own when it is linked to the task, names the task id in its
title, description or source branch, or was authored by the agent's GitLab user:
`gitlab_username` in the agent's block (default: the agent name lower-cased; `''`
turns the author rule off). A merge request authored by anyone else is not the
task's own unless it is linked or names the task. The returned turn says what will not happen and the next step (run the review
and the merge, or fix/retry and wait on the new result). A target that cannot be read
keeps the wait as before.

`mr:... until=merged` on any open merge request that will not merge as is is sent back
too: one that GitLab reports in conflict with its target (`has_conflicts`, or
`detailed_merge_status` `conflict` / `need_rebase`), and one whose head pipeline is
`failed`, `canceled` or `skipped` once that result was already delivered to the agent
(a red head not yet delivered is accepted once, so the owner notice reaches it). The
turn names the way out: fix and push (or rebase and push), get the review verdict on
the new head, then `⏳ WAIT merge:...` when a merge gate is configured. While such a
wait is parked, the owner is told once per head when the MR enters a merge conflict.

A typed `pipeline:...` wait (`until=change` or `until=success`) on a pipeline in the
terminal `canceled` or `skipped` state with no queued or running job is sent back once
per target and status: no retry is coming, so nothing will change it. The turn names the
newer pipeline of the same ref when GitLab has one, with a ready `⏳ WAIT` line for it.
If the agent declares the same dead wait again, it is kept as an ordinary wait and the
runner records a `dead_pipeline_rewait` mark instead of bouncing again.

To wait for the merge itself rather than CI, declare an explicit MR-state wait:

```
⏳ WAIT mr:group/project!56 until=merged after=<full-head-sha>:opened
```

Copy the observed opened MR's SHA; it records the baseline and is **not an approval
pin**. A different head may merge. Green/manual CI, new pipelines and new heads do
not complete this predicate. `merged` completes it; `closed` without merge wakes
once as terminal failure, never as successful merge. A missing/invalid terminal
RFC3339 timestamp (full date/time with seconds and explicit timezone) or unavailable
observer remains visible as `wait_observation: unknown`;
it neither acknowledges the result nor triggers an age wake. Restore observer
access to recover observation; existing addressed comments and human/ownership
gates retain their normal behavior. Malformed explicit fields are rejected, never
silently treated as legacy waits.

MR-state delivery uses the existing atomic receipt transition, scoped to agent,
source task and exact MR target. State plus terminal timestamp suppresses the same
terminal event across replacement attempts/review/restart; reopening and closing
again with a new timestamp is a new event. No additional poller or schema is added.
This is opt-in too: the current owner adopts the marker on a normal turn after
checking the actual MR. Existing legacy or typed CI waits are not rewritten.

#### Merge waits (`merge_command`)

Once a review verdict covers the MR head, the only thing left is often a deterministic merge
script that may answer "queued behind pipeline N" several times. `merge:` hands that loop
to the runner so no model turn is spent on it. It is off unless `[runner] merge_command` is set
(and a `[gitlab]` section exists); otherwise the declaration is sent back with a clear message.

```
⏳ WAIT merge:group/project!56 after=<full-head-sha>
```

While the wait is open the runner runs `merge_command` (an argv template, no shell; placeholders
`{target}` `{project}` `{iid}` `{sha}`) in `merge_command_cwd`, with the same environment it builds for
that agent's sessions (so it acts under the lane's identity), and judges the result by a fresh read of
the merge request, never by the command's text:

- MR state `merged`: the agent is woken once ("MR !56 merged at `<sha>`: deploy / verify ...").
- The command's last output line is `⏳ WAIT pipeline:<project>#<id> ...` (queued): the agent stays
  asleep. The command is run again when that pipeline or the MR's head pipeline reaches a final
  status, and in any case every `merge_rerun_minutes`; once the wait has been
  queued for `merge_queue_hours` the agent is woken with the run history.
- MR head differs from `after=`, MR closed, a non-zero exit, a refusal, a timeout, or any other
  output: the agent is woken with the tail (last 40 lines) of the command's output.

| key (`[runner]`) | default | meaning |
|---|---|---|
| `merge_command` | unset (feature off) | argv template, e.g. `["tools/merge", "{iid}", "{project}", "--expected-sha", "{sha}"]` |
| `merge_command_cwd` | runner cwd | working directory of the command |
| `merge_timeout_minutes` | `15` | one run; the process group is killed after it |
| `merge_rerun_minutes` | `5` | fallback re-run interval while queued |
| `merge_max_runs` | `30` | deprecated, ignored (polling a queue is not a try; see `merge_queue_hours`) |
| `merge_queue_hours` | `24` | a wait that stays queued this long wakes the agent |

This is an explicit opt-in contract, not a migration of existing stored waits. Legacy
`WAIT pipeline:` / `WAIT mr:` declarations retain their current completion semantics;
switching those untyped aliases can still repeat an already known failed result.
For a live affected task, its current authorized owner should read the exact current
MR head/pipeline/status and, on its next normal turn, declare `until=change` or
`until=success` with that baseline. Do not rewrite other tasks, mutate Runner state,
force a wake, or substitute a guessed baseline. Updated instructions reach existing
sessions only when the normal harness/prompt lifecycle supplies them; an operator
may give the specific owner a scoped migration note, respecting human/ownership gates.
Confirm the next stored wait carries `ci_change`, that the unchanged baseline starts
no new writer, and that a qualifying subsequent observation does. Historical legacy
loop suppression is not claimed until that task has adopted the typed contract.

Delivery receipts belong to the agent/source task and canonical project/pipeline,
so MR/pipeline aliases, restarts and replacement attempts cannot redeliver the
same last observation. A waiting wake and its receipt commit together with the
durable `ready` state and wake text. Review eligibility only observes; its receipt
commits with the successfully spawned attempt's `running` state. A crash between
process creation and that commit may replay; this is not distributed exactly-once
delivery. Unrelated linked pipelines and review age cannot bypass a typed wait;
addressed comments, review verdicts and existing ownership/human gates still apply.
No new service, configuration, query cadence or database schema is introduced.

A wait declared right after the previous wait was met (the time came, the card
closed, the pipeline or merge request moved, the gate lifted, the awaited answer came)
is the next step of the work, not churn: it counts against `daily_step_budget` (24)
instead of `daily_wait_budget` (8), so a task moving through several CI rounds is not
stopped for the day while an endless retry loop still is. The idle-wait budget counts
waits since the last verified CI step that day, across attempts and runner restarts.
A changed terminal pipeline result (on the current head for MR waits) or an MR
merge/close delivered by the watcher resets it only when the next outcome is a new
step. Each pipeline identity counts once per agent/task, shared across MR/pipeline
spellings; status flapping cannot reset it again. Already-final seeds, timers,
comments, card/decision steps and unknown observations do not reset idle waits.
Previously recorded outcomes have no CI receipt and are not reclassified. The daily
NEXT and step caps still apply; UTC midnight starts a new daily budget.

By default any comment by someone other than the agent wakes a waiting session
(`comment_wake = "all"`). With `[runner] comment_wake = "addressed"` a comment written by
another agent no longer ends a pipeline, MR, merge, card, time or decision wait early unless it
@-mentions the waiting agent, comes from an author listed in `comment_wake_always_from`,
or (decision waits) is written by the addressee or carries an `Answer:`/`Decision:`
marker. A comment that opens with an FYI marker (`FYI`, `**FYI`, `[FYI]`, `fyi:`, or a first
bold or heading line with `(FYI`) does not wake by authorship alone, so an `always_from`
author's cross-posted note stays quiet unless it @-mentions the waiting agent. A person's
comment always wakes, and so does any comment whose author kind the API does not state.
`❓ Blocking` waits are not filtered; card and merge waits still end on their own event (the
awaited card closing or shipping, the MR merging or closing). A held-back comment is
not lost: the session reads every comment since its previous turn when the wait ends for
its own reason. Measured on one fleet over a week, such a wake ended in an unchanged
re-declared wait in about a quarter of the cases.

A decision wait whose addressee has a request card still wakes on every comment the addressee
writes on the waiting card, progress notes included. `[runner] decision_wake = "answer"` (or the
same key on one `[[agents]]` block, which wins) narrows that to an answer: a comment on the
request card from its addressee or a person, or the request card closed; or, on the waiting card,
a person's comment, an @-mention of the waiting agent, a comment that opens with `Answer`,
`Decision`, `Ответ` or `Решение` (or contains `Answer:`/`Decision:` at a line start), one that
quotes the request card id, or an author listed in `comment_wake_always_from`. Other notes stay
in the thread and are read at the next wake, or at the wait's first re-check interval if nothing else ends it. The request text then tells the addressee to start
the answer with `Answer:`. A wait without a request card keeps the default (`"any"`). Measured,
the filter removes the repeated-wake waste for a lane whose addressee posts many notes, and
delays real answers that arrive as plain comments, so enable it per lane, not fleet-wide.

A decision request belongs to its source task, resolved recipient and question
(with whitespace normalized). An observational comment or periodic re-check wakes
the session but keeps the unanswered request. Restarting or replacing an attempt
reuses it; changing the recipient explicitly supersedes it. Source-task
completion or cancellation closes it, while an infrastructure failure or source
reassignment retains it. Ownership is checked again before reuse or supersession.
Failed closes and terminal sources are reconciled at most once per five minutes
per retained question, with at most ten questions checked per pass.

Within `[runner] decision_reuse_hours` (default 24, `0` = off) of filing a request, a new
question from the same source to the same agent does not file another card: the question is
added as a comment on that request, which is reopened (assignee kept) if it was closed, and the
wait is bound to it. A different source or addressee files a new request as before. When the
new question repeats the previous one verbatim (whitespace collapsed, case ignored) and the
addressee has already commented after it was asked, the wait is sent back at declaration with
the start of that answer: the same question gets the same answer, so the agent waits on the
event that changes it or asks something else. People are never asked here.

Three kinds of decision wait to an agent file no request at all, each behind its own `[runner]`
switch (all default on, `false` restores filing the request):

- `decision_live_task_card_wait`: the question names exactly one other task (`#<8hex>` or its
  uuid) that the addressee holds and is working on (todo, in progress or review), such as "pass
  the handback from #x when ready". The addressee could only answer "not ready yet", so the wait
  becomes a card wait on that task (its `blocks` edge included): it wakes when the task closes or
  ships, or when the addressee comments on it. Nothing is posted to the addressee. A task held by
  someone else, a closed or backlog task, or several named tasks file the request as before.
- `decision_identical_no_reopen`: the same question again (case, whitespace, trailing punctuation
  and trailing references to the asking task ignored) from the same asker and source to the same
  agent is attached to the request that already carries it, open or closed within
  `decision_reuse_hours`: no new card, no comment, no reopen, since each of those starts a new
  session for the addressee. Only a changed question is asked on that request (and reopens it) as
  above. A closed request attached this way hands the question back to the asker after the
  re-check interval (`wait_first_recheck_hours`) unless a comment answers it first.
- `decision_default_proceeds`: the question states the asker's own proposed default (`default:`,
  `proposed:`, `if no objection`, `дефолт:`, `по умолчанию:`, `предлагаю`, `рекомендую`) and no
  hard rule (`hard_ask_patterns`, `hard_ask_labels`) matches it. The runner posts the question on
  the asking task, @-mentioning the addressee, which may object there, and the asker continues
  with its default at once (counted like a `↻ NEXT`). Hard questions and questions to people are
  asked as before.

Each is logged in `runner.log` and recorded as an attempt event (`decision_to_card_wait`,
`decision_attached`, `decision_default_proceeds`); the first and the last are also written to
`events.jsonl`.

New decision requests can opt into no-Git metadata admission for explicit resolved
recipient UUIDs with `[runner] decision_request_metadata_only_recipients = [...]`
(default `[]`, OFF). Existing and ambiguous requests are never relabelled. This
setting is loaded at Runner startup; see [rollout and rollback](docs/decision-metadata-producer.md).

The existing answer contract is unchanged: a comment from the addressed recipient,
or an explicit `Answer:`, `Decision:`, `Ответ:` or `Решение:` marker, is an answer.
The runner does not infer approval from language or distinguish automated noise
from other comments by that recipient without an explicit API contract.

A decision create with an ambiguous API result (timeout, 5xx, crash before the
returned ID was recorded) is held in state `creating` and settled by the runner. The
request body carries `[runner decision-key:<hash>]` and a per-create
`[runner decision-create:<id>]` marker. Each reconcile pass searches the source
task's project for a card with that marker: a match is adopted and the normal
lifecycle continues. Only when the create window (10 minutes) has passed, two
searches found nothing, the source is still open and the lane still waits on the same
question, the create is repeated once with the same marker. A card found for a lane that moved on is closed, not adopted. A failed search never
triggers a create. The Mesh API offers no server-side idempotency; this is
search-before-retry, not exactly-once.

## Quick start

```toml
# runner.toml
[runner]
mesh_url = "https://mesh.example.com"
state_dir = "~/.local/state/mesh-runner"

# launch_mode = "interactive"             # turns go into live interactive sessions in tmux (default);
#                                         # "headless" runs claude -p / codex exec per turn
# interactive_idle_minutes = 60           # a warm interactive session idle this long is closed
# interactive_max_sessions = 20           # live interactive sessions on this host, all agents
# background_wait_minutes = 45            # an interactive turn's prompt waits this long while the session's
#                                         # background agents run (0 = paste at once); see docs/architecture.md
# turn_first_output_minutes = 0           # end a turn whose harness has not replied this long (0 = off)
# turn_idle_minutes = 0                   # end a turn with no progress this long (0 = off)
# turn_external_wait_minutes = 0          # same, while the harness runs a tool / background task (0 = off)
# basic_auth_file = "~/.config/mesh-runner/mesh-basic-auth"   # "user:pass", a Mesh behind HTTP basic auth
# review_own_hold_hours = 1               # own work in review waits this long for someone else
# wait_first_recheck_hours = 2            # first fallback re-check; healthy observable waits stay asleep
# max_wait_hours = 6
# comment_wake = "all"                    # "addressed": agents' unaddressed comments do not end a pipeline/MR/merge/card/time/decision wait
# comment_wake_always_from = []           # author names that always wake, with comment_wake = "addressed"
# decision_wake = "any"                   # "answer": a decision wait with a request card wakes on an answer only, not on the addressee's progress notes
# decision_reuse_hours = 24               # a new question from the same source to the same agent reuses its request card (0 = file a new one)
# decision_live_task_card_wait = true     # a question about the addressee's own live task waits on that task, no request
# decision_identical_no_reopen = true     # the same question again is attached to its request: no comment, no reopen
# decision_default_proceeds = true        # a non-hard question carrying the asker's default proceeds; the addressee may object
# review_unstartable_retry_minutes = 15   # a review no reviewer could start is tried again

[chat]                                    # optional: Agent Chat (interactive sessions only, never headless)
# enabled = false
# agents = ["builder"]                    # agents whose chats this runner serves
# credential_file = "~/.config/mesh-runner/chat.credential"   # runner credential, chmod 600
# max_chat_sessions = 3                   # live chat sessions per agent
# host_cap = 6                            # live sessions on this host, chats and cards together
# session_idle_minutes = 10               # an idle chat session is stopped; the next message resumes it
# chat_workspace = "none"                 # "none" | "read" | "write"; none/read: no repository checkout (read-only tools);
                                          # write: a slot of the card worktree pool (first repo) for the life of the session
# mcp_command = ["/usr/local/bin/evc-mesh-mcp", "--transport", "stdio"]   # Mesh MCP of a chat, run with the
#                                         # thread's token (MESH_CHAT_TOKEN_FILE), never the agent key; it
#                                         # replaces the profile's `evc-mesh` server in the session
# mcp_url = ""                            # MESH_API_URL for it; empty = [runner] mesh_url

[gitlab]                                  # optional: pipeline/mr waits
base_url = "https://gitlab.example.com"
token_file = "~/.config/mesh-runner/gitlab.token"

[[agents]]
name = "builder"
key_file = "~/.config/mesh-runner/builder.key"   # Mesh agent key, chmod 600
harness = "claude"                               # or "codex"
profile_dir = "~/agents/builder"                 # the agent's rules, skills, MCP config
args = ["--model", "sonnet"]
# gitlab_username = "builder"                    # its GitLab user (default: name lower-cased; "" = off)

  [[agents.repos]]
  name = "app"
  base = "~/src/app"                             # an existing clone
```

```sh
pip install .
mesh-runner once --config runner.toml     # one pass, prints what it did
mesh-runner run  --config runner.toml     # service loop
mesh-runner status --config runner.toml   # local view of attempts
```

Set the agent's **Max concurrent tasks** in Mesh to choose how many sessions run
at once. Cost of parallel sessions is the operator's choice.

An exact terminal Claude authentication banner (expired OAuth session or the
CLI's `Not logged in … Please run /login`) creates a durable local hold for the
profile used by that task process. The hold gates task admission and resumption;
standing chat sessions keep their existing, separate lifecycle. The runner preserves the attempt/session/worktrees,
releases its checkout and gate reservation, and does not hand product work to a
lead or invoke the quota-switch hook. Other profiles remain eligible; processes
already running are allowed to finish. `status` and `plan` list safe profile
fingerprints and the command to clear each hold. No credential content is read.

After repairing authentication outside the runner, explicitly clear only that
profile using the command from `status`, for example:

```sh
mesh-runner clear-auth-hold --config runner.toml --profile <exact-fingerprint-from-status>
```

Normal routing, ownership and budget checks still apply. The ready attempt is
eligible on the next admission check (an existing two-minute admission backoff
can still apply). This command does not log in, rotate accounts or start work.
Reauth in the same profile does not lift a hold implicitly. If a process started
before this runner version has no recorded launch profile, its attempt is held
under a separate fingerprint; no current account is guessed or quarantined.
Physical turn counts remain intact. Only auth-only turns with no assistant/tool
work (except the CLI repeating the exact auth banner) are excluded from the
per-attempt product turn cap; other budgets and real work remain counted.

Optional hooks per agent:

- `env_command = ["..."]` runs before every spawn and prints `KEY=VALUE` lines
  for the session (identity, the account the agent is routed to now). A failure
  stops the start. `MESH_RUNNER_PROFILE=<name>` is taken out of the env and
  becomes the harness profile (`codex --profile`) for that spawn.
- `on_limit_command = ["..."]` runs when the harness reports a usage limit;
  exit 0 means it moved the agent to another account and the turn is retried.

- `start_gate_command = ["..."]` is the operator's admission control (routing,
  capacity reservations). It runs in phases, `MESH_RUNNER_PHASE` =
  `reserve` (before a session starts or resumes), `admit` (the Mesh checkout is
  held: `MESH_RUNNER_CHECKOUT_REQUEST/GENERATION/SESSION` identify the writer) and
  `renew` (every heartbeat of a running turn), with `MESH_RUNNER_AGENT/TASK/MODE/
  ATTEMPT/HARNESS`. Exit 0 = go; `KEY=VALUE` lines are kept for the turn and passed
  to later phases as `MESH_RUNNER_GATE_<KEY>`. Exit 75 holds new starts for the
  agent: repeated refusals back off for 2, 4, 8, then at most 10 minutes. Exit 76
  holds only that task for 10 minutes. An observed update of the refused task permits a fresh gate decision; a
  successful local turn release also rechecks a capacity refusal. Neither event
  authorizes a start. Successful admission resets the backoff.
  Chat starts use a separate `transport` phase (15-second hook timeout), with
  empty task/attempt and mode `chat`. It must check only transport, without
  reserving capacity or reading a task. Any refusal leaves a standing pane
  unchanged and Mesh turns queued before claim; it does not spend task failure
  budgets. Active turns, commands, idle reap and lease renewal continue. Deploy
  support for this phase in the operator hook first; older hooks may allow an
  unknown phase. This phase does not reconfigure or restart live chats.
  `HOLD_REASON_CODE=human|profile|capacity|review|unknown` selects a fixed public
  diagnostic; emit exactly one code. Unknown or duplicate codes stay generic.
  Legacy plain text remains supported; recognized routing text containing `=`
  is reduced to a safe category, preserving finite QA tags. Never print secrets:
  hook key/value outputs are stored in the runner database. These caches are
  process-local; restart permits a fresh decision. Board discovery and running
  turn renewals retain their normal cadence. Other exits hold too. Hooks get only the allowlisted
  environment, in their own process group.
  QA infrastructure refusals (`qa_artifact_unresolved:*`, `qa_fail_closed:*`, and
  exactly `qa_no_selection:sibling_evidence_unknown`) use the durable, bounded
  `review_unstartable_retry_minutes` retry even without a card change: pool or
  controller evidence can recover externally. Every retry still runs admission
  control. Reviewer identity and independent-reviewer absence refusals keep their
  existing hold until an observable card change; they are not infrastructure failures.
  Older generic `qa_no_selection: review admission unavailable` marks retain that
  hold: their original subtype is unknown, so a card event must request a new gate decision.
- `turn_end_command = ["..."]` runs once when that turn ends or a start is
  abandoned (`MESH_RUNNER_PHASE=end`, `MESH_RUNNER_OUTCOME`) with the gate's output:
  release the reservation. A gate must be phase-aware: reserve once in `reserve`;
  `admit` and `renew` must be idempotent.

Several harnesses (`[agents.alt.codex] profile = "…"`, or `[agents.alt.claude] args = […]`):
when the env command prints `MESH_RUNNER_HARNESS=codex|claude`, a new task starts on
that harness, and a waiting task the agent was moved away from continues in a new
session there (a session cannot move between harnesses). Each attempt keeps its own.

Standing chat session (`[agents.chat]` `session = "lead"`, optional `command`
(required for non-Claude harnesses), `idle_command`, `max_age_hours`, `adopt`):
for an agent that a person talks to through a bridge typing into a tmux session.
The runner keeps that session alive in the agent's profile dir with only the
agent's environment and account (`env -i`; nothing from the tmux server), with a
back-off when it keeps dying, and restarts it when the account changes or it is
too old, only while `idle_command` exits 0 (without one: only when it is gone).
The session records its owner, routing digest and start time as tmux options, so
a runner restart keeps them. A same-named session the runner did not start is
left alone unless `adopt = true`. The environment goes through a 0600 file the
shell reads and deletes.

Host memory gate (`[runner] memory_hold_free_pct = 15`, `memory_release_free_pct = 20`,
`memory_hold_swap_mb`, `memory_release_swap_mb`): no new or resumed sessions while
the host is short of memory; running turns are never touched. Off by default.

Host load gate (`[runner] load_hold_per_cpu = 4.0`, `load_release_per_cpu`): the same hold
for CPU. No new or resumed sessions while the 5-minute load average divided by the CPU count
is over `load_hold_per_cpu`; released below `load_release_per_cpu` (0 = three quarters of the
hold value). On by default at 4.0; `0` turns it off. An overloaded host starts a session so
slowly that the driver's start-up waits run out, and every failed start adds load. The
runner logs `host load: HOLD new sessions (...)` / `host load: resume new sessions (...)`
(the memory gate logs `host memory: ...`) and reports `load_hold` in its status.

Codex start-up: a resumed Codex shows its prompt box before it is done starting
(`Resuming session…`), and a prompt entered then is queued under `Waiting for startup`.
The driver does not call Codex ready while those lines are on screen (the `ready` wait is
180 s), and while the queued prompt waits for start-up it extends the accept wait up to the
driver's `startup_wait` (300 s). Past that the turn fails with `start-up stalled` and the
window is kept, not closed.

A `model:<name>` label on a task runs every turn of that task with
`--model <name>` (Claude; a Codex agent takes its model from the profile). The label
is read when the task starts; a value that is not a plain model name is ignored.

Review cards (`feed_review = true`, the default) are fed to their assignee in review
mode. A card the agent itself just moved to review is its own work and waits for
someone else to review it for `review_own_hold_hours` (`[runner]`, or per agent;
default 1), measured from the agent's last comment in that attempt. It comes back to
the agent earlier, with what changed in the prompt, when something happens that the
agent has not seen: someone else comments, a review verdict is posted, a linked merge
request is merged or closed, gets a new head or a finished pipeline (Mesh VCS links;
with `[gitlab]`, GitLab itself), or the wait the agent declared in its last comment is
over. A question to a human (`❓ Blocking`) ends only with someone's comment or with
the human gate being lifted, not with code news. If nothing happens for the whole
window, the author gets it once to route it on (0 = at once). On its own work the
author never gets the accept protocol, at any age, unless someone else commented or a
SHIP verdict came after that work: its prompt forbids moving the card to done and asks
it to fix (after DO-NOT-SHIP or a failed pipeline), route the card to a reviewer, an
acceptor or its lead, or wait. An attempt that ended because the task left the agent
while it was parked is not the agent's work. A fed review card is not fed again for a
day without a new comment.

A verdict is a line matched by `review_verdict_pattern` in anyone's comment and by the
stricter `review_own_verdict_pattern` in the author's own (it may run the reviewer
itself and post the verdict; its requests and plans must not count). Both are regexes
with a `verdict` group and an optional `sha` group, set in `[runner]` or per agent
(`""` = off). The defaults read `Verify: SHIP @1a2b3c4` and `Verdict: DO-NOT-SHIP on
1a2b3c4d`; the verdict word is case-sensitive, and a line naming both verdicts
(`reply SHIP or DO-NOT-SHIP`) is a request, not a verdict. A verdict naming a commit
counts only on the head of a linked merge request when GitLab is configured.

A task blocked by a card in backlog shows `blocked by a card in backlog` on the board
(reason code `blocker_in_backlog`; a Mesh that does not know the code gets the text
only): nobody feeds backlog, so it would otherwise wait silently. The runtime states
the runner writes on tasks it holds but does not run are kept in its database and
cleared after a restart too, once a task is done, cancelled, in backlog or another
agent's; a state someone else wrote since is left alone.

Hand-offs (`daily_start_cap`, the turn budget, a detected loop, `max_compactions`, a runner give-up) go
to the agent's `lead`, that lead's `lead` while it is an agent of this runner, then the
project's lead (`[project_leads]`). A lead that itself started the task
`daily_start_cap` times today is passed over. A task this agent already handed up
today that came back is kept, as is one whose leads are all passed over or not in this
workspace (a hand-off cannot cross workspaces): no start before the next UTC day, said
once on the task. A blocker card the agent may not read (403) is read through another
agent of the same runner when one can; otherwise the task shows `blocker card is not
readable by this agent` (reason code `dependencies_unreadable`). Each new reason a
task is held for is logged once (`<agent>: #<task> held: <reason>`).

### Loops are caught by repetition, not by a count

`loop_streak_limit` (6, `0` = off) is the loop guard. The runner keeps, per task and agent, a streak
of consecutive turns that made no progress (a store mark, so it survives attempts and restarts). At
the limit the task is handed up like any other cap (`requeue_on_cap` or the lead chain) under the
banner `🔁 LOOP`, whose text lists the last turns (NEXT, which wait was sent back and declared again).
A loop is never held to the next UTC day by this path.

A turn extends the streak when nothing moved and it is one of:

- a `↻ NEXT` after which the leased worktrees have the same HEAD and diff as at the turn start and
  the agent wrote nothing new on the card;
- a wait that the runner sent back (the time, dead, stale, unattended, card and repeated-decision
  bounces) and the agent declared again unchanged;
- a wait the agent already declared in the current window but not just before it (alternating
  between waits A and B);
- the same wait as the previous one, accepted, declared again in a turn the agent started itself
  with `↻ NEXT`.

An accepted wait declared again right after itself, after a wake that was not the agent's own NEXT,
leaves the streak as it is: a runner re-check of a standing wait (a person's answer, a decision, a
slow pipeline, a scheduled time) that the agent correctly keeps is not an agent loop, and the cost of
waking it is the wake policy's concern. It neither extends nor resets the streak.

Any of these resets it:

- a comment of the agent's own that is news. The text (declaration lines and `[runner]` notes
  removed) is reduced to its gist: lowercase, URL query strings, dates, times, hex ids and shas
  (7+ hex characters) and numbers removed, punctuation and whitespace collapsed. The gist must be at
  least 40 characters and not seen among the agent's comments in the window. "still waiting for
  pipeline 21234 (checked 14:05)" an hour later is the same comment, and so is a short status line;
- a change in the worktrees (HEAD or diff; an unreadable digest counts as a change);
- a change of the card's status;
- a wake by a met event (a pipeline finished, a card closed);
- a wait the agent has not declared in the current window (the first wait is new).

The window is the turns since the last change in the worktrees, the card's status or a met event.
A new wait or a new comment resets the count but joins the window, so alternating between two waits
or two comments is a repeat, while a succession of new waits (A, B, C, ...) never is. A task that
keeps waking and waiting with real progress is never stopped by this guard, however many turns it
takes.

Not seen by the streak: an agent that writes a genuinely different comment (40+ characters of
new gist) on every turn, and work that leaves no trace in the worktree, a comment or the card
status (a task without a worktree has only the last two). The per-day counters
`daily_next_budget` (6), `daily_wait_budget` (8) and `daily_step_budget` (24) remain in the code and
the config as a backstop and a rollback; they count turns whether or not the task progresses, so a
fleet that relies on the streak sets them high.

### Caps without a clock, questions without a wait

Three switches in `[runner]` change what happens when work stalls on a cap or a question.
Everything here restarts on an event, never on a timer. Details: `docs/requeue-on-event.md`.

- `requeue_on_cap = false`: when true, a task that hit START CAP, TURN CAP, LOOP or COMPACTION CAP goes
  back to `todo` with the same assignee (no `start_after`, no backoff) and starts again only when
  its inputs change (a comment by someone else, a linked merge request's state, pipeline status or
  head commit, a closed dependency, a new assignee). A free peer of the agent's pool (`pool` in the
  agent block; without it, another agent with the same repos and the same `lead`) takes it at once
  for a fresh start. `RUNNER GAVE UP` still goes to the lead.
- `turn_cap_continue_on_progress = false`: when true, an attempt that reaches `max_turns` but kept
  making progress (at least `turn_cap_progress_min`, default 3, of its last 10 declared outcomes are
  `step` or `next`) ends as `turn cap: continued in a fresh session` and the same agent starts the
  task again in a new session instead of `RUNNER GAVE UP` to the lead. The restart counts as a start,
  so `daily_start_cap` (required, > 0) bounds the loop; with no recent progress, a spent start cap or
  `daily_start_cap = 0` the task goes to the lead as before. With the switch on the cap is applied
  when the attempt resumes, after the capped turn's own outcome is recorded (a `WAIT` waits first). Both paths log `turn cap: ...` and record
  a `turn_cap` attempt event.
- `unanswerable_ask_to_lead = true`: a `WAIT decision:@x` whose addressee cannot answer (not run by
  this runner, paused, itself, or a user listed in `unanswerable_users`) is put to the project lead.
- `soft_person_asks_to_lead = false`: when true, a `WAIT decision:@person` that no hard rule matches
  (`hard_ask_patterns`, `hard_ask_labels`: money, secrets and access, irreversible actions, publishing, legal, actions only the person can take)
  is put to the project lead instead of raising a human gate; the person can still override by
  commenting on the card.

`mesh_runner.cli asks classify --config runner.toml` lists the human gates open now with the
hard or soft verdict and the rule that decided it. It only reads.

## Mesh extension

`mesh-extension.json` declares the runner as an external plugin. With `[extension] enabled = true`
the runner serves the endpoints Mesh needs for the install handshake:

- `GET /healthz`
- `POST /mesh/install` — body `extension_id`, `version`, `workspace_id`, `signing_secret`, `events`,
  `events_path`. The secret is stored in `state_dir/extension-signing-secret.json` (mode 600) and never
  logged. A repeated install with the same secret is idempotent; a different secret is refused (409)
  until that file is removed or `allow_rekey = true`.
- `POST /mesh/events` — `task.assigned`, `task.updated`, `comment.created` wake the poll loop instead
  of waiting for `poll_seconds`; other events are acknowledged and ignored. The event type is read from
  the `X-Mesh-Event` header (an `event`/`type` field in the body is accepted as a fallback).

Every request must carry `X-Mesh-Timestamp` and `X-Mesh-Signature` (hex HMAC-SHA256 of
`"{timestamp}.{body}"`); a wrong signature or a timestamp more than `tolerance_seconds` (300) off is
refused with 401. The install signature is made with the secret that arrives in the same body, so it does not
authorise the installer by itself. Create the workspace secret `EXT_AGENT_RUNNER_SIGNING_SECRET` in
Mesh first (the handshake reuses an existing one) and give the same value to the runner with
`signing_secret_file` / `signing_secret_env`: an install with any other secret, or for another
`workspace_id`, is refused (403). `trust_first_install = true` drops the pre-shared secret and lets the
first install win; use it only on a private link nothing else can reach.

```toml
[extension]
enabled = true
workspace_id = "00000000-0000-0000-0000-000000000000"   # the workspace this runner serves
signing_secret_file = "~/.config/mesh-runner/extension-signing.secret"   # mode 600; same value as in Mesh
host = "127.0.0.1"
port = 8787
# authenticate to Mesh as the extension actor instead of basic auth + agent key
token_file = "~/.config/mesh-runner/extension.token"   # mode 600; or token_env = "NAME"
rotate_days = 30                                       # POST /ext/me/token/rotate; 0 = never
```

`runner_url` in the Mesh extension settings must reach `host:port`.

### Updating the runner

The version this host runs is the version installed in Mesh. A new release reaches the host in one
way only:

1. The release is published to the extension catalog (signed artifact, `mesh-extension.json`
   `version` = `mesh_runner.__version__` = `pyproject.toml` `version`).
2. An instance admin opens **Extensions → Updates** in Mesh and updates Agent Runner
   (`POST /api/v1/extensions/agent-runner/update`; new permissions ask for consent first). The
   Installed column now shows the new version.
3. The operator installs exactly that release on the host: download the artifact, check its
   sha256 against the catalog entry, unpack it next to the previous one and point the service at
   it (`PYTHONPATH=<release>/src`), then restart the runner.
4. Check: `python3 -c 'import mesh_runner; print(mesh_runner.__version__)'` under the service's
   `PYTHONPATH` equals the Installed version, and the runner's lease in Mesh reports it.

Never update the host from a git checkout or a hand-copied directory: Mesh would keep showing the
old version. Rollback is the reverse: **Updates → Roll back** in Mesh, then point the service at the
previous release directory and restart.

## Runtime catalog

With `[catalog] enabled = true` the runner is the catalog controller of the listed workspaces: it
reads its slice of the runtime catalog, checks that this host can execute it and reports what it
applied, under its own name (`mesh-runner`, the same name its agent leases carry) and version.
Off by default.

Every `interval_seconds` (120), per `[[catalog.workspace]]`:

- `GET /api/v1/workspaces/:ws/runtime/controllers/mesh-runner/desired` with `If-None-Match`; 304 keeps
  what is applied.
- The slice is checked against the host manifest (`manifest`): references resolve, every profile
  belongs to this controller and names an account, mode and harness the manifest knows with
  capabilities this host offers, and every account carries the manifest's own `credential_ref` and
  asks only for pools the manifest gives that mode. Credentials never travel in the slice.
- A slice that passes becomes `applied.json` (revision + digest; never an older revision than the
  one applied). A slice that fails is reported `rejected` for exactly its revision and digest, and
  `applied.json` stays as it was.
- `POST .../controllers/mesh-runner/report` (schema v2): the applied revision and digest, the
  controller's capabilities, `emergency_paused` (a file `emergency-pause` in `home`), pool states
  and profile models.

Pool states are read from files other host jobs write; the runner never probes a provider for them.
`observations` (`quota-observations/v1`: provider numbers per window) is mapped with `thresholds`
(default: block new launches at 90 %, transfer at 95 %, readmit below 80 % after 10 min).
`harness_limits` (`harness-limits/v1`) holds usage limits a lane's own session printed: while one is
in effect the account's pools are `exhausted`. No data, stale data or an error is `unknown`, never
0 %. Without `observations` every pool is reported `unknown`.

Each pass writes `home/state.json` (`status: ok|error`, `outcome`, `revision`, `last_report_at`, and
`version_mismatch` when the catalog's controller version differs from this host's) and
`home/pools.json`; `desired.json` and `applied.json` go to `catalog_dir`. All files are mode 600.

```toml
[catalog]
enabled = true
manifest = "~/runtime/accounts.json"
observations = "~/runtime/quota-observations.json"
harness_limits = "~/runtime/harness-limits.json"
thresholds = "~/runtime/thresholds.json"
# interval_seconds = 120

[[catalog.workspace]]
id = "00000000-0000-0000-0000-000000000000"
reporter_key_file = "~/.config/mesh-runner/reporter.key"   # mode 600: an agent with the reporter grant
# home = "~/.local/state/mesh-runner/catalog/<id>"
# catalog_dir = "<home>"
# manifest = "..."                                        # this workspace's own manifest
```

## Accounts

The publisher of this extension holds no accounts and receives none of yours. The runner uses, on
the operator's machine: a Mesh agent key for each agent it serves (or the extension actor token,
`token_file` / `token_env`); the operator's own Claude Code / Codex logins for the sessions it
starts; and, optionally, the operator's GitLab token for merge and pipeline checks. All of them are
read from files or environment variables the operator provides and are never logged.

## Network

The runner talks only to the Mesh instance in its config, the git remotes of the repositories the
operator lists (it fetches them) and, if configured, the operator's GitLab. The harnesses it launches (Claude Code, Codex) reach their own providers under their own
configuration. With `[extension] enabled = true` it listens on `host:port` (`runner_url`) for
`/healthz`, `/mesh/install` and `/mesh/events`, signed requests only.

## Telemetry

Nothing is sent to the publisher or to any third party. The runner writes local state only:
`runner.sqlite`, `status.json`, the event and turn logs under `state_dir`, and, if `metrics_file` is
set, a Prometheus textfile for the operator's own collector.

## License

Apache 2.0 — see [LICENSE](LICENSE).

### Held slots of finished attempts

A slot with uncommitted tracked edits is `held`, never wiped on release. A held slot is
not leased again, and a WAIT restores into a fresh lease, so such a slot only keeps the
pool smaller. About once an hour per slot (at most three per maintenance pass) the runner
frees a held slot when its attempt is finished (`completed`, `failed`, `aborted`) and its
task can no longer resume there: the task is done, cancelled or deleted, or is no longer
assigned to the agent, or the slot has been held longer than `[runner] dead_slot_hours`
(default `24`; `0` turns the age rule off) with no newer attempt of the agent on the task.
A task still assigned to the agent with an open (running, waiting, ready) attempt is never
touched.

Before the slot is freed its uncommitted state (tracked edits and untracked files that are
not ignored) is committed, on top of the slot's HEAD, to `refs/mesh-runner/archive/<attempt>-<slot>-<UTC stamp>`
in the base clone. The ref lives outside `refs/heads` and `refs/tags`, is never pushed by
the runner, and is recorded in the log and as a `slot-archived` event of the attempt. If the
work cannot be saved and verified the slot stays held and the reason is logged.

`mesh-runner free-slot --config runner.toml --path <slot> --archive` does the same for one
held slot on demand; without `--archive` an unsafe slot is still refused.

### Worktree disk admission

`[runner] min_free_gb = 0` disables the disk admission floor by default. Set a
positive value explicitly (GiB, e.g. `15`) after measuring the filesystem that
contains `state_dir/worktrees`. Below it, new task admission waits before Mesh
checkout; leasing also rechecks before fetch, free-slot reuse and worktree
creation. Ready attempts with existing leases also wait before reserving writer
authority; session preparation and process creation recheck the floor. Their
leases, checkpoints and WAIT context remain intact. Existing writers are not
stopped. This is a free-space floor, not a
reservation or a limit on growth by writers already running. No automatic
cleanup runs. WAIT checkpoints and held slots remain intact. On restart, an
interrupted checkpoint release (`verified`/`releasing`) may finish below the
floor after verifying its durable snapshot; this returns the source lease and
can free space. The journal reaches `released`, but checkpoint restore checks
the floor before acquiring a target or replaying files. A low-floor hold does
not admit a new writer, discard the checkpoint, or erase its WAIT context.
Release verification itself performs a temporary scratch replay with a separate
budget of snapshot bytes + twice object bytes + 512 MiB; the configured floor
guards target restore and new writers, rather than forbidding all writes.
Below the floor, resume finishes release only when every remaining leased repo
already has a `verified`/`releasing` journal entry; a partial inventory needing
a new snapshot is retained until sufficient space returns.

New WAIT captures also reserve `min_free_gb` (at least the existing 512 MiB
technical reserve) beyond their estimated snapshot/replay peak: twice the tree
bytes plus three times the shared Git object bytes. Free space is checked again
before scratch replay. A capture that cannot fit retains its source lease and
WAIT, without starting a harness; another smaller WAIT can still be captured.
Checkpoint capture and replay allocations are serialized across lane threads
inside one Runner process. External writers are not locked or reserved, so a
later loss of space fails the capture safely and removes its pending copy.
Restart recovery of already `verified`/`releasing` checkpoints keeps the technical
replay budget above: it may finish returning a lease below the configured floor,
but subsequent target restore/new work still requires the floor.

`mesh-runner pool --config runner.toml` reports `pool_size_mb`, slot counts,
`disk_free_gb` and durable `evicted_total`. The same `pool` object is added to
`status.json`. Size sampling uses a bounded `du` and is cached for 60 seconds;
`null` means the sample failed/timed out. Size includes checkpoint data under
the pool root, which eviction never touches.

Preview a conservative batch:

```
mesh-runner pool --config runner.toml --evict --idle-hours 6 --keep 2 --max-evictions 4
```

Only detached, registered, idle **free** managed slots with no tracked edits,
untracked files, ignored files/directories, index flags that can hide tracked
edits, or commits missing from local remote refs qualify. Index-flagged slots
are quarantined for operator inspection. Thus a warm slot containing `node_modules`, build outputs or
ignored evidence is preserved. There is no `git clean`, forced removal or cache
delete. The lowest numbered `--keep` slots of each pool are retained regardless
of state. Inspection is bounded (64 rows/10 seconds per pool, 30 seconds per
command); the result is a batch, not an exhaustive inventory.

For an approved batch, stop the service, ensure no other slot maintenance tool
is running, then repeat with `--apply`. Apply refuses while another Runner owns
its exclusive lock, reinspects and claims each free row as held before normal
`git worktree remove`. Removal failure or interruption leaves a durable held
row requiring explicit operator inspection; automatic held-slot recovery will
not free it after restart. There is no deletion fallback and no lease is stolen.
Restart normally afterward. Eviction is
irreversible checkout removal; it is intentionally explicit and conservative.

Roll out separately from the software update: inspect current disk and pool;
review the dry run; obtain capacity safely before enabling a floor above current
free space; apply a bounded approved batch with the runner stopped; remeasure;
then opt in to the measured floor and verify `plan`/`pool`/`status.json` and one
normal admission. To roll back the policy, set the floor back to `0`. Removed
clean worktrees are recreated on demand, while the base clone's refs remain.
This feature alone does not prove any host's pool is below a size target.

### Rearm one start after infrastructure recovery

Preview an explicit, scoped permission after repairing infrastructure:

```
mesh-runner rearm-cap --config runner.toml --agent my-agent --task <full-task-uuid> --reason "transport repaired and verified"
```

Add `--apply` to record the permission; `--dry-run` is the default. Only a current
start cap with no open attempt or declared WAIT qualifies. One permission may
be issued per exact cap record. The next attempt atomically consumes it and
records its ID, reason, time and attempt in durable audit history. Failed launch
also spends the permission: there is no retry loop or counter reset. Normal
authentication, human, dependency, WAIT and turn-budget admission checks still
run. Holds before attempt reservation leave it pending; later refusal, including
authentication at launch, spends it without bypassing the hold. A changed cap invalidates
it. This command never edits Mesh or clears other holds.
