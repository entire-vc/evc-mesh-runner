# Repeat-safe writes

Mesh stores the answer of a write that carries an `Idempotency-Key` header for 24 hours, in the scope of
the caller, the method and the path. A repeat with the same key and body gets the stored answer
(`Idempotent-Replayed: true`) and writes nothing; the same key with another body is refused with
`409 idempotency_key_reused`; a repeat while the first send is still running gets
`409 idempotency_request_in_progress` with `Retry-After`. A request without the header behaves as before.

This is the server's contract, documented in evc-mesh `docs/idempotency.md`. The runner only depends on
it; whether a given Mesh instance honours a key on a given route is a property of that instance, checked
live after it is deployed, not by this repository's tests (they emulate the server in
`tests/test_idempotent_writes.py`).

## What the runner keys

| write | route | key is made of |
|---|---|---|
| spend report of a turn | `POST /agents/me/sessions/report` | the turn (`<attempt>:<turn>`) |
| completion line | `POST /tasks/:id/comments` | `completion`, the attempt |
| `NO OUTCOME`, unknown-delivery, quiet-resolution notes | same | the kind, the attempt, the turn |
| decision question raised from `WAIT decision:@person`, proposed-default notice | same | the kind, the card and how many of that kind were delivered on it |
| re-ask on a reused decision request, close note of a request | same | the kind, the attempt and turn or the request |
| retained-lease, review-unstartable notices | same | the kind and the attempt or day |
| assignment and repository preflight notices | same | the kind, the card and how many of that kind were delivered on it |
| Gmail watch comments | same | the outbox key |
| every other runner note (hand-up, start cap, requeue, integration holds, ...) | same | the kind and the attempt and turn, or the kind and the UTC day |

The key never depends on the text of the write (a note must name its key; there is no text fallback,
and no key is a digest of the question, evidence or fingerprint): a note rebuilt after a restart with
another timestamp, figure or fact keeps the key of the first send, Mesh refuses it as
`idempotency_key_reused`, and the runner counts it as written. A note that has no attempt and turn to
name it counts its delivered notes of that kind on the card; the count moves only on Mesh's answer, so
a restart between send and answer reuses the key and the next diagnosis gets the next one.

## Lost answers

A keyed write whose answer never came (status 0: timeout, connection reset) is sent again with the same key
after 1 s and 3 s; Mesh answers the repeat from the stored answer. A `409 idempotency_request_in_progress`
is waited out by its `Retry-After`, within the rate-limit budget. An unkeyed write is never repeated.

A `409 idempotency_request_in_progress` whose `Retry-After` is longer than the rate-limit cap is not
repeated early: the write fails and the caller handles it as before.

With keys on, the spend report of a turn is marked as sent only after Mesh answered (or refused it with a
4xx other than 429). A restart before the answer runs the settle again, which sends the report again with
the same key, and Mesh counts it once. This relies on Mesh honouring the key on that route, so this runner
version is installed only where the Mesh instance answers the spend report idempotently. With keys off the
report is marked before it is sent, as before: a lost report is better than a double count.

## Rollback

`[runner] idempotency_keys = false` sends no key, does not repeat a write whose answer was lost and does
not wait out `idempotency_request_in_progress`; the spend report is marked before it is sent. It applies to
every Mesh client of the runner, the Gmail service's included. The runner then writes exactly as before
this change, which includes the existing rate-limit repeat: a write refused with `429` and a `Retry-After`
from the rate-limit middleware (which ran nothing) is still sent again after that delay. The change takes
effect on the next runner restart.
