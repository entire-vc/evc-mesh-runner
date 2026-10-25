# Turn progress watchdog

A turn counts as running while its process lives, and is cut at `max_turn_minutes`
(default 180). Inside that window a turn can be stuck without anything saying so: the
interactive session waits on a dialog nobody answers, the harness never started
replying, the end-of-turn hook was lost. The card stays checked out and the worktree
slot stays held for up to three hours.

The watchdog ends such a turn early. It is **off by default**: all three limits are 0.

## What a turn reports

An interactive turn's log is written when the turn ends (the driver converts the
session transcript then), so an empty or old log says nothing about a live
interactive turn. The driver (`tui_driver.py`) instead writes
`<state_dir>/interactive/<agent>/<attempt>.progress.json` at least every 5 seconds:

| field | meaning |
|-------|---------|
| `pid` | the driver process; the runner ignores a file written by another turn's driver |
| `phase` | `launcher` (driver runs, harness not ready), `ready` (harness takes input), `sent` (prompt accepted, no reply yet), `output` (first reply written), `done` |
| `progress_at` | when the phase last changed or the harness's own file (Claude transcript, Codex rollout) last grew |
| `external_wait` | the screen shows a running tool, a background task or a spinner: the silence is the agent waiting on something it started |
| `updated` | last write; a driver silent for 10 minutes is itself stuck |

A headless turn has no driver: its log is only a progress clock (modification time = progress).
Its first records are written at startup, before the model answers, so `turn_first_output_minutes`
does not apply to headless turns; `turn_idle_minutes` does.

## Limits (`[runner]`, minutes, 0 = off)

| key | default | ends the turn when |
|-----|---------|--------------------|
| `turn_first_output_minutes` | 0 | the harness has not replied this long after the turn started (phases `launcher`, `ready`, `sent`). Keep it above start-up (about 2 minutes) and above `busy_wait` (30 minutes) if a previous cut-off turn may still occupy the session |
| `turn_idle_minutes` | 0 | no progress this long while nothing is running on the screen (a dialog, a dead prompt, a lost hook). For a headless turn this is the quiet time of the log: set it above the longest silent tool call |
| `turn_external_wait_minutes` | 0 | no progress this long while the harness runs a tool or background task. 0 leaves such a turn to `max_turn_minutes` |

## What happens

1. The runner records a `watchdog` event with the reason and phase (and a `kill` event
   `turn watchdog: ...`), and logs a warning.
2. The turn's process group is terminated, as for `max_turn_minutes`. The driver
   interrupts the session and exits.
3. Only after the driver is gone, an interactive attempt's window is closed, so a stuck
   session cannot write beside the one the next turn resumes (a new window, same session id).
4. Nothing else is touched: the card stays checked out, the worktree and everything
   saved in it stay, and no card or comment is written. The next tick settles the
   cut-off turn through the usual paths: a declaration the agent already wrote is
   honoured, otherwise the task is resumed with the no-outcome prompt, and a second
   such turn is handed to the lead.

A driver of a runner that predates this feature reports nothing; its turns are not
judged (only `max_turn_minutes` applies) until they end.
