
# Stall WAIT

One native, durable wait for the FIRST new result of the mesh-stall-control notifier on the waiting
card. The source card sleeps until the notifier posts its result comment; nothing else wakes it.
Always available; nothing happens without an operator-armed registration. No timer, no polling turn, no self-dependency, no relay card, no Telegram receipt.
State lives in Runner's store marks of the source card (`stall_reg`, `stall_pending`,
`stall_delivered`, `stall_seen:*`).

## Declaration

```
⏳ WAIT stall:#<this-card-8hex>
```

Accepted only when the id is the declaring card and a live undelivered
registration exists for this card and this lane's Mesh agent id. Otherwise the turn is sent back with
guidance. `stall:soon` or another target stays unparsed.

## Operator interface

```
mesh-runner stall-wait arm      --config C --agent <lane> --task <source-card-uuid> --owner-id <lane-mesh-agent-id>
mesh-runner stall-wait inspect  --config C --agent <lane> --task <source-card-uuid>
mesh-runner stall-wait disarm   --config C --agent <lane> --task <source-card-uuid>
```

`arm` reads the card's CURRENT comments with the lane's own Mesh key and records every notifier event
UUID already there as the baseline, plus the arm time (sub-second precision) as a floor. More than 4096 existing notifier events refuse the arm rather than truncating history. If that read fails the arm is
refused (fail closed). It is idempotent for the same owner, refuses another owner while live, and starts
a new generation after delivery. `inspect` prints metadata only (baseline, pending event, delivered).

## What counts as the event

A comment on the source card that is ALL of:

- authored by the registered owner id (own author; another author or another card never counts),
- created at or after the arm floor,
- shaped like the reviewed notifier output: first line `[mesh-stall-control:<uuid>]`, exactly one
  fenced `json` block holding an object, last line `↻ NEXT`,
- carrying an event UUID not in the baseline and not seen before (a replay of a UUID never wakes).

Delivery is one transaction: `waiting -> ready` + wake text + `stall_delivered`. Restart, retry, a
failed commit or a repeated scan cannot produce a second wake. A Mesh read error, a corrupt
registration, a disarmed or other-generation registration wake nothing.

A `stall` wait is dispatched to its own predicate before the generic path, so the agent's own
`↻ NEXT` and foreign comments never supersede it. If the notifier's own `↻ NEXT` comment is itself
the newest declaration of the turn (it lands before Runner settles the turn), the session continues
at once and the evidence is marked delivered, so re-declaring the wait cannot wake on the same event.

Evidence only: it says the bounded control posted a result, never that the stall is repaired.
