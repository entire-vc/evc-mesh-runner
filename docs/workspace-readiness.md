# Owner-native workspace readiness v1

This is an observation protocol inside the existing owner `AgentRunner.tick`,
disabled by default (`[runner] workspace_readiness_v1 = true` is separately
admitted configuration). It does not install anything, restore a checkpoint,
reserve a pool slot, alter a lease, wake a task, or grant operational authority.
The consumer never opens Store, SQLite, another lane's pool, or checkpoint files.

## Private request and addressed delivery

`mesh-runner workspace-readiness submit --config <actual-config> --agent <owner>
--request <private-json>` submits one immutable request. The file must be owned
by the current uid, regular, nonsymlink and mode 0600. The CLI uses only private
files and the exact configured owner's existing signing key. Keyless lanes
refuse submission. It never starts a controller or changes configuration.

Unsigned JSON has exactly these fields (the CLI adds the HMAC-SHA256 signature):

```json
{
  "version": 1,
  "request_id": "<new canonical UUID>",
  "agent": "<exact configured owner>",
  "task": "<full task UUID>",
  "attempt": "<full native attempt UUID>",
  "session": "<exact native harness session UUID>",
  "generation": "<64 lowercase hex from own existing checkpoint inventory>",
  "created": 0,
  "expires": 60,
  "previous": "",
  "paths": [{
    "repo": "<configured repository name>",
    "source": "<absolute original owner pool path>",
    "checkpoint": "<32 lowercase hex own checkpoint ID>",
    "manifest_sha256": "<64 lowercase hex raw manifest pin>"
  }]
}
```

Use actual epoch seconds: expiry must be later than creation and at most 60
seconds later. Limits are 8 exact unique paths, 32 KiB per transport object,
4 MiB per manifest, and 64 directory entries (request plus receipt count).
Admission refuses a full directory; it never deletes durable evidence.
The input inventory/pins come from the owner's existing checkpoint contract,
not consumer SQL or filesystem discovery. No foreign identity is an input.

One existing owner tick consumes at most one request. It checks fresh config,
HMAC, Mesh identity, assignment/status, saved checkout attempt and checkout
epoch before and after the first native snapshot. A held checkout must belong
to this owner; a released checkout must retain this exact attempt binding.
Two native captures must agree. Native pool/checkpoint locks and a bounded
SQLite transaction fence each capture; HTTP calls hold neither lock. Missing
schema, unavailable authority, symlinks, busy state, drift, expired or mismatched
input fail closed. The observer does not call schema initializers, checkpoint
recovery, verification replay, pool maintenance, or leasing code.

The result is atomically and durably published as an immutable addressed file
under `state_dir/workspace-readiness/<sha256(owner)>/<request-id>.receipt.json`.
`mesh-runner workspace-readiness receipt --config <actual-config> --agent <owner>
--request-id <UUID>` retrieves that exact receipt once; `null` means it has not
been delivered. Duplicate submission returns the existing result, never a fresh
observation. A reused request ID with changed content is rejected. There is no
poll loop, timer, background client, automatic retry, or wake side effect. An
already scheduled consumer continuation may read its addressed result; a
separate live integration must supply its admitted event-driven continuation.
This code does not claim that integration is installed or accepted.

## Receipt and freshness

`READY` requires the complete requested inventory to match the own native
attempt and journal, all manifest hashes and session/harness/cwd/inventory
bindings to match, and each source to be either native `free` with a fully
`released` journal or native `exact-own` with an `active`/`restored` journal whose
target is that same path. A conflicting or unknown occupancy never yields
`READY`. A partial journal cannot yield a partial positive. Foreign holder IDs,
raw DB rows, secret configuration and manifest content are never exported.

The receipt includes observation UUID/time/expiry/generation, request digest,
actual process instance/PID/start/executable/package version/source digest and
config digest, current owner/checkout epoch, and exact own task/attempt/session,
harness/cwd/inventory plus per-path occupancy and checkpoint continuity.
`NOT_READY` is a coherent negative observation. `UNKNOWN` is a refused or
unavailable observation; its bounded reason contains no external error text.
`operational_authority` is always false.

A receipt is historical evidence, including after restart. Before any subsequent
action, submit a **new** request with `previous` set to the prior request UUID and
all current exact pins/identity/generation. The owner repeats current authority
and native checks; only an unexpired prior `READY` with an identical observation
generation revalidates as `READY`. Restart, config/source/authority/checkout or
native drift yields `STALE` or `UNKNOWN`. A caller must require this current
revalidation and the unchanged exact request/controller/snapshot bindings, and
must still use the normal atomic acquisition and same-path enforcement at the
point of use. Revalidation does not reserve anything or authorize a future use.
Never treat file presence, a historical `READY`, or aggregate health as ownership.

