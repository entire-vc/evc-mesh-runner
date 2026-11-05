# Provider quota admission: rollout and rollback

A terminal provider limit requires a fresh account-capacity acknowledgement
from both reserve and admit. Install a compatible account-capacity gate before
activating this version: an older gate cannot acknowledge
`MESH_RUNNER_REQUIRE_ACCOUNT_CAPACITY=1` and correctly fails closed. When no
allowed account has verified capacity, keep the same attempt ready without a
provider retry; do not create a task date, WAIT or lane pause. Authentication,
security, human, disk and independently owned scheduling/checkout gates remain.

API overload, including structured `server_overloaded`, uses the existing
provider-outage backoff and same-session resume. It does not require an account
capacity ACK. The existing multi-lane outage circuit breaker remains unchanged.
An arbitrary substring `429` in another error is not HTTP status evidence.
Explicit HTTP/API/status 429 and typed rate/usage-limit messages still require
capacity admission. The selected account environment is read after the gate.

## Rollback

A source revert is not runtime rollback. Preserve the merge receipt and use a
normal reviewed revert of the actual squash or merge commit, preserving unrelated
changes. Runtime rollback requires a supported signed artifact, a recorded
installed/config/process anchor, fresh access/CAS checks and an independently
reviewed operation. Preserve a compatible launch guard; do not launch a second
controller. Restore the account gate only through its separately reviewed backup
and restore operation. Do not clear task dates, human/security holds, account
grants, sessions, leases or cache as a rollback shortcut.

## Limits and required evidence

- The provider-capacity WAIT text filter intentionally errs toward refusing a
  schedule: broad `quota` and `account...reset` matching can also reject legitimate
  external quota-approval or password-reset windows. Rejection returns guidance;
  it is not evidence to clear a foreign date or bypass a human gate.
- Terminal provider-error classification is text-based. Narrow HTTP 429 context
  prevents git-hash collisions; arbitrary error text does not verify account
  identity or capacity.
- An unbound session-log cache cannot establish account provenance. A live
  fallback may lose window/reset metadata; fixture coverage does not supply
  missing production provenance.
- Exact-source CI, review and merge do not prove installation. Before activation,
  verify signed version availability, selected bytes and modes, catalog/artifact
  signatures, native access and the actual loaded runtime. Historical receipts
  do not admit changed bytes.
- Final acceptance requires an ordinary affected attempt with actual
  source/artifact/catalog/config/process IDs, account admission and same-session
  receipts. Reconcile only an erroneous quota schedule proven to belong to that
  attempt using supported date/version/origin CAS. Do not clear other tasks'
  schedules or claim every waiting lane is quota-blocked.
