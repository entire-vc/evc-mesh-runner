# Receipt WAIT (`receipt_wait_v1`)

One native, bounded event wait: the source card sleeps until the **first new provider-accepted,
correlated Telegram text receipt** appears in journals an operator registered. Off by default.

`receipt_wait_v1` is a retired `[runner]` option fixed at `false` (README "Retired options"): a
config file can no longer turn this wait on, and a file that still sets the key loads with a warning.

## Declaration

```
⏳ WAIT receipt:#<this-card-8hex>
```

Accepted only when `receipt_wait_v1 = true`, the card id matches the declaring card, and a live
undelivered registration exists for this card and this lane's Mesh agent id. Otherwise the turn is
sent back with guidance. No path, journal or timer appears in the comment; there is no `start_after`.

## Operator interface (writes through Runner's own store API)

```
mesh-runner receipt-wait arm --config C --agent <lane> --task <card-uuid> --owner-id <mesh-agent-id> \
    --journal journal-a=/absolute/path/a/telegram-receipts.jsonl --journal journal-b=/absolute/path/b/telegram-receipts.jsonl \
    --journal journal-c=/absolute/path/c/telegram-receipts.jsonl
mesh-runner receipt-wait inspect --config C --agent <lane> --task <card-uuid>
mesh-runner receipt-wait disarm  --config C --agent <lane> --task <card-uuid>
mesh-runner receipt-wait rebaseline --config C --agent <lane> --task <card-uuid> --name journal-a
```

* `arm` is idempotent (same card/owner/journals returns the stored registration and baseline).
  It records UTC `installed_at` and per-journal device/inode/EOF cursor before it prints anything.
  A journal that does not exist yet is a registered absent source. Journal files must be named
  `telegram-receipts.jsonl` and be absolute, regular, non-symlink.
* Arm **before** the card declares the wait. Runner scans armed registrations every wake check,
  waiting or not, so a receipt that lands between arming and the declaration stays pending and
  wakes the card on its first tick as a receipt wait.
* `inspect` prints metadata only: state, cursors, pending evidence ids, delivered flag.
* `disarm` stops observing; cursors, pending evidence and dedup marks are kept. `arm` again with
  the same arguments resumes the same registration. Once a receipt was delivered, a later `arm`
  starts a new generation with a fresh baseline.
* `rebaseline` is the explicit exit from a `blocked` journal (rotation, truncation, removal,
  unsafe file, over-long line): it continues from the journal's current last complete line.

## What counts

`kind=text`, `event=accepted`, integer `schema_version=1`, producer `owner-gate-alert` or
`owner-digest`, canonical full UUID `request_id`/`batch_id`, at least one canonical task UUID in
`task_ids` or `asks[].task_id`, non-empty bounded `bot_identity`, numeric `chat_id`, positive
integer `message_id` and `provider_ts` with `time_source=provider`, and `provider_ts >= installed_at`.
Missing/enqueued/suppressed/failed/unknown/legacy/document/malformed rows never wake. The receipt's
task UUID is the delivered ask; it need not equal the source card. A provider message
(`bot_identity`, `chat_id`, `message_id`) counts once whatever its `event_id`.

## Guarantees

* Cursor, dedup marks and pending evidence commit in one transaction; a crash re-reads the same
  complete lines. The cursor never passes a partial line. Reads are bounded (256 KiB, 16 KiB/line).
* The wake is `waiting -> ready` + wake text + one-shot `receipt_delivered` mark in one
  transaction, checked against the wait's registration generation and the owner. `ready` is the
  outbox: an admission refusal keeps it, a repeat tick adds nothing.
* Only allowlisted metadata is kept or shown, never a message body or document name.

## Rollback

`disarm` (or route to the previous catalog). Receipts, baseline
and dedup marks stay in the store; nothing is written to the journals.
