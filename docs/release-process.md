# Release process

One release = one card, one `release/<version>` branch, one version, one install. Ordinary merge
requests never touch the version or `CHANGELOG.md`, so they never conflict with each other there.

## Ordinary merge requests

- The changelog entry goes into its own file, `changelog.d/<card>-<slug>.md` (format:
  `changelog.d/README.md`). A `src/` change without one is refused by the `mr-policy` CI job;
  a change that needs no entry carries the label `no-changelog` or a description line
  `Changelog: none` (labels do not start a pipeline: re-run the job after adding one).
- The version in `pyproject.toml`, `src/mesh_runner/__init__.py` and `mesh-extension.json` and the
  file `CHANGELOG.md` are left alone. `mr-policy` refuses either outside a `release/*` branch and
  says what to do instead.
- Merge requests go into `main` through the ordinary merge gate whenever they are ready: green
  pipeline and an independent SHIP verdict on the head commit. Merging one does not make another
  stale: nothing to rebase for the changelog.
- Files that reach the public export carry no internal card id (`#` and 8 hex) and no team member
  name, in code, comments, docs or fragment text. `mr-policy` scans the whole tree at the head of
  every merge request (release branches too) except `NOT_EXPORTED` in `ops/check_mr_policy.py`, and
  lists each hit with its line. The card belongs in the fragment's file name, the branch and the
  merge request; tests use a stub id from `STUB_CARD_IDS`. The names are kept there only as sha256
  of the word. `tests/test_release_flow.py` runs the same scan on the checkout, so a `main` that
  gained one goes red too.

## The release branch

Who: the owner of the release card. There is exactly one open release card per version; the
version is named on it and is new (a published version is never reused, see
`docs/release-package.md`). Nobody else sets a version.

What goes in, fixed at the cut-off named on the release card:

1. everything already merged into `main`;
2. optionally, a merge request that is not merged yet but has a green pipeline **and** an
   independent SHIP verdict on its current head commit, merged at exactly that commit;
3. one release commit made by the script. Nothing else: no fixes on the release branch.

A merge request that misses the cut-off (red pipeline, no verdict, verdict on an older head, a
code conflict) waits for the next version; its fragment simply stays in `changelog.d/`.

```sh
git fetch origin
git switch -c release/<version> origin/main
git merge --no-ff <reviewed-head-sha>          # only for item 2, one per merge request
python3 ops/release.py <version> --dry-run     # read the section it will write
python3 ops/release.py <version>               # version x3, CHANGELOG section, fragments removed
git add pyproject.toml src/mesh_runner/__init__.py mesh-extension.json CHANGELOG.md changelog.d
git commit -m "release: <version>"
git push -u origin release/<version>
```

`ops/release.py` refuses (and writes nothing) when the version is not newer than the current
one, the three version files disagree, the version already has a section, a fragment is
malformed or there is nothing to release. On the `release/*` branch `mr-policy` checks that all
three files carry the same new version, `CHANGELOG.md` has a non-empty section for it, no
fragment is left, and every commit made on the branch itself (not merged in) touches `src/` only in
the version line: code enters a release branch only by merging a reviewed merge request with
`--no-ff`, never as a commit on the branch.

The release merge request then takes the ordinary path: full pipeline, one independent review
on its head, the merge gate. Merge requests merged at their head commit in item 2 are marked
merged by GitLab together with it.

## After the merge: one install, then the other hosts

Each step is its own card with a named owner (or a fail-closed CI job), in this order; a step
starts only after the previous one is verified:

1. **Install** the release commit once: the head of `release/<version>`, which is the second parent
   of the release merge (`git rev-parse <release-merge>^2`). Not the merge itself: `main` moves on
   after the cut-off, so the merge also carries merge requests the version's `CHANGELOG.md` section
   does not name. `git archive <release-sha>`, unpack,
   `python3 ops/install.py --source <dir>` on the fleet host (README, "Updating the runner");
   `install.py` refuses a tree exported from a merge commit. The receipt is `ops/install.py --status`
   showing the new version running and `commit.sha` equal to the release commit.
2. **Catalog**: publish that same archive (of the release commit) as the version in the extension catalog
   (`docs/release-package.md`), so hosts that follow the catalog see it.
3. **Proto** runner: upgrade from the published catalog version by its reviewed upgrade procedure.
4. **Partner host**: the same, last.

A failure at any step stays on that step's card; the fix ships as the next version through the
same process, never by editing a published one.
