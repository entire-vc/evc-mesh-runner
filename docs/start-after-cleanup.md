# Taking back `start_after`

A waiting attempt sets the task's `start_after` so that no other scheduler picks the task
up while its session is parked: a time wait uses its own time, an event wait a rolling
24 h horizon. When the wait ends (woken, resumed, or the attempt closes because the task
left the agent) the runner takes that schedule back. A schedule the runner did not set is
never cleared.

## Whose schedule is it

`same_instant()` compares parsed instants cut to whole milliseconds, the precision Mesh
keeps. Mesh answers in UTC whatever was written (`Z`, `+00:00`, `+03:00`), so text is never
compared. A schedule one millisecond apart is someone else's.

`own_schedule()` also accepts `scheduled_maybe`: a schedule write whose answer was lost
(timeout or 5xx) may still have committed, so it is treated as ours until taken back.
Queue waits use it to tell the runner's horizon from an external start gate.

## When the clear fails

- HTTP 409 (concurrent change): re-read the task and retry, at most 3 writes, only while
  the fresh read still shows our instant.
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
