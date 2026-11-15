# Checkout fence

A lane works a card under a Mesh checkout (a lock: token, session, generation). Mesh accepts that lock
as an optional precondition on the writes that publish a result (`POST /tasks/{id}/comments`,
`/artifacts`, `/finish`, `/move`):

| Header | Value |
|---|---|
| `X-Mesh-Checkout-Token` | the checkout token |
| `X-Mesh-Checkout-Session-Id` + `X-Mesh-Checkout-Generation` | instead of the token |
| `X-Mesh-Expected-Input-Revision` | the task's `input_revision` the writer worked from |

With it the write commits only while that exact lock is live (and, with the revision, while title,
description and custom fields are unchanged); otherwise Mesh answers `409` and writes nothing:
`checkout_precondition_failed` (the lock is gone, or the card was given to someone else:
`context.reassigned`) or `input_revision_changed` (`context.input_revision` is the current one). Without
the headers the routes behave as before.

## How the lock reaches the agent

The agent writes through its own Mesh MCP process, which its harness starts from the agent's profile;
the Runner does not configure it. The lock reaches it through a file, one per card the lane holds:

```
~/.mesh-runner/checkout-fence/<agent_id>/<task_id>.json            Runner writes, MCP reads
~/.mesh-runner/checkout-fence/<agent_id>/<task_id>.refusals.jsonl  MCP appends, Runner reads
```

`MESH_CHECKOUT_FENCE_DIR` overrides the directory for both sides (tests, a host without that home).

- The Runner writes the task's file (mode 0600, by rename) at the start of every turn, before the prompt
  can reach a session: `attempt_id`, `turn`, `checkout_token`, `session_id`, `generation` and the
  `input_revision` read from the task at that moment. A resumed session therefore writes with the lock
  of the attempt that resumed it, never an earlier one.
- The file could not be written (the fence is on): the turn does not start, so it never runs unfenced.
  The lock is handed back and the start is held and retried like a disk hold (`fence_publish_failed`).
- The MCP reads the file again on every comment, artifact, finish and move of that card (no copy is
  kept) and sends the headers; `X-Mesh-Expected-Input-Revision` only on finish. No file: no headers (a
  card the turn does not hold is written as before).
- On a `409` with one of the two codes the MCP appends a line to the refusal journal: `attempt_id` and
  `turn` copied from the file, `code`, `at`, `tool`, `reassigned`, `input_revision`.
- The Runner removes the file when the attempt lets the lock go (release, a turn command that released
  it in Mesh, a lost lock) and, once per start, every file whose attempt no longer holds its lock.

The Runner's own notes on a card it holds (banners, operator notes, its moves) carry the lock too, never
the revision.

## A lost lock stops the turn

The turn is stopped, the attempt ends `failed` with `checkout lost: <reason>` in its note, and nothing
more is written under it (no completion comment, no runtime-state clear, no decision close: the card may
be another holder's now), when:

- the MCP journals `checkout_precondition_failed` for this turn (read every tick), or Mesh refuses one
  of the Runner's own fenced writes that way;
- Mesh refuses the heartbeat of a card that is not closed.

A refusal of one of the Runner's own writes while it settles the turn (the session has exited) ends the
attempt at once and stops the settlement there: no later write of that settlement is sent.

Not a lost lock: the turn's own command (`wait_for`, `ask_*`, `finish_task`, `hand_off`) released it in
Mesh while a child of the session still runs. When this agent's releasing turn outcome is newer than the
turn start, the Runner drops the token and the file and lets settle read the outcome
(`checkout_released_by_turn`). Mesh unreadable: the token is kept and asked about again next beat.

The reason is read from the card as Mesh shows it: `reassigned`, `self-checkout` (the card was checked out
again under the agent's own identity: same identity, new lock, not the Runner's), `taken over` (another
holder), `released or expired`. A card that is closed keeps its existing path (two observations, then the
turn is killed and the attempt completes).

## A task that changed under the turn

A finish refused with `input_revision_changed` tells the agent that the task changed and to read it
again (`get_task`) before closing. If the turn still ends without an outcome, the Runner does not count it
as a turn without an outcome: the next turn starts with that instruction and a fresh `input_revision`.

## Rollback

`[runner] checkout_fence = false`: no file is written (so the MCP sends no headers), the Runner sends
none, and a refused heartbeat only drops the token as before. No release is needed; restart the Runner.
