# Queue WAIT freshness v1

Default OFF. Enable `[runner] queue_wait_freshness_v1 = true` only together with
board-services `queue_wait_freshness_v1.enabled=true`, matching protocol versions,
and the same private state directory. This change alters no live configuration.
Runner owns `queue-wait-requests.json`; board-services alone writes
`queue-wait-receipts.json`. Both are regular effective-user-owned private files;
writers fsync a random 0600 temporary, replace atomically and fsync the directory.
Readers use O_NOFOLLOW, fstat and a 4 MiB bound. No provider payloads are stored.

A time WAIT is converted only on a `fleet-starvation-wake` card with an exact
canonical `[fleet queue-request:{...}]` plus matching machine
`[fiddler starvation-wake:<observed_at integer>]` source. A title or prose is
insufficient. Current assignee, explicit human_gate=false, labels, human/park
holds and checkout ownership must permit it. The newest exact typed source is
used; ambiguous or malformed input remains unknown. Existing parked machine
time WAITs can be adopted without executing a turn. Other timers keep their
existing behavior. Existing queue WAITs stay parked if this flag is disabled.

Each new WAIT gets a fresh UUID generation even for the same card/contract.
SQLite persists its owning WAIT and exact pending intent together before the
single Runner status writer exports it. Intent marks are keyed by generation,
bound to attempt, owner, task and exact request, and include a clock watermark.
Source checks use a durable 300-second attempt cadence, including failures/429
and restart; intervening ticks only read the local receipt and write no intent.
A due check also runs without a receipt, so changed demand is detected within
300 seconds. Every real ready transition requires a freshly reread source; a new
receipt may therefore wait up to 300 seconds before delivery. Source replacement
cancels the old intent and arms a new generation in one transaction, without waking the agent. Ownership loss cancels the pending intent.

No queue WAIT reaches generic timer, error-budget or age rechecks. Missing,
expired, corrupt or unavailable receipts and clock rollback retain pending demand.
A fresh receipt must exactly match the current generation, task, contract and
all proof pointers. The receipt revision must be an exact positive integer equal
to the durable SQLite outbox revision, and its digest must equal that complete
manifest's canonical SHA256. The exact pending request must belong to that
manifest. Missing, corrupt or superseded manifest evidence stays UNKNOWN;
Runner rereads the durable manifest inside the ready transaction too.
Times must satisfy `observed_at <= checked_at <= verified_at <= row watermark
<= receipt watermark <= now < expires_at`,
`expires_at == verified_at + 600`, and nondecreasing durable watermarks.
Board-services verifies completion after all bounded target reads, at most two
requests/four targets per 300 seconds, and rechecks healthy requests too.

The wake destination is the durable `waiting -> ready` transition. Its exact WAIT
CAS, wake text, met marker and complete intent share one SQLite transaction. The
proof is checked again immediately before commit, so expiry during a delayed read
or database write rolls everything back. Crash before the transaction retains
pending; restart revalidates proof. Crash after it sees ready+complete and cannot
redeliver this generation. Ordinary resume admission and checkout gates still
run before any harness starts. No readiness proof bypasses a human gate or lease.

## Revision and terminal GC

Manifest wire: `{version:1, revision:<positive durable counter>, requests:[<exact
requests>], terminal:[<complete/cancelled generation UUIDs>]}`. Each request is
`{version:1,generation_id,task_id,contract:{version:1,observed_at,targets:[{agent_id,
project_id,lane}]}}`. Changed bodies allocate a new persisted revision before
file publication; retry/restart republishes the same body and revision. Maximum
current requests and terminal batch are 128 each.

Observer keeps `manifest_revision` plus canonical JSON SHA256 `manifest_digest`.
Lower revisions and changed content at the same revision are rejected. It may GC
only a superseded identity explicitly acknowledged terminal by Runner, then
returns that batch in `terminal_ack`. Runner may accept an older terminal ack at
or below its durable revision, since terminal identities are immutable; concurrent
new demands must not starve GC. Terminal marks remain in SQLite for deduplication
but stop being exported after acknowledgement. Unconfirmed identities are never
pruned. Observer history capacity is 4096; exhaustion remains UNKNOWN until safe
terminal cleanup frees space. Neither durable store may be reset to work around
that safeguard.

The protocol predicate and I/O live in `queue_wait.py`; provider reads remain in
the existing board-services adapter. No additional scheduler or provider client
is introduced. The consumer has no direct Mesh queue reads.

## Operator recovery and rollback

Turning either flag OFF pauses this protocol; it does not restore ordinary
timer semantics for an existing queue WAIT. An older Runner binary is not a
recovery mechanism for these WAITs. Preserve the Runner database, manifest,
receipt and their revision/generation watermarks throughout any outage.

If the observer fails, retain the compatible Runner and pending intents. Check
the observer service, the configured private file paths/permissions and protocol
versions; repair the cause, then enable both flags and verify fresh receipts
for the current manifest generations. UNKNOWN or an expired receipt is not
permission to force a wake. Healthy observation resumes on the durable
300-second cadence; every ready transition still requires a current source and
unexpired proof. If an emergency flag-OFF pause was used, explicitly re-enable
both compatible components after repair; OFF alone leaves demand parked.

Before enabling, record the compatible source versions and private state paths.
Before a binary rollback, stop the existing owner through its normal service
procedure and inventory that exact Runner database read-only. With the service
stopped, no new adoption can race the inventory. For example, use the actual
configured database path, not an assumed home directory:

```sh
python3 - /absolute/configured/state/runner.sqlite <<'PY'
import json, pathlib, sqlite3, sys
path = pathlib.Path(sys.argv[1]).resolve(strict=True)
db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
db.execute('PRAGMA query_only=ON')
queries = {
    'queue_attempts': "SELECT count(*) FROM attempts WHERE state NOT IN ('completed','aborted') AND CASE WHEN json_valid(wait) THEN json_extract(wait,'$.kind')='queue' ELSE 0 END",
    'pending_intents': "SELECT count(*) FROM marks WHERE key LIKE 'queue_wait:%' AND CASE WHEN json_valid(value) THEN json_extract(value,'$.state')='pending' ELSE 0 END",
    'invalid_attempts': "SELECT count(*) FROM attempts WHERE state NOT IN ('completed','aborted') AND CASE WHEN json_valid(wait) THEN json_type(wait)!='object' ELSE 1 END",
    'invalid_intents': "SELECT count(*) FROM marks WHERE key LIKE 'queue_wait:%' AND CASE WHEN json_valid(value) THEN coalesce(json_extract(value,'$.state') NOT IN ('pending','complete','cancelled'),1) ELSE 1 END",
}
print(json.dumps({name: db.execute(sql).fetchone()[0] for name, sql in queries.items()}))
PY
```

All four counts must be zero before considering an older binary; a read/query
error is UNKNOWN and blocks that rollback. This is only the queue-protocol
precondition: other deployment/checkpoint rollback requirements still apply.
If any count is nonzero, restart the compatible owner instead. Do not edit the
database, clear an intent, remove machine labels, rewrite a WAIT, or reset a
watermark to make this check pass.

There is no protocol-only command to drain an unsatisfied demand. A compatible
owner drains it through fresh fulfilment, or cancels it when the actual source
task becomes terminal or genuinely leaves that owner. Do not manufacture either
condition solely for rollback. Keep both compatible components enabled while
draining and repeat the stopped-owner inventory afterward. If no legitimate
fulfilment/cancellation is available, retain the compatible binary and durable
state until the demand can be resolved; rollback remains blocked.
