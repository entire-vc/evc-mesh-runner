# WAIT observation budget

One wake of one waiting attempt reads Mesh (task, comments, dependencies, statuses, targets) and
GitLab/GitHub. All reads and all retry backoff of that wake share **one monotonic deadline**,
45 seconds (`wake_budget_seconds`, a retired `[runner]` option: the value is fixed).

**Why 45s.** A single `MeshClient` GET allows a 30s socket timeout, and the permitted GET retries
(1s, 3s) made one request cost up to ~94s with no shared limit, so one slow Mesh held a lane's
tick for minutes. A healthy wake is a few sub-second reads; 45s covers one full-timeout read plus
margin, and stays well under the two-minute wake poll.

**What the budget does.**
- Every read's socket timeout is `min(own timeout, time left)`; a spent budget fails the next read
  without sending it.
- A GET retry sleeps only if the sleep still fits; otherwise the last real error is raised.
- Mutations are never repeated (unchanged). A 429 with `Retry-After` follows the same rule.
- A failed observation changes nothing: WAIT, session, workspace lease, checkpoint, auth hold and
  dependencies stay as they were; the next regular check runs again. A read error is never a
  satisfied dependency and never an auth restoration. The attempt is not quarantined for it.
- Channel faults (`timeout`, `transport`) are never counted toward a wake. A target-side answer
  (`http` other than the documented 403 path, `parse`) keeps the existing contract: after
  `max_errors` consecutive checks the agent is told to check the wait itself, so a wait on a target
  that can never be read does not sit silent.

**Recorded cause.** Event `wake_read_failed` in the runner store (and one log line):

    wake read failed: kind=<timeout|transport|parse|http> [status=<n>] stage=<METHOD /path/:id/...>
        elapsed=<s> budget=<s> retries=<n>

`stage` is the endpoint shape with ids and query removed. No URL query, body, credential, host or
identity is written. `kind`: `timeout` (socket timeout or budget spent), `transport` (reset, DNS,
TLS), `parse` (not JSON), `http` (status answer, e.g. 403 for an auditor without access).

Tests: `tests/test_wake_budget.py` (real `MeshClient`, local HTTP fixture, real runner and SQLite).
