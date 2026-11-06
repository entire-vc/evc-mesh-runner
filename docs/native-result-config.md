# Native-result path adoption

This is a source interface, not permission to install, enroll, initialize, arm,
rebind or watch a native channel. A loaded pair receipt is neither registration
nor consumption ACK. Native paths and operator control remain off by default.

The existing Runner owns the operation, on its existing extension listener:

```
mesh-runner native-result-config --config runner.toml --action status --request-file status.json
mesh-runner native-result-config --config runner.toml --action adopt --request-file adopt.json
mesh-runner native-result-config --config runner.toml --action rollback --request-file rollback.json
```

All three are signed loopback POSTs. Status body is `{}`. No redirect or proxy
is followed. Requests and authority files must be private owned regular files;
file mode and task ownership alone grant no authority. The CLI never edits TOML,
fills in a guessed receiver, emits a key or manufactures a loaded receipt.

## Independently admitted startup authority

An operator provisions a **separate** ASCII HMAC key of at least 32 bytes and a
reviewed policy. Neither Mesh install secrets, CI JOB-TOKEN nor editor lifecycle
keys are native operator credentials. No key is minted by this command.

The initially loaded config, admitted before this process starts, pins:

```toml
[native_result_adoption]
policy = "/private/operator/native-policy.json"
policy_sha256 = "<exact SHA256 of policy bytes>"
revocations = "/private/operator/native-revocations.json"
```

The policy has exactly these fields:

```json
{
  "version": 1,
  "purpose": "native-result-config-v1",
  "operator": "<canonical operator UUID>",
  "agent": "<configured lane name>",
  "receiver": {"task": "<UUID>", "attempt": "<UUID>", "session": "<UUID>", "generation": 1},
  "paths": ["/private/destination/new-journal", "/private/destination/new-channels"],
  "key_file": "/private/operator/native.key",
  "key_sha256": "<SHA256 of stripped key bytes>",
  "not_before": 0,
  "expires": 1,
  "min_revision": 1
}
```

Times are actual Unix seconds chosen by the operator, with a finite reviewable
validity window; the sample is deliberately expired. Both paths must be fresh,
canonical absolute non-overlapping destinations with private owned parents.
Existing directories, even empty ones, symlinks and unknown history refuse.
They cannot contain the config, control state or authority files. This does
not claim that unrelated dormant channels elsewhere on the machine are absent.

Revocations contain `version:1`, the exact `policy_sha256`, integer `revision`,
integer `expires` no later than policy expiry, boolean `revoked`, and `signature`.
The signature is hex HMAC-SHA256 of canonical JSON of those fields excluding
`signature`, using the distinct native key. Canonical JSON sorts keys and uses
ASCII with compact comma/colon separators (`native_result_ledger.canonical`).
Missing, expired, corrupt or unauthenticated documents refuse. Runner persists
the largest trusted revision and a revocation latch; replaying an older signed
document, including after a process epoch change, cannot clear a revocation.
Authority changes need a separately admitted new startup policy pin. Request
signatures are checked before durable control-state writes. Read-only status
writes only when a trusted revision or revocation latch changes (or on first
admission), rather than rewriting unchanged history on every request.

The startup config, process and private state directory are trusted. A fully
compromised OS account can replace process memory and authority/state files;
this interface does not claim to defend against that account. Deleting or
restoring control state is unsupported, never a rollback recipe.

## Exact request and receipt

An adopt/rollback request has exactly `request_id` (one-use UUID),
`policy_sha256`, `authority_revision`, `receiver`, `expected`,
`candidate_config_sha256`, `paths`, `rollback_of`.

`expected` copies `pid`, `started_ns`, random per-process `epoch`, `generation`,
`loaded_digest` and `startup_config_sha256` from the authenticated controller
status receipt. The receiver must also match the real Store attempt and a fresh
Mesh task/checkout generation, current assignee, session and access gates.
The task pin is a scope restriction; only the pinned operator key authenticates.

For adoption, the operator has already prepared an exact two-path TOML delta.
Its bytes must match `candidate_config_sha256`; every parsed field outside those
two keys must equal the startup preimage. Request `paths` must equal disk and the
admitted policy. `rollback_of` is empty. Runner rechecks disk, receiver and signed
revocations immediately before publishing one immutable pair under the lock
shared by all five native interaction sites. It initializes no directories or
ledgers. Every native consumer reads this loaded pair, not today's disk values.
The shared lock spans the operation's two fresh Mesh reads: a signed operator
request can delay native consumers while those reads complete. This is a bounded
control operation, not a background watcher or an unbounded network retry.

The successful response is authored by the process after publication and names
the actual pair, generation, startup preimage, candidate digest, process epoch,
operation, operator and authority revision. `loaded_digest` describes this
native-pair binding, **not** adoption of unrelated candidate TOML settings.
Standalone config.load or a CLI-created JSON file cannot substitute for it.

Rollback requires a fresh status/CAS, a new request UUID and `rollback_of` equal
to the successful adopt operation. The candidate disk pair and request paths
must both be `[null,null]`. It only detaches the loaded pair if neither native
destination exists. Any initialized, registered, active or UNKNOWN history
refuses; there is no automatic source detach/reset. Later unrelated TOML changes
and signed revocations are preserved byte-for-byte: no whole-file restore,
lease/hold/registration/cursor mutation, replay or channel rebind occurs.

## Finite release boundary

1. Source acceptance: exact merged main SHA, successful substantive CI including
   `native-result-config`, and routed independent security-context SHIP on HEAD.
   Test: `PYTHONPATH=src:tests python -m unittest -v test_native_result_config`.
2. Separate release admission: current operator capability and actual receiver,
   stage/package/source bindings; exact new artifact/catalog input and startup
   config pin; fresh access/revocations, rollback anchor and independent exact
   PRE-CHANGE approval. Source SHIP grants none of these live effects.
3. Only the authorized installer/operator may install that exact artifact and
   admit the pinned startup authority. Read back the actual process/version,
   then execute status, exact prepared two-path delta, adopt, loaded readback.
4. Any later initialization/enrollment/ordinary ACK needs its existing separate
   operational gate. Stop on refusal. Before history exists, scoped rollback is
   available as above; afterwards preserve state and use the established owner.

A deployment is complete only after registration, ordinary consumption,
negative controls and operational acceptance are evidenced for it. Nothing in
this document enables native result delivery by itself.
