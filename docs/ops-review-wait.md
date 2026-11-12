# Retired Board ops-review wait

The separate ops-review route has been retired. Submit the original source task
with `finish_task to=review`; independent review happens on that same card.

`ops_review_wait_registry` and `ops_review_wait_notifier_id` are retired
`[runner]` keys. Older configurations still load with one warning per present
key; all values are ignored, including values rejected by the former validators.
The Runner no longer reads the Board registry or observes eligibility receipts.

New `WAIT ops-review:` declarations are refused. Existing persisted ops-review
waits remain held; comments and elapsed time do not reinterpret their predicate.
Reconcile such a source explicitly through same-card review. Keep private Board
registry/receipt data and Runner delivered marks for audit or rollback; this
package change does not delete or migrate them.

Rollback uses the previous reviewed Runner package. No live configuration or
service restart is part of this schema cleanup.
