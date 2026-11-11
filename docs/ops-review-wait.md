# Board ops-review eligibility wait (opt-in)

An ops-review author with a registered deferred request can declare on that
request's **source task**:

```
⏳ WAIT ops-review:<64hex request-id>
```

This is a Board eligibility receipt observer, not a queue refill request or an
account reset timer. It never selects/adopts another owner, creates a reviewer,
runs `--deliver`, writes the registry, or bypasses admission. No new service is
needed: the existing Runner wait observer reads the existing Board registry.

Enable explicitly in the affected Runner configuration:

```toml
[runner]
ops_review_wait_registry = "/absolute/path/to/private/ops-review-pending.json"
ops_review_wait_notifier_id = "<canonical Board Mesh actor UUID>"
```

Both options are required together; otherwise the observer is off. The file
must be a same-user private regular file, at most 4 MiB, with the Board version-1
envelope and at most 128 requests. Symlinks and special files are refused. No
package or receipt path inside the file is opened by Runner.

Registration validates the request's canonical binding digest, source task,
owner, unchanged AC/labels/project/assignee context, HEAD/package/manifest/criteria
pins, active state and current source gates. A parent or another lane cannot arm
the source's wait. An invalid declaration returns a specific refusal; it does
not fall back to queue, a comment or a timer.

Pending and notifying requests stay asleep. A ready notification arriving before
the declaration is retained. Wake requires all of:

- Board state `notified`, with persisted `comment_id` and both notification hashes;
- accepted publication after registration and unchanged structured notification intent;
- the exact Mesh comment ID, the configured notifier's `author_id`, and the
  complete body digest fenced by Board before POST/reconciliation;
- the request marker at the start of that body, with unchanged source ownership
  and context at delivery.

An explanatory comment containing the marker, a mention, old/changed pins,
unknown/exhausted quota, a reset timestamp and time passing cannot wake it.
Legacy notifying rows can acquire hashes through Board's exact uncertain-POST
reconciliation. Legacy notified rows without hashes stay held; do not recreate
or delete their request to force a wake.

Runner commits `waiting -> ready`, wake text and a per-source/per-owner/per-request
delivered mark in one Store transaction under the current wait CAS. A stale
observer, replay, restart or repeated declaration cannot deliver it again. The
ready outbox survives an admission refusal. The source owner then rechecks
inputs/holds/lease/route and absence of dispatch receipt and executes **one**
ordinary `--deliver`. That command still owns all existing gates. Eligibility is
neither admission nor SHIP.

Local focused verification:

```
PYTHONPATH=src:tests python3 -m unittest discover -s tests -p test_ops_review_wait.py -q
```

Full Runner tests run in CI. Installation requires a reviewed release input that
preserves the actual installed composition and a guarded affected-unit update;
main's version alone is not an installable replacement for a newer private runtime.
After install, verify the running source/config receipt and a real registered
source's supported wait/wake; a unit test alone is not live acceptance.

Rollback disables these two options and restores the previous reviewed executable
and configuration through the existing guarded install path. Keep the private
registry, notification intent/receipt and Runner delivered marks. A rollback does
not reinterpret this wait as a generic event: repair its observer before rearming.
