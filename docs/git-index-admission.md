# Narrow reviewed Git-index admission

One closed operation, `git-index-clear-skip-worktree`, on the two literal paths
`scripts/macmini-disk-cleanup.sh` and `scripts/slot-archive-remove.py`. No wildcard, no free command,
no source writes. The consumer issues **authority only**; it never touches a Git index, and fresh
pins/CAS, quiescence, the cron mutex and bounded rollback stay the executor's preconditions.

## Path

`mesh-runner admit-git-index --config C --agent A --task T --attempt X --wait-generation G --request-id R`
publishes a bounded immutable request (spool `git-index-admission`, private, TTL <= 900 s). The owning
Runner tick is the single issuer: it writes a durable receipt file next to the request and a
per-task grant mark in its store. The next turn of the same attempt receives scoped instructions.
Submitting, or a task comment, grants nothing.

## Issuer (chosen mechanism)

An immutable policy JSON whose SHA-256 is pinned in the Runner config:

```toml
[git_index_admission]
policy = "/path/policy.json"
policy_sha256 = "<64 hex>"
revocations = "/path/revocations.json"   # {"version":1,"revoked":["<revision or request id>"]}
```

The pin reaches the host through the signed, independently reviewed release/activation chain, so the
digest is the reviewed revision. Policy and revocation files must be regular, owned by the Runner user,
not group/other writable. A mismatching digest, schema error, unreadable or unsafe file, or missing
revocation list is UNKNOWN and refuses. The policy binds actor UUID, source task, package SHA-256 and
artifact, the exact path set, `target_root`, `not_before`, `expires` and `grant_seconds` (<= 3600).
The operation and allowed paths are constants of the code, not policy input.

## Checks (each on issue; the grant ones again on every delivery)

Agent/task/attempt/generation of an open attempt, with the task equal to the policy's source task (an actor's other task is refused); actor == the Runner's Mesh agent id; request bound to
the pinned digest and equal to the policy in operation, paths, package, source task and actor; not
revoked, inside the policy window; fresh task ownership (Mesh error = refuse); no other open attempt on
the task; no leased slot overlapping `target_root`; no maintenance hold.

## Binding, TTL, retries

Every issued request id is recorded durably (SQLite, never purged); a replay of a used id is answered `expired` and never reissues. Receipt expiry is fixed at issue: `min(issued + grant_seconds, policy.expires)`. A repeated request while
the grant lives returns `already_granted` with the original expiry; a replay of the same request after a
crash returns the same grant. A new attempt never inherits it. Revocation, expiry, policy change,
ownership loss or UNKNOWN end it: every later delivery of the attempt states that the exception ENDED and nothing
permissive stays in resumed context; a new explicit admission is required.
