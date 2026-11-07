# Gmail WAIT (`gmail_watch_v1`)

One native, bounded event wait: a card sleeps until **one new inbound message** matches a predicate an
operator registered, then wakes exactly once. Off by default. It is a Runner transport, not an agent
turn: no `start_after`, no retry timer, no polling session, no body reading.

This is a **pull** observer (headers only, on Runner's own schedule). It is not PubSub/push and must
not be described as one.

## Declaration

```
⏳ WAIT gmail:gmr_<12hex>
```

Accepted only when `gmail_watch_v1 = true`, the id names a registration whose `observer_task` is the
declaring card and whose `resume_agent` is this lane, and the registration is `active` (or `fired` with
its wake not yet delivered, so a message that lands between registration and the declaration is kept).
A cancelled or foreign id is sent back with guidance. Time never completes it. `gmail` is excluded from
the time-wait guidance.

## Operator interface (writes through Runner's own store API)

```
mesh-runner watch gmail register --config C --mailbox <addr> --observer-task <card-uuid> \
    --resume-agent <mesh-agent-id> [--source-task <card-uuid>] \
    ( --from <addr> --thread <gmail-thread-id> \
    | --sender-domain <d> [--sender-domain <d>...] --marker "<text>" [--marker "<text>"...] ) \
    [--since <epoch-ms> | --baseline-from-now] [--dry-run]
mesh-runner watch gmail status --config C [<registration-id>] [--json]
mesh-runner watch gmail cancel --config C <registration-id> [--reason "<text>"]
```

* Register is idempotent on `(mailbox, predicate, observer_task)`; a repeat returns the stored id.
* The baseline is taken inside register: one provider read, then one SQLite transaction. It is the
  provider's own message time, never the local clock and never a mailbox/thread historyId.
* `--baseline-from-now` (default) seeds `gmail_seen` with the snapshot. `--since` seeds nothing: a
  matching message newer than `since` that already exists fires on the first tick (catch-up).
* `--dry-run` runs the predicate against one read and prints the candidate count; it writes nothing.
* `cancel` wakes the observer once with a terminal `cancelled` text; a later message never fires.

## Predicates

* **thread**: `mailbox + From address + thread id`.
* **search**: `mailbox + sender domains + markers`. Proves only that a matching message arrived from the
  domain, never that it answers anything; the consumer reads it itself.

The provider answer is a candidate source. The Runner evaluates the exact predicate on header metadata:
no `SENT` label, `internalDate` above the baseline, id not in `gmail_seen`, From parsed by an RFC 5322
parser (exactly one mailbox; the address, not the display name, is matched), domain equal to a wanted
domain or a dot-separated subdomain after lowercase + IDNA (`notvendor.example`, `vendor.example.evil.tld`
do not match). Markers are validated at register (1..80 characters, no control characters, quotes,
backslash, braces, brackets, parentheses, `:` or a leading `-`) and refused, never escaped. A label-only
change of an old message is not an event.

## Reader boundary

`gmail_watch_reader` is a fixed argv (never a shell string). Runner writes one JSON request on its stdin
and reads one JSON object from its stdout:

```
request : {"mailbox": "...", "after_s": <int>, "form": "thread", "thread": "<id>"}
          {"mailbox": "...", "after_s": <int>, "form": "search", "sender_domains": [...], "markers": [...]}
response: {"now_ms": <provider time, optional>, "records": [
           {"message_id": "...", "thread_id": "...", "internal_ms": <int>, "from": "<From header>", "labels": ["INBOX", ...]}]}
exit    : 0 ok, 3 auth, 4 rate, anything else transport; unreadable output is a parse failure
```

The wrapper must be read-only, build its own fixed provider argv from the structured request, fix the
mailbox, and return headers only (no subject, no body). It is the install-time producer; its exact transport
(a restricted `ssh` wrapper or a local command) is chosen at install and reviewed with it.

## Schedule, capture and failures

* Every `gmail_watch_interval_seconds` (default 120, floor 30) per active registration, with jitter, at most
  one read in flight per registration (the guard is empty after a restart).
* Each read asks for `after = high_water - 600 s`; the overlap absorbs provider time skew and late indexing,
  and `gmail_seen` makes it harmless.
* A tick is: read, pure `evaluate`, then ONE `BEGIN IMMEDIATE` transaction. Either it advances `high_water`
  and `last_ok_at` and records the evaluated ids, or it fires.
* Fire = CAS `active -> fired`, receipt row, and outbox rows (wake to the observer card; a comment to the
  source card when one is registered) committed together; no provider I/O inside. A lost CAS (cancel won)
  leaves nothing.
* Auth/transport/parse/rate failures change only the error counters and back off the **next tick**
  (30 s doubling, cap 30 min). Baseline, seen and state are untouched; nothing wakes and no turn starts.
  After 5 consecutive failures one `gmail_alert` event and log line per error class until recovery;
  `status` shows `error`.

## Delivery

Only from `gmail_outbox`, with an idempotency key (`gmail:<id>:<message_id>:wake|comment`).

* Wake: the Runner commits `waiting -> ready` + wake text + the outbox ack in one transaction, so an
  admission refusal or a crash keeps the row pending and a repeat adds nothing.
* Comment: the sender first looks for an existing comment on the target carrying `outbox-key: <key>`, posts
  only if absent, then acks. A kill between POST and ack therefore never duplicates it. A Mesh outage keeps
  the row pending for the next scan.
* Comment text: registration id, message id, thread id, From address, times, and for the search form the
  limitation above. No subject, body or marker.

## Install and rollback

Ships disabled. Install the signed Runner version, set `gmail_watch_v1 = true` and `gmail_watch_reader`
on the lane that resumes the observer, restart, `register --dry-run` (positive control), `register`, then
the card declares `⏳ WAIT gmail:<id>`.

Rollback: set `gmail_watch_v1 = false` (scanning and binding stop, registrations stay) or `cancel` each
registration; downgrading to the previous Runner leaves the four additive `gmail_*` tables unused.

## Schema (additive, created on first use)

`gmail_registration`, `gmail_seen`, `gmail_receipt` (one row max per registration), `gmail_outbox`.
