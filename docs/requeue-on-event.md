# Caps and questions restart on events

No timer holds a task in these paths. A task that stalls goes back to the queue and
starts again when something new reaches it, or on another agent that is free.

## Caps (`requeue_on_cap`)

START CAP (`daily_start_cap` starts of one task by one agent per UTC day), TURN CAP
(`daily_next_budget`, `daily_wait_budget`, `daily_step_budget`), LOOP (`loop_streak_limit`
consecutive turns without progress; see the README) and COMPACTION CAP
(`max_compactions`) no longer hand the task to the lead or keep it until tomorrow:

1. The task moves to the project's `todo` status, the assignee stays, `start_after` is cleared
   (compare-clear; one that will not clear, a person's date or a card that keeps changing, is retried per
   tick or left in place, and never sends the task up) and no backoff is set. The attempt note is `cap: requeued (<start cap|turn budget|loop|compaction cap>)`
   (a START CAP has no attempt, so the runner writes a closed row of its own for the audit) and
   an event `cap` goes to `events.jsonl`. One comment per cap event says where the task is and
   what restarts it.
2. The runner stores the task's **input fingerprint** at that moment (`inputs.py`):
   - the id of the newest comment by anyone but the agent, a `[runner]` note or Mesh itself;
   - for each VCS link of the task: URL, state, pipeline status and, when Mesh records one, the
     head commit (`head_sha`/`sha`/`commit_sha`, on the link or in its metadata). These come from
     Mesh's webhook-fed fields: the runner adds no GitLab polling, and a task without a link
     simply has none;
   - the open `blocks` dependencies;
   - the assignee.
3. While the fingerprint is unchanged the task is held (`waiting for new input after <kind>`, shown
   on the board as a waiting state), holds no slot, and sorts after ready work in the agent's queue.
   When it changes the task starts normally, the stored fingerprint is dropped, and the daily start
   cap is not applied to that one start. The same fingerprint never starts twice. A fingerprint that
   cannot be read counts as unchanged. Because the day's counters still stand, a task that is
   capped again after its new start waits for the next change.
4. **Peers.** If another agent of the same pool is configured, not paused (`paused = true` or
   `<state_dir>/pause/<Agent>`), serves the task's project and policy, has a free slot, has not hit
   the cap on this task itself and is not itself waiting for input on it, the task is assigned to it
   at once ("fresh look"). A task a cap moved to an agent is not moved on again the same UTC day.
   *Pool* is, in order: the same non-empty `pool` name in the agent block; or, when neither agent
   sets one, the same set of `[[agents.repos]]` names and the same `lead` (same product, same
   team). A pinned task refuses the reassignment and stays.
5. `RUNNER GAVE UP` (harness errors, turn limit) is mechanics, not work, and still goes to the lead.
   Turns without a readable outcome use the same wait: the second in a row puts the task back in todo
   with the same lane (`🔁 NO OUTCOME`, no peer reassignment) until its inputs change; the
   `no_outcome_route_after`-th in a row (default 3, reset by any readable outcome) goes to the lead.

With the flag off the code path is the previous one.

## Questions to agents (`unanswerable_ask_to_lead`, default on)

`WAIT decision:@agent` files a decision request, a task labelled `decision-request` assigned to the
addressee. The addressee's queue puts such requests ahead of ordinary tasks, whatever their priority.
If the addressee cannot answer (not resolvable by this runner, not run by it, paused by config or by
the pause file, the asker itself, or a user listed in `unanswerable_users` or flagged `is_test`,
`is_service`, `is_bot`, `test_user` on its roster row), the request goes to the project lead
(agent `lead`, that lead's lead, then `[project_leads]`) with a comment naming whom and why. The
asking task waits for a comment as before. Without any lead the older behaviour stands.

## Questions to people (`soft_person_asks_to_lead`, default off)

Where the runner turns `WAIT decision:@person` into a `❓ Blocking` ask it first classifies the
question (`asks.py`). **Hard** questions stay a human gate: a keyword in the question or a task label
from `hard_ask_patterns` / `hard_ask_labels` (money, secrets and access, irreversible or production
data actions, external publishing, legal, and actions only the person can take: logins, clicks, a physical device; English and Russian); no question text or unreadable labels
count as hard. **Soft** ones are put to the project lead as a decision request without raising a
gate, with the comment "Decided by lead, owner may override by commenting here". A comment by the
person wakes the task like any foreign comment. A `❓ Blocking` line the agent wrote itself is not
touched: Mesh has already raised that gate.

## Review before switching on

    python -m mesh_runner.cli asks classify --config runner.toml [--json]

lists the open human gates of the configured agents' tasks, the hard/soft verdict and the matched
rule for each. It makes GET requests only.
