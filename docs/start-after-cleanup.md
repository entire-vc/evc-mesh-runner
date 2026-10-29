# Taking back `start_after`

A time wait sets the task's `start_after` to its own time so that no other scheduler picks
the task up while its session is parked. An event wait (pipeline, mr, merge, card, decision,
blocking, queue) writes nothing: it follows its event with no timer (owner rule of 28.10, a
`start_after` is for a specific external event with a known timestamp). A rolling horizon that an
older runner wrote for an event wait is compare-cleared the next time the runner touches that wait
(renew, adopt or wake). When the wait ends (woken, resumed, or the attempt closes because the task
left the agent) the runner takes that schedule back. A schedule the runner did not set is
never cleared.

## Mesh's contract for an agent's `start_after`

- **Setting** (`PATCH /tasks/:id`, time waits only): a date from an agent needs `start_after_evidence`
  `{event_ref, source, why}` (each at most 500 characters; `why` at least 10; `source` an https URL or a
  Mesh receipt the agent did not write; `event_ref` must not read as a timer: `retry`, `poll`, `timer`,
  `later` ...). Without it Mesh answers 422 `missing_evidence`. The runner sends `event_ref` =
  `time-<task8>-<UTC moment>`, `source` = the first https URL in the agent's WAIT line (the external
  event's source), else the card URL (`<mesh>/api/v1/tasks/<id>`), and `why` = the WAIT line (padded with
  the reason when shorter than 10 characters). A 422 is logged once for the wait and not repeated; the wait
  stays local. With no cited URL and a Mesh URL that is not https nothing is written, one log line.
- **Clearing**: `PATCH ... {"start_after": null}` from an agent is a 422 `clear_requires_compare`. The
  runner calls `POST /tasks/:id/start-after/compare-clear` with the `start_after`, `version`,
  `start_after_origin` and `start_after_receipt_id` of the task it has just read, plus an
  `idempotency_key` (the same read repeats the same key) and a `reason`. 409 `cas_conflict`: the task is
  read again and the clear tried once more. A Mesh whose cards show no `start_after_origin` predates the
  contract and gets the plain null write.

## Whose schedule is it

`same_instant()` compares parsed instants cut to whole milliseconds, the precision Mesh
keeps. Mesh answers in UTC whatever was written (`Z`, `+00:00`, `+03:00`), so text is never
compared. A schedule one millisecond apart is someone else's.

`own_schedule()` also accepts `scheduled_maybe`: a schedule write whose answer was lost
(timeout or 5xx) may still have committed, so it is treated as ours until taken back.
Queue waits use it to tell the runner's horizon from an external start gate.

## When the clear fails

- HTTP 409 (`cas_conflict`: the card changed since it was read): re-read the task and retry once,
  only while the fresh read still shows our instant.
- HTTP 403 "set by a person": the date is a person's, an agent cannot clear it. It is left alone and
  not retried.
- Anything still failing (timeout, 5xx, 409 after retries) is stored as a pending
  `unschedule` mark and retried every tick with a pause that doubles up to 30 min. The
  mark is dropped once the clear lands, once the task carries another schedule, or once
  the instant has passed.
- A new schedule from the same lane drops any pending clear for that task.

## Orphans

A task held by a future `start_after` with no open attempt is checked at most every
10 min: if a closed attempt of any lane recorded that same instant, however long ago,
the schedule is an orphan and is cleared. This runs only on a real selection pass,
never from `plan`.
