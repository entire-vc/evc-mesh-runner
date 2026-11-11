# Lifecycle WAIT

One native, bounded event wait: the source card sleeps until the **first new, correlated
SessionStart lifecycle event** (native `startup` or `compact`) of one lane profile appears in the
lifecycle journal an operator registered. Always available; nothing happens without an
operator-armed registration. Not a second scheduler: state lives in
Runner's store marks of the source card, journal machinery is shared with `receipt_wait`.

## Declaration

```
⏳ WAIT lifecycle:#<this-card-8hex>
```

Accepted only when the card id matches the declaring card and a live
undelivered registration exists for this card and this lane's Mesh agent id. Otherwise the turn is
sent back with guidance. No timer, no `start_after`.

## Operator interface (through Runner's own store API)

```
mesh-runner lifecycle-wait arm --config C --agent <lane> --task <card-uuid> --owner-id <mesh-agent-id> \
    --profile khan --manifest /var/tmp/example-lifecycle/manifest.json \
    --codex-home /var/tmp/example-codex --journal /var/tmp/example-lifecycle/khan-lifecycle.jsonl
mesh-runner lifecycle-wait inspect|disarm|rebaseline --config C --agent <lane> --task <card-uuid>
```

`arm` is idempotent, records UTC `installed_at` (the post-install floor), the journal EOF cursor
(a row partial at arm time is history), the manifest sha256[:16], `config_path`, `hook_key`,
`codex_home` and the ACK root (the manifest's directory). The manifest must belong to the profile
(`khan.config.toml`; this observer accepts only `--profile khan`), and its `codex_home` is required and must
be `--codex-home` (the config lives in it). Arm before the card declares the wait: an event in between stays pending.
`disarm` keeps cursors/pending/dedup; a delivered registration is one-shot, a later `arm` starts a
new generation with a fresh baseline. `rebaseline` exits a `blocked` journal (rotation, truncation,
removal, unsafe file, over-long line).

## Journal row (schema_version 1, one JSON object per line)

```
{"kind":"lifecycle","schema_version":1,"producer":"khan-session-start-hook","hook_event":"SessionStart",
 "source":"startup|resume|clear|compact","result":"ok|failed","session_id":"<uuid>","epoch":"<uuid, ok only>","ts":<unix s>}
```

Only `startup`, `resume` (continuation) and `compact` rows with `ts >= installed_at` count. `clear` never wakes.

## Correlation and classes

* `hook_failure`: a valid `result=failed` row whose session has its rollout file (a failing hook never
  writes the ACK, so the ACK is not required for it). Wakes with text that says FAILURE.
* `candidate`: `result=ok` AND the helper ACK `<pid>-<session_id>.ack` in the ACK root has the same
  `session_id` and `epoch` AND a rollout file `rollout-*-<session_id>.jsonl` exists under
  `<codex-home>/sessions/*/*/*/`. Missing any of the three is uncorrelated: no wake. A valid row whose artifacts have not appeared yet is
  held (max 16 rows, 600 s after its own `ts`) and re-checked on every scan, so a late ACK/rollout still
  wakes once; after the TTL it is dropped.
* Correlation is bounded and fails closed: the rollout is looked up in the newest 64 day directories of
  `<codex-home>/sessions` and the ACK in the ACK root, within a shared budget of 20000 directory entries;
  beyond it the row is not correlated (no wake) rather than the scan growing with the tree.
* A candidate is evidence for the operator's AC5 acceptance, never the acceptance itself: the wake text
  says so. Nothing here waives a missing field.
* The ACK only proves the helper's reset; it carries no hook stdout. The journal's `result` is the
  producer's statement about stdout/exit.

## Producer contract

The observer consumes the registered `khan-lifecycle.jsonl` journal. The reserved
profile `khan` and producer `khan-session-start-hook` are literal protocol values
in this version; arbitrary profile names are not supported. Paths above are
synthetic examples and must be replaced with operator-owned locations.

An external SessionStart hook must append the metadata-only row above: `ok`
after the helper ACK, or `failed` on a hook failure. Producer installation and
its operational review are separate from this observer. A registration alone
does not prove that a producer is installed or that any event has been emitted.

## Guarantees

* Cursor, dedup mark and pending evidence commit in one transaction; a crash re-reads the same complete
  lines. Reads are bounded (256 KiB, 16 KiB/line); the cursor never passes a partial line.
* Wake = `waiting -> ready` + wake text + one-shot `lifecycle_delivered` mark in one transaction,
  checked against the registration generation and the owner. `ready` is the outbox.
* Metadata only: journal fields, ACK file names/`session_id`/`epoch`, rollout file existence. No dialog
  body, no secret, no hook stdout.

## Rollback

`disarm`. Nothing is written to the journal, ACKs or rollouts.
