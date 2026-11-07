# Repository preflight before a code/review start

Before a work/review attempt leases anything, the runner compares the repository named by the
card's typed MR links (`group/project` on the configured GitLab host) with this lane's actual
config. Task prose and arbitrary URLs never name or grant a repository; metadata-only cards and
cards without an MR link are not blocked.

Reasons (`repo_requirements.py`): `undeclared_repo`, `no_membership`, `missing_approved_clone`,
`no_assigned_checkout`, `checkpoint_conflict`, `no_free_slot`. Each durable reason names
`next_actor` and the supported action (reviewed config/admission path, the existing
`admit-workspace-repo` request). One handoff comment per distinct fact set (stored fingerprint); a
refused start aborts the attempt with no lease and no model turn. `no_free_slot` stays with the
existing pool hold. The check never edits config, clones, uses another actor key, or touches
foreign leases. Once the approved config carries the repo the same card starts normally.
