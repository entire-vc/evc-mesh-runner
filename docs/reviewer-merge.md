# Reviewer merge-only handoff (opt-in)

Default is unchanged: the author merges their own MR after an independent SHIP and green CI.
This is an explicit request for a case where the project's reviewer should press merge.

## Invocation

```
mesh-runner reviewer-merge --config runner.toml --agent <lane> \
    --task <product card uuid> --mr group/project!iid --sha <full head sha> [--apply]
```

- Without `--apply` it only reads (card, MR, router) and prints `{ready, refusals, request}`. Dry-run readiness is not a merge.
- `--apply` creates **one** card `[merge-only] group/project!iid @sha (rm-<key>)`, label `merge-only`, assigned to the reviewer the router chose. The key is `sha256(source|project|iid|sha)`; a second apply or a restart finds the card and creates nothing. A new head is a new request. Creation is serialized per key by a file lock under `state_dir` (one host). Mesh has no uniqueness constraint, so two hosts applying at the same instant can both create; apply re-reads after creating and reports `duplicates`. Run apply from one host; a stray twin is harmless (its gate run on the same head finds the MR merged) and is cancelled by hand.
- The product card (assignee, checkout, status, deploy, live QA) is not touched.
- Reviewer choice: `[runner] reviewer_select_command` (the bundled `scripts/select-reviewer.py` adapter requires a compatible separately installed router using the project reviewer group, author's current family and fresh pool observations). Only `cross-family` and `same-family: quota` assign; unknown or stale pools, no group, or the author themself refuse.
- It refuses on: head drift, MR not open, no `verified-by:` label, a `hold` label, CI not `success`/`manual` on the head, a `verify:<other lane>` label, a human gate, a closed product card. The SHIP receipt is reused; no review is started. The gate re-checks receipt, holds and CI itself.

### Router dependency

The bundled adapter does not ship its external router dependency. It requires the
specific compatible module interfaces used in `scripts/select-reviewer.py`, plus
runtime catalog, active profile and fresh pool observations. An arbitrary router
checkout is not compatible merely because it accepts these directory options.
Operators must supply and independently review that dependency, or configure a
selector implementing the same JSON output contract (`lane`, `reason`, and
`author_family`) and refusal semantics. For the bundled adapter, pass both
locations in the configured command; there is no default:

```
reviewer_select_command = ["python3", "<release>/scripts/select-reviewer.py", "{project_id}", "{author}",
                           "--router-home", "<router checkout>", "--state-dir", "<router state dir>"]
```

(or set `MESH_ROUTER_HOME` / `MESH_ROUTER_STATE_DIR` in the runner's environment). Without the
router the adapter prints `unknown: inputs`, the request is refused and nothing is created.

## Gate side

The reviewer's runner starts the card; the session ends with `⏳ WAIT merge:<target> after=<sha>`. Before the first gate run `_merge_start` reads the card: if it carries `merge-only`, the request block must match the wait (target, sha) and this lane, and `GET /user` with the **same fresh env the gate gets** (`env_command`) must return the lane's `gitlab_username`. Missing, wrong or unreadable identity: no gate run, the agent is woken with the reason. No token or env is printed. The merge itself is the unchanged `merge_command` (`glab-merge --expected-sha … --use-lead-accept --gate`).
Set `gitlab_username` on the reviewer lanes that differ from their lane name.

## Canary (read-only)

```
PYTHONPATH=src python3 scripts/reviewer-canary.py --config runner.toml \
    [--adapter scripts/select-reviewer.py] <project_id> <author-lane>
```

Prints selected lane, reason, lane `gitlab_username` and the `/user` its fresh env resolves to.

## Positive check after install

Run on the installed runtime, with a real open MR that already carries a `verified-by:` label and a green head pipeline
(the product merge itself is not part of this check). `<sha>` is the full MR head.

1. Version: the installed package reports `0.1.28` or newer.
2. Dry-run: the command from "Invocation" without `--apply`. Expect `ready: true`, empty `refusals`, and a `request`
   whose lane is the router's cross-family (or quota same-family) reviewer, not the author.
3. Canary: `scripts/reviewer-canary.py` for the same project and author. The `/user` it prints must equal that
   reviewer's `gitlab_username`.
4. Apply: the same command with `--apply`. Expect one new card `[merge-only] group/project!iid @sha (rm-<key>)` in
   `todo` (a real Todo status, not backlog), label `merge-only`, assigned to the dry-run's reviewer.
5. Product card: assignee, checkout and status are the same as before step 4.
6. Repeat: run step 4 again. Expect the same card id, `created: false`, no second card with that `rm-<key>`.
7. Rollback: cancel the one merge-only card; nothing else was changed.

A refusal in step 2 is a result, not a reason to bypass the router: fix the named input and rerun.

## Rollback

Do not run `--apply`; cancel the one merge-only card. Nothing else changes.
