---
status: review
type: runbook
---

# Recovery WAIT

One native, durable wait for a shared recovery card that does not exist yet. The consumer card sleeps
until that card has been created by its producer, bound, finished as `done`, and carries an explicit
receipt comment. Always available; nothing happens without an operator-armed registration. No future UUID is guessed, no timer, no relay card; state lives in
Runner's store marks of the consumer card (`recovery_reg`, `recovery_bound`, `recovery_pending`,
`recovery_delivered`).

## Declaration

```
⏳ WAIT recovery:#<this-card-8hex>
```

Accepted only when the id is the declaring card and a live undelivered
registration exists for this card and this lane's Mesh agent id. Otherwise the turn is sent back with
guidance. `recovery:#future-systemic` or any other target stays unparsed.

## Operator interface

```
mesh-runner recovery-wait arm --config C --agent <lane> --task <consumer-card-uuid> --owner-id <lane-mesh-agent-id> \
    --project-id <uuid> --marker '[secret-rotation·systemic]' \
    --recovery-owner-id <uuid of the agent the shared card is assigned to> \
    --receipt-author-id <uuid of the root owner whose receipt completes it>
mesh-runner recovery-wait inspect|disarm --config C --agent <lane> --task <consumer-card-uuid>
```

```
mesh-runner recovery-wait supersede --config C --agent <lane> --task <consumer-card-uuid> \
    --reg-id <generation from inspect> --reason '<one line>'
```

`supersede` replaces a wrong recovery gate for exactly one generation (`--reg-id` is the CAS; a stale,
delivered or already superseded one is refused). It is authenticated by Mesh, not by the operator's
word: the root owner (the registered receipt author) or an id in `recovery_supersede_authors` must have
written a line `supersede-recovery: <reg_id>` as a comment on the source card; its author id is recorded. The registration is disarmed and the attempt that
declared that generation resumes once on its own session (a re-arm before the wake does not strand it); the wake text says it is a decision about the
next action, not recovery evidence. It never satisfies the gate: the security activation gate stays
closed until a real receipt. `disarm` alone, a `↻ NEXT` or any comment does not resume the attempt.
A later `arm` is a new generation.

`arm` is idempotent; a different contract for a live registration needs `disarm` first; a delivered
registration is one-shot and a later `arm` starts a new generation.

## Semantics

* Candidate = card in the registered project whose title contains the marker (a mention in the description
  does not count: such a card is about the producer, not its result) and whose
  assignee is the registered recovery owner (the card itself never matches). Unrelated cards never bind.
* Create before arm and arm before create are both reconciled: every tick (throttled to
  `wake_poll_seconds`) Runner reads the actual cards, waiting or not.
* The earliest candidate binds (by `created_at`). A cancelled bound card is rejected and the next real
  candidate may bind. Failed/cancelled/not-done targets and a missing receipt give nothing.
* Completion = bound card `done` + a comment by the receipt author with a line
  `carrier-receipt: <ref>`. Wake = ready + wake text + delivered mark in one Store transaction, so a
  duplicate tick, retry or restart never delivers twice; stale generation or owner never wakes.
* The wake is EVIDENCE only, not a security clearance. Secrets are never read; only ids, statuses and
  the short receipt reference are kept.
