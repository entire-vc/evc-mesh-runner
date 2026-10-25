# Card WAIT blocking-path diagnostic

`Effects.publish` enriches only an attempt already in `waiting` with a typed
`card` wait. It retains the exact runtime `kind` and original `ref`. It does not
change an attempt, checkout, dependency, gate, wake predicate or eligibility.
Admission continues to use the existing selection code; the diagnostic must
never be consumed as proof that work is available or that a person was asked.
Skipped cards and other wait kinds are outside this change.

The reason contains the observed dependency path and observation timestamp.
A human gate is reported only from a successfully read task's literal boolean
`human_gate: true`. Its `human_gate_info.owner_agent_id`, when present, is the
asker/owner and is explicitly named `gate_owner`. It is not the addressee.
Neither labels, description, creator nor assignee can supply either proof.
Without a typed addressee in the supported API, `addressee=unknown` is retained;
no comments are fetched and no person receives a message.

Every publish refresh shares a budget of 12 GETs, 8 nodes, depth 4 and 3 seconds.
Only the existing GET task/by-short-id/dependencies routes are used. No project
status fallback, identity fallback, retry or `Retry-After` sleep is permitted.
The diagnostic GET runs in a separate one-shot child: credentials are sent on
stdin, stderr is discarded, its response is capped at 1 MiB, and the remaining
wall deadline bounds DNS, transport and body reads. Cleanup reserves 100 ms,
terminates then escalates to kill if necessary, and reaps the child. Normal Mesh
queries keep their existing timeout and retry behavior. Ordinary OS process
creation/reaping overhead is not a real-time scheduler guarantee.

Missing/invalid identity, status or gate fields, incomplete or paginated edges,
cycles, edge responses over 256 rows, budget cuts and API errors produce `UNKNOWN`, never an inferred gate or
successful traversal. The error reason contains only a fixed category or HTTP
status, never exception bodies. Only the canonical `relates_to` and `is_child_of`
types are ignored. `blocks` and the legacy `depends_on` spelling supported by
the fleet audit digest are traversed; missing, malformed, differently cased or
unknown types produce `UNKNOWN`. Closed tasks are terminal observations, not
permission to wake.

The full-list dependency contract was checked against Mesh
`0cd22c6cee8f9f09bfa8915b471394451f547be0`: `DependencyHandler.List` returns
`outgoing`/`incoming`, `ListByTaskBothDirections` reads both complete lists, and
`TaskDependencyRepo.ListByTask` has no SQL limit. Thus absent `total`, `has_more`
and `next_page` are valid; the API does not require these fields. If present,
`total` must be an integer exactly equal to the number of outgoing rows
(including nonblocking edges), `has_more` must be literal `false`, and
`next_page` must be `null`. Boolean, negative, fractional, string or null totals,
inconsistent counts and invalid/continuation metadata produce `UNKNOWN`.
The canonical edge types are also confirmed by the `add_dependency` API schema;
legacy compatibility follows the reviewed fleet audit digest's `DEP_TYPES`
at `f30b961cf03238086149f2bd61b0aab851184cbc`.

Successes and failures are cached for 300 seconds in the existing SQLite marks
under `blocking_path_diagnostic_v1`, keyed by lane/task and exact wait target.
The cache survives restart; a different target, malformed cache, expiry or a
clock rollback forces a bounded refresh. This is a diagnostic observation, not
an authorization receipt or generation acknowledgement. A new WAIT for the
same card/target can share a recent diagnostic observation; all actual wake
checks remain independent. No state migration is required.

Rollback is a code revert. The marks can remain: older versions ignore them.
Do not delete or edit attempts, leases, protocol watermarks or SQLite rows.
